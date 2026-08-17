"""create_report (atomic, render-verified) + present_report (inert transport).

S3 is real (moto); the chart renderer is a recording fake — the real one
(chat.report_render) needs Chromium and is exercised live. create_report is
the whole pipeline in-process: lint → rasterize → compose → size gate → PDF →
S3 puts; there is deliberately NO database row to assert (the composite id
carries the coordinates)."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from okf_core import reports as rp
from chat.reports import make_report_tools

SUB = "sub-123"
BUCKET = "okf-bundles"
PNG = "data:image/png;base64,iVBORw0KGgo="
PNG_DARK = "data:image/png;base64,ZGFyaw=="
SCOPE = {"data_domain": "motorsport", "dataset": "f1"}

BLOCKS = [
    {"type": "markdown", "md": "## Answer\n\nLap times cluster at 83s."},
    {
        "type": "kpi",
        "label": "Total laps",
        "value": "1,204",
        "provenance": {"kind": "computation", "slug": "laps_by_season"},
    },
    {
        "type": "chart",
        "title": "Distribution",
        "spec": {"type": "histogram", "labels": ["81s", "82s"], "series": [{"data": [12, 87]}]},
        "provenance": {"kind": "adhoc_sql", "sql": "SELECT bucket, count(*) FROM laps GROUP BY 1"},
    },
]


class FakeRenderer:
    def __init__(self, fail: bool = False):
        self.charts: list[list[dict]] = []
        self.pdfs = 0
        self.fail = fail

    def render_charts(self, charts):
        self.charts.append(charts)
        if self.fail:
            raise RuntimeError("chart render failed (light) — chart(s) ['0']")
        # The real renderer's two-pass shape: one transparent PNG per theme.
        return [{"light": PNG, "dark": PNG_DARK} for _ in charts]

    def pdf(self, html):
        self.pdfs += 1
        return b"%PDF-1.4 stub"


@pytest.fixture()
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _tools(s3, renderer, scope=SCOPE, **kw):
    return {
        t.name: t
        for t in make_report_tools(
            s3,
            renderer,
            bundle_bucket=BUCKET,
            user_sub=SUB,
            dataset_scope=scope,
            **kw,
        )
    }


def test_create_report_full_flow(s3):
    renderer = FakeRenderer()
    tools = _tools(s3, renderer)
    out = tools["create_report"].invoke(
        {"title": "Monza lap analysis", "blocks_json": json.dumps(BLOCKS)}
    )
    assert out["pdf"] is True and out["blocks"] == 3
    parsed = rp.parse_report_id(out["report_id"])
    assert parsed["domain"] == "motorsport" and parsed["dataset"] == "f1"
    prefix = rp.report_s3_prefix(
        parsed["domain"], parsed["dataset"], parsed["stamp"], parsed["suffix"]
    )
    keys = {
        o["Key"]
        for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)["Contents"]
    }
    assert keys == {
        rp.report_blocks_key(prefix),
        rp.report_html_key(prefix),
        rp.report_pdf_key(prefix),
    }
    html = s3.get_object(Bucket=BUCKET, Key=rp.report_html_key(prefix))["Body"].read().decode()
    # Provenance renders as appendix notes, never body badges (executive-clean).
    assert "Notes &amp; methodology" in html
    assert "verified computation <code>laps_by_season</code>" in html
    assert "direct read-only query" in html
    # Both theme PNGs ship in the figure; the viewer's data-theme picks one.
    assert f'<img class="chart-light" src="{PNG}"' in html
    assert f'<img class="chart-dark" src="{PNG_DARK}"' in html
    doc = json.loads(
        s3.get_object(Bucket=BUCKET, Key=rp.report_blocks_key(prefix))["Body"].read()
    )
    assert doc["created_by"] == SUB and doc["domain"] == "motorsport"
    # The renderer saw exactly the chart blocks — the render IS the verify.
    assert [c["spec"]["type"] for c in renderer.charts[0]] == ["histogram"]
    assert renderer.pdfs == 1


def test_create_report_refusals_have_no_side_effects(s3):
    tools = _tools(s3, FakeRenderer())
    assert "not valid JSON" in tools["create_report"].invoke(
        {"title": "T", "blocks_json": "nope"}
    )["error"]
    assert "title is required" in tools["create_report"].invoke(
        {"title": "", "blocks_json": json.dumps(BLOCKS)}
    )["error"]
    failing = _tools(s3, FakeRenderer(fail=True))
    out = failing["create_report"].invoke(
        {"title": "T", "blocks_json": json.dumps(BLOCKS)}
    )
    assert "chart render failed" in out["error"]
    chartless = _tools(s3, None)
    out = chartless["create_report"].invoke(
        {"title": "T", "blocks_json": json.dumps(BLOCKS)}
    )
    assert "no chart renderer" in out["error"]
    assert s3.list_objects_v2(Bucket=BUCKET, Prefix="reports/").get("KeyCount", 0) == 0


def test_create_report_without_renderer_still_saves_chartless(s3):
    tools = _tools(s3, None)
    out = tools["create_report"].invoke(
        {"title": "T", "blocks_json": json.dumps([b for b in BLOCKS if b["type"] != "chart"])}
    )
    assert out["pdf"] is False and rp.parse_report_id(out["report_id"])
    parsed = rp.parse_report_id(out["report_id"])
    prefix = rp.report_s3_prefix(
        parsed["domain"], parsed["dataset"], parsed["stamp"], parsed["suffix"]
    )
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)["Contents"]}
    assert rp.report_pdf_key(prefix) not in keys  # honest degrade: HTML only


def test_create_report_size_cap_and_scope(s3):
    tools = _tools(s3, FakeRenderer(), max_report_bytes=10)
    out = tools["create_report"].invoke(
        {"title": "T", "blocks_json": json.dumps([{"type": "markdown", "md": "hi"}])}
    )
    assert "cap 10" in out["error"]
    unscoped = _tools(s3, FakeRenderer(), scope=None)
    out = unscoped["create_report"].invoke(
        {"title": "T", "blocks_json": json.dumps([{"type": "markdown", "md": "hi"}])}
    )
    assert "name the dataset" in out["error"]
    ok = unscoped["create_report"].invoke(
        {
            "title": "T",
            "blocks_json": json.dumps([{"type": "markdown", "md": "hi"}]),
            "data_domain": "d",
            "dataset": "ds",
        }
    )
    assert rp.parse_report_id(ok["report_id"])["dataset"] == "ds"


def test_present_report_is_an_inert_ack(s3):
    tools = _tools(s3, FakeRenderer())
    rid = "rep~motorsport~f1~20260815T120000Z~a1b2c3d4"
    out = tools["present_report"].invoke({"report_id": rid, "title": "Laps"})
    assert out["status"] == "presented" and out["report_id"] == rid
    bad = tools["present_report"].invoke({"report_id": "nope"})
    assert "not a report id" in bad["error"]


def test_report_tools_wire_into_the_agent_toolset():
    """build_agent binds the pair whenever the run has a verified subject and
    the deploy has a bucket — the render_chart wiring test's shape."""
    import chat.server as server
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    import chat.tools as chat_tools
    from chat.config import ChatConfig
    from consumption_mcp.tools import ConsumptionConfig

    from .fakes import FakeConsumptionTools

    captured = {}

    def fake_build_graph(model, tools, checkpointer, *, system_prompt=None, middleware=None):
        captured["names"] = [t.name for t in tools]
        return object()

    cfg = ChatConfig(
        bundle_bucket="b",
        vector_bucket="v",
        vector_index="i",
        registry_table="r",
        checkpoint_table="cp",
        threads_table="th",
        catalog=[],
        sql_enabled=False,
    )
    cons_cfg = ConsumptionConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r"
    )
    orig = (chat_graph_mod.build_graph, chat_config_mod.build_chat_model, chat_tools.build_consumption_tools)
    try:
        chat_graph_mod.build_graph = fake_build_graph
        chat_config_mod.build_chat_model = lambda *a, **k: object()
        chat_tools.build_consumption_tools = lambda **kw: FakeConsumptionTools()
        build_agent = server.make_agent_factory(
            cfg,
            cons_cfg,
            {"s3": object(), "s3vectors": None, "bedrock_runtime": None, "ddb": None},
        )
        build_agent(
            "global.anthropic.claude-opus-5",
            "high",
            None,
            object(),
            features=set(),
            user_sub=SUB,
        )
    finally:
        chat_graph_mod.build_graph, chat_config_mod.build_chat_model, chat_tools.build_consumption_tools = orig

    assert "create_report" in captured["names"]
    assert "present_report" in captured["names"]
    assert "read_page" in captured["names"]
    # read_skill rides every run (no flag, no opt-in) — the report pair's
    # descriptions reference it by name, so it must be bound alongside them.
    assert "read_skill" in captured["names"]


def test_create_report_points_at_the_authoring_skill(s3):
    # The methodology itself is served by the generic read_skill tool
    # (chat.skills, tested in test_skills.py) — the report pair only has to
    # point the model at it by name.
    tools = _tools(s3, FakeRenderer())
    assert "report_skill" not in tools
    assert 'read_skill("report-authoring")' in tools["create_report"].description

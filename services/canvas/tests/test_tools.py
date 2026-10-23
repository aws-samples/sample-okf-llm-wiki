"""The authoring trio: lint's three layers over the live path, render's
degrade/inline-image paths, and the single-step lint-gated save."""

from __future__ import annotations

import json

from okf_core import canvas as cv

from canvas.tools import build_all_tools, build_canvas_tools
from tests.conftest import CANVAS, DOMAIN, SUB, VALID_CONFIG


def _by_name(tools):
    return {t.name: t for t in tools}


def test_lint_full_green_and_live_layer_runs(tool_ctx):
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["lint_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert out["ok"] is True
    assert [s["name"] for s in out["steps"]] == ["static", "contract", "live"]
    assert all(s["status"] == "ok" for s in out["steps"])


def test_lint_names_contract_violations(tool_ctx):
    bad = json.loads(json.dumps(VALID_CONFIG))
    bad["dials"][0]["default"] = "MOON"  # outside the declared enum
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["lint_canvas"].invoke({"config_json": json.dumps(bad)})
    assert out["ok"] is False
    contract = next(s for s in out["steps"] if s["name"] == "contract")
    assert any("MOON" in f["message"] for f in contract["findings"])


def test_lint_refuses_offscope_config(tool_ctx):
    other = {**VALID_CONFIG, "dataset": "somewhere_else"}
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["lint_canvas"].invoke({"config_json": json.dumps(other)})
    assert out["ok"] is False and "this job authors for" in out["error"]


def test_adhoc_sql_tile_lints_and_saves(tool_ctx, aws):
    config = json.loads(json.dumps(VALID_CONFIG))
    config["tiles"][0]["data"] = {
        "sql": "SELECT region AS label, SUM(amount) AS revenue FROM orders "
               "WHERE region = @region GROUP BY region",
        "parameters": [
            {"name": "region", "type": "string", "required": True,
             "example": "EMEA", "enum": ["EMEA", "NA"]},
        ],
        "bind": {"region": {"dial": "region"}},
    }
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["lint_canvas"].invoke({"config_json": json.dumps(config)})
    assert out["ok"] is True, out
    out = tools["save_canvas"].invoke({"config_json": json.dumps(config)})
    assert out["saved"] is True


def test_save_canvas_single_step_lifecycle(tool_ctx, aws):
    tools = _by_name(build_canvas_tools(tool_ctx))
    # The gate refuses a broken config outright.
    broken = {**VALID_CONFIG, "tiles": []}
    out = tools["save_canvas"].invoke({"config_json": json.dumps(broken)})
    assert out["saved"] is False
    assert tool_ctx.saved is False
    # A clean save CREATES the canvas — live immediately, no draft/submit.
    out = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert out["saved"] is True
    canvas_id = out["canvas_id"]
    row = aws["ddb"].get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": canvas_id}},
    )["Item"]
    assert row["status"]["S"] == cv.STATUS_CREATED
    assert row["data_domain"]["S"] == DOMAIN
    assert tool_ctx.saved is True
    # Saving again overwrites the SAME canvas (revision semantics).
    out = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert out["canvas_id"] == canvas_id


def test_render_degrades_without_browser_and_uses_renderer(tool_ctx):
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["render_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    summary = json.loads(out[0]["text"])
    assert summary["rendered"] is False and "no browser" in summary["error"]

    class FakeRenderer:
        def __init__(self):
            self.calls = []

        def render(self, *, config, results, dials):
            self.calls.append({"config": config, "results": results, "dials": dials})
            return {"rendered": True, "screenshots": [], "console_errors": []}

    tool_ctx.renderer = FakeRenderer()
    out = tools["render_canvas"].invoke(
        {"config_json": json.dumps(VALID_CONFIG), "dials_json": json.dumps({"region": "NA"})}
    )
    summary = json.loads(out[0]["text"])
    assert summary["rendered"] is True
    call = tool_ctx.renderer.calls[0]
    # The dial override reached execution AND the renderer payload.
    assert call["dials"]["region"] == "NA"
    assert call["results"]["rev"]["columns"] == ["label", "revenue"]


def test_render_returns_inline_images(tool_ctx, tmp_path):
    png = tmp_path / "canvas-light-1280.png"
    png.write_bytes(b"\x89PNG fake")

    class ShotRenderer:
        def render(self, **kwargs):
            return {
                "rendered": True,
                "screenshots": [
                    {"path": str(png), "theme": "light", "width": 1280},
                ],
                "console_errors": [],
            }

    tool_ctx.renderer = ShotRenderer()
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["render_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    kinds = [b.get("type") for b in out]
    # Text summary first, then the screenshot as an INLINE image block — the
    # model must SEE the pixels (no real filesystem in the sandbox).
    assert kinds[0] == "text" and "image" in kinds
    img = next(b for b in out if b["type"] == "image")
    assert img["source_type"] == "base64" and img["mime_type"] == "image/png"


def test_raising_tool_returns_corrective_error_not_a_crash(tool_ctx):
    """A tool raise must come back as an error RESULT — an uncaught NoSuchKey
    from a wiki read killed a whole job live; the model must get a string it
    can self-correct from instead."""

    class Exploding:
        def read_page(self, **kwargs):
            raise RuntimeError("NoSuchKey: The specified key does not exist.")

    tool_ctx.consumption = Exploding()
    tools = _by_name(build_all_tools(tool_ctx))
    out = tools["read_page"].invoke({"concept_id": "tables/ghost"})
    assert "NoSuchKey" in out["error"] and "retry" in out["error"]


def test_wiki_tools_scope_and_execute(tool_ctx):
    tools = _by_name(build_all_tools(tool_ctx))
    listed = tools["list_computations"].invoke({})
    names = [c.get("computation") for c in listed.get("computations", [])]
    assert "revenue_by_region" in names
    receipt = tools["run_computation"].invoke(
        {"name": "revenue_by_region", "parameters_json": json.dumps({"region": "EMEA"})}
    )
    assert receipt["executed"] is True and receipt["rows"] == [["EMEA", "42.5"]]


def test_wiki_tools_match_consumption_signatures(tool_ctx):
    """Every wiki tool forwards ONLY kwargs ConsumptionTools accepts: an
    autospec'd instance raises TypeError on any drift, which _corrective
    would otherwise hide as a per-call error string (semantic_search once
    passed `limit=`, grep a nonexistent `path=` — both failed every call)."""
    from unittest import mock

    from consumption_mcp.tools import ConsumptionTools

    tool_ctx.consumption = mock.create_autospec(ConsumptionTools, instance=True)
    tools = _by_name(build_all_tools(tool_ctx))
    calls = {
        "read_page": {"concept_id": "tables/orders"},
        "list_directory": {"path": "tables"},
        "semantic_search": {"query": "revenue", "top_k": 3},
        "grep": {"pattern": "region", "max_results": 5},
        "get_backlinks": {"concept_id": "tables/orders"},
        "list_computations": {},
        "describe_computation": {"name": "revenue_by_region"},
        "run_computation": {"name": "revenue_by_region", "parameters_json": "{}"},
    }
    for name, args in calls.items():
        out = tools[name].invoke(args)
        assert not (isinstance(out, dict) and "error" in out), (name, out)


def test_render_names_failed_tiles_and_uses_the_adhoc_runtime(tool_ctx):
    """A tile whose execution fails is reported in the text block (a data
    problem, not a layout one) and its error rides the payload to the
    harness; ad-hoc tiles resolve with the job's runtime, like lint/save."""
    seen = {}

    def execute(comp, values):
        seen.setdefault("runtimes", []).append(comp.runtime)
        raise RuntimeError("no glue_database for this source")

    class FakeRenderer:
        def render(self, *, config, results, dials):
            seen["results"] = results
            return {"rendered": True, "screenshots": [], "console_errors": []}

    cfg = json.loads(json.dumps(VALID_CONFIG))
    cfg["tiles"][0]["data"] = {
        "sql": "SELECT region AS label, 1 AS revenue FROM orders",
        "parameters": [],
    }
    cfg["dials"] = []
    tool_ctx.execute = execute
    tool_ctx.renderer = FakeRenderer()
    tool_ctx.adhoc_runtime = "redshift"
    tools = _by_name(build_canvas_tools(tool_ctx))
    out = tools["render_canvas"].invoke({"config_json": json.dumps(cfg)})
    summary = json.loads(out[0]["text"])
    assert "no glue_database" in summary["tile_errors"]["rev"]
    assert seen["results"]["rev"]["error"]
    assert seen["runtimes"] == ["redshift"]


def _rename_row(aws, canvas_id, title):
    import datetime

    aws["ddb"].update_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": canvas_id}},
        UpdateExpression="SET title = :t, updated_at = :u",
        ExpressionAttributeValues={
            ":t": {"S": title},
            ":u": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        },
    )


def test_save_never_overwrites_a_human_edit_made_mid_job(tool_ctx, aws):
    tools = _by_name(build_canvas_tools(tool_ctx))
    first = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert first["saved"] is True
    _rename_row(aws, first["canvas_id"], "Renamed by the user")

    out = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert out["saved"] is False and "edited this canvas" in out["error"]
    assert out["current_layouts"]["rev"] == VALID_CONFIG["tiles"][0]["layout"]
    row = aws["ddb"].get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": first["canvas_id"]}},
    )["Item"]
    assert row["title"]["S"] == "Renamed by the user"  # not reverted

    # The refusal adopted the current version: the merged retry lands.
    merged = {**VALID_CONFIG, "title": "Renamed by the user"}
    assert tools["save_canvas"].invoke({"config_json": json.dumps(merged)})["saved"]


def test_save_does_not_resurrect_a_new_canvas_deleted_after_its_first_save(
    tool_ctx, aws
):
    tools = _by_name(build_canvas_tools(tool_ctx))
    first = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    aws["ddb"].delete_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": first["canvas_id"]}},
    )
    out = tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
    assert out["saved"] is False and "deleted" in out["error"]
    assert "Item" not in aws["ddb"].get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": first["canvas_id"]}},
    )

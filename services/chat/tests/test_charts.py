"""The render_chart tool: it's an inert transport (no server work), its return is
an ack the model reads, and its description carries the full authoring contract.

render_chart does NOT touch AWS or execute the model's code — the browser renders
the chart in a sandboxed iframe. So these tests assert the tool's SHAPE (schema,
description contract) and its ack behavior, plus that it's always wired into the
agent's toolset (no deploy flag / opt-in, unlike run_sql). The rendering itself is
verified on the UI side.
"""

from __future__ import annotations

import json

from chat.charts import (
    RENDER_CHART_DESC,
    SUPPORTED_CHART_TYPES,
    make_chart_tool,
    render_chart_ack,
)


# --- the tool's shape + authoring contract ----------------------------------


def test_chart_tool_schema_is_code_and_title():
    tool = make_chart_tool()
    assert tool.name == "render_chart"
    # The model authors `code` (required-ish) + a `title`. No location/scope args:
    # a chart is drawn from data the model already has, not fetched.
    assert set(tool.args) == {"code", "title"}


def test_chart_tool_description_documents_the_render_api():
    # The description is the model's ONLY spec for how to author a chart (kept out
    # of the system prompt so the base agent stays brace-free + cacheable). It must
    # name the exact in-frame global and the spec fields the UI helper reads.
    assert "renderChart(el, spec)" in RENDER_CHART_DESC
    for field in ("type", "labels", "series", "title"):
        assert field in RENDER_CHART_DESC
    # Every supported chart type is offered to the model.
    for t in SUPPORTED_CHART_TYPES:
        assert t in RENDER_CHART_DESC
    # The load-bearing guardrails: real data only + don't hard-code colors (match
    # the app palette). These are what keep charts truthful + on-brand.
    low = RENDER_CHART_DESC.lower()
    assert "real" in low and "invent" in low
    assert "--chart-1" in RENDER_CHART_DESC and "color" in low


# --- the ack (what flows back to the model, not a render result) ------------


def test_chart_ack_signals_handoff_not_success():
    ack = render_chart_ack("Race wins")
    assert ack["status"] == "rendered"
    assert ack["title"] == "Race wins"
    # The ack tells the model to CONTINUE (the render happens out-of-band in the
    # browser); it deliberately carries no success/failure of the actual render.
    assert "continue" in ack["note"].lower()


def test_chart_tool_returns_ack_json_for_valid_code():
    tool = make_chart_tool()
    out = tool.invoke(
        {
            "code": "renderChart(el, {type:'bar', labels:['a'], series:[{name:'x', data:[1]}]});",
            "title": "T",
        }
    )
    data = json.loads(out)
    assert data["status"] == "rendered"
    assert data["title"] == "T"


def test_chart_tool_defaults_title_when_missing():
    tool = make_chart_tool()
    out = tool.invoke({"code": "renderChart(el, {type:'pie', labels:['a'], series:[{data:[1]}]});"})
    assert json.loads(out)["title"] == "Chart"


def test_chart_tool_errors_on_empty_code():
    # Empty/whitespace code can't render anything — return a clean error the model
    # can react to, rather than handing the UI a no-op.
    tool = make_chart_tool()
    out = tool.invoke({"code": "   ", "title": "T"})
    data = json.loads(out)
    assert data["status"] == "error"
    assert "renderChart" in data["error"]


# --- always wired into the agent's toolset ----------------------------------


def test_chart_tool_is_always_in_the_agent_toolset():
    """render_chart is appended unconditionally by make_agent_factory (no deploy
    flag, no per-run opt-in — unlike run_sql). Drive the factory with stub clients
    and a captured build_graph to assert the tool list the agent is built with.
    """
    import sys
    import types

    import chat.server as server
    from chat.config import ChatConfig

    captured = {}

    def fake_build_graph(model, tools, checkpointer, *, system_prompt=None):
        captured["names"] = [t.name for t in tools]
        return object()

    # Patch build_graph + build_chat_model (imported inside make_agent_factory's
    # build_agent) so nothing hits Bedrock. make_agent_tools is real (pure).
    server_charts = sys.modules["chat.charts"]

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

    # Minimal consumption config + fake clients (never called: make_agent_tools
    # only wraps the methods; build_chat_model is stubbed).
    from consumption_mcp.tools import ConsumptionConfig

    from .fakes import FakeConsumptionTools

    cons_cfg = ConsumptionConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r"
    )

    # Stub build_consumption_tools to return a FakeConsumptionTools, and
    # build_chat_model / build_graph to avoid AWS + capture the toolset.
    import chat.tools as chat_tools

    orig_bct = chat_tools.build_consumption_tools
    fake_tools_impl = FakeConsumptionTools()

    # make_agent_factory imports these names lazily inside the function body, from
    # their defining modules — patch there.
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod

    orig_build_graph = chat_graph_mod.build_graph
    orig_build_model = chat_config_mod.build_chat_model
    orig_build_cons = server.__dict__.get("build_consumption_tools")

    try:
        chat_graph_mod.build_graph = fake_build_graph
        chat_config_mod.build_chat_model = lambda *a, **k: object()
        # Patch build_consumption_tools where make_agent_factory looks it up.
        chat_tools.build_consumption_tools = lambda **kw: fake_tools_impl

        build_agent = server.make_agent_factory(cfg, cons_cfg, {"s3": None, "s3vectors": None, "bedrock_runtime": None, "ddb": None})
        build_agent("us.anthropic.claude-opus-4-8", "high", None, object(), features=set())
    finally:
        chat_graph_mod.build_graph = orig_build_graph
        chat_config_mod.build_chat_model = orig_build_model
        chat_tools.build_consumption_tools = orig_bct

    assert "render_chart" in captured["names"]
    # And the wiki read tools are still there (chart is additive, not a swap).
    assert "read_page" in captured["names"]
    assert "semantic_search" in captured["names"]

"""Agent tool wrapping: schema shape, dataset-scope injection, delegation.

Uses FakeConsumptionTools (records call args) so we can prove the scoped
data_domain/dataset are injected at call time and dropped from the LLM-visible
schema. Needs langchain_core (installed) for StructuredTool.
"""

from __future__ import annotations

from chat.tools import make_agent_tools

from .fakes import FakeConsumptionTools


def _by_name(tools):
    return {t.name: t for t in tools}


def test_unscoped_exposes_all_ten_tools_with_location_args():
    tools = _by_name(make_agent_tools(FakeConsumptionTools()))
    assert set(tools) == {
        "list_domains",
        "list_declared_domains",
        "search_domains",
        "list_directory",
        "read_page",
        "get_backlinks",
        "glob",
        "grep",
        "semantic_search",
        "get_bundle_diff",
    }
    # read_page keeps its location args when unscoped.
    assert set(tools["read_page"].args) == {
        "concept_id",
        "data_domain",
        "dataset",
        "offset",
        "limit",
    }


def test_scoped_drops_location_args_from_schema():
    tools = _by_name(
        make_agent_tools(
            FakeConsumptionTools(),
            dataset_scope={"data_domain": "sales", "dataset": "orders"},
        )
    )
    # data_domain/dataset are removed from what the model sees.
    assert set(tools["read_page"].args) == {"concept_id", "offset", "limit"}
    assert "data_domain" not in tools["glob"].args
    assert "dataset" not in tools["glob"].args
    # tools without location args are unaffected.
    assert tools["list_domains"].args == {}
    assert set(tools["search_domains"].args) == {"query", "top_k"}


def test_scoped_injects_domain_dataset_at_call_time():
    fake = FakeConsumptionTools()
    tools = _by_name(
        make_agent_tools(
            fake, dataset_scope={"data_domain": "sales", "dataset": "orders"}
        )
    )
    result = tools["read_page"].invoke({"concept_id": "tables/orders", "offset": 5})
    assert result == {
        "concept_id": "tables/orders",
        "data_domain": "sales",
        "dataset": "orders",
    }
    name, kwargs = fake.calls[-1]
    assert name == "read_page"
    assert kwargs["data_domain"] == "sales"
    assert kwargs["dataset"] == "orders"
    assert kwargs["concept_id"] == "tables/orders"
    assert kwargs["offset"] == 5


def test_unscoped_passes_through_caller_location_args():
    fake = FakeConsumptionTools()
    tools = _by_name(make_agent_tools(fake))
    tools["grep"].invoke(
        {"pattern": "raceId", "data_domain": "ops", "dataset": "logs"}
    )
    name, kwargs = fake.calls[-1]
    assert name == "grep"
    assert kwargs["data_domain"] == "ops"
    assert kwargs["dataset"] == "logs"


def test_scoped_tool_description_lifted_from_method_docstring():
    tools = _by_name(
        make_agent_tools(
            FakeConsumptionTools(),
            dataset_scope={"data_domain": "sales", "dataset": "orders"},
        )
    )
    # docstring-derived description survives the wrapper.
    assert "concept" in tools["read_page"].description.lower()


def test_descriptions_are_cleandoc_normalized_markdown():
    """A docstring's continuation indent and RST double-backticks are tokens the
    model pays for on EVERY request and learns nothing from. Descriptions must be
    cleandoc'd (no leading whitespace on continuation lines) and use Markdown
    single backticks.
    """
    for tool in make_agent_tools(FakeConsumptionTools()):
        desc = tool.description
        assert "``" not in desc, f"{tool.name} still has RST double-backticks"
        for line in desc.splitlines():
            # Indented lines are legitimate INSIDE a doc (bullet continuations,
            # literal blocks); what must be gone is the uniform docstring indent,
            # i.e. every non-blank line after the first being indented.
            assert not line.startswith(" " * 8), f"{tool.name}: 8-space indent kept"
        assert desc == desc.strip()


# --- tool errors come back as a RESULT, not a crash --------------------------


class _BoomTools(FakeConsumptionTools):
    """A tools double whose read_page raises a NoSuchKey-style error, and whose
    grep raises a ValueError (bad input) — to prove both degrade to a result."""

    def read_page(self, *a, **k):
        raise Exception(
            "An error occurred (NoSuchKey) when calling the GetObject operation: "
            "The specified key does not exist."
        )

    def grep(self, *a, **k):
        raise ValueError("invalid regex pattern: unbalanced parenthesis")


def test_tool_error_is_returned_as_result_not_raised():
    tools = _by_name(make_agent_tools(_BoomTools()))
    # The wrapper must NOT propagate — it returns the error text so the agent loop
    # gets a ToolMessage and keeps going (regression for the NoSuchKey crash).
    out = tools["read_page"].invoke(
        {"concept_id": "tables/x", "data_domain": "bird", "dataset": "formula_1"}
    )
    assert isinstance(out, str)
    assert out.startswith("Error:")
    assert "NoSuchKey" in out


def test_tool_valueerror_is_returned_concisely():
    tools = _by_name(make_agent_tools(_BoomTools()))
    out = tools["grep"].invoke(
        {"pattern": "(", "data_domain": "bird", "dataset": "formula_1"}
    )
    assert out == "Error: invalid regex pattern: unbalanced parenthesis"


def test_tool_error_handling_works_scoped_too():
    tools = _by_name(
        make_agent_tools(
            _BoomTools(), dataset_scope={"data_domain": "bird", "dataset": "formula_1"}
        )
    )
    out = tools["read_page"].invoke({"concept_id": "tables/x"})
    assert isinstance(out, str) and out.startswith("Error:")


# --- submit_annotation (agent files on the user's behalf) ---------------------


class _FakeTable:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)


def test_submit_annotation_files_in_users_partition_with_agent_provenance():
    import json as _json

    from chat.tools import make_submit_annotation_tool

    table = _FakeTable()
    tool = make_submit_annotation_tool(
        table,
        dataset_scope={"data_domain": "sales", "dataset": "orders"},
        user_sub="sub-123",
    )
    out = _json.loads(
        tool.func(note="join cardinality is wrong", concept_id="tables/races")
    )
    assert out["status"] == "filed" and out["dataset_wide"] is False
    item = table.items[0]
    assert item["pk"] == "ANNO#sales#orders#sub-123"  # isolation via verified sub
    assert item["submitted_via"] == "agent"
    assert item["quote"] == ""  # agent notes are unanchored
    assert item["status"] == "open"


def test_submit_annotation_defaults_to_dataset_wide_sentinel():
    import json as _json

    from chat.tools import make_submit_annotation_tool

    table = _FakeTable()
    tool = make_submit_annotation_tool(
        table,
        dataset_scope={"data_domain": "sales", "dataset": "orders"},
        user_sub="sub-123",
    )
    out = _json.loads(tool.func(note="docs miss the late-arriving-data caveat"))
    assert out["dataset_wide"] is True and out["concept_id"] == "_dataset"
    assert table.items[0]["sk"].startswith("_dataset#")
    # Bad input comes back as a tool-result error, never a raise.
    err = _json.loads(tool.func(note="x", concept_id="bad##id"))
    assert "invalid concept_id" in err["error"]
    assert len(table.items) == 1


def test_submit_annotation_description_is_cleandoc_and_explains_extra_args():
    from chat.tools import make_submit_annotation_tool

    table = _FakeTable()
    scoped = make_submit_annotation_tool(
        table, dataset_scope={"data_domain": "sales", "dataset": "orders"},
        user_sub="s",
    )
    unscoped = make_submit_annotation_tool(table, user_sub="s")
    # cleandoc'd: no 8-space continuation indent from the nested triple-quote.
    for tool in (scoped, unscoped):
        assert not any(
            line.startswith(" " * 8) for line in tool.description.splitlines()
        )
    # The UNSCOPED variant takes two more args, so it must explain them — this is
    # the only place data_domain/dataset are documented for the model.
    assert "data_domain" not in scoped.description
    assert "`data_domain`/`dataset` name the dataset" in unscoped.description
    assert "list_domains" in unscoped.description
    # Both keep the shared body (confirm-first + the concept_id semantics).
    for tool in (scoped, unscoped):
        assert "ALWAYS confirm" in tool.description
        assert "`concept_id` targets one" in tool.description


def test_submit_annotation_unscoped_takes_dataset_args():
    import json as _json

    from chat.tools import make_submit_annotation_tool

    table = _FakeTable()
    tool = make_submit_annotation_tool(table, user_sub="sub-9")
    # The model must name the dataset explicitly when the run isn't scoped.
    assert {"note", "data_domain", "dataset", "concept_id"} <= set(tool.args)
    out = _json.loads(
        tool.func(note="stale enum", data_domain="sales", dataset="orders")
    )
    assert out["status"] == "filed"
    assert table.items[0]["pk"] == "ANNO#sales#orders#sub-9"
    err = _json.loads(tool.func(note="x", data_domain="", dataset="orders"))
    assert "required" in err["error"]

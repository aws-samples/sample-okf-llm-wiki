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


def test_unscoped_exposes_all_nine_tools_with_location_args():
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

"""server.py registers the tools and delegates to ConsumptionTools.

We do not need FastMCP installed: register_tools takes any object with a
``tool()`` method returning a decorator, so we pass a tiny fake registry and
assert the tools are registered and delegate correctly.
"""

from __future__ import annotations

from consumption_mcp import server

from .conftest import DATASET, DOMAIN


class FakeMCP:
    """Mimics the FastMCP ``@mcp.tool()`` registration contract."""

    def __init__(self):
        self.registered: dict[str, object] = {}
        self.descriptions: dict[str, str | None] = {}

    def tool(self, description: str | None = None):
        def deco(fn):
            self.registered[fn.__name__] = fn
            self.descriptions[fn.__name__] = description
            return fn

        return deco


def test_register_tools_registers_all(tools):
    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    assert set(mcp.registered) == {
        "read_me",
        "list_domains",
        "list_declared_domains",
        "search_domains",
        "list_directory",
        "read_page",
        "get_backlinks",
        "semantic_search",
        "glob",
        "grep",
    }


def test_read_me_leads_with_the_use_first_instruction(tools):
    # The description is the contract: an agent must be told to call this
    # BEFORE exploring, and the body must teach the moves this surface has.
    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    desc = mcp.descriptions["read_me"]
    assert desc is not None
    assert desc.startswith("Use this tool FIRST, before exploring the wiki")
    primer = mcp.registered["read_me"]()
    assert "get_backlinks" in primer
    assert "index.md" in primer


def test_registered_wrappers_delegate(tools):
    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    page = mcp.registered["read_page"]("tables/races", DOMAIN, DATASET, 0, 2)
    assert page["returned_lines"] == 2
    domains = mcp.registered["list_domains"]()
    assert any(d["dataset"] == DATASET for d in domains)


def test_glob_and_grep_wrappers_delegate(tools):
    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    globbed = mcp.registered["glob"]("tables/*", DOMAIN, DATASET)
    assert {g["concept_id"] for g in globbed} == {"tables/races", "tables/results"}
    grepped = mcp.registered["grep"]("Races table", DOMAIN, DATASET)
    assert grepped["match_count"] >= 1
    assert grepped["matches"][0]["concept_id"] == "tables/races"

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


def _wrapper_doc(tools, name: str) -> str:
    """The description an MCP client sees for a wrapper (its own docstring)."""
    import inspect

    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    return inspect.getdoc(mcp.registered[name]) or ""


def test_read_page_wrapper_explains_paging(tools):
    doc = _wrapper_doc(tools, "read_page")
    assert "0-indexed" in doc
    assert "total_lines" in doc and "returned_lines" in doc


def test_get_backlinks_wrapper_names_the_heading(tools):
    doc = _wrapper_doc(tools, "get_backlinks")
    assert "heading" in doc


def test_semantic_search_wrapper_documents_the_type_vocabulary(tools):
    from okf_core.concept_types import (
        CROSS_DATASET_REFERENCE_TYPE,
        GLUE_DATABASE_TYPE,
        GLUE_TABLE_TYPE,
        REDSHIFT_DATABASE_TYPE,
        REDSHIFT_EXTERNAL_TABLE_TYPE,
        REDSHIFT_TABLE_TYPE,
    )
    from okf_core.domain import DOMAIN_DOC_TYPE

    doc = _wrapper_doc(tools, "semantic_search")
    # `type` is an EXACT filter match: a value outside the vocabulary returns
    # nothing with no error, so external MCP agents need the list too.
    assert "EXACT" in doc
    for concept_type in (
        GLUE_TABLE_TYPE,
        GLUE_DATABASE_TYPE,
        REDSHIFT_TABLE_TYPE,
        REDSHIFT_EXTERNAL_TABLE_TYPE,
        REDSHIFT_DATABASE_TYPE,
        CROSS_DATASET_REFERENCE_TYPE,
        DOMAIN_DOC_TYPE,
    ):
        assert concept_type in doc, concept_type
    assert "`Reference`" in doc and "`Playbook`" in doc
    assert "ANY given tag" in doc
    from consumption_mcp import tools as toolmod

    assert f"capped at {toolmod._SEMANTIC_TOP_K_MAX}" in doc


def test_list_domains_wrapper_surfaces_the_cross_reference_semantics(tools):
    # This signal was absent from the WHOLE MCP surface: an external agent got the
    # fields in the payload with nothing telling it which direction each means or
    # where to read that direction's pair docs.
    doc = _wrapper_doc(tools, "list_domains")
    assert "cross_references" in doc and "cross_referenced_by" in doc
    assert "external/<d>/<ds>/" in doc
    assert "<that dataset>/external/<this>/" in doc


def test_glob_and_grep_wrappers_delegate(tools):
    mcp = FakeMCP()
    server.register_tools(mcp, tools)
    globbed = mcp.registered["glob"]("tables/*", DOMAIN, DATASET)
    assert {g["concept_id"] for g in globbed} == {"tables/races", "tables/results"}
    grepped = mcp.registered["grep"]("Races table", DOMAIN, DATASET)
    assert grepped["match_count"] >= 1
    assert grepped["matches"][0]["concept_id"] == "tables/races"

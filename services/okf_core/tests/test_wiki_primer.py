"""The read_me primer: short, and accurate to each surface's toolset."""

from okf_core import wiki_primer as wp


def test_description_opens_with_the_use_first_instruction():
    # The tool definition must LEAD with when to call it — before exploring.
    assert wp.READ_ME_DESCRIPTION.startswith(
        "Use this tool FIRST, before exploring the wiki"
    )


def test_both_primers_cover_structure_and_trap_locations():
    for primer in (wp.MCP_PRIMER, wp.SOLVER_PRIMER):
        assert "index.md" in primer
        assert "usage_guardrails" in primer
        assert "known_issues" in primer
        assert "grain" in primer
        # The honesty rule: never invent what the wiki doesn't state.
        assert "not tracked" in primer


def test_each_primer_names_only_the_tools_its_surface_has():
    # MCP: backlinks + semantic search exist and are the underused moves.
    assert "get_backlinks" in wp.MCP_PRIMER
    assert "semantic_search" in wp.MCP_PRIMER
    assert "read_page" in wp.MCP_PRIMER
    # Solver: those tools DON'T exist there — naming them would teach the
    # solver to make failing calls. Its grep is literal, not regex.
    assert "get_backlinks" not in wp.SOLVER_PRIMER
    assert "semantic_search" not in wp.SOLVER_PRIMER
    assert "LITERAL" in wp.SOLVER_PRIMER


def test_primers_stay_short():
    # "On point, not super long" is part of the contract: a primer the agent
    # skims past is dead weight in every solve's context. Budget, not vibes.
    for primer in (wp.MCP_PRIMER, wp.SOLVER_PRIMER):
        assert len(primer) < 3000, f"primer grew to {len(primer)} chars"

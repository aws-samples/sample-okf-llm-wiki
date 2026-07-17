"""reference-author sub-agent: prompt contract + supervisor fan-out + guardrails.

Mirrors test_context_extractor_wiring.py — asserts prompt strings and the builder
source (deepagents/AWS aren't present offline), so the wiring is checked without
constructing the agent.
"""

from harvest import prompts


def test_reference_author_prompt_owns_cross_cutting_refs_only():
    p = prompts.REFERENCE_AUTHOR_PROMPT
    # Authors exactly one cross-cutting reference file.
    assert "EXACTLY ONE" in p
    assert "references/metrics/" in p
    assert "references/named_sets/" in p
    assert "references/usage_guardrails.md" in p
    # Does NOT re-author the per-table enums/joins (table-author owns those).
    low = p.lower()
    assert "table-author" in low
    assert "enums" in low and "joins" in low


def test_reference_author_verifies_against_live_data():
    p = prompts.REFERENCE_AUTHOR_PROMPT
    # A brief is a hypothesis confirmed against the source, not transcribed.
    low = p.lower()
    assert "hypothesis" in low
    assert "run_sql" in p
    assert "data wins" in low
    # Shares the runtime conventions (built from _RUNTIME).
    assert "okf-authoring" in p


def test_reference_author_guardrail_rules_are_derived_not_invented():
    p = prompts.REFERENCE_AUTHOR_PROMPT
    # The guardrails doc concentrates ASK/BLOCK/REFUSE + additivity, sourced from
    # verified facts + .context/, never invented.
    for token in ("usage_guardrails", "additivity", "ASK", "BLOCK", "REFUSE"):
        assert token in p, token
    low = p.lower()
    assert "never" in low and "invent" in low  # never invent a rule


def test_supervisor_fans_out_reference_authors_and_always_writes_guardrails():
    p = prompts.SUPERVISOR_PROMPT
    assert "reference-author" in p
    # Discovers instances then dispatches (does not first-draft them).
    low = p.lower()
    assert "fan out" in low or "fan-out" in low or "dispatch" in low
    # The guardrails doc is ALWAYS authored.
    assert "usage_guardrails.md" in p
    # The dataset overview must link the guardrails doc (guaranteed-read pointer).
    # ("read first" may wrap across a line in the prompt source, so normalize.)
    norm = " ".join(low.split())
    assert "read\nfirst" in low or "read first" in norm
    assert "datasets/<dataset>.md" in p


def test_reference_author_is_wired_as_a_subagent():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    assert '"name": "reference-author"' in src
    assert "reference_author" in src
    assert (
        "subagents=[table_author, reference_author, reviewer, context_extractor]"
        in src
    )

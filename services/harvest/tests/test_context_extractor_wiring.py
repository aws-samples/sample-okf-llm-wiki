"""Context fact-extractor sub-agent: prompt contract + supervisor fan-out wiring.

Mirrors test_review_wiring.py — these assert the prompt strings only (deepagents/
quickjs aren't needed locally; the interpreter middleware is imported lazily in
build_harvest_agent and degrades gracefully when absent).
"""

from harvest import prompts


def test_context_extractor_prompt_is_read_only_and_returns_plaintext():
    p = prompts.CONTEXT_EXTRACTOR_PROMPT
    # Read-only: it mines .context/ but writes nothing to the bundle.
    assert "READ-ONLY" in p
    assert "writes nothing" in p.lower() or "write NOTHING" in p
    # Returns plain markdown prose, not structured output (same constraint as the
    # reviewer — thinking-on models reject native structured output).
    assert "plain markdown" in p.lower() or "plain text" in p.lower()
    assert "structured output" in p.lower()


def test_context_extractor_uses_fact_types_and_verifies():
    p = prompts.CONTEXT_EXTRACTOR_PROMPT
    # Extracts through the fact-type lens and names CODE_ENUM as the top find.
    assert "fact-types.md" in p
    assert "CODE_ENUM" in p
    # Facts are hypotheses verified against live data, not transcribed on faith.
    low = p.lower()
    assert "hypothesis" in low
    assert "run_sql" in p
    assert "data wins" in low
    # Reads both text and binary .context/ docs (the run_code sandbox).
    assert "run_code" in p
    assert "/tmp/okf_context/" in p


def test_context_extractor_returns_routed_digest():
    # The output is a routed digest: each fact tagged with its target concept id +
    # section so the supervisor/table-authors can place it without re-reading.
    p = prompts.CONTEXT_EXTRACTOR_PROMPT
    low = p.lower()
    assert "digest" in low
    assert "concept id" in low
    assert "citations" in low  # source .context/<file> for provenance


def test_context_extractor_shares_runtime_conventions():
    # Built by concatenating _RUNTIME (fixed source tools / dialect / no-web rule).
    assert "run_sql" in prompts.CONTEXT_EXTRACTOR_PROMPT
    assert "okf-authoring" in prompts.CONTEXT_EXTRACTOR_PROMPT


def test_supervisor_dispatches_context_extractor_before_authoring():
    p = prompts.SUPERVISOR_PROMPT
    assert "context-extractor" in p
    # Reached for LARGE .context/ so docs are read once, not re-read per author.
    low = p.lower()
    assert "read once" in low or "read them once" in low or "read once" in low
    assert "re-read" in low
    # Digest is threaded to the table-authors.
    assert "digest" in low


def test_context_extractor_is_wired_as_a_subagent():
    # The builder registers context-extractor alongside table-author + reviewer.
    # Assert on source rather than building the agent (deepagents/AWS not present
    # offline), consistent with the other wiring tests.
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    assert '"name": "context-extractor"' in src
    assert "context_extractor" in src
    assert (
        "subagents=[table_author, reference_author, reviewer, context_extractor]"
        in src
    )

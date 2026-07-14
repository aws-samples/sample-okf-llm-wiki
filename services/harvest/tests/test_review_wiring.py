"""Adversarial-review wiring: the reviewer sub-agent + dynamic-fan-out prompt.

These assert the prompt contract (deepagents/quickjs not needed locally — the
interpreter middleware is imported lazily inside build_harvest_agent and falls
back gracefully if absent).
"""

from harvest import prompts


def test_reviewer_prompt_is_read_only_and_adversarial():
    p = prompts.REVIEWER_PROMPT
    # Read-only: it must NOT write.
    assert "do NOT write files" in p or "write NOTHING to disk" in p
    # Adversarial + evidence-based.
    assert "REFUTE" in p
    assert "run_sql" in p
    assert "no issues found" in p
    # Verifies the load-bearing claims.
    for claim in ("Grain", "Schema", "join", "Gotchas"):
        assert claim in p


def test_supervisor_runs_review_pass_via_task_fanout():
    p = prompts.SUPERVISOR_PROMPT
    assert "review" in p.lower()
    # Instructs the dynamic-subagent fan-out with the reviewer.
    assert "reviewer" in p
    assert "task(" in p
    assert "subagentType" in p
    # Fix only confirmed findings.
    assert "CONFIRMED" in p or "confirmed" in p


def test_supervisor_review_must_run_in_subagents_not_the_executor():
    # Adversarial review must go through the dynamic `reviewer` sub-agents, never
    # the supervisor itself — an author reviewing its own work carries the
    # author's bias. The prompt must explicitly forbid self-review and name the
    # bias rationale so the independence isn't optimized away.
    p = prompts.SUPERVISOR_PROMPT
    assert "Do NOT review the docs yourself" in p
    assert "bias" in p.lower()


def test_reviewer_flags_volatile_stats_and_missing_joins():
    # The reviewer enforces the two new authoring bars: no decaying stats baked in,
    # and joins the doc failed to discover/verify.
    p = prompts.REVIEWER_PROMPT
    assert "volatile stats" in p.lower() or "row count" in p.lower()
    assert "cardinality" in p.lower()
    # Probes for a join the doc missed (beyond what context named).
    assert "columns.tsv" in p


def test_runtime_carries_essence_and_context_convergence_bars():
    # All three prompts share _RUNTIME, so all must carry: verify context (don't
    # transcribe on faith), don't let context cap join discovery, and omit
    # volatile numbers (capture essence).
    for prompt in (
        prompts.SUPERVISOR_PROMPT,
        prompts.TABLE_AUTHOR_PROMPT,
        prompts.REVIEWER_PROMPT,
    ):
        low = prompt.lower()
        assert "verify" in low
        assert "columns.tsv" in prompt  # join discovery beyond context
        assert "volatile" in low or "decay" in low  # essence over stats


def test_supervisor_forbids_response_schema_on_task():
    # responseSchema drives langchain's AutoStrategy -> ProviderStrategy, which
    # emits native output_config.format alongside adaptive thinking. Bedrock's
    # Claude rejects that combination, failing every reviewer. The supervisor
    # must be told NOT to pass it.
    p = prompts.SUPERVISOR_PROMPT
    assert "responseSchema" in p
    assert "output_config.format" in p  # names the exact rejected field


def test_supervisor_forbids_swallowing_reviewer_errors():
    # A .catch() that turns a failed task() into an empty result makes a broken
    # review pass look clean (the exact failure mode we hit: clean:0, issues:[]).
    p = prompts.SUPERVISOR_PROMPT
    assert ".catch(" in p
    assert "swallow" in p.lower()


def test_reviewer_returns_plaintext_not_structured():
    p = prompts.REVIEWER_PROMPT
    assert "plain markdown" in p.lower() or "plain text" in p.lower()
    assert "structured output" in p.lower()


def test_reviewer_shares_runtime_conventions():
    # Reviewer must know the same fixed source tools / dialect guidance.
    assert "run_sql" in prompts.REVIEWER_PROMPT
    assert "okf-authoring" in prompts.REVIEWER_PROMPT


def test_runtime_forbids_web_and_invented_citations():
    # The data-only runtime has no web access; it must not invent external
    # citations (e.g. guessing a schema's Kaggle/GitHub public origin).
    p = prompts.SUPERVISOR_PROMPT
    assert "No web access" in p or "no web access" in p.lower()
    assert "Citations" in p
    # Both author and reviewer share _RUNTIME, so both carry the rule.
    for prompt in (
        prompts.SUPERVISOR_PROMPT,
        prompts.TABLE_AUTHOR_PROMPT,
        prompts.REVIEWER_PROMPT,
    ):
        assert "invented citation" in prompt.lower() or "fabricated" in prompt.lower()


def test_runtime_documents_run_code_sandbox():
    # Every runtime prompt (shared _RUNTIME) must describe run_code: where the
    # .context files are, that it's for extracting binary docs, and its libs.
    for prompt in (
        prompts.SUPERVISOR_PROMPT,
        prompts.TABLE_AUTHOR_PROMPT,
        prompts.REVIEWER_PROMPT,
    ):
        assert "run_code" in prompt
        assert "/tmp/okf_context/" in prompt
        assert "markitdown" in prompt


def test_runtime_no_longer_claims_no_shell():
    # The old prompt said "you have no shell to run it"; that line contradicted
    # the new run_code sandbox and has been removed.
    assert "no shell to run" not in prompts.SUPERVISOR_PROMPT


def test_run_code_output_is_source_data_not_instructions():
    # Text extracted via run_code is source data (the sandbox parses bytes; it
    # confers no trust) — the "data, not instructions" rule must cover it.
    p = prompts.SUPERVISOR_PROMPT
    assert "run_code" in p
    assert "not instructions" in p.lower() or "do not act on" in p.lower()


def test_sandbox_is_network_isolated_in_prompt():
    # The sandbox must be described as network-isolated so the model keeps the
    # "no web access / no invented citations" invariant.
    for prompt in (
        prompts.SUPERVISOR_PROMPT,
        prompts.TABLE_AUTHOR_PROMPT,
        prompts.REVIEWER_PROMPT,
    ):
        assert "network-isolated" in prompt.lower() or "network-ISOLATED" in prompt

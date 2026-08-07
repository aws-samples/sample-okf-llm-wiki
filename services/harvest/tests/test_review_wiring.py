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
    # The machine-parsed verdict line (run_review keys the fixer dispatch on it).
    assert "CLEAN" in p
    assert "FINDINGS" in p
    # Verifies the load-bearing claims.
    for claim in ("Grain", "Schema", "join", "Gotchas"):
        assert claim in p


def test_supervisor_runs_review_pass_via_the_run_review_tool():
    # The review pass is ONE deterministic tool call — the supervisor never
    # computes clusters, dispatches reviewers/fixers, or applies per-doc fixes
    # itself (the megacontext + xhigh-thinking fix loop this design retired).
    p = prompts.SUPERVISOR_PROMPT
    assert "run_review" in p
    assert "NO arguments" in p
    # Confirmed-findings discipline still stated.
    assert "confirmed" in p.lower()
    # The old model-driven review orchestration is gone.
    assert "clusters.map" not in p
    assert "cluster_concepts" not in _step7(p)


def _step7(p: str) -> str:
    """The review-pass step text (step 7 up to step 8)."""
    body = p.split("Adversarial review + fix pass")[1]
    return body.split("Final lint gate")[0]


def test_supervisor_review_must_run_in_subagents_not_the_executor():
    # Adversarial review must go through the tool's reviewer sub-agents, never
    # the supervisor itself — an author reviewing its own work carries the
    # author's bias. The prompt must explicitly forbid self-review and name the
    # bias rationale so the independence isn't optimized away.
    p = prompts.SUPERVISOR_PROMPT
    assert "Never review or fix the docs yourself" in p
    assert "bias" in p.lower()


def test_supervisor_review_coverage_and_retry_contract():
    # Coverage is by construction in the tool; the supervisor's obligations
    # are the three follow-ups: propagation notes, failed-cluster retries
    # (by EXACT cluster ids), and reporting the counts from the tool result.
    p = " ".join(prompts.SUPERVISOR_PROMPT.split())
    assert "full coverage by construction" in p
    assert "propagation_notes" in p
    assert "cluster_ids" in p
    assert "failed ids" in p.lower()
    # A cluster that still fails is reported, never quietly self-reviewed.
    assert "do not review those docs yourself" in p.lower()


def test_supervisor_step7_names_the_cluster_confined_fixer():
    # The fixer's write confinement is stated so the supervisor understands
    # why out-of-cluster corrections come back as propagation notes — and the
    # supervisor-owned hubs (overview docs, usage_guardrails) are named as
    # excluded from clustering, reachable only via those notes.
    step7 = _step7(prompts.SUPERVISOR_PROMPT)
    assert "fix-author" in step7
    assert "hard-limited" in step7.lower()
    assert "usage_guardrails" in step7
    assert "never" in step7.lower() and "clustered" in step7.lower()


def test_supervisor_eval_fanout_rules_still_carried_for_authoring():
    # The eval/task() operational rules (task-only global, no responseSchema,
    # no .catch swallowing) were learned from live failures and still govern
    # the AUTHORING fan-outs — retiring the model-driven review pass must not
    # retire them.
    p = prompts.SUPERVISOR_PROMPT
    assert "ONLY the `task()` global" in p
    assert "await cluster_concepts" not in p
    assert "await glob" not in p


def test_reviewer_reviews_a_cluster_with_cross_doc_checks():
    # The reviewer receives 1-5 related concept ids: every doc gets the full
    # checklist, findings come back grouped by doc, and the docs are checked
    # against EACH OTHER (the consistency bugs per-doc review can't see).
    p = prompts.REVIEWER_PROMPT
    assert "CLUSTER" in p
    assert "EVERY doc" in p
    assert "cross-doc" in p.lower()
    assert "grouped by concept id" in p.lower()


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
    # Claude rejects that combination, failing every dispatch. The supervisor
    # must be told NOT to pass it (authoring fan-outs still ride task()).
    p = prompts.SUPERVISOR_PROMPT
    assert "responseSchema" in p
    assert "output_config.format" in p  # names the exact rejected field


def test_supervisor_forbids_swallowing_dispatch_errors():
    # A .catch() that turns a failed task() into an empty result makes a broken
    # fan-out look clean (the exact failure mode we hit: clean:0, issues:[]).
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

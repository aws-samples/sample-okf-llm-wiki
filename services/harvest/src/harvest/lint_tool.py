"""The supervisor's whole-bundle lint gate.

One no-argument tool, ``lint_bundle``: the offline checks live in
``okf_core.lint`` (coverage vs the ``.metadata/`` snapshot, required docs,
frontmatter, links, join keys); this wrapper adds the one
check that needs the live engine — ``EXPLAIN`` over every runnable ```sql
fence — and formats the whole report as one model-facing dict.

Deliberately argument-free: expected tables come from the snapshot on disk
and EXPLAIN availability from this run's source, so there is nothing for the
model to pass (or get wrong) — and the same call is correct however many
source databases fed the snapshot. Steps are isolated: when the engine
doesn't support EXPLAIN (or a step crashes) that step reports
``skipped``/``failed`` and the rest still run; the tool itself never raises.

MAIN AGENT ONLY — not in the sub-agent specs. Authors and reviewers work one
doc/cluster at a time; a whole-bundle scan in their hands is wasted tokens.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from harvest.source_base import Source
from okf_core.lint import LintFinding, SqlFence, collect_sql_fences
from okf_core.lint import lint_bundle as _lint_offline

# EXPLAIN scans no data but costs engine round-trips; a runaway bundle must
# not turn the gate into an hour of queries. Two bounds, both reported in the
# step note, never silent: a statement-count cap AND a wall-clock budget —
# each Athena poll can take up to its 60s timeout, so a count cap alone still
# allows a multi-minute stall on a congested workgroup.
_MAX_EXPLAIN_STATEMENTS = 100
_EXPLAIN_TIME_BUDGET_S = 300.0
# Findings shown to the model per call; the rest are counted in `note` (fix
# these, re-run, the next batch surfaces). Errors always outrank warnings.
_MAX_FINDINGS_SHOWN = 60
_ENGINE_ERROR_CHARS = 300


def _snippet(sql: str, limit: int = 60) -> str:
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _explain_key(stmt: str) -> str:
    """Cache key for one statement: whitespace-normalized, so re-flowing a
    query between lint passes still hits."""
    return " ".join(stmt.split())


def _explain_step(
    source: Source, root: Path, explained_ok: set[str]
) -> tuple[dict[str, Any], list[LintFinding]]:
    """Run ``EXPLAIN`` for every runnable, non-templated ```sql statement.

    Skipped (with counts in the note, never silently): templated fences
    (placeholders — not runnable as written), bare ON-clause fragments (the
    ``joins`` lint step owns those), and ``external/`` docs — a counterpart
    dataset's tables are outside this run's engine session, so EXPLAIN there
    would fail on access, not on the SQL.

    ``explained_ok`` caches SUCCESSES for the run (the gate runs at least
    twice per harvest, and most fences don't change between passes): a cached
    statement costs no engine round-trip and doesn't consume the cap.
    Failures are never cached — they re-run every pass, so a transient engine
    error can't be frozen in as a permanent finding, and the source schema is
    fixed for the run so a real failure stays a failure until the doc's SQL
    (and with it the cache key) changes.
    """
    if not getattr(source, "supports_explain", False):
        step = {
            "step": "explain_sql",
            "status": "skipped",
            "errors": 0,
            "warnings": 0,
            "note": "engine does not support EXPLAIN",
        }
        return step, []

    deadline = time.monotonic() + _EXPLAIN_TIME_BUDGET_S
    fences: list[SqlFence] = collect_sql_fences(root)
    findings: list[LintFinding] = []
    ran = cached = templated = external = capped = timed_out = 0
    for fence in fences:
        if not fence.statements:
            continue
        if fence.path.startswith("external/"):
            external += len(fence.statements)
            continue
        if fence.templated:
            templated += len(fence.statements)
            continue
        for stmt in fence.statements:
            key = _explain_key(stmt)
            if key in explained_ok:
                cached += 1
                continue
            if ran >= _MAX_EXPLAIN_STATEMENTS:
                capped += 1
                continue
            if time.monotonic() > deadline:
                timed_out += 1
                continue
            ran += 1
            try:
                source.run_query(f"EXPLAIN {stmt}")
            except Exception as e:  # noqa: BLE001 — engine error = finding, not crash
                msg = str(e)
                if len(msg) > _ENGINE_ERROR_CHARS:
                    msg = msg[: _ENGINE_ERROR_CHARS - 1] + "…"
                findings.append(
                    LintFinding(
                        "error",
                        "sql-explain-failed",
                        fence.path,
                        f"EXPLAIN failed for the ```sql fence starting "
                        f"`{_snippet(stmt)}`: {msg}",
                    )
                )
            else:
                explained_ok.add(key)
    notes = [f"validated {ran} statement(s)"]
    if cached:
        notes.append(
            f"{cached} unchanged statement(s) already validated this run (cached)"
        )
    if templated:
        notes.append(f"skipped {templated} templated (placeholder) statement(s)")
    if external:
        notes.append(f"skipped {external} external/ statement(s) (outside this run's engine session)")
    if capped:
        notes.append(f"CAP HIT: {capped} statement(s) beyond {_MAX_EXPLAIN_STATEMENTS} not validated")
    if timed_out:
        notes.append(
            f"TIME BUDGET HIT: {timed_out} statement(s) not validated "
            f"(budget {int(_EXPLAIN_TIME_BUDGET_S)}s)"
        )
    step = {
        "step": "explain_sql",
        "status": "issues" if findings else "ok",
        "errors": len(findings),
        "warnings": 0,
        "note": "; ".join(notes),
    }
    return step, findings


def make_lint_tool(source: Source, dataset_root: Path) -> Any:
    from langchain_core.tools import tool

    root = Path(dataset_root)
    # Per-run EXPLAIN cache (successes only — see _explain_step). Closure-
    # scoped: the tool is built once per harvest, so the cache lives exactly
    # as long as the run and can never go stale across harvests.
    explained_ok: set[str] = set()

    @tool
    def lint_bundle() -> dict[str, Any]:
        """Lint the WHOLE bundle and report every error/warning. Takes NO
        arguments — everything is derived from the bundle on disk.

        Deterministic checks: every snapshot table has its tables/<table>.md;
        references/usage_guardrails.md and the dataset overview exist; every
        doc's frontmatter is valid; every intra-bundle link resolves (plus
        orphaned references); join conditions use existing, type-comparable
        columns. When the engine
        supports it, every runnable ```sql fence is also validated with
        EXPLAIN (no data scanned).

        Run it once ALL authoring is done (before the review fan-out, so
        reviewers verify a complete bundle) and again before finishing, after
        the reviewer fixes are applied. Fix every ERROR it reports and re-run
        until none remain; warnings are judgment calls (fix or justify).
        `ok: true` means no errors and no failed steps.
        """
        try:
            report = _lint_offline(root)
            steps: list[dict[str, Any]] = []
            for s in report.steps:
                entry: dict[str, Any] = {
                    "step": s.name,
                    "status": s.status,
                    "errors": sum(1 for f in s.findings if f.severity == "error"),
                    "warnings": sum(1 for f in s.findings if f.severity == "warning"),
                }
                if s.note:
                    entry["note"] = s.note
                steps.append(entry)

            # The explain phase gets ITS OWN isolation: a crash here (an
            # OSError re-walking the S3 mount, say) must not discard the
            # already-computed offline findings — the docstring promises
            # step isolation, so honor it.
            try:
                explain_entry, explain_findings = _explain_step(
                    source, root, explained_ok
                )
            except Exception as e:  # noqa: BLE001 — failed step, not a lost report
                explain_findings = []
                explain_entry = {
                    "step": "explain_sql",
                    "status": "failed",
                    "errors": 0,
                    "warnings": 0,
                    "note": f"{type(e).__name__}: {str(e)[:300]}",
                }
            steps.append(explain_entry)

            all_findings = report.findings + explain_findings
            ordered = [f for f in all_findings if f.severity == "error"] + [
                f for f in all_findings if f.severity != "error"
            ]
            shown = [f.to_dict() for f in ordered[:_MAX_FINDINGS_SHOWN]]

            result: dict[str, Any] = {
                "ok": (
                    report.ok
                    and not explain_findings
                    and explain_entry["status"] != "failed"
                ),
                "steps": steps,
                "findings": shown,
            }
            hidden = len(ordered) - len(shown)
            if hidden > 0:
                result["note"] = (
                    f"{hidden} more finding(s) not shown — fix the above and "
                    f"re-run lint_bundle for the next batch."
                )
            return result
        except Exception as e:  # noqa: BLE001 — the gate must never abort the run
            return {
                "ok": False,
                "steps": [],
                "findings": [],
                "note": f"lint_bundle crashed: {type(e).__name__}: {str(e)[:300]}",
            }

    return lint_bundle

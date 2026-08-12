"""The supervisor's no-arg `lint_bundle` gate: EXPLAIN step gating + shape.

The offline checks are okf_core.lint's (tested there); here we test what the
harvest wrapper adds: EXPLAIN runs only on runnable, non-templated,
non-external statements; engine failures become findings (never raises); a
source without `supports_explain` skips the step; and the tool is wired to
the MAIN agent only.
"""

from __future__ import annotations

from pathlib import Path

from harvest.lint_tool import make_lint_tool


def _fm(type_: str, title: str = "T") -> str:
    return f"---\ntype: {type_}\ntitle: {title}\ndescription: d\ntimestamp: t\n---\n\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fence_bundle(tmp_path: Path) -> Path:
    """Docs exercising every EXPLAIN gate: a good statement, a failing one,
    a bare join fragment, a templated statement, and an external/ statement."""
    root = tmp_path / "sales" / "f1"
    _write(
        root,
        "tables/races.md",
        _fm("Glue Table") + '```sql\nSELECT raceid FROM "f1"."races"\n```\n',
    )
    _write(
        root,
        "tables/results.md",
        _fm("Glue Table") + "```sql\nSELECT x FROM badtable\n```\n",
    )
    _write(
        root,
        "references/joins/a__b.md",
        _fm("Reference") + "```sql\na.x = b.y\n```\n",
    )
    _write(
        root,
        "references/metrics/m.md",
        _fm("Reference") + "```sql\nSELECT * FROM t WHERE y = <year>\n```\n",
    )
    _write(
        root,
        "external/dom/ds/joins/x__y.md",
        _fm("Reference") + "```sql\nSELECT 1\n```\n",
    )
    return root


class _ExplainSource:
    supports_explain = True

    def __init__(self):
        self.queries: list[str] = []

    def run_query(self, query, **kwargs):
        self.queries.append(query)
        if "badtable" in query:
            raise RuntimeError("TABLE_NOT_FOUND: badtable")
        return (["plan"], [["ok"]])


def _step(result, name):
    return next(s for s in result["steps"] if s["step"] == name)


def test_explain_runs_only_runnable_local_statements(tmp_path):
    root = _fence_bundle(tmp_path)
    src = _ExplainSource()
    result = make_lint_tool(src, root).invoke({})

    # Two EXPLAINs: races + results. The fragment has no statement, the
    # templated fence and the external/ doc are skipped by design.
    assert len(src.queries) == 2
    assert all(q.startswith("EXPLAIN SELECT") for q in src.queries)
    step = _step(result, "explain_sql")
    assert step["status"] == "issues" and step["errors"] == 1
    assert "skipped 1 templated" in step["note"]
    assert "skipped 1 external/" in step["note"]

    failed = [f for f in result["findings"] if f["code"] == "sql-explain-failed"]
    assert len(failed) == 1
    assert failed[0]["path"] == "tables/results.md"
    assert failed[0]["severity"] == "error"
    assert "TABLE_NOT_FOUND" in failed[0]["message"]
    assert result["ok"] is False


def test_explain_successes_are_cached_across_passes_failures_are_not(tmp_path):
    """The gate runs at least twice per harvest (step 6a + step 8, plus
    fix→re-lint loops): an unchanged, already-validated statement must not
    cost another engine round-trip, while a failed one re-runs every pass
    (never cached — a transient engine error must not become permanent) and a
    FIXED statement is a new cache key that gets one fresh EXPLAIN."""
    root = _fence_bundle(tmp_path)
    src = _ExplainSource()
    tool = make_lint_tool(src, root)

    tool.invoke({})
    assert len(src.queries) == 2  # races (ok) + badtable (fails)

    result = tool.invoke({})
    # Only the failure re-ran; the good statement came from the cache.
    assert len(src.queries) == 3
    assert "badtable" in src.queries[-1]
    step = _step(result, "explain_sql")
    assert "1 unchanged statement(s) already validated this run (cached)" in step["note"]
    assert step["errors"] == 1  # the failure is still reported every pass

    # "Fix" the bad doc: the new statement is a new key — explained once, then
    # the whole bundle is cache-served and clean.
    _write(
        root,
        "tables/results.md",
        _fm("Glue Table") + "```sql\nSELECT x FROM goodtable\n```\n",
    )
    result = tool.invoke({})
    assert len(src.queries) == 4
    assert "goodtable" in src.queries[-1]
    assert _step(result, "explain_sql")["errors"] == 0

    result = tool.invoke({})
    assert len(src.queries) == 4  # nothing changed — zero engine calls
    assert "validated 0 statement(s)" in _step(result, "explain_sql")["note"]


def test_no_explain_support_skips_the_step_not_the_lint(tmp_path):
    root = _fence_bundle(tmp_path)

    class _NoExplain:
        pass  # no supports_explain attribute at all

    result = make_lint_tool(_NoExplain(), root).invoke({})
    step = _step(result, "explain_sql")
    assert step["status"] == "skipped"
    assert "does not support EXPLAIN" in step["note"]
    # The offline steps still ran and reported.
    assert {s["step"] for s in result["steps"]} > {"coverage", "links", "explain_sql"}


def test_tool_takes_no_arguments_and_never_raises(tmp_path):
    tool = make_lint_tool(_ExplainSource(), tmp_path / "does-not-exist")
    assert tool.name == "lint_bundle"
    assert tool.args == {}  # nothing for the model to pass (or get wrong)
    result = tool.invoke({})  # missing bundle root: findings, not a crash
    assert isinstance(result, dict) and result["ok"] is False


def test_lint_gate_is_wired_to_every_main_agent_except_cross():
    import inspect

    from harvest import agent as ag
    from harvest.prompts import (
        build_annotation_supervisor_prompt,
        build_maintenance_supervisor_prompt,
    )

    src = inspect.getsource(ag.build_harvest_agent)
    # Full + scoped supervisors get the tool; cross runs stay excluded (their
    # writes are pair-confined, so bundle-wide errors would be unfixable).
    assert "full_harvest = cross_target is None and supervisor_prompt is None" in src
    assert (
        "if cross_target is None:\n        main_tools.append(make_lint_tool(source, dataset_root))"
        in src
    )
    assert "tools=main_tools," in src
    # Sub-agent specs keep all_tools (no bundle-wide scan in their hands).
    assert src.count('"tools": all_tools') >= 4
    # The scoped prompts carry the final-check instruction — scoped to the
    # docs THIS run touched, never the full gate's fix-to-zero obligation.
    for prompt in (
        build_maintenance_supervisor_prompt(),
        build_annotation_supervisor_prompt(results_rel=".harvest/results.json"),
    ):
        assert "lint_bundle" in prompt
        assert "pre-existing" in prompt


def test_explain_crash_does_not_discard_the_offline_report(tmp_path, monkeypatch):
    """Step isolation: a crash in the EXPLAIN phase reports a failed step but
    keeps every offline finding — and the result is NOT ok."""
    from okf_core import lint as core_lint

    from harvest import lint_tool as lt

    root = _fence_bundle(tmp_path)

    def boom(*_a, **_k):
        raise OSError("mount went away")

    # Break BOTH fence sources: the shared in-report collection (lint_bundle
    # swallows this and leaves sql_fences None) and the tool's fallback
    # re-collection — the failure then lands as a failed explain STEP while
    # the offline report survives intact.
    monkeypatch.setattr(core_lint, "_collect_fences", boom)
    monkeypatch.setattr(lt, "collect_sql_fences", boom)
    result = make_lint_tool(_ExplainSource(), root).invoke({})
    step = _step(result, "explain_sql")
    assert step["status"] == "failed" and "mount went away" in step["note"]
    # The offline steps survived (this bundle lacks guardrails -> error).
    assert any(f["code"] == "missing-usage-guardrails" for f in result["findings"])
    assert result["ok"] is False


def test_explain_wall_clock_budget_stops_the_engine_calls(tmp_path, monkeypatch):
    """A congested engine must not stall the gate: past the time budget the
    remaining statements are counted, not run."""
    from harvest import lint_tool as lt

    root = _fence_bundle(tmp_path)
    src = _ExplainSource()

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            # First call sets the deadline; every later look is past it.
            self.now += lt._EXPLAIN_TIME_BUDGET_S + 1
            return self.now

    monkeypatch.setattr(lt, "time", _Clock())
    result = make_lint_tool(src, root).invoke({})
    assert src.queries == []  # nothing ran
    step = _step(result, "explain_sql")
    assert "TIME BUDGET HIT: 2 statement(s)" in step["note"]
    # A gate that stopped checking must not claim it passed: budget-skipped
    # statements make the step INCOMPLETE and the report not-ok, with the
    # note pointing at the resume path (the per-run success cache).
    assert step["status"] == "incomplete"
    assert step["unvalidated"] == 2
    assert result["ok"] is False
    assert "re-run lint_bundle" in step["note"]


def test_explain_statement_cap_also_blocks_ok_and_rerun_converges(
    tmp_path, monkeypatch
):
    from harvest import lint_tool as lt

    # A clean two-statement bundle (the shared fixture's badtable statement
    # would rightly keep ok False on the re-run for a different reason).
    root = tmp_path / "sales" / "f1"
    _write(
        root,
        "tables/races.md",
        _fm("Glue Table") + '```sql\nSELECT raceid FROM "f1"."races"\n```\n',
    )
    _write(
        root,
        "tables/results.md",
        _fm("Glue Table") + '```sql\nSELECT resultid FROM "f1"."results"\n```\n',
    )
    monkeypatch.setattr(lt, "_MAX_EXPLAIN_STATEMENTS", 1)
    src = _ExplainSource()
    tool = make_lint_tool(src, root)
    first = tool.invoke({})
    step = _step(first, "explain_sql")
    assert step["status"] == "incomplete"
    assert step["unvalidated"] == 1
    assert first["ok"] is False
    # The re-run the note prescribes resumes via the per-run success cache:
    # the already-validated statement costs nothing, the remainder runs, and
    # the explain step converges to ok with nothing left unvalidated. (The
    # bundle still has offline findings — missing guardrails/overview — so
    # overall ok stays False for THAT reason, not the explain step's.)
    second = tool.invoke({})
    step2 = _step(second, "explain_sql")
    assert step2["status"] == "ok"
    assert "unvalidated" not in step2


def test_supervisor_prompt_prescribes_the_lint_gate_twice():
    from harvest import prompts

    p = prompts.SUPERVISOR_PROMPT
    assert "lint_bundle" in p
    assert "NO arguments" in p
    # Two checkpoints: once all authoring is done (before the review fan-out,
    # so reviewers see a complete bundle) and again before finishing, after
    # the reviewer fixes are applied.
    norm = " ".join(p.split())
    assert "BEFORE the review fan-out" in norm
    assert "after the review pass (and your propagation-note edits)" in norm
    # Errors must be fixed and lint re-run; warnings are judgment calls.
    low = norm.lower()
    assert "re-run" in low and "warnings" in low

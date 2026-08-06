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


def test_lint_gate_is_wired_to_the_main_agent_of_full_harvests_only():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    # Full harvests only: scoped modes hand in supervisor_prompt and cross
    # runs are pair-confined — neither can run the fix-to-zero workflow.
    assert "full_harvest = cross_target is None and supervisor_prompt is None" in src
    assert "if full_harvest:" in src
    assert "main_tools.append(make_lint_tool(source, dataset_root))" in src
    assert "tools=main_tools," in src
    # Sub-agent specs keep all_tools (no bundle-wide scan in their hands).
    assert src.count('"tools": all_tools') >= 4


def test_explain_crash_does_not_discard_the_offline_report(tmp_path, monkeypatch):
    """Step isolation: a crash in the EXPLAIN phase reports a failed step but
    keeps every offline finding — and the result is NOT ok."""
    from harvest import lint_tool as lt

    root = _fence_bundle(tmp_path)

    def boom(_root):
        raise OSError("mount went away")

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
    note = _step(result, "explain_sql")["note"]
    assert "TIME BUDGET HIT: 2 statement(s)" in note


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
    assert "after the reviewer fixes have been applied" in norm
    # Errors must be fixed and lint re-run; warnings are judgment calls.
    low = norm.lower()
    assert "re-run" in low and "warnings" in low

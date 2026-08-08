"""harvest.review — the deterministic run_review workflow tool.

Drives the real tool coroutine against a fake link graph, a fake deepagents
``task`` tool, and a real ``ToolRuntime`` dataclass, asserting the workflow's
contracts: coverage by construction, the reviewer→fixer pipeline, the
per-dispatch fixer allowlist binding (isolated across parallel dispatches),
failure recording + retry-by-cluster-ids on the persisted clustering, the
fleet-square event stream, and the unique on-disk report.
"""

import asyncio
import json
import re

import pytest
from langchain.tools import ToolRuntime

from harvest.review import (
    _propagation_notes,
    _reviewer_is_clean,
    current_fix_allowlist,
    make_run_review_tool,
)

CLEAN = "CLEAN\nEvery doc checked out."
FINDINGS = (
    "FINDINGS\n- `tables/races`: grain is per (race, driver), doc says per race."
)
FIX_DONE = (
    "Fixed the grain statement.\n\n## PROPAGATION NOTES\n- none"
)


class FakeGraph:
    def __init__(self, clusters):
        self.clusters = clusters
        self.calls = 0
        self.last_max_size = None
        self.last_exclude = None

    def cluster(self, max_size=5, exclude=None):
        self.calls += 1
        self.last_max_size = max_size
        self.last_exclude = exclude
        out = []
        for c in self.clusters:
            kept = [i for i in c if exclude is None or not exclude(i)]
            if kept:
                out.append(kept)
        return out


class FakeTaskTool:
    """Duck-types the deepagents task tool: routes on subagent_type and
    records each dispatch (including the fixer allowlist SEEN AT CALL TIME
    and the max number of concurrently running dispatches)."""

    name = "task"

    def __init__(self, review_texts=None, fix_texts=None, context_texts=None):
        self.review_texts = review_texts or {}
        self.fix_texts = fix_texts or {}
        self.context_texts = context_texts or {}
        self.dispatches = []
        self.running = 0
        self.max_running = 0
        self.running_by_type = {}
        self.max_running_by_type = {}

    def _text_for(self, payload):
        desc = payload["description"]
        sub = payload["subagent_type"]
        if sub == "reviewer":
            table = self.review_texts
        elif sub == "context-reviewer":
            table = self.context_texts
        else:
            table = self.fix_texts
        for key, value in table.items():
            if key in desc:
                return value
        return CLEAN if sub in ("reviewer", "context-reviewer") else FIX_DONE

    async def arun(self, payload, **kwargs):
        sub = payload["subagent_type"]
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.running_by_type[sub] = self.running_by_type.get(sub, 0) + 1
        self.max_running_by_type[sub] = max(
            self.max_running_by_type.get(sub, 0), self.running_by_type[sub]
        )
        record = {
            "subagent_type": sub,
            "description": payload["description"],
            "tool_call_id": payload["runtime"].tool_call_id,
            "allowlist": current_fix_allowlist(),
        }
        self.dispatches.append(record)
        await asyncio.sleep(0)  # let siblings interleave
        self.running -= 1
        self.running_by_type[sub] -= 1
        value = self._text_for(payload)
        if isinstance(value, Exception):
            raise value
        return value


def _runtime(task_tool, events=None):
    return ToolRuntime(
        state=None,
        context=None,
        config=None,
        stream_writer=(events.append if events is not None else None),
        tool_call_id="call_review_1",
        store=None,
        tools=[task_tool],
        execution_info=None,
        server_info=None,
    )


def _run(tool, runtime, cluster_ids=None):
    # Through the REAL sync invoke path (BaseTool.run) — the path the sync
    # `agent.stream(...)` driver executes tools on. An async-only tool raises
    # NotImplementedError exactly here (the live incident this pins): the
    # tool must be sync-invokable while hosting its async orchestration on a
    # private event loop.
    return tool.run({"cluster_ids": cluster_ids, "runtime": runtime})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_reviewer_verdict_parse():
    assert _reviewer_is_clean(CLEAN)
    assert _reviewer_is_clean("**CLEAN**\nnothing to report")
    assert _reviewer_is_clean("clean.")
    assert not _reviewer_is_clean(FINDINGS)
    # A findings-bearing reply that merely STARTS with "clean..." must not
    # be classified clean (the prefix match silently skipped the fixer).
    assert not _reviewer_is_clean("Cleanup required: tables/x overstates grain")
    assert not _reviewer_is_clean("CLEAN except for one thing")
    assert not _reviewer_is_clean("clean — nothing to report")
    # No verdict line → treated as findings (fail toward fixing).
    assert not _reviewer_is_clean("the grain looks wrong somewhere")
    assert not _reviewer_is_clean("")


def test_propagation_notes_extraction():
    text = (
        "Fixed both findings.\n\n## PROPAGATION NOTES\n"
        "- `tables/results`: update the stated join cardinality to 1:many\n"
        "- `references/metrics/wins`: formula now uses positionOrder\n"
    )
    assert _propagation_notes(text) == [
        "`tables/results`: update the stated join cardinality to 1:many",
        "`references/metrics/wins`: formula now uses positionOrder",
    ]
    assert _propagation_notes(FIX_DONE) == []
    assert _propagation_notes("no section at all") == []


def test_propagation_notes_ignore_prose_mentions_and_use_last_section():
    # A prose line CONTAINING the word "propagation" must not open the
    # section — the old substring match harvested unrelated summary bullets
    # as machine-applied notes.
    text = (
        "Summary: fixed X; one issue needs propagation notes below.\n"
        "- corrected the grain claim in tables/races.md\n"
        "- re-ran the proof query\n\n"
        "## PROPAGATION NOTES\n"
        "- `tables/other`: change stated cardinality to 1:many\n"
    )
    assert _propagation_notes(text) == [
        "`tables/other`: change stated cardinality to 1:many"
    ]
    # Heading dressing variants all count; the LAST section wins.
    assert _propagation_notes("**PROPAGATION NOTES**\n- fix `datasets/x`\n") == [
        "fix `datasets/x`"
    ]
    assert _propagation_notes(
        "PROPAGATION NOTES:\n- stale one\n\n## PROPAGATION NOTES\n- real one\n"
    ) == ["real one"]


# ---------------------------------------------------------------------------
# the workflow
# ---------------------------------------------------------------------------


def test_full_pass_reviews_every_cluster_and_fixes_only_findings(tmp_path):
    graph = FakeGraph([["tables/a", "references/joins/a__b"], ["tables/b"]])
    fake = FakeTaskTool(review_texts={"tables/a": FINDINGS, "tables/b": CLEAN})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    events = []
    result = _run(tool, _runtime(fake, events))

    assert result["ok"] is True
    assert result["clusters"] == 2
    assert result["docs"] == 3
    assert result["fixed"] == ["c1"]
    assert result["clean"] == ["c2"]
    assert result["failed"] == []
    assert result["propagation_notes"] == []

    # Two reviewers (one per cluster), ONE fixer (only the findings cluster).
    kinds = [(d["subagent_type"], d["allowlist"]) for d in fake.dispatches]
    assert sum(1 for k, _ in kinds if k == "reviewer") == 2
    fixers = [d for d in fake.dispatches if d["subagent_type"] == "fix-author"]
    assert len(fixers) == 1
    # The fixer saw EXACTLY its cluster's file paths as its write allowlist,
    # and the reviewer findings verbatim in its brief.
    assert fixers[0]["allowlist"] == frozenset(
        {"tables/a.md", "references/joins/a__b.md"}
    )
    assert FINDINGS.splitlines()[1] in fixers[0]["description"]
    # Reviewers never carry an allowlist (their guard is read-only anyway).
    assert all(
        d["allowlist"] is None
        for d in fake.dispatches
        if d["subagent_type"] == "reviewer"
    )

    # The clustering was persisted with stable ids.
    persisted = json.loads(
        (tmp_path / ".harvest/review/clusters.json").read_text()
    )
    assert persisted["clusters"][0] == {
        "id": "c1",
        "docs": ["tables/a", "references/joins/a__b"],
    }

    # The report exists at the returned path and holds the transcripts.
    report = (tmp_path / result["report_path"]).read_text()
    assert "grain is per (race, driver)" in report
    assert "CLEAN" in report


def test_fleet_events_ride_two_explicit_batches(tmp_path):
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(review_texts={"tables/a": FINDINGS})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    events = []
    result = _run(tool, _runtime(fake, events))
    assert result["ok"] is True

    assert [e["phase"] for e in events] == ["start", "complete", "start", "complete"]
    rev_start, rev_done, fix_start, fix_done = events
    assert rev_start["batch"] == "call_review_1:review"
    assert fix_start["batch"] == "call_review_1:fix"
    assert rev_start["subagent_type"] == "reviewer"
    assert fix_start["subagent_type"] == "fix-author"
    # The full brief rides the start event; the final answer the complete.
    assert rev_start["description"].startswith("Adversarially verify")
    assert rev_done["result"] == FINDINGS
    assert fix_done["result"] == FIX_DONE
    assert rev_done["id"] == rev_start["id"]


def test_failed_cluster_is_recorded_and_retryable_on_same_clustering(tmp_path):
    graph = FakeGraph([["tables/a"], ["tables/b"]])
    fake = FakeTaskTool(
        review_texts={"tables/a": RuntimeError("provider 400"), "tables/b": CLEAN}
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))

    assert result["ok"] is False
    assert result["clean"] == ["c2"]
    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["cluster"] == "c1"
    assert failure["docs"] == ["tables/a"]
    assert failure["stage"] == "review"
    assert "provider 400" in failure["error"]
    assert "cluster_ids=['c1']" in result["retry_hint"]
    # The report names the failure and the uncovered docs.
    report = (tmp_path / result["report_path"]).read_text()
    assert "NOT covered" in report

    # Retry ONLY the failed cluster: the clustering is NOT recomputed
    # (persisted ids stay meaningful) and only c1 is dispatched.
    fake.review_texts["tables/a"] = CLEAN
    before = graph.calls
    retry = _run(tool, _runtime(fake), cluster_ids=["c1"])
    assert graph.calls == before
    assert retry["ok"] is True
    assert retry["clusters"] == 1
    assert retry["clean"] == ["c1"]
    # A fresh report file — nothing overwritten.
    assert retry["report_path"] != result["report_path"]


def test_retry_input_validation(tmp_path):
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool()
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    # cluster_ids before any full pass → clear error, nothing dispatched.
    result = _run(tool, _runtime(fake), cluster_ids=["c1"])
    assert result["ok"] is False
    assert "no prior clustering" in result["error"]
    assert fake.dispatches == []

    _run(tool, _runtime(fake))
    bad = _run(tool, _runtime(fake), cluster_ids=["c9"])
    assert bad["ok"] is False
    assert "unknown cluster ids" in bad["error"]
    assert "c1" in bad["error"]


def test_failed_fix_stage_is_recorded(tmp_path):
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(
        review_texts={"tables/a": FINDINGS},
        fix_texts={"tables/a": RuntimeError("mount EACCES")},
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["ok"] is False
    assert result["failed"][0]["stage"] == "fix"
    assert "mount EACCES" in result["failed"][0]["error"]


def test_empty_reviewer_text_is_a_failure_not_a_clean(tmp_path):
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(review_texts={"tables/a": "   "})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["ok"] is False
    assert "no text" in result["failed"][0]["error"]


def test_dispatch_timeout_fails_the_cluster_not_the_tool(tmp_path):
    class HangingTaskTool(FakeTaskTool):
        async def arun(self, payload, **kwargs):
            await asyncio.sleep(30)

    graph = FakeGraph([["tables/a"]])
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=0.05
    )
    result = _run(tool, _runtime(HangingTaskTool()))
    assert result["ok"] is False
    assert result["failed"][0]["stage"] == "review"
    # str(TimeoutError()) is "" — the failure reason must never be blank,
    # and the report must still print the failure + NOT-covered warning.
    assert "timed out" in result["failed"][0]["error"]
    report = (tmp_path / result["report_path"]).read_text()
    assert "timed out" in report
    assert "NOT covered" in report


def test_long_propagation_note_is_marked_truncated(tmp_path):
    long_note = "`datasets/main`: replace the additivity section with: " + "x" * 3000
    fix_text = f"Fixed it.\n\n## PROPAGATION NOTES\n- {long_note}\n"
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(
        review_texts={"tables/a": FINDINGS}, fix_texts={"tables/a": fix_text}
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    note = result["propagation_notes"][0]
    assert note["truncated"] is True
    assert "[TRUNCATED" in note["note"]
    assert result["report_path"] in note["note"]
    # A normal-length note carries no truncation marker.
    assert all(
        "TRUNCATED" not in n["note"]
        for n in result["propagation_notes"][1:]
    )


def test_retry_reuses_one_event_loop_across_calls(tmp_path):
    """Model clients pool connections on the loop they first run on — a fresh
    loop per call broke the documented cluster_ids retry ('Event loop is
    closed'). Every run_review call must run on the SAME background loop."""
    loops: list[Any] = []

    class LoopRecordingTaskTool(FakeTaskTool):
        async def arun(self, payload, **kwargs):
            loops.append(asyncio.get_running_loop())
            return await super().arun(payload, **kwargs)

    graph = FakeGraph([["tables/a"]])
    fake = LoopRecordingTaskTool(review_texts={"tables/a": RuntimeError("boom")})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=1, timeout_s=5
    )
    first = _run(tool, _runtime(fake))
    assert first["ok"] is False
    fake.review_texts["tables/a"] = CLEAN
    retry = _run(tool, _runtime(fake), cluster_ids=["c1"])
    assert retry["ok"] is True
    assert len(loops) == 2
    assert loops[0] is loops[1]


def test_concurrency_is_bounded_and_allowlists_are_isolated(tmp_path):
    """Six findings clusters at concurrency 2: never more than two dispatches
    in flight, and every fixer sees ITS OWN cluster's allowlist even while
    other fixers run — the contextvar binding is per-dispatch."""
    clusters = [[f"tables/t{i}"] for i in range(6)]
    graph = FakeGraph(clusters)
    fake = FakeTaskTool(review_texts={f"tables/t{i}": FINDINGS for i in range(6)})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["ok"] is True
    assert fake.max_running <= 2
    fixers = [d for d in fake.dispatches if d["subagent_type"] == "fix-author"]
    assert len(fixers) == 6
    for d in fixers:
        doc = re.search(r"tables/t\d", d["description"]).group(0)
        assert d["allowlist"] == frozenset({f"{doc}.md"})
    # Outside any dispatch, no allowlist is bound (the guard fails closed).
    assert current_fix_allowlist() is None


def test_propagation_notes_surface_in_the_result(tmp_path):
    fix_text = (
        "Fixed it.\n\n## PROPAGATION NOTES\n"
        "- `tables/other`: change stated cardinality to 1:many\n"
    )
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(
        review_texts={"tables/a": FINDINGS}, fix_texts={"tables/a": fix_text}
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["ok"] is True
    assert result["propagation_notes"] == [
        {
            "cluster": "c1",
            "note": "`tables/other`: change stated cardinality to 1:many",
        }
    ]


def test_model_facing_schema_hides_the_runtime():
    tool = make_run_review_tool(
        link_graph=FakeGraph([]), dataset_root="/tmp/x", concurrency=1
    )
    assert tool.name == "run_review"
    assert list(tool.tool_call_schema.model_fields.keys()) == ["cluster_ids"]


def test_missing_task_tool_reports_instead_of_raising(tmp_path):
    tool = make_run_review_tool(
        link_graph=FakeGraph([["tables/a"]]), dataset_root=tmp_path, concurrency=1
    )
    runtime = ToolRuntime(
        state=None,
        context=None,
        config=None,
        stream_writer=None,
        tool_call_id="c",
        store=None,
        tools=[],
        execution_info=None,
        server_info=None,
    )
    result = _run(tool, runtime)
    assert result["ok"] is False
    assert "dispatch tool" in result["error"]


def test_review_workflow_is_wired_to_full_harvests_only():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    assert "if full_harvest:" in src
    assert "make_run_review_tool(" in src
    # The fixer's guard is the allowlist variant, bound to the contextvar.
    assert "write_allowlist=current_fix_allowlist" in src
    assert '"name": "fix-author"' in src
    assert '"middleware": [fixer_guard, tool_errors, prompt_cache]' in src


def test_supervisor_owned_hubs_are_excluded_and_size_defaults_to_seven(tmp_path):
    """The dataset overview docs and usage_guardrails are the SUPERVISOR's:
    never clustered (they'd hub-steal unrelated spokes), never in a fixer
    allowlist — corrections reach the supervisor as propagation notes. The
    cluster size defaults to 7 and is env-overridable."""
    graph = FakeGraph(
        [["tables/a", "references/usage_guardrails", "datasets/main"], ["tables/b"]]
    )
    fake = FakeTaskTool()
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert graph.last_max_size == 7
    assert graph.last_exclude("references/usage_guardrails")
    assert graph.last_exclude("datasets/main")
    assert graph.last_exclude("datasets/other_db")
    assert not graph.last_exclude("tables/a")
    assert not graph.last_exclude("references/joins/a__b")
    # The excluded hubs appear in NO cluster (and thus no fixer allowlist).
    persisted = json.loads((tmp_path / ".harvest/review/clusters.json").read_text())
    all_docs = [d for c in persisted["clusters"] for d in c["docs"]]
    assert "references/usage_guardrails" not in all_docs
    assert "datasets/main" not in all_docs
    assert result["docs"] == 2


def test_cluster_size_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OKF_HARVEST_REVIEW_CLUSTER_SIZE", "4")
    graph = FakeGraph([["tables/a"]])
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=1, timeout_s=5
    )
    _run(tool, _runtime(FakeTaskTool()))
    assert graph.last_max_size == 4


def test_full_harvest_resets_review_state_before_authoring():
    """A clustering from a PREVIOUS harvest describes docs the run is about to
    rebuild — the full-harvest path must wipe .harvest/review/ alongside the
    authored-output wipe so no retry can run against it."""
    import inspect

    from harvest import runner

    src = inspect.getsource(runner.run_full_harvest)
    assert 'remove_tree(Path(dataset_root) / ".harvest" / "review")' in src
    assert src.index("clean_authored_output(dataset_root)") < src.index(
        '".harvest" / "review"'
    )


def test_supervisor_prompt_prescribes_the_tool_not_the_fanout():
    from harvest.prompts import (
        build_fixer_prompt,
        build_reviewer_prompt,
        build_supervisor_prompt,
    )

    sup = build_supervisor_prompt()
    assert "run_review" in sup
    # The old model-driven orchestration is gone from the prompt.
    assert "Promise.all" not in sup
    assert "cluster_concepts" not in sup.split("## Your job (supervisor)")[1]
    assert "propagation_notes" in sup
    rev = build_reviewer_prompt()
    assert "CLEAN" in rev and "FINDINGS" in rev
    fix = build_fixer_prompt()
    assert "PROPAGATION NOTES" in fix
    assert "HARD-LIMITED" in fix
    # The fixer WRITES, so it carries the authoring/guard contract like every
    # writing agent (an editor that doesn't know the frontmatter/augmentation
    # rules just fights the guard); the read-only reviewer must NOT carry it.
    assert "## Authoring (write path + guard)" in fix
    assert "## Authoring (write path + guard)" not in rev


# ---------------------------------------------------------------------------
# context-fidelity phase
# ---------------------------------------------------------------------------

CTX_FINDINGS = (
    "FINDINGS\n- `tables/races`: the digest's sentinel code `99` (means "
    "unknown) is not flagged in the schema row."
)


def _write_digests(root, n):
    d = root / ".harvest" / "context"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(1, n + 1):
        (d / f"digest-{i:02d}.md").write_text(f"# digest {i}", encoding="utf-8")
        out.append(f".harvest/context/digest-{i:02d}.md")
    return out


def _write_doc(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")


def test_context_phase_skipped_when_no_extractor_ran(tmp_path):
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool()
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert "skipped" in result["context"]
    assert all(d["subagent_type"] != "context-reviewer" for d in fake.dispatches)


def test_context_phase_pairs_digests_and_persists_x_ids(tmp_path):
    digests = _write_digests(tmp_path, 3)
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool()
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    events = []
    result = _run(tool, _runtime(fake, events))

    # 3 digests -> two pairs (2 + 1), one context-reviewer each, briefs
    # naming exactly that pair's digest files.
    ctx = [d for d in fake.dispatches if d["subagent_type"] == "context-reviewer"]
    assert len(ctx) == 2
    assert digests[0] in ctx[0]["description"] and digests[1] in ctx[0]["description"]
    assert digests[2] in ctx[1]["description"]
    assert result["context"] == {"pairs": 2, "clean": ["x1", "x2"], "fixed": []}

    # The pairing is persisted next to the clusters (same retry contract).
    persisted = json.loads((tmp_path / ".harvest/review/clusters.json").read_text())
    assert persisted["context"] == [
        {"id": "x1", "docs": digests[:2]},
        {"id": "x2", "docs": digests[2:]},
    ]

    # The context dispatches ride their own fleet batch, and the report
    # renders them as context pairs.
    ctx_events = [e for e in events if e.get("batch") == "call_review_1:context"]
    assert any(e["phase"] == "start" for e in ctx_events)
    report = (tmp_path / result["report_path"]).read_text()
    assert "Context pair `x1`" in report


def test_context_findings_pipe_into_bundle_wide_confined_fixer(tmp_path):
    _write_digests(tmp_path, 1)
    for rel in (
        "tables/races.md",
        "references/enums/status.md",
        "datasets/f1.md",
        "references/usage_guardrails.md",
    ):
        _write_doc(tmp_path, rel)
    (tmp_path / "tables" / "index.md").write_text("idx", encoding="utf-8")

    graph = FakeGraph([["tables/races"]])
    fake = FakeTaskTool(context_texts={"digest-01": CTX_FINDINGS})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    result = _run(tool, _runtime(fake))

    # The cluster pass was clean (default), so the ONLY fixer is the context
    # one — its allowlist is the whole authored bundle MINUS the hubs,
    # generated files, and reserved dirs (the digests themselves live under
    # .harvest/ and must never be editable).
    fixers = [d for d in fake.dispatches if d["subagent_type"] == "fix-author"]
    assert len(fixers) == 1
    allow = fixers[0]["allowlist"]
    assert "tables/races.md" in allow
    assert "references/enums/status.md" in allow
    assert "datasets/f1.md" not in allow
    assert "references/usage_guardrails.md" not in allow
    assert not any(p.startswith(".harvest/") for p in allow)
    assert not any(p.endswith("index.md") for p in allow)
    # The findings travel verbatim, and the result reports the fixed pair.
    assert CTX_FINDINGS.splitlines()[1] in fixers[0]["description"]
    assert result["context"] == {"pairs": 1, "clean": [], "fixed": ["x1"]}


def test_failed_context_pair_is_retryable_by_x_id(tmp_path):
    _write_digests(tmp_path, 1)
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(context_texts={"digest-01": RuntimeError("model choked")})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["ok"] is False
    assert [f["cluster"] for f in result["failed"]] == ["x1"]
    assert result["failed"][0]["stage"] == "context-review"
    assert "x1" in result["retry_hint"]

    # Retry EXACTLY the failed pair: no cluster re-runs, same digest files.
    fake2 = FakeTaskTool()
    result2 = _run(tool, _runtime(fake2), cluster_ids=["x1"])
    assert result2["ok"] is True
    assert [d["subagent_type"] for d in fake2.dispatches] == ["context-reviewer"]
    assert result2["clusters"] == 0
    assert result2["context"] == {"pairs": 1, "clean": ["x1"], "fixed": []}


def test_context_fixers_are_serialized_reviewers_are_not(tmp_path):
    # Context fixers share one editable file set, so two pairs with findings
    # must fix ONE AT A TIME (the lock) even though their reviewers ran in
    # parallel.
    _write_digests(tmp_path, 4)  # -> x1 (01, 02) and x2 (03, 04)
    graph = FakeGraph([])
    fake = FakeTaskTool(
        context_texts={"digest-01": CTX_FINDINGS, "digest-03": CTX_FINDINGS}
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=8, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["context"]["fixed"] == ["x1", "x2"]
    assert fake.max_running_by_type["context-reviewer"] == 2
    assert fake.max_running_by_type["fix-author"] == 1


def test_context_fixer_propagation_notes_reach_the_supervisor(tmp_path):
    _write_digests(tmp_path, 1)
    graph = FakeGraph([])
    fake = FakeTaskTool(
        context_texts={"digest-01": CTX_FINDINGS},
        fix_texts={
            "context-fidelity": (
                "Fixed the sentinel note.\n\n## PROPAGATION NOTES\n"
                "- `datasets/f1`: mention the sentinel in the overview"
            )
        },
    )
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=4, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["propagation_notes"] == [
        {"cluster": "x1", "note": "`datasets/f1`: mention the sentinel in the overview"}
    ]


def test_empty_cluster_ids_is_a_noop_not_a_fresh_review(tmp_path):
    # run_review(cluster_ids=[]) is a natural literalism when `failed` is
    # empty — it must NOT fall into the fresh-call branch (recompute, re-run
    # every reviewer, overwrite the persisted clustering).
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool()
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake), cluster_ids=[])
    assert result["ok"] is True and "nothing to retry" in result["note"]
    assert fake.dispatches == [] and graph.calls == 0


def test_empty_dispatch_text_emits_error_square_not_complete(tmp_path):
    # An empty answer is recorded as a FAILED cluster — the fleet square must
    # agree (phase error), not render a green `complete` the result contradicts.
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(review_texts={"tables/a": "   "})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    events = []
    result = _run(tool, _runtime(fake, events))
    assert [f["cluster"] for f in result["failed"]] == ["c1"]
    phases = [e["phase"] for e in events if e.get("id") == f"rev-c1-{result['review_id']}"]
    assert phases == ["start", "error"]
    assert "returned no text" in result["failed"][0]["error"]


def test_report_write_failure_degrades_without_losing_results(tmp_path, monkeypatch):
    # The module contract: the tool never raises. A persistent report-write
    # error after every dispatch completed must return the statuses/notes,
    # not discard 30 minutes of review.
    import harvest.review as rv

    def boom(*_a, **_k):
        raise OSError("mount went away")

    monkeypatch.setattr(rv, "_write_report", boom)
    graph = FakeGraph([["tables/a"]])
    fake = FakeTaskTool(review_texts={"tables/a": FINDINGS})
    tool = make_run_review_tool(
        link_graph=graph, dataset_root=tmp_path, concurrency=2, timeout_s=5
    )
    result = _run(tool, _runtime(fake))
    assert result["fixed"] == ["c1"]  # the review results survived
    assert result["report_path"] is None
    assert "could not be written" in result["report_error"]


def test_propagation_notes_keep_wrapped_bullet_continuations():
    # The supervisor applies notes VERBATIM — a wrapped bullet must not be
    # silently cut at its first physical line (a blank line still ends it, so
    # a trailing sign-off never glues on).
    text = (
        "Fixed.\n\n## PROPAGATION NOTES\n"
        "- `datasets/overview`: replace \"X is unique per race\"\n"
        "  with \"X is unique per (race, driver)\"\n"
        "- `tables/b`: single-line note\n"
        "\n"
        "That is all.\n"
    )
    assert _propagation_notes(text) == [
        '`datasets/overview`: replace "X is unique per race" with "X is unique per (race, driver)"',
        "`tables/b`: single-line note",
    ]

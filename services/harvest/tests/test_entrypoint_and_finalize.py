import contextvars
import json
import time
from pathlib import Path

import harvest.entrypoint as ep
from harvest.clients import dataset_root
from harvest.finalize import finalize_bundle, mark_in_progress


def test_validate_rejects_missing_fields():
    assert ep.start_harvest({})["status"] == "rejected"
    assert ep.start_harvest({"data_domain": "d"})["status"] == "rejected"
    r = ep.start_harvest({"data_domain": "d", "dataset": "x", "mode": "incremental"})
    assert r["status"] == "rejected"
    assert "changed_table" in r["error"]


def test_ar_rules_mode_validates_and_dispatches_to_the_build_trigger(monkeypatch):
    # ar_rules is a benchmark-style non-harvest mode: no lease, no mount — the
    # dispatch must route straight to maybe_build_policy and return.
    assert ep._validate({"mode": "ar_rules", "data_domain": "d"}) is not None
    assert ep._validate({"mode": "ar_rules", "data_domain": "d", "dataset": "x"}) is None

    calls: list[dict] = []
    monkeypatch.setattr(
        "harvest.ar_build.maybe_build_policy",
        lambda **kw: calls.append(kw) or "unchanged",
    )
    ep._dispatch({"mode": "ar_rules", "data_domain": "d", "dataset": "x"})
    assert calls == [{"data_domain": "d", "dataset": "x", "force": False}]

    # A forced dispatch (manual Sync) carries the flag through to the trigger.
    calls.clear()
    ep._dispatch(
        {"mode": "ar_rules", "data_domain": "d", "dataset": "x", "force": True}
    )
    assert calls == [{"data_domain": "d", "dataset": "x", "force": True}]


def test_model_config_from_payload_absent_is_none():
    # No model/effort in the payload -> None so the runner uses env defaults.
    assert ep._model_config_from_payload({"data_domain": "d", "dataset": "x"}) is None


def test_model_config_from_payload_builds_override(monkeypatch):
    monkeypatch.delenv("OKF_HARVEST_MAX_TOKENS", raising=False)
    cfg = ep._model_config_from_payload({"model": "openai.gpt-5.6-sol", "effort": "high"})
    assert cfg["model"] == "openai.gpt-5.6-sol"
    assert cfg["effort"] == "high"


def test_model_config_from_payload_reads_subagent_keys(monkeypatch):
    # The same builder resolves the SUB-AGENT pair from its own payload keys —
    # and ignores the supervisor's keys, so a supervisor-only override yields
    # None (sub-agents then run on the supervisor's config).
    monkeypatch.delenv("OKF_HARVEST_MAX_TOKENS", raising=False)
    payload = {
        "model": "global.anthropic.claude-opus-4-8",
        "effort": "xhigh",
        "subagent_model": "openai.gpt-5.6-sol",
        "subagent_effort": "high",
    }
    cfg = ep._model_config_from_payload(
        payload, model_key="subagent_model", effort_key="subagent_effort"
    )
    assert cfg["model"] == "openai.gpt-5.6-sol"
    assert cfg["effort"] == "high"
    assert (
        ep._model_config_from_payload(
            {"model": "openai.gpt-5.6-sol"},
            model_key="subagent_model",
            effort_key="subagent_effort",
        )
        is None
    )


def test_model_config_from_payload_reads_reviewer_keys(monkeypatch):
    monkeypatch.delenv("OKF_HARVEST_MAX_TOKENS", raising=False)
    cfg = ep._model_config_from_payload(
        {"reviewer_model": "global.anthropic.claude-sonnet-5", "reviewer_effort": "high"},
        model_key="reviewer_model",
        effort_key="reviewer_effort",
    )
    assert cfg["model"] == "global.anthropic.claude-sonnet-5"
    assert cfg["effort"] == "high"


def test_cleanup_removes_domain_subtree(monkeypatch, tmp_path):
    # Seed a mount with two domains; cleanup must remove only the target one.
    mount = tmp_path / "mnt"
    (mount / "health_care" / "toxicology" / ".harvest").mkdir(parents=True)
    (mount / "health_care" / "toxicology" / ".harvest" / "pending.json").write_text("x")
    (mount / "education" / "california_schools").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest({"mode": "cleanup", "data_domain": "health_care"})
    assert r["status"] == "cleaned"
    assert r["removed"] is True
    assert not (mount / "health_care").exists()
    assert (mount / "education").exists()  # other domain untouched


def test_cleanup_scoped_to_dataset(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "health_care" / "toxicology").mkdir(parents=True)
    (mount / "health_care" / "thrombosis_prediction").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest(
        {"mode": "cleanup", "data_domain": "health_care", "dataset": "toxicology"}
    )
    assert r["status"] == "cleaned" and r["removed"] is True
    assert not (mount / "health_care" / "toxicology").exists()
    assert (mount / "health_care" / "thrombosis_prediction").exists()  # sibling kept


def test_cleanup_kills_the_commit_marker_even_when_partial(monkeypatch, tmp_path):
    # A partial cleanup (poisoned inodes survive the walk) must NEVER leave a
    # complete commit marker behind: a surviving state.json + graph.json pair
    # over a gutted bundle reads as a healthy wiki (is_bundle_ready true, the
    # /graph fast path serving the pre-cleanup graph). The markers go first.
    import os as _os

    mount = tmp_path / "mnt"
    ds = mount / "health_care" / "toxicology"
    (ds / ".harvest").mkdir(parents=True)
    (ds / ".harvest" / "state.json").write_text('{"status": "complete"}')
    (ds / ".harvest" / "graph.json").write_text('{"completed_at": "t"}')
    (ds / "tables").mkdir()
    (ds / "tables" / "molecule.md").write_text("doc")
    poisoned = str(ds / "tables" / "bond.md")
    (ds / "tables" / "bond.md").write_text("doc")
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    real_unlink = _os.unlink

    def unlink(path, *a, **kw):  # one poisoned inode the mount uid can't remove
        if str(path) == poisoned:
            raise OSError(13, "Permission denied", str(path))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(_os, "unlink", unlink)
    r = ep.start_harvest(
        {"mode": "cleanup", "data_domain": "health_care", "dataset": "toxicology"}
    )
    assert r["status"] == "partial" and r["removed"] is False
    # The poisoned doc survived, but both markers are gone regardless.
    assert (ds / "tables" / "bond.md").exists()
    assert not (ds / ".harvest" / "state.json").exists()
    assert not (ds / ".harvest" / "graph.json").exists()


def test_cleanup_absent_target_is_noop(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    r = ep.start_harvest({"mode": "cleanup", "data_domain": "ghost"})
    assert r["status"] == "cleaned" and r["removed"] is False


def test_cleanup_rejects_traversal_and_missing_domain(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "sport").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    # path traversal / separators in components are rejected before any removal
    assert (
        ep.start_harvest({"mode": "cleanup", "data_domain": ".."})["status"]
        == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "cleanup", "data_domain": "a/b"})["status"]
        == "rejected"
    )
    assert ep.start_harvest({"mode": "cleanup"})["status"] == "rejected"
    assert (mount / "sport").exists()  # nothing removed


def test_provision_creates_dataset_and_context_dirs(monkeypatch, tmp_path):
    # provision must create the dataset root AND .context/ through the mount so a
    # later out-of-band .context/ upload lands inside an already-writable tree.
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest(
        {"mode": "provision", "data_domain": "sport", "dataset": "spider2_ipl"}
    )
    assert r["status"] == "provisioned"
    assert (mount / "sport" / "spider2_ipl").is_dir()
    assert (mount / "sport" / "spider2_ipl" / ".context").is_dir()


def test_provision_is_idempotent(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "sport" / "spider2_ipl" / ".context").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    # Re-provisioning an existing tree must succeed (exist_ok), not error.
    r = ep.start_harvest(
        {"mode": "provision", "data_domain": "sport", "dataset": "spider2_ipl"}
    )
    assert r["status"] == "provisioned"


def test_provision_rejects_traversal_and_missing_fields(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    assert (
        ep.start_harvest({"mode": "provision", "data_domain": "sport"})["status"]
        == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "provision", "dataset": "x"})["status"] == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "provision", "data_domain": "..", "dataset": "x"})[
            "status"
        ]
        == "rejected"
    )
    assert (
        ep.start_harvest(
            {"mode": "provision", "data_domain": "sport", "dataset": "a/b"}
        )["status"]
        == "rejected"
    )


def test_start_harvest_accepts_and_runs_background(monkeypatch):
    called = {}

    def fake_dispatch(payload, session_id=None):
        called["payload"] = payload
        called["session_id"] = session_id

    monkeypatch.setattr(ep, "_dispatch", fake_dispatch)
    r = ep.start_harvest(
        {"data_domain": "sales", "dataset": "orders"}, session_id="sid-1"
    )
    assert r["status"] == "accepted"
    assert r["dataset"] == "orders"
    # let the daemon thread run
    for _ in range(50):
        if "payload" in called:
            break
        time.sleep(0.01)
    assert called["payload"]["dataset"] == "orders"
    # The run's session id is threaded into the crawl (used to correlate the feed).
    assert called["session_id"] == "sid-1"
    # busy flag clears after the job finishes
    for _ in range(50):
        if not ep.is_busy():
            break
        time.sleep(0.01)
    assert ep.is_busy() is False


def test_crawl_thread_inherits_context(monkeypatch):
    # The crawl runs on a background thread; OTEL context lives in contextvars,
    # which a bare threading.Thread does NOT inherit. start_harvest must copy the
    # current context into the worker (contextvars.copy_context + ctx.run) so the
    # crawl's spans stay parented under the invoke span. Stand-in for the OTEL
    # span/baggage: a ContextVar set before the call must be visible in _dispatch.
    marker = contextvars.ContextVar("okf_trace_marker", default=None)
    seen = {}

    def fake_dispatch(payload, session_id=None):
        seen["marker"] = marker.get()

    monkeypatch.setattr(ep, "_dispatch", fake_dispatch)
    marker.set("trace-abc")
    ep.start_harvest({"data_domain": "sales", "dataset": "orders"})
    for _ in range(50):
        if "marker" in seen:
            break
        time.sleep(0.01)
    assert seen["marker"] == "trace-abc", "crawl thread lost the OTEL context"


def test_dataset_root_layout():
    assert dataset_root("/mnt/data", "sales", "orders") == "/mnt/data/sales/orders"


def test_finalize_has_no_policy_build_binding():
    # The harvest is DONE at the commit marker: the policy build moved to the
    # RUNNER as a follow-on step after the terminal status write. The
    # regression to guard is finalize regaining the import — that would run
    # the build back inside the harvest window. (A monkeypatch on
    # harvest.ar_build would be vacuous here: it can never intercept a
    # module-level from-import binding, and with the env flag unset the real
    # function no-ops anyway.)
    import harvest.finalize as fin

    assert not hasattr(fin, "maybe_build_policy")


def test_finalize_writes_commit_marker_last(tmp_path):
    # a minimal bundle
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "races.md").write_text(
        "---\ntype: Glue Table\ntitle: Races\ndescription: d\ntimestamp: t\n---\n\nbody\n"
    )
    state = finalize_bundle(
        tmp_path,
        data_domain="sales",
        dataset="orders",
        tables=["races"],
        timestamp="2026-07-01T00:00:00Z",
        table_versions={"races": "1"},
    )
    marker = tmp_path / ".harvest" / "state.json"
    assert marker.is_file()
    doc = json.loads(marker.read_text())
    assert doc["status"] == "complete"
    assert doc["tables"] == ["races"]
    assert doc["table_versions"] == {"races": "1"}
    # index.md regenerated
    assert (tmp_path / "index.md").is_file()
    assert (tmp_path / "tables" / "index.md").is_file()


def test_finalize_cleans_recorded_context_digests_after_the_marker(tmp_path):
    # The extractor digests exist for run_review's context-fidelity phase,
    # which is over by finalize time — a COMPLETED run deletes them (a run
    # that fails before the marker keeps them for debugging; the next full
    # harvest's start-of-run wipe covers that path).
    ctx = tmp_path / ".harvest" / "context"
    ctx.mkdir(parents=True)
    (ctx / "digest-01.md").write_text("# digest", encoding="utf-8")
    finalize_bundle(tmp_path, data_domain="s", dataset="o", tables=[], timestamp="t")
    assert not ctx.exists()
    # The rest of .harvest/ survives (the marker just got written there).
    assert (tmp_path / ".harvest" / "state.json").is_file()


def test_finalize_precomputes_the_graph_artifact(tmp_path):
    # finalize writes .harvest/graph.json stamped with the SAME timestamp the
    # commit marker carries — the Control API's /graph endpoint serves it iff
    # the two match, so every mode that funnels through finalize (full,
    # scoped/incremental, annotation, cross) refreshes the graph.
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "races.md").write_text(
        "---\ntype: Glue Table\ntitle: Races\ndescription: d\ntimestamp: t\n---\n\n"
        "See [results](results.md).\n"
    )
    (tmp_path / "tables" / "results.md").write_text(
        "---\ntype: Glue Table\ntitle: Results\ndescription: d\ntimestamp: t\n---\n\nbody\n"
    )
    # Run inputs must stay out of the graph.
    meta = tmp_path / ".metadata" / "tables"
    meta.mkdir(parents=True)
    (meta / "races.md").write_text("snapshot, not a concept")

    finalize_bundle(
        tmp_path,
        data_domain="sales",
        dataset="f1",
        tables=["races", "results"],
        timestamp="2026-07-01T00:00:00Z",
    )
    graph = json.loads((tmp_path / ".harvest" / "graph.json").read_text())
    state = json.loads((tmp_path / ".harvest" / "state.json").read_text())
    assert graph["completed_at"] == state["completed_at"]
    assert {n["id"] for n in graph["nodes"]} == {"tables/races", "tables/results"}
    assert {(e["source"], e["target"]) for e in graph["edges"]} == {
        ("tables/races", "tables/results")
    }


def test_finalize_graph_failure_never_fails_the_run(tmp_path, monkeypatch):
    # The graph is derived data: a crash precomputing it must not fail a
    # finished multi-hour run — the endpoint just computes live instead.
    from harvest import finalize as fz

    def boom(files):
        raise RuntimeError("graph builder exploded")

    monkeypatch.setattr(fz, "build_graph_json", boom)
    state = finalize_bundle(
        tmp_path, data_domain="s", dataset="o", tables=[], timestamp="t"
    )
    assert state["status"] == "complete"  # returned, not raised
    assert (tmp_path / ".harvest" / "state.json").is_file()  # marker still landed
    assert not (tmp_path / ".harvest" / "graph.json").exists()


def test_mark_in_progress_then_complete(tmp_path):
    mark_in_progress(tmp_path, data_domain="s", dataset="o", timestamp="t0")
    marker = tmp_path / ".harvest" / "state.json"
    assert json.loads(marker.read_text())["status"] == "in_progress"
    finalize_bundle(tmp_path, data_domain="s", dataset="o", tables=[], timestamp="t1")
    assert json.loads(marker.read_text())["status"] == "complete"


def test_finalize_heals_a_read_only_index_instead_of_failing(tmp_path):
    """The live finalize failure: the S3 Files mount presents an existing
    index.md read-only, so okf_core's RAW rewrite raised EACCES and took the
    whole harvest down AFTER every doc was authored (seen on
    references/enums/index.md). finalize injects fsutil's healing writer."""
    from harvest.finalize import finalize_bundle

    root = tmp_path / "ds"
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "races.md").write_text(
        "---\ntype: Glue Table\ntitle: Races\ndescription: d\ntimestamp: t\n---\n"
    )
    stale = root / "tables" / "index.md"
    stale.write_text("old\n")
    stale.chmod(0o444)  # the mount's read-only presentation

    state = finalize_bundle(
        root,
        data_domain="sales",
        dataset="f1",
        tables=["races"],
        timestamp="2026-01-01T00:00:00Z",
    )
    assert state["status"] == "complete"
    assert "Races" in stale.read_text()  # healed + rewritten, not a crash


def test_finalize_digest_cleanup_failure_never_fails_a_committed_harvest(
    tmp_path, monkeypatch
):
    # The harvest is COMMITTED once the marker is written — a stubborn NFS
    # error deleting .harvest/context/ afterwards must not flip a finished
    # run to failed (status row saying failed while the marker says complete).
    from harvest import finalize as fz

    ctx = tmp_path / ".harvest" / "context"
    ctx.mkdir(parents=True)
    (ctx / "digest-01.md").write_text("# digest", encoding="utf-8")

    def boom(path):
        raise OSError("EACCES: mount presented the dir read-only")

    monkeypatch.setattr(fz, "remove_tree", boom)
    state = finalize_bundle(
        tmp_path, data_domain="s", dataset="o", tables=[], timestamp="t"
    )
    assert state["status"] == "complete"  # returned, not raised
    assert (tmp_path / ".harvest" / "state.json").is_file()


def test_runner_has_a_mechanical_lint_backstop_before_finalize():
    # The supervisor prompt prescribes the fix-to-zero gate, but a prompt is
    # advice: the runner must measure the shipped bundle itself and surface
    # the counts on the status row (never blocking publish).
    import inspect

    from harvest import runner as rn

    src = inspect.getsource(rn.run_full_harvest)
    norm = " ".join(src.split())
    assert "from okf_core.lint import lint_bundle as _offline_lint" in norm
    # The backstop sits between the agent run and finalize, and its detail
    # rides the COMPLETE status write.
    assert norm.index("_offline_lint") < norm.index("state = finalize_bundle(")
    assert 'status="complete", detail=lint_detail,' in norm
    # Best-effort: the backstop must never fail the run.
    assert "never fail the run" in src

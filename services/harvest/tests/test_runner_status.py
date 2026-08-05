"""The runner drives the registry status: running -> complete | failed.

Offline: build_harvest_agent / finalize_bundle / _table_versions and the status
reporter are patched, so no deepagents/AWS/DynamoDB is needed. We record the
(status, detail) sequence report_status is called with.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import harvest.runner as runner


class _OkAgent:
    def invoke(self, *_a, **_k):
        return {"messages": []}

    def stream(self, *_a, **_k):
        # The runner drives the agent via .stream(); yield nothing (an empty crawl).
        return iter(())


class _BoomAgent:
    def invoke(self, *_a, **_k):
        raise ValueError("crawl exploded")

    def stream(self, *_a, **_k):
        raise ValueError("crawl exploded")


class _Src:
    # Real Source classes always carry a prompt profile (source_base contract);
    # the incremental path reads it to build the scoped maintenance prompt.
    from harvest.glue_source import GlueAthenaSource as _G

    prompt_profile = _G.prompt_profile

    def table_names(self):
        return ["races"]


def _patch(monkeypatch, agent, transitions):
    class _Built:
        pass

    _Built.agent = agent
    monkeypatch.setattr(runner, "build_harvest_agent", lambda *a, **k: _Built())
    monkeypatch.setattr(
        runner, "finalize_bundle", lambda *a, **k: {"status": "complete"}
    )
    monkeypatch.setattr(runner, "_table_versions", lambda *_a, **_k: {})
    # No real DynamoDB client; capture the transition sequence.
    monkeypatch.setattr(runner, "build_registry_client", lambda: ("fake", "tbl"))

    def fake_report(
        registry,
        *,
        data_domain,
        dataset,
        status,
        detail=None,
        only_if_active=False,
        session_id=None,
        model=None,
        effort=None,
        subagent_model=None,
        subagent_effort=None,
        reviewer_model=None,
        reviewer_effort=None,
    ):
        transitions.append(
            (status, detail, subagent_model, subagent_effort, reviewer_model)
        )
        return True  # the write landed (False = a cancel won the race)

    monkeypatch.setattr(runner, "report_status", fake_report)
    # The follow-on build's feature flag reads as ON here so the stubs below
    # are reachable (the flag gate itself has its own test); the real
    # maybe_build_policy is stubbed so nothing ever authors.
    monkeypatch.setattr(runner, "build_enabled", lambda: True)
    monkeypatch.setattr(runner, "maybe_build_policy", lambda **kw: "disabled")
    monkeypatch.setattr(runner, "publish_rebuild_event", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_wait_for_bundle_flush", lambda **kw: None)


def test_full_harvest_reports_running_then_complete(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
    )

    assert [t[0] for t in transitions] == ["running", "complete"]


def test_full_harvest_builds_policy_after_the_terminal_write(tmp_path, monkeypatch):
    # The follow-on guardrails build fires exactly once, AFTER the `complete`
    # write has landed — the moved-out-of-finalize contract (the harvest is
    # DONE at the commit; the build serializes via its own `building` lock).
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)
    builds: list[list[str]] = []
    monkeypatch.setattr(
        runner,
        "maybe_build_policy",
        lambda **kw: builds.append([t[0] for t in transitions]) or "authored",
    )

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
    )

    assert len(builds) == 1
    assert builds[0] == ["running", "complete"]  # complete landed FIRST


def test_full_harvest_skips_policy_build_when_cancel_won(tmp_path, monkeypatch):
    # report_status(complete) returning False means an operator cancel flipped
    # the row first — authoring guardrails for that run would hold the build
    # lock (and 409 the operator's next action) for a ghost.
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    def cancelled_report(registry, *, status, **kw):
        transitions.append((status,))
        return status != "complete"

    monkeypatch.setattr(runner, "report_status", cancelled_report)
    builds: list[str] = []
    monkeypatch.setattr(
        runner, "maybe_build_policy", lambda **kw: builds.append("build")
    )

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
    )

    assert builds == []


def test_wait_for_bundle_flush_polls_until_the_marker_lands(monkeypatch):
    # The bundle is written through the S3 Files mount, whose flush can lag
    # the terminal write by a minute — gathering early fingerprints a partial
    # wiki and the fresh document immediately reads "out of date" (live
    # 2026-08-04). The wait polls for the commit marker, then adds a settle
    # margin (the flush is not strictly ordered).
    import time as _time

    import boto3

    import okf_aws

    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())
    states = iter([None, {"status": "in_progress"}, {"status": "complete"}])
    monkeypatch.setattr(
        okf_aws, "bundle_marker_state", lambda *a, **k: next(states)
    )
    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))

    runner._wait_for_bundle_flush(data_domain="s", dataset="db")

    # Two 5s polls while the marker was absent/mid-write, then the settle.
    assert sleeps == [5, 5, runner._POST_MARKER_SETTLE_S]


def test_wait_for_bundle_flush_ignores_the_previous_runs_marker(monkeypatch):
    # On a re-harvest, the PREVIOUS run's `complete` marker is still the
    # visible S3 object until the mount flushes (mark_in_progress's overwrite
    # lags too) — a bare status check would return immediately and the build
    # would fingerprint the PRE-run wiki. Pinning on completed_at holds the
    # wait until THIS run's marker is the one visible.
    import time as _time

    import boto3

    import okf_aws

    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())
    states = iter(
        [
            {"status": "complete", "completed_at": "2026-08-04T20:00:00+00:00"},
            {"status": "complete", "completed_at": "2026-08-04T21:00:00+00:00"},
        ]
    )
    monkeypatch.setattr(
        okf_aws, "bundle_marker_state", lambda *a, **k: next(states)
    )
    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))

    runner._wait_for_bundle_flush(
        data_domain="s", dataset="db", completed_at="2026-08-04T21:00:00+00:00"
    )

    # One 5s poll rejecting the OLD marker, then this run's marker + settle.
    assert sleeps == [5, runner._POST_MARKER_SETTLE_S]


def test_wait_for_bundle_flush_noop_without_a_bucket(monkeypatch):
    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    runner._wait_for_bundle_flush(data_domain="s", dataset="db")  # returns fast


def test_follow_on_build_skips_the_flush_wait_when_disabled(monkeypatch):
    # With OKF_POLICY_BUILD_ENABLED off (the default), maybe_build_policy is
    # a no-op — the up-to-190s flush wait would be pure billed runtime waste,
    # so the flag is checked FIRST and nothing below it runs.
    monkeypatch.setattr(runner, "build_enabled", lambda: False)
    touched: list[str] = []
    monkeypatch.setattr(
        runner, "_wait_for_bundle_flush", lambda **kw: touched.append("wait")
    )
    monkeypatch.setattr(
        runner, "maybe_build_policy", lambda **kw: touched.append("build")
    )

    runner._follow_on_policy_build(data_domain="s", dataset="db", completed=True)

    assert touched == []


def test_full_harvest_locked_build_leaves_a_rebuild_trigger(tmp_path, monkeypatch):
    # Losing the flip race (the previous harvest's author still running) must
    # leave a policy_rebuild trigger behind, or the newer wiki's re-author
    # waits for the nightly reconcile.
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)
    monkeypatch.setattr(
        runner, "maybe_build_policy", lambda **kw: runner.OUTCOME_LOCKED
    )
    published: list[tuple] = []
    monkeypatch.setattr(
        runner,
        "publish_rebuild_event",
        lambda d, ds, *, reason: published.append((d, ds, reason)),
    )

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
    )

    assert published == [("s", "db", "post_harvest_build_locked")]


def test_full_harvest_stamps_subagent_override_on_running(tmp_path, monkeypatch):
    # A run with a separate sub-agent config records it at `running`; without
    # one, nothing is stamped (the UI reads absence as "same as harvester").
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
        subagent_model_config={
            "model": "openai.gpt-5.6-sol",
            "effort": "high",
            "max_tokens": 32000,
        },
    )

    running = next(t for t in transitions if t[0] == "running")
    assert running[2] == "openai.gpt-5.6-sol"
    assert running[3] == "high"


def test_full_harvest_stamps_reviewer_override_on_running(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
        reviewer_model_config={
            "model": "openai.gpt-5.6-sol",
            "effort": "high",
            "max_tokens": 32000,
        },
    )

    running = next(t for t in transitions if t[0] == "running")
    assert running[4] == "openai.gpt-5.6-sol"


def test_full_harvest_no_subagent_stamp_without_override(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    runner.run_full_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
    )

    running = next(t for t in transitions if t[0] == "running")
    assert running[2] is None and running[3] is None


def test_full_harvest_reports_failed_and_reraises(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _BoomAgent(), transitions)

    with pytest.raises(ValueError, match="crawl exploded"):
        runner.run_full_harvest(
            source=_Src(),
            dataset_root=tmp_path / "s" / "db",
            data_domain="s",
            dataset="db",
        )

    assert transitions[0][0] == "running"
    assert transitions[-1][0] == "failed"
    # The failure detail carries the exception type + message for the UI.
    assert "ValueError" in transitions[-1][1]
    assert "crawl exploded" in transitions[-1][1]
    # It must NOT report complete after a failure.
    assert "complete" not in [t[0] for t in transitions]


def test_full_harvest_marks_failed_when_mark_in_progress_crashes(tmp_path, monkeypatch):
    # Regression: a crash in mark_in_progress (e.g. EACCES from the S3 Files mount)
    # used to happen BEFORE the status flip, leaving the registry stuck at `queued`
    # forever and holding the lease. It must now report `failed` and re-raise.
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    def boom_mkdir(*_a, **_k):
        raise PermissionError("[Errno 13] Permission denied: '/mnt/data/s/db'")

    monkeypatch.setattr(runner, "mark_in_progress", boom_mkdir)

    with pytest.raises(PermissionError):
        runner.run_full_harvest(
            source=_Src(),
            dataset_root=tmp_path / "s" / "db",
            data_domain="s",
            dataset="db",
        )

    # Never reached `running` (crash was earlier), but DID report `failed`.
    assert "running" not in [t[0] for t in transitions]
    assert transitions[-1][0] == "failed"
    assert "PermissionError" in transitions[-1][1]
    assert "complete" not in [t[0] for t in transitions]


def test_incremental_harvest_marks_failed_when_mark_in_progress_crashes(
    tmp_path, monkeypatch
):
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)
    monkeypatch.setattr(
        runner,
        "mark_in_progress",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("EACCES")),
    )

    with pytest.raises(PermissionError):
        runner.run_incremental_harvest(
            source=_Src(),
            dataset_root=tmp_path / "s" / "db",
            data_domain="s",
            dataset="db",
            changed_table="races",
        )

    assert "running" not in [t[0] for t in transitions]
    assert transitions[-1][0] == "failed"


def test_incremental_harvest_reports_running_then_complete(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _OkAgent(), transitions)

    runner.run_incremental_harvest(
        source=_Src(),
        dataset_root=tmp_path / "s" / "db",
        data_domain="s",
        dataset="db",
        changed_table="races",
    )

    assert [t[0] for t in transitions] == ["running", "complete"]


def test_incremental_harvest_reports_failed_and_reraises(tmp_path, monkeypatch):
    transitions: list[tuple] = []
    _patch(monkeypatch, _BoomAgent(), transitions)

    with pytest.raises(ValueError):
        runner.run_incremental_harvest(
            source=_Src(),
            dataset_root=tmp_path / "s" / "db",
            data_domain="s",
            dataset="db",
            changed_table="races",
        )

    assert transitions[0][0] == "running"
    assert transitions[-1][0] == "failed"
    assert "complete" not in [t[0] for t in transitions]

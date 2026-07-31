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

    monkeypatch.setattr(runner, "report_status", fake_report)


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

"""The runner owns the sandbox lifecycle around a crawl (offline).

Asserts that when a sandbox is available it is started, has .context uploaded,
is passed into build_harvest_agent, and is always stopped — and that when none
is available the harvest still runs (sandbox=None) without wedging.
"""

from __future__ import annotations

from pathlib import Path

import harvest.runner as runner


class _FakeSandbox:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.uploaded_from = None

    def start(self):
        self.started = True

    def upload_context(self, dataset_root):
        self.uploaded_from = Path(dataset_root)
        return ["spec.docx"]

    def stop(self):
        self.stopped = True


class _Src:
    def table_names(self):
        return ["new"]


def _patch_common(monkeypatch, captured: dict):
    class _Agent:
        def invoke(self, *_a, **_k):
            return {"messages": []}

        def stream(self, *_a, **_k):
            # The runner drives the agent via .stream(); yield nothing.
            return iter(())

    class _Built:
        agent = _Agent()

    def _build(*_a, **kwargs):
        captured["sandbox"] = kwargs.get("sandbox")
        return _Built()

    monkeypatch.setattr(runner, "build_harvest_agent", _build)
    monkeypatch.setattr(
        runner, "finalize_bundle", lambda *a, **k: {"status": "complete"}
    )
    monkeypatch.setattr(runner, "_table_versions", lambda *_a, **_k: {})


def test_full_harvest_starts_uploads_and_stops_sandbox(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "f1"
    root.mkdir(parents=True)
    fake = _FakeSandbox()
    captured: dict = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(runner, "build_sandbox", lambda: fake)

    runner.run_full_harvest(
        source=_Src(), dataset_root=root, data_domain="sport", dataset="f1"
    )

    assert fake.started is True
    assert fake.uploaded_from == root
    assert captured["sandbox"] is fake  # passed into the agent build
    assert fake.stopped is True  # stopped in the finally


def test_incremental_harvest_uses_sandbox(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "f1"
    root.mkdir(parents=True)
    fake = _FakeSandbox()
    captured: dict = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(runner, "build_sandbox", lambda: fake)

    runner.run_incremental_harvest(
        source=_Src(),
        dataset_root=root,
        data_domain="sport",
        dataset="f1",
        changed_table="new",
    )
    assert fake.started and fake.stopped
    assert captured["sandbox"] is fake


def test_harvest_runs_without_sandbox(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "f1"
    root.mkdir(parents=True)
    captured: dict = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(runner, "build_sandbox", lambda: None)

    state = runner.run_full_harvest(
        source=_Src(), dataset_root=root, data_domain="sport", dataset="f1"
    )
    assert state == {"status": "complete"}
    assert captured["sandbox"] is None  # agent built with no run_code tool


def test_sandbox_start_failure_degrades_gracefully(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "f1"
    root.mkdir(parents=True)
    captured: dict = {}
    _patch_common(monkeypatch, captured)

    class _BadSandbox(_FakeSandbox):
        def upload_context(self, dataset_root):
            raise RuntimeError("upload boom")

    bad = _BadSandbox()
    monkeypatch.setattr(runner, "build_sandbox", lambda: bad)

    # The harvest must complete even though the sandbox failed to prepare.
    state = runner.run_full_harvest(
        source=_Src(), dataset_root=root, data_domain="sport", dataset="f1"
    )
    assert state == {"status": "complete"}
    assert captured["sandbox"] is None  # degraded to no run_code
    assert bad.stopped is True  # cleaned up after the failure

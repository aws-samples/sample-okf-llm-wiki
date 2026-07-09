"""A full harvest is a CLEAN rebuild; an incremental harvest is not.

These run offline: build_harvest_agent / finalize_bundle / _table_versions are
patched so no deepagents/AWS is needed. We assert the ORDER of operations by
snapshotting the dataset dir at the moment the agent is invoked.
"""

from __future__ import annotations

from pathlib import Path

import harvest.runner as runner


class _FakeAgent:
    """Records the on-disk state of the dataset root when the crawl runs."""

    def __init__(self, dataset_root: Path, seen: dict):
        self._root = dataset_root
        self._seen = seen

    def _author(self):
        # What survives into the authoring phase?
        self._seen["present_at_invoke"] = sorted(p.name for p in self._root.iterdir())
        # The agent would author here; simulate one fresh table doc.
        (self._root / "tables").mkdir(exist_ok=True)
        (self._root / "tables" / "new.md").write_text("---\ntype: Glue Table\n---\n")

    def invoke(self, *_a, **_k):
        self._author()
        return {"messages": []}

    def stream(self, *_a, **_k):
        # The runner drives the crawl via .stream(); author, then yield nothing.
        self._author()
        return iter(())


def _patch_offline(monkeypatch, dataset_root: Path, seen: dict):
    class _Built:
        agent = _FakeAgent(dataset_root, seen)

    monkeypatch.setattr(runner, "build_harvest_agent", lambda *a, **k: _Built())
    monkeypatch.setattr(
        runner, "finalize_bundle", lambda *a, **k: {"status": "complete"}
    )
    monkeypatch.setattr(runner, "_table_versions", lambda *_a, **_k: {})


class _Src:
    def table_names(self):
        return ["new"]


def _seed_prior_bundle(root: Path) -> None:
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "OLD_dropped_table.md").write_text(
        "---\ntype: Glue Table\n---\n"
    )
    (root / "datasets").mkdir()
    (root / "datasets" / "db.md").write_text("---\ntype: Glue Database\n---\n")
    (root / "index.md").write_text("# stale index mentioning OLD_dropped_table\n")
    (root / ".context").mkdir()
    (root / ".context" / "notes.md").write_text("user upload\n")


def test_full_harvest_wipes_prior_output_before_authoring(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "formula_1"
    _seed_prior_bundle(root)
    seen: dict = {}
    _patch_offline(monkeypatch, root, seen)

    runner.run_full_harvest(
        source=_Src(), dataset_root=root, data_domain="sport", dataset="formula_1"
    )

    # At authoring time the prior output was already gone; only dot-dirs remained.
    assert "OLD_dropped_table.md" not in str(seen["present_at_invoke"])
    assert "datasets" not in seen["present_at_invoke"]
    assert "index.md" not in seen["present_at_invoke"]
    # .context (user input) + .harvest (marker written by mark_in_progress) survive.
    assert ".context" in seen["present_at_invoke"]
    assert ".harvest" in seen["present_at_invoke"]
    # And the stale dropped-table doc is gone from disk after the run.
    assert not (root / "tables" / "OLD_dropped_table.md").exists()
    assert (root / ".context" / "notes.md").read_text() == "user upload\n"


def test_incremental_harvest_does_not_wipe(tmp_path, monkeypatch):
    root = tmp_path / "sport" / "formula_1"
    _seed_prior_bundle(root)
    seen: dict = {}
    _patch_offline(monkeypatch, root, seen)

    runner.run_incremental_harvest(
        source=_Src(),
        dataset_root=root,
        data_domain="sport",
        dataset="formula_1",
        changed_table="new",
    )

    # Incremental preserves the existing bundle — the prior docs are still there.
    assert "datasets" in seen["present_at_invoke"]
    assert (root / "datasets" / "db.md").exists()

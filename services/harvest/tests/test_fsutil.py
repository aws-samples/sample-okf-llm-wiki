"""NFS-resilient fs helpers: retry transient ESTALE, then succeed / give up."""

import errno
from pathlib import Path

import pytest

import harvest.fsutil as fsutil


def test_mkdirs_creates_nested(tmp_path):
    target = tmp_path / "sport" / "formula_1" / ".harvest"
    fsutil.mkdirs(target)
    assert target.is_dir()


def test_write_text_creates_parent(tmp_path):
    p = tmp_path / "a" / "b" / "state.json"
    fsutil.write_text(p, '{"status":"complete"}\n')
    assert p.read_text().startswith("{")


def test_retry_recovers_from_transient_estale(monkeypatch):
    # First two calls raise ESTALE, third succeeds — _retry should return it.
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.ESTALE, "Stale file handle")
        return "ok"

    monkeypatch.setattr(fsutil.time, "sleep", lambda *_: None)  # no real delay
    assert fsutil._retry(flaky, what="test") == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_persistent_estale(monkeypatch):
    def always_stale():
        raise OSError(errno.ESTALE, "Stale file handle")

    monkeypatch.setattr(fsutil.time, "sleep", lambda *_: None)
    with pytest.raises(OSError) as e:
        fsutil._retry(always_stale, what="test")
    assert e.value.errno == errno.ESTALE


def test_retry_does_not_swallow_non_retryable(monkeypatch):
    # e.g. EACCES (permission) must propagate immediately, not be retried.
    def denied():
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fsutil.time, "sleep", lambda *_: None)
    with pytest.raises(OSError) as e:
        fsutil._retry(denied, what="test")
    assert e.value.errno == errno.EACCES


# --- clean_authored_output (full-harvest clean rebuild) ---------------------


def _seed_bundle(root: Path) -> None:
    """A representative post-harvest bundle: authored output + preserved dirs."""
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "races.md").write_text("---\ntype: Glue Table\n---\n")
    (root / "tables" / "index.md").write_text("# index\n")
    (root / "datasets").mkdir()
    (root / "datasets" / "formula_1.md").write_text("---\ntype: Glue Database\n---\n")
    (root / "references" / "joins").mkdir(parents=True)
    (root / "references" / "joins" / "a__b.md").write_text(
        "---\ntype: Reference\n---\n"
    )
    (root / "index.md").write_text("# root index\n")
    (root / "log.md").write_text("# log\n")
    # Preserved: user-uploaded input + the commit marker.
    (root / ".context").mkdir()
    (root / ".context" / "notes.md").write_text("user upload\n")
    (root / ".harvest").mkdir()
    (root / ".harvest" / "state.json").write_text('{"status":"in_progress"}\n')


def test_clean_removes_authored_output_preserves_dotdirs(tmp_path):
    root = tmp_path / "sport" / "formula_1"
    _seed_bundle(root)

    removed = fsutil.clean_authored_output(root)

    # Authored output gone.
    assert not (root / "tables").exists()
    assert not (root / "datasets").exists()
    assert not (root / "references").exists()
    assert not (root / "index.md").exists()
    assert not (root / "log.md").exists()
    # Preserved: user input + state (deleting .context would destroy user data).
    assert (root / ".context" / "notes.md").read_text() == "user upload\n"
    assert (root / ".harvest" / "state.json").exists()
    # Reports what it removed (names only, sorted, no dot-dirs).
    assert set(removed) == {"tables", "datasets", "references", "index.md", "log.md"}
    assert not any(n.startswith(".") for n in removed)


def test_clean_on_missing_root_is_noop(tmp_path):
    assert fsutil.clean_authored_output(tmp_path / "does" / "not" / "exist") == []


def test_clean_on_fresh_dataset_removes_nothing(tmp_path):
    # First-ever harvest: only .harvest exists (just marked in-progress).
    root = tmp_path / "sport" / "new_db"
    (root / ".harvest").mkdir(parents=True)
    (root / ".harvest" / "state.json").write_text('{"status":"in_progress"}\n')
    assert fsutil.clean_authored_output(root) == []
    assert (root / ".harvest" / "state.json").exists()

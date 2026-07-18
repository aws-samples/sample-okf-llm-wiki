"""Bundle snapshot: authored content copied, all dot-dirs physically excluded."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harvest.benchmark.snapshot import restore_authored, snapshot_bundle


def _make_bundle(root: Path):
    # Authored output.
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "races.md").write_text("# races\none row per race")
    (root / "references").mkdir()
    (root / "references" / "metrics.md").write_text("# metrics")
    (root / "index.md").write_text("# index")
    # Inputs the solver must NOT see.
    (root / ".metadata").mkdir()
    (root / ".metadata" / "columns.tsv").write_text("races\tresult_id\tint\tSECRET SCHEMA")
    (root / ".context").mkdir()
    (root / ".context" / "manual.pdf").write_text("source doc secrets")
    (root / ".benchmark").mkdir()
    (root / ".benchmark" / "questions.csv").write_text("question,gold_sql\nq,SELECT 1")
    (root / ".harvest").mkdir()
    (root / ".harvest" / "state.json").write_text("{}")


def test_snapshot_copies_authored_excludes_dotdirs():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "bundle"
        src.mkdir()
        _make_bundle(src)
        dest = Path(tmp) / "snap"

        snapshot_bundle(src, dest)

        # Authored content is present.
        assert (dest / "tables" / "races.md").read_text().startswith("# races")
        assert (dest / "references" / "metrics.md").exists()
        assert (dest / "index.md").exists()
        # Every dot-dir is physically absent.
        for hidden in (".metadata", ".context", ".benchmark", ".harvest"):
            assert not (dest / hidden).exists(), f"{hidden} leaked into snapshot"


def test_snapshot_grep_cannot_find_schema_secret():
    # The whole point: a recursive scan of the snapshot must not surface the
    # schema snapshot's contents (what a solver's grep would see).
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "bundle"
        src.mkdir()
        _make_bundle(src)
        dest = Path(tmp) / "snap"
        snapshot_bundle(src, dest)

        all_text = "".join(
            p.read_text(errors="ignore") for p in dest.rglob("*") if p.is_file()
        )
        assert "SECRET SCHEMA" not in all_text
        assert "source doc secrets" not in all_text
        assert "SELECT 1" not in all_text  # gold from .benchmark not present


def test_nested_dotdir_under_authored_dir_excluded():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "bundle"
        (src / "tables").mkdir(parents=True)
        (src / "tables" / "races.md").write_text("ok")
        (src / "tables" / ".scratch").mkdir()
        (src / "tables" / ".scratch" / "leak.txt").write_text("nested secret")
        dest = Path(tmp) / "snap"
        snapshot_bundle(src, dest)

        assert (dest / "tables" / "races.md").exists()
        assert not (dest / "tables" / ".scratch").exists()


def test_missing_source_yields_empty_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "snap"
        out = snapshot_bundle(Path(tmp) / "does-not-exist", dest)
        assert out == dest
        assert dest.exists()
        assert list(dest.iterdir()) == []


def test_restore_replaces_authored_content_without_copystat(monkeypatch):
    # Restore must NOT use shutil.copy2/copytree — they call copystat, which the
    # S3 Files NFS mount rejects with Errno 524. Guard: fail loudly if either is
    # called during a restore, and assert the content still lands correctly.
    import shutil as _shutil

    def _boom(*a, **k):
        raise AssertionError("restore must not use shutil.copy2/copytree (copystat → Errno 524)")

    monkeypatch.setattr(_shutil, "copy2", _boom)
    monkeypatch.setattr(_shutil, "copytree", _boom)

    with tempfile.TemporaryDirectory() as tmp:
        # Best-round checkpoint (the good content).
        snap = Path(tmp) / "best"
        (snap / "tables").mkdir(parents=True)
        (snap / "tables" / "races.md").write_text("GOOD")
        (snap / "index.md").write_text("# index")
        # Live mount tree: a regressed doc + a stale doc + dot-dirs to preserve.
        dst = Path(tmp) / "mount"
        (dst / "tables").mkdir(parents=True)
        (dst / "tables" / "races.md").write_text("REGRESSED")
        (dst / "tables" / "stale.md").write_text("should be removed")
        (dst / ".harvest").mkdir()
        (dst / ".harvest" / "state.json").write_text("{}")

        restore_authored(snap, dst)

        assert (dst / "tables" / "races.md").read_text() == "GOOD"
        assert (dst / "index.md").read_text() == "# index"
        assert not (dst / "tables" / "stale.md").exists()  # pruned
        assert (dst / ".harvest" / "state.json").exists()  # dot-dir untouched


def test_restore_missing_snapshot_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "mount"
        dst.mkdir()
        (dst / "index.md").write_text("keep")
        restore_authored(Path(tmp) / "nope", dst)
        assert (dst / "index.md").read_text() == "keep"

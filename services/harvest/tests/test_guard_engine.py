from datetime import datetime, timezone
from pathlib import Path

from harvest.guard_engine import OKFGuardEngine
from okf_core.document import OKFDocument
from okf_core.link_graph import LinkGraph

_FIXED = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _engine(tmp_path):
    return OKFGuardEngine(LinkGraph(tmp_path), now_fn=lambda: _FIXED)


def _doc(body="# Overview\ntext\n", **fm):
    base = {"type": "Glue Table", "title": "Races", "description": "d"}
    base.update(fm)
    return OKFDocument(frontmatter=base, body=body).serialize()


def test_write_ok_normalizes_and_fills_timestamp(tmp_path):
    eng = _engine(tmp_path)
    content = _doc()  # no timestamp
    d = eng.guard_write_file(content, existing_text=None)
    assert d.allow
    assert d.new_content is not None
    parsed = OKFDocument.parse(d.new_content)
    assert parsed.frontmatter["timestamp"] == _FIXED.isoformat(timespec="seconds")
    # canonical key order: type first
    assert list(parsed.frontmatter)[0] == "type"


def test_write_rejected_missing_frontmatter(tmp_path):
    eng = _engine(tmp_path)
    d = eng.guard_write_file("no frontmatter here", existing_text=None)
    assert not d.allow
    assert "frontmatter" in d.message.lower()


def test_write_rejected_missing_required_key(tmp_path):
    eng = _engine(tmp_path)
    content = OKFDocument(frontmatter={"type": "Glue Table"}, body="x").serialize()
    d = eng.guard_write_file(content, existing_text=None)
    assert not d.allow
    assert "title" in d.message


def test_write_augmentation_blocks_schema_shrink(tmp_path):
    eng = _engine(tmp_path)
    existing = _doc(body="# Schema\n| `a` | int |\n| `b` | int |\n| `c` | int |\n")
    shrunk = _doc(body="# Schema\n| `a` | int |\n")
    d = eng.guard_write_file(shrunk, existing_text=existing)
    assert not d.allow
    assert "`b`" in d.message


def test_write_marks_graph_dirty_on_success(tmp_path):
    lg = LinkGraph(tmp_path)
    eng = OKFGuardEngine(lg, now_fn=lambda: _FIXED)
    lg.dirty = False
    eng.guard_write_file(_doc(), existing_text=None)
    assert lg.dirty is True


def test_edit_ok_when_result_valid(tmp_path):
    eng = _engine(tmp_path)
    existing = _doc(body="# Schema\n`a` `b`\n")
    d = eng.guard_edit_file("`a` `b`", "`a` `b` `c`", existing_text=existing)
    assert d.allow


def test_edit_blocks_schema_shrink(tmp_path):
    eng = _engine(tmp_path)
    existing = _doc(body="# Schema\n| `a` | int |\n| `b` | int |\n| `c` | int |\n")
    # Removing the `b` and `c` rows from the schema table.
    d = eng.guard_edit_file(
        "| `b` | int |\n| `c` | int |\n", "", existing_text=existing
    )
    assert not d.allow
    assert "Schema" in d.message


def test_edit_blocks_breaking_frontmatter(tmp_path):
    eng = _engine(tmp_path)
    existing = _doc()
    # Delete the title line via edit -> result missing required key.
    d = eng.guard_edit_file("title: Races\n", "", existing_text=existing)
    assert not d.allow


def test_edit_passthrough_when_no_match(tmp_path):
    eng = _engine(tmp_path)
    existing = _doc()
    d = eng.guard_edit_file("NOT PRESENT", "x", existing_text=existing)
    assert d.allow  # let the built-in tool report the no-match


def test_edit_passthrough_when_file_absent(tmp_path):
    eng = _engine(tmp_path)
    d = eng.guard_edit_file("a", "b", existing_text=None)
    assert d.allow

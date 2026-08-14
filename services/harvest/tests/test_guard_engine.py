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


def test_engine_refuses_join_docs_without_a_fenced_condition(tmp_path):
    eng = _engine(tmp_path)
    doc = (
        "---\ntype: Reference\ntitle: J\ndescription: d\n---\n\n"
        'Use `a."id" = aa."annotation"` inline.\n'
    )
    d = eng.guard_write_file(
        doc, existing_text=None, rel_path="references/joins/a__b.md"
    )
    assert not d.allow and "```sql" in d.message
    # Same content is fine anywhere else...
    assert eng.guard_write_file(
        doc, existing_text=None, rel_path="tables/a.md"
    ).allow
    # ...and fine as a join doc once the fence is added.
    fixed = doc + "\n```sql\na.id = aa.annotation\n```\n"
    assert eng.guard_write_file(
        fixed, existing_text=None, rel_path="references/joins/a__b.md"
    ).allow
    # An edit that REMOVES the only fenced condition is refused too.
    d = eng.guard_edit_file(
        "```sql\na.id = aa.annotation\n```\n",
        "",
        existing_text=fixed,
        rel_path="references/joins/a__b.md",
    )
    assert not d.allow and "```sql" in d.message


# -- Attested Computations (docs/ATTESTED_COMPUTATIONS.md) --------------------


def _comp_doc(sql="SELECT r FROM t WHERE r = @region", verified="null",
              verified_by="null", verified_sha256=None):
    fm = {
        "type": "Attested Computation",
        "title": "C",
        "description": "d",
        "runtime": "athena",
        "parameters": [
            {"name": "region", "type": "string", "required": True, "example": "EMEA"}
        ],
        "verified": None if verified == "null" else verified,
        "verified_by": None if verified_by == "null" else verified_by,
    }
    if verified_sha256 is not None:
        fm["verified_sha256"] = verified_sha256
    body = f"prose\n\n# Computation\n\n```sql\n{sql}\n```\n"
    return OKFDocument(frontmatter=fm, body=body).serialize()


_COMP_REL = "references/computations/c.md"


def test_engine_allows_valid_computation_and_refuses_bad_shape(tmp_path):
    eng = _engine(tmp_path)
    assert eng.guard_write_file(
        _comp_doc(), existing_text=None, rel_path=_COMP_REL
    ).allow
    d = eng.guard_write_file(
        _comp_doc(sql="SELECT r FROM t"),  # declared param never used
        existing_text=None,
        rel_path=_COMP_REL,
    )
    assert not d.allow and "never appears" in d.message
    # The computation type must live in its folder.
    d = eng.guard_write_file(
        _comp_doc(), existing_text=None, rel_path="references/metrics/c.md"
    )
    assert not d.allow and "references/computations/" in d.message


def test_engine_refuses_agent_set_verification_and_allows_preservation(tmp_path):
    from okf_core.computations import parse_computation_text

    eng = _engine(tmp_path)
    # Inventing a stamp on create -> refused, on ANY doc type.
    d = eng.guard_write_file(
        _comp_doc(verified="2026-08-14T00:00:00Z"),
        existing_text=None,
        rel_path=_COMP_REL,
    )
    assert not d.allow and "HUMAN" in d.message

    # A human-verified doc (as the fold-in would write it)...
    comp, _ = parse_computation_text(_COMP_REL, _comp_doc())
    stamped = _comp_doc(
        verified="2026-08-14T00:00:00Z",
        verified_by="analyst@example.com",
        verified_sha256=comp.sha256,
    )
    # ...may be re-written PRESERVING the triple verbatim...
    assert eng.guard_write_file(
        stamped, existing_text=stamped, rel_path=_COMP_REL
    ).allow
    # ...but never with an altered identity.
    tampered = stamped.replace("analyst@example.com", "attacker@example.com")
    d = eng.guard_write_file(tampered, existing_text=stamped, rel_path=_COMP_REL)
    assert not d.allow and "HUMAN" in d.message
    # An edit_file that flips a verification field is refused the same way.
    d = eng.guard_edit_file(
        "verified: null", "verified: 2026-08-14T00:00:00Z",
        existing_text=_comp_doc(), rel_path=_COMP_REL,
    )
    assert not d.allow and "HUMAN" in d.message


# -- code-review regressions (2026-08-13 xhigh pass) --------------------------


def test_edit_replace_all_is_simulated_faithfully(tmp_path):
    """The guard must validate the SAME text the backend writes: replace_all
    replaces every occurrence, and validating a single-replacement simulation
    would let e.g. an undeclared-hole computation land on disk."""
    eng = _engine(tmp_path)
    existing = _comp_doc(sql="SELECT r FROM t WHERE r = @region -- prose @region too")
    # Single replace only touches the first (comment) site — result still has
    # the declared hole in the WHERE, so it would pass; replace_all renames
    # BOTH sites and must be refused (hole `@r2` undeclared).
    d = eng.guard_edit_file(
        "@region", "@r2", existing_text=existing, rel_path=_COMP_REL,
        replace_all=True,
    )
    assert not d.allow and "not declared" in d.message


def test_write_refuses_frontmatter_that_breaks_the_round_trip(tmp_path):
    """safe_dump renders a '---' line inside a quoted scalar; parse's
    terminator scan then truncates the frontmatter — the doc would brick for
    every later reader, so the guard must refuse, not write."""
    eng = _engine(tmp_path)
    # A DOUBLE-QUOTED one-line scalar parses fine on the way in; only the
    # canonical re-serialization (block style) puts the `---` on its own line.
    content = (
        "---\n"
        "type: Glue Table\n"
        "title: T\n"
        'description: "Line one.\\n---\\nLine two."\n'
        "---\n\nbody\n"
    )
    assert OKFDocument.parse(content).frontmatter["description"].count("---")
    d = eng.guard_write_file(content, existing_text=None, rel_path="tables/t.md")
    assert not d.allow and "round-trip" in d.message


def test_unparseable_existing_doc_is_repairable(tmp_path):
    """A doc bricked on disk (torn write / earlier bug) must accept a full
    rewrite — raising on the existing parse refused every repair forever."""
    eng = _engine(tmp_path)
    bricked = "---\ntype: Glue Table\ntitle: T\ndescription: |\n  x\n"  # unterminated
    d = eng.guard_write_file(_doc(), existing_text=bricked, rel_path="tables/t.md")
    assert d.allow

"""read_skill: the generic tool serving the vendored methodology skills.

The catalog is scanned from ``services/chat/skills/<name>/SKILL.md`` at
import; these tests cover both the REAL shipped set (the report-authoring
skill must load — a frontmatter typo there would silently drop the report
methodology from every conversation) and the loader's tolerance rules over a
tmp_path root.
"""

from __future__ import annotations

import pathlib

from chat.skills import CATALOG, load_skill_catalog, make_read_skill_tool


def _write_skill(root: pathlib.Path, dirname: str, text: str) -> None:
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


# --- the shipped catalog ------------------------------------------------------


def test_shipped_catalog_serves_report_authoring():
    names = [s["name"] for s in CATALOG]
    assert "report-authoring" in names
    skill = next(s for s in CATALOG if s["name"] == "report-authoring")
    # description = the discovery line the tool description carries
    assert "create_report" in skill["description"]
    # body = the methodology itself, frontmatter stripped
    assert skill["body"].startswith("# Report authoring")
    assert "claim" in skill["body"].lower() and "provenance" in skill["body"].lower()
    assert "name: report-authoring" not in skill["body"]


def test_read_skill_returns_the_body_and_lists_the_catalog():
    tool = make_read_skill_tool()
    assert tool.name == "read_skill"
    # every catalog entry is discoverable from the description alone
    for s in CATALOG:
        assert f"- {s['name']}: " in tool.description
    body = tool.invoke({"name": "report-authoring"})
    assert body.startswith("# Report authoring")


def test_read_skill_unknown_name_is_a_correctable_error():
    tool = make_read_skill_tool()
    out = tool.invoke({"name": "no-such-skill"})
    assert "unknown skill" in out["error"]
    # the error names the valid choices so the model can re-issue
    assert "report-authoring" in out["error"]


# --- loader tolerance (tmp roots) --------------------------------------------


def test_catalog_sorted_and_multi_skill(tmp_path):
    _write_skill(
        tmp_path, "zeta", "---\nname: zeta\ndescription: last\n---\nZ body"
    )
    _write_skill(
        tmp_path, "alpha", "---\nname: alpha\ndescription: first\n---\nA body"
    )
    cat = load_skill_catalog(tmp_path)
    assert [s["name"] for s in cat] == ["alpha", "zeta"]
    tool = make_read_skill_tool(cat)
    assert tool.invoke({"name": "zeta"}) == "Z body"


def test_skill_without_frontmatter_is_skipped_not_fatal(tmp_path):
    _write_skill(tmp_path, "bare", "# No frontmatter here\n")
    _write_skill(tmp_path, "unterminated", "---\nname: x\n")  # never closed
    _write_skill(
        tmp_path, "nodesc", "---\nname: nodesc\n---\nbody"
    )  # missing description
    _write_skill(tmp_path, "ok", "---\nname: ok\ndescription: fine\n---\nbody")
    cat = load_skill_catalog(tmp_path)
    assert [s["name"] for s in cat] == ["ok"]


def test_missing_root_serves_nothing_and_tool_still_answers(tmp_path):
    cat = load_skill_catalog(tmp_path / "ghost")
    assert cat == []
    tool = make_read_skill_tool(cat)
    assert "(none" in tool.description
    out = tool.invoke({"name": "anything"})
    assert "unknown skill" in out["error"]

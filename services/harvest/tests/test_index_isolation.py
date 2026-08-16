"""Regression: deepagents internal scratch dirs must never pollute the bundle.

The harvest backend routes /large_tool_results/ and /conversation_history/ to an
ephemeral StateBackend, but as defense-in-depth okf_core.index_gen must also
ignore those (and dot-prefixed reserved) dirs if any leak to disk — else
finalize would generate index.md inside them and the reindex worker would embed
them.
"""

from pathlib import Path

from okf_core.index_gen import regenerate_indexes


def _concept(root: Path, rel: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: Glue Table\ntitle: T\ndescription: d\ntimestamp: t\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_index_gen_ignores_internal_and_reserved_dirs(tmp_path):
    _concept(tmp_path, "tables/races.md")
    # Simulate leaked deepagents scratch + reserved dirs with stray .md content.
    _concept(tmp_path, "large_tool_results/blob.md")
    _concept(tmp_path, "conversation_history/turn1.md")
    _concept(tmp_path, ".context/spec.md")

    written = regenerate_indexes(tmp_path)
    written_names = {str(p.relative_to(tmp_path)) for p in written}

    # index.md written for the real tree only.
    assert "index.md" in written_names
    assert "tables/index.md" in written_names
    # NOT inside internal/reserved dirs.
    assert "large_tool_results/index.md" not in written_names
    assert "conversation_history/index.md" not in written_names
    assert ".context/index.md" not in written_names

    # The root index lists tables/ but none of the ignored dirs.
    root_index = (tmp_path / "index.md").read_text()
    assert "tables/index.md" in root_index
    assert "large_tool_results" not in root_index
    assert "conversation_history" not in root_index
    assert ".context" not in root_index

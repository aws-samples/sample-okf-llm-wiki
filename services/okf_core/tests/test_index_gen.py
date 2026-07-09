from pathlib import Path

from okf_core.index_gen import regenerate_indexes


def _write(root: Path, rel: str, typ: str, title: str, desc: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {typ}\ntitle: {title}\ndescription: {desc}\ntimestamp: t\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_regenerate_indexes_groups_by_type(tmp_path):
    _write(tmp_path, "tables/races.md", "Glue Table", "Races", "One row per race.")
    _write(
        tmp_path, "tables/circuits.md", "Glue Table", "Circuits", "One row per circuit."
    )
    _write(tmp_path, "datasets/f1.md", "Glue Database", "F1", "The F1 dataset.")

    written = regenerate_indexes(tmp_path)
    # root + tables/ + datasets/
    assert any(p.name == "index.md" and p.parent == tmp_path for p in written)

    tables_index = (tmp_path / "tables" / "index.md").read_text()
    assert "# Glue Table" in tables_index
    assert "[Circuits](circuits.md)" in tables_index
    assert "[Races](races.md)" in tables_index
    # alphabetical within a type group
    assert tables_index.index("Circuits") < tables_index.index("Races")

    root_index = (tmp_path / "index.md").read_text()
    assert "# Subdirectories" in root_index
    assert "[tables](tables/index.md)" in root_index


def test_custom_synthesizer_used_for_multi_entry_dirs(tmp_path):
    _write(tmp_path, "tables/a.md", "Glue Table", "A", "desc a")
    _write(tmp_path, "tables/b.md", "Glue Table", "B", "desc b")

    calls = []

    def synth(rel, pairs):
        calls.append((rel, pairs))
        return "SYNTH SUMMARY"

    regenerate_indexes(tmp_path, synthesize=synth)
    root_index = (tmp_path / "index.md").read_text()
    assert "SYNTH SUMMARY" in root_index
    assert calls  # synthesizer invoked for the multi-entry tables/ dir

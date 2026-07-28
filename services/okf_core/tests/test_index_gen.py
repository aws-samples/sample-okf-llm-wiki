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


def test_stale_index_chain_is_deleted_when_concepts_are_removed(tmp_path):
    # A cross-dataset pair re-run that authors nothing removes the pair's docs
    # but not the previously GENERATED index files above them — regenerate must
    # delete those, or consumers walk a phantom chain (external/index.md ->
    # crm/ -> ...) into a subtree that no longer exists.
    doc = "---\ntype: Reference\ntitle: T\ndescription: d\n---\nbody\n"
    root = tmp_path
    pair = root / "external" / "crm" / "customers"
    pair.mkdir(parents=True)
    (pair / "overview.md").write_text(doc, encoding="utf-8")
    (root / "tables").mkdir()
    (root / "tables" / "orders.md").write_text(doc, encoding="utf-8")

    from okf_core.index_gen import regenerate_indexes

    regenerate_indexes(root)
    assert (root / "external" / "index.md").is_file()
    assert (root / "external" / "crm" / "index.md").is_file()

    # The pair is removed (remove_tree analogue); empty parents linger on disk.
    (pair / "overview.md").unlink()
    (pair / "index.md").unlink()
    regenerate_indexes(root)

    # The whole generated chain above the removed pair is gone...
    assert not (root / "external" / "index.md").exists()
    assert not (root / "external" / "crm" / "index.md").exists()
    # ...and the root index no longer lists the phantom external/ subtree.
    root_index = (root / "index.md").read_text(encoding="utf-8")
    assert "external" not in root_index
    assert "tables" in root_index

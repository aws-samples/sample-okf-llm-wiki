"""okf_core.stats — the deterministic bundle inventory (counts, no judgment)."""

from pathlib import Path

from okf_core.stats import KNOWN_REFERENCE_SUBTYPES, bundle_stats


def _write(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def test_counts_by_concept_type_with_zeros_visible(tmp_path):
    _write(tmp_path, "datasets/f1.md")
    _write(tmp_path, "tables/races.md")
    _write(tmp_path, "tables/results.md")
    _write(tmp_path, "references/usage_guardrails.md")
    _write(tmp_path, "references/joins/races__results.md")
    _write(tmp_path, "references/enums/status.md")
    stats = bundle_stats(tmp_path)
    assert stats["datasets"] == 1 and stats["tables"] == 2
    assert stats["references"]["usage_guardrails"] == 1
    assert stats["references"]["joins"] == 1
    assert stats["references"]["enums"] == 1
    # Absence is VISIBLE: every known subtype appears, zero or not.
    for k in KNOWN_REFERENCE_SUBTYPES:
        assert k in stats["references"]
    assert stats["references"]["named_sets"] == 0
    assert stats["total_docs"] == 6
    assert stats["external"] == 0 and stats["other_docs"] == 0


def test_generated_and_internal_files_are_excluded(tmp_path):
    _write(tmp_path, "tables/races.md")
    _write(tmp_path, "index.md")
    _write(tmp_path, "tables/index.md")
    _write(tmp_path, "log.md")
    _write(tmp_path, ".metadata/tables/races.md")
    _write(tmp_path, ".harvest/review/report-1.md")
    _write(tmp_path, ".context/notes.md")
    stats = bundle_stats(tmp_path)
    assert stats["total_docs"] == 1 and stats["tables"] == 1


def test_unknown_reference_subdirs_and_loose_docs_still_count(tmp_path):
    _write(tmp_path, "references/lineage/flow.md")
    _write(tmp_path, "references/notes.md")
    stats = bundle_stats(tmp_path)
    assert stats["references"]["lineage"] == 1  # unknown is never invisible
    assert stats["references"]["other"] == 1  # loose doc under references/


def test_snapshot_tables_ride_along_for_coverage(tmp_path):
    _write(tmp_path, "tables/races.md")
    _write(tmp_path, ".metadata/tables/races.md")
    _write(tmp_path, ".metadata/tables/results.md")
    stats = bundle_stats(tmp_path)
    assert stats["snapshot_tables"] == 2 and stats["tables"] == 1
    # No snapshot on disk -> nothing to compare against, not zero.
    assert bundle_stats(tmp_path / "empty")["snapshot_tables"] is None


def test_external_and_other_top_level_docs(tmp_path):
    _write(tmp_path, "external/dom/ds/tables/races.md")
    _write(tmp_path, "notes.md")
    stats = bundle_stats(tmp_path)
    assert stats["external"] == 1
    assert stats["other_docs"] == 1


def test_missing_root_is_all_zeros(tmp_path):
    stats = bundle_stats(tmp_path / "nowhere")
    assert stats["total_docs"] == 0
    assert stats["references"]["named_sets"] == 0

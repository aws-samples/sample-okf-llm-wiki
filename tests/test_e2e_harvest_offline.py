"""Offline end-to-end test of the harvest pipeline WITHOUT the LLM or AWS.

The real harvest drives a deepagents agent (Claude on Bedrock) to author docs;
we can't run that here (no model, no creds). But every other moving part is pure
Python and unit-testable, so this test simulates the agent's authoring by
driving the SAME machinery the middleware/runner use:

  Glue/Athena source (fakes)  ->  read metadata + sample
  OKFGuardEngine              ->  guard + normalize each authored doc (as the
                                  middleware would) before it lands on disk
  LinkGraph                   ->  impact analysis (get_backlinks)
  finalize_bundle             ->  index.md regen + commit marker

It then asserts the produced bundle is structurally a valid OKF bundle,
byte-compatible in shape with the golden F1 bundle (frontmatter keys, ARNs,
index grouping, commit marker). This is the strongest E2E guarantee available
without a live model.
"""

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "services" / "harvest" / "tests"))

from harvest.finalize import finalize_bundle  # noqa: E402
from harvest.glue_source import GlueAthenaSource  # noqa: E402
from harvest.guard_engine import OKFGuardEngine  # noqa: E402
from harvest.metadata_export import METADATA_DIR, export_metadata  # noqa: E402
from okf_core.document import OKFDocument  # noqa: E402
from okf_core.index_gen import regenerate_indexes  # noqa: E402
from okf_core.link_graph import LinkGraph  # noqa: E402
from okf_core.paths import concept_id_to_path  # noqa: E402

from fakes import FakeAthena, f1_like_glue  # noqa: E402


def _author(
    engine: OKFGuardEngine, root: Path, concept_id: str, frontmatter: dict, body: str
):
    """Simulate the agent's write_file, gated by the guard (as middleware does)."""
    doc = OKFDocument(frontmatter=frontmatter, body=body)
    path = concept_id_to_path(root, tuple(concept_id.split("/")))
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    decision = engine.guard_write_file(doc.serialize(), existing)
    assert decision.allow, f"guard rejected {concept_id}: {decision.message}"
    path.parent.mkdir(parents=True, exist_ok=True)
    # The middleware writes decision.new_content (normalized) when present.
    path.write_text(decision.new_content or doc.serialize(), encoding="utf-8")
    return path


def test_offline_harvest_produces_valid_bundle(tmp_path):
    root = tmp_path / "sales" / "na_mi_formula_1_curated"
    root.mkdir(parents=True)

    source = GlueAthenaSource(
        database="na_mi_formula_1_curated",
        glue=f1_like_glue(),
        athena=FakeAthena(rows=[{"raceid": "1", "year": "2009"}]),
        region="us-east-1",
        account_id="123456789012",
    )
    link_graph = LinkGraph(root)
    engine = OKFGuardEngine(link_graph)

    # 0) Snapshot Glue metadata to the read-only .metadata/ dir, as the runner
    #    does before the agent runs. The agent would read these with read_file/grep.
    snap = export_metadata(source, root)
    assert snap["table_count"] == 2
    assert (root / METADATA_DIR / "index.md").is_file()
    assert (root / METADATA_DIR / "columns.tsv").is_file()

    # Drive the source exactly as the agent's tools would.
    concepts = source.list_concepts()
    assert {c.id_str for c in concepts} >= {
        "datasets/na_mi_formula_1_curated",
        "tables/races",
        "tables/results",
    }

    # 1) Author each table doc from real Glue metadata (guard-gated).
    for name in source.table_names():
        ref = source.find(("tables", name))
        meta = source.read_concept(ref)
        schema_rows = "\n".join(
            f"| `{f['name']}` | {f['type']} | {f['comment']} |"
            for f in meta["flat_schema"]
        )
        body = (
            f"# Overview\nThe `{name}` table. One row per {name} record.\n\n"  # nosec B608 - markdown fixture text for a test doc, not a SQL query; the SELECT inside a ```sql``` code fence is illustrative content, never executed.
            f"# Schema\n| Column | Type | Description |\n|---|---|---|\n{schema_rows}\n\n"
            f"# Common query patterns\n```sql\nSELECT * FROM {source.database}.{name} LIMIT 10\n```\n\n"
            f"# Citations\n- {meta['resource']}\n"
        )
        # results links to races (a real FK) -> exercises the link graph.
        if name == "results":
            body = body.replace(
                "# Common query patterns",
                "# Joins\n- join to [races](races.md) on `raceid`.\n\n# Common query patterns",
            )
        _author(
            engine,
            root,
            f"tables/{name}",
            {
                "type": "Glue Table",
                "resource": meta["resource"],
                "title": name.title(),
                "description": f"One row per {name} record.",
                "tags": ["formula 1", name],
            },
            body,
        )

    # 2) Author the dataset overview (links to the tables).
    db_meta = source.read_concept(source.find(("datasets", "na_mi_formula_1_curated")))
    _author(
        engine,
        root,
        "datasets/na_mi_formula_1_curated",
        {
            "type": "Glue Database",
            "resource": db_meta["resource"],
            "title": "Formula 1 (curated)",
            "description": "Curated F1 relational dataset.",
            "tags": ["formula 1", "glue database"],
        },
        "# Overview\nThe F1 curated database.\n\n# Tables\n"
        "- [races](../tables/races.md)\n- [results](../tables/results.md)\n\n"
        f"# Citations\n- {db_meta['resource']}\n",
    )

    # 3) Impact analysis: who links to races? -> results (the agent's key tool).
    backlinks = link_graph.get_backlinks("tables/races")
    ids = {b["id"] for b in backlinks}
    assert "tables/results" in ids
    assert "datasets/na_mi_formula_1_curated" in ids
    results_backlink = next(b for b in backlinks if b["id"] == "tables/results")
    assert results_backlink["heading"] == "Joins"  # knows WHERE to edit

    # 4) Finalize: regenerate indexes + write the commit marker last.
    state = finalize_bundle(
        root,
        data_domain="sales",
        dataset="na_mi_formula_1_curated",
        tables=source.table_names(),
        timestamp="2026-07-01T00:00:00Z",
        table_versions={"races": "1", "results": "1"},
    )
    assert state["status"] == "complete"

    # --- Assert the produced bundle is a valid OKF bundle -------------------

    # Commit marker present and last-written.
    marker = json.loads((root / ".harvest" / "state.json").read_text())
    assert marker["status"] == "complete"
    assert sorted(marker["tables"]) == ["races", "results"]

    # Every PUBLISHED concept doc parses, validates, and carries a normalized
    # timestamp + canonical key order (type first) — the middleware's
    # normalization. Dot-prefixed dirs (.metadata/, .harvest/) are not published,
    # so they're skipped here exactly as reindex/consumption skip them.
    for md in root.rglob("*.md"):
        if md.name == "index.md":
            continue
        if any(seg.startswith(".") for seg in md.relative_to(root).parts):
            continue
        doc = OKFDocument.parse(md.read_text(encoding="utf-8"))
        doc.validate()
        assert list(doc.frontmatter)[0] == "type"
        assert doc.frontmatter.get("timestamp")

    # The .metadata/ snapshot survives finalize but is NOT published: index
    # regeneration never descends into it, and it carries no OKF concepts.
    assert (root / METADATA_DIR / "tables" / "races.md").is_file()
    assert not (root / METADATA_DIR / "index.md").read_text().startswith("---")
    indexed = set(regenerate_indexes(root))
    assert not any(METADATA_DIR in p.relative_to(root).parts for p in indexed)

    # Index files were generated and group by type (as in the golden bundle).
    tables_index = (root / "tables" / "index.md").read_text()
    assert "# Glue Table" in tables_index
    assert "[Races](races.md)" in tables_index
    root_index = (root / "index.md").read_text()
    assert "# Subdirectories" in root_index
    assert "[tables](tables/index.md)" in root_index
    # The snapshot dir never appears as a bundle subdirectory entry.
    assert METADATA_DIR not in root_index


def test_offline_harvest_guard_blocks_bad_write(tmp_path):
    """The guard rejects a schema-shrinking rewrite (augmentation guard)."""
    root = tmp_path / "d" / "ds"
    root.mkdir(parents=True)
    engine = OKFGuardEngine(LinkGraph(root))

    fm = {"type": "Glue Table", "title": "Races", "description": "d"}
    _author(
        engine,
        root,
        "tables/races",
        fm,
        "# Schema\n| `a` | int |\n| `b` | int |\n| `c` | int |\n",
    )

    # A rewrite that drops columns must be rejected (no disk change).
    path = root / "tables" / "races.md"
    before = path.read_text()
    shrunk = OKFDocument(frontmatter=fm, body="# Schema\n| `a` | int |\n").serialize()
    decision = engine.guard_write_file(shrunk, before)
    assert not decision.allow
    assert "`b`" in decision.message

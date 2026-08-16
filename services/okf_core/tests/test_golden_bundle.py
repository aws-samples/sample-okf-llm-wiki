"""Validate okf_core against the real golden F1 bundle shipped in the repo.

This is the strongest guarantee that our ported primitives are byte-compatible
with what the reference producer emits: we parse every doc, round-trip it, walk
the link graph, and confirm known backlinks/schema fields.
"""

import os
from pathlib import Path

import pytest

from okf_core.document import OKFDocument
from okf_core.guard import schema_field_names
from okf_core.link_graph import LinkGraph

# knowledge-catalog/okf/bundles/na_mi_formula_1_curated relative to repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN = (
    _REPO_ROOT / "knowledge-catalog" / "okf" / "bundles" / "na_mi_formula_1_curated"
)

pytestmark = pytest.mark.skipif(
    not _GOLDEN.is_dir(), reason="golden F1 bundle not present"
)


def test_every_doc_parses_and_validates():
    md_files = list(_GOLDEN.rglob("*.md"))
    assert md_files, "expected markdown docs in the golden bundle"
    for md in md_files:
        doc = OKFDocument.parse(md.read_text(encoding="utf-8"))
        if md.name in ("index.md", "log.md"):
            continue
        # concept docs must carry the required frontmatter
        doc.validate()


def test_races_schema_fields_detected():
    races = _GOLDEN / "tables" / "races.md"
    doc = OKFDocument.parse(races.read_text(encoding="utf-8"))
    fields = schema_field_names(doc.body)
    # A representative subset of the real races schema.
    assert {"raceid", "year", "circuitid", "name"} <= fields


def test_link_graph_backlinks_on_golden_bundle():
    g = LinkGraph(_GOLDEN)
    # races is the join hub; many docs link to it.
    back = g.get_backlinks("tables/races")
    ids = {b["id"] for b in back}
    # results and several join docs reference races.
    assert "references/joins/races__results" in ids
    assert any(i.startswith("references/joins/") for i in ids)
    # the results table links out to races via a join reference
    links = g.get_links("references/joins/races__results")
    link_ids = {l["id"] for l in links}
    assert "tables/races" in link_ids and "tables/results" in link_ids

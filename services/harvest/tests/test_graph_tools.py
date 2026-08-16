"""The cluster_concepts graph tool — the review fan-out's grouping query.

The clustering algorithm itself is tested in okf_core (test_links_and_graph);
these cover the tool surface the agent sees: all three graph tools exposed,
whole-bundle coverage in ≤5-doc clusters, and the hard cap on max_size.
"""

from pathlib import Path

from harvest.graph_tools import MAX_CLUSTER_SIZE, make_graph_tools
from okf_core.link_graph import LinkGraph


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: Glue Table\ntitle: {rel}\ndescription: d\ntimestamp: t\n---\n\n{body}",
        encoding="utf-8",
    )


def _tools(root: Path) -> dict:
    return {t.name: t for t in make_graph_tools(LinkGraph(root))}


def test_exposes_three_graph_tools(tmp_path):
    assert set(_tools(tmp_path)) == {"get_backlinks", "get_links", "cluster_concepts"}


def test_cluster_concepts_covers_bundle_in_capped_clusters(tmp_path):
    _write(tmp_path, "tables/races.md", "see [j](../references/joins/j.md)\n")
    _write(tmp_path, "references/joins/j.md", "[races](../../tables/races.md)\n")
    for i in range(6):
        _write(tmp_path, f"tables/t{i}.md", "no links\n")
    clusters = _tools(tmp_path)["cluster_concepts"].invoke({})
    flat = [c for cluster in clusters for c in cluster]
    assert len(flat) == len(set(flat)) == 8  # every doc, exactly once
    assert all(len(cluster) <= MAX_CLUSTER_SIZE for cluster in clusters)
    # The linked pair reviews together.
    assert any(
        {"tables/races", "references/joins/j"} <= set(c) for c in clusters
    )


def test_cluster_concepts_clamps_oversized_max_size(tmp_path):
    # A reviewer given a huge cluster skims instead of verifying — requests
    # above the cap are clamped, not honored.
    for i in range(12):
        _write(tmp_path, f"tables/t{i}.md", "no links\n")
    clusters = _tools(tmp_path)["cluster_concepts"].invoke({"max_size": 50})
    assert all(len(cluster) <= MAX_CLUSTER_SIZE for cluster in clusters)
    assert sum(len(cluster) for cluster in clusters) == 12

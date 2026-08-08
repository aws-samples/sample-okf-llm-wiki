from pathlib import Path

from okf_core.links import extract_links, extract_links_with_headings
from okf_core.link_graph import LinkGraph


def _write(root: Path, rel: str, frontmatter_title: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f"---\ntype: Glue Table\ntitle: {frontmatter_title}\n"
        f"description: d\ntimestamp: t\n---\n\n"
    )
    p.write_text(fm + body, encoding="utf-8")


def test_extract_links_resolves_relative(tmp_path):
    body = "See [circuits](circuits.md) and [dataset](../datasets/f1.md).\n"
    doc_dir = tmp_path / "tables"
    doc_dir.mkdir(parents=True)
    links = extract_links(body, doc_dir, tmp_path)
    assert "tables/circuits" in links
    assert "datasets/f1" in links


def test_extract_links_ignores_external_and_absolute(tmp_path):
    body = "[ext](https://x.com/a.md) [abs](/foo/bar.md) [ok](sib.md)\n"
    doc_dir = tmp_path / "tables"
    doc_dir.mkdir(parents=True)
    links = extract_links(body, doc_dir, tmp_path)
    assert links == ["tables/sib"]


def test_extract_links_with_headings(tmp_path):
    body = "# Overview\ntext\n# Joins\n- [races](races.md) join\n"
    doc_dir = tmp_path / "tables"
    doc_dir.mkdir(parents=True)
    links = extract_links_with_headings(body, doc_dir, tmp_path)
    assert len(links) == 1
    assert links[0].target == "tables/races"
    assert links[0].heading == "Joins"


def test_link_graph_backlinks_and_dirty(tmp_path):
    _write(tmp_path, "tables/races.md", "Races", "# Overview\nrace hub.\n")
    _write(
        tmp_path,
        "tables/results.md",
        "Results",
        "# Joins\nJoin to [races](races.md).\n",
    )
    g = LinkGraph(tmp_path)
    # get_links: results -> races
    links = g.get_links("tables/results")
    assert any(l["id"] == "tables/races" for l in links)
    assert links[0]["heading"] == "Joins"
    # get_backlinks: who links to races? -> results, under the Joins heading
    back = g.get_backlinks("tables/races")
    assert len(back) == 1
    assert back[0]["id"] == "tables/results"
    assert back[0]["title"] == "Results"
    assert back[0]["heading"] == "Joins"


def test_link_graph_rebuilds_on_dirty(tmp_path):
    _write(tmp_path, "tables/a.md", "A", "no links\n")
    g = LinkGraph(tmp_path)
    assert g.get_backlinks("tables/a") == []
    # Add a referencing doc, mark dirty, and confirm the read rebuilds.
    _write(tmp_path, "tables/b.md", "B", "link to [a](a.md)\n")
    g.mark_dirty()
    back = g.get_backlinks("tables/a")
    assert len(back) == 1 and back[0]["id"] == "tables/b"


def test_link_graph_ignores_out_of_subtree_links(tmp_path):
    # A link that resolves outside the root is dropped (no phantom node).
    _write(tmp_path, "tables/a.md", "A", "[out](../../other/x.md)\n")
    g = LinkGraph(tmp_path)
    assert g.get_links("tables/a") == []


def test_link_graph_excludes_dot_dirs(tmp_path):
    # .metadata/.context/.harvest hold authoring inputs, not bundle concepts —
    # they must never become graph nodes (nor land in review clusters).
    _write(tmp_path, "tables/a.md", "A", "no links\n")
    _write(tmp_path, ".metadata/tables/a.md", "A sheet", "raw metadata\n")
    _write(tmp_path, ".context/notes.md", "Notes", "uploaded doc\n")
    g = LinkGraph(tmp_path)
    assert set(g.cluster()[0]) == {"tables/a"}
    assert ".metadata/tables/a" not in g.graph
    assert ".context/notes" not in g.graph


def test_cluster_groups_linked_docs_and_covers_every_doc_once(tmp_path):
    # A hub table with its spoke references clusters together; every doc lands
    # in exactly one cluster and none exceeds max_size.
    _write(tmp_path, "tables/races.md", "Races", "see [join](../references/joins/j.md)\n")
    _write(tmp_path, "references/joins/j.md", "J", "[races](../../tables/races.md)\n")
    _write(tmp_path, "references/enums/status.md", "S", "[races](../../tables/races.md)\n")
    _write(tmp_path, "tables/lonely.md", "Lonely", "no links\n")
    _write(tmp_path, "index.md", "Index", "reserved\n")
    clusters = LinkGraph(tmp_path).cluster(max_size=5)
    flat = [c for cluster in clusters for c in cluster]
    assert sorted(flat) == [
        "references/enums/status",
        "references/joins/j",
        "tables/lonely",
        "tables/races",
    ]  # each doc exactly once; index.md excluded
    # The linked trio stays together in one cluster.
    trio = {"tables/races", "references/joins/j", "references/enums/status"}
    assert any(trio <= set(cluster) for cluster in clusters)
    assert all(len(cluster) <= 5 for cluster in clusters)


def test_cluster_respects_max_size_on_large_components(tmp_path):
    # One hub linked by 9 spokes: a single component larger than max_size must
    # split into clusters that each stay within the cap.
    spokes = [f"references/glossary/t{i}.md" for i in range(9)]
    for s in spokes:
        _write(tmp_path, s, s, "[hub](../../tables/hub.md)\n")
    _write(tmp_path, "tables/hub.md", "Hub", "no links\n")
    clusters = LinkGraph(tmp_path).cluster(max_size=5)
    assert sorted(c for cluster in clusters for c in cluster) == sorted(
        ["tables/hub"] + [s[:-3] for s in spokes]
    )
    assert all(1 <= len(cluster) <= 5 for cluster in clusters)
    # 10 docs at ≤5 per cluster packs into exactly 2 clusters.
    assert len(clusters) == 2


def test_cluster_packs_singletons_together(tmp_path):
    # Unlinked docs don't each cost their own cluster — they pack up to max_size.
    for i in range(7):
        _write(tmp_path, f"tables/t{i}.md", f"T{i}", "no links\n")
    clusters = LinkGraph(tmp_path).cluster(max_size=5)
    assert len(clusters) == 2
    assert sorted(len(cluster) for cluster in clusters) == [2, 5]


def test_cluster_is_deterministic(tmp_path):
    _write(tmp_path, "tables/a.md", "A", "[b](b.md)\n")
    _write(tmp_path, "tables/b.md", "B", "no links\n")
    _write(tmp_path, "tables/c.md", "C", "no links\n")
    g = LinkGraph(tmp_path)
    first = g.cluster(max_size=2)
    g.mark_dirty()
    assert g.cluster(max_size=2) == first


def test_cluster_exclude_removes_docs_before_clusters_form(tmp_path):
    # An excluded hub must not just vanish from the OUTPUT — it must not
    # participate in cluster FORMATION either. The guardrails doc links to
    # every table (highest degree), so if it merely got filtered afterwards it
    # would still have seeded a cluster of unrelated tables and stolen them
    # from their own spokes.
    _write(
        tmp_path,
        "references/usage_guardrails.md",
        "Guardrails",
        "[a](../tables/a.md) [b](../tables/b.md) [c](../tables/c.md)\n",
    )
    _write(tmp_path, "tables/a.md", "A", "[ea](../references/enums/ea.md)\n")
    _write(tmp_path, "references/enums/ea.md", "EA", "[a](../../tables/a.md)\n")
    _write(tmp_path, "tables/b.md", "B", "[eb](../references/enums/eb.md)\n")
    _write(tmp_path, "references/enums/eb.md", "EB", "[b](../../tables/b.md)\n")
    _write(tmp_path, "tables/c.md", "C", "no links\n")

    def owned(concept_id):
        return concept_id == "references/usage_guardrails"

    clusters = LinkGraph(tmp_path).cluster(max_size=2, exclude=owned)
    flat = [c for cluster in clusters for c in cluster]
    assert "references/usage_guardrails" not in flat
    assert sorted(flat) == [
        "references/enums/ea",
        "references/enums/eb",
        "tables/a",
        "tables/b",
        "tables/c",
    ]
    # With the hub gone, each table clusters with ITS OWN enum — the pairing
    # the hub would have broken by grabbing the tables as its neighbors.
    assert ["tables/a", "references/enums/ea"] in [sorted(c, reverse=True) for c in clusters]
    assert ["tables/b", "references/enums/eb"] in [sorted(c, reverse=True) for c in clusters]
    # Backlinks are untouched — exclusion is a review-partition concern only.
    g = LinkGraph(tmp_path)
    assert any(
        b["id"] == "references/usage_guardrails" for b in g.get_backlinks("tables/a")
    )


def test_doc_stem_matching_a_scratch_dir_name_stays_in_the_graph(tmp_path):
    # The reserved rule names DIRECTORIES (deepagents scratch, dot-dirs) — a
    # table legitimately named `conversation_history` must not silently
    # vanish from the graph (no backlinks, no review cluster) while lint
    # still demands its doc. Parents-only, exactly as lint applies it.
    _write(tmp_path, "tables/conversation_history.md", "Conv", "History.\n")
    _write(
        tmp_path,
        "tables/events.md",
        "Events",
        "See [conv](conversation_history.md).\n",
    )
    _write(tmp_path, "conversation_history/scratch.md", "S", "runtime leak\n")
    g = LinkGraph(tmp_path)
    g.rebuild()
    assert "tables/conversation_history" in g.graph
    assert "conversation_history/scratch" not in g.graph
    assert [b["id"] for b in g.get_backlinks("tables/conversation_history")] == [
        "tables/events"
    ]

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

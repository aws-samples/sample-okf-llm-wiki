"""okf_core.graph_json — the ONE graph builder both producers share.

The Control API serves ``{nodes, edges}`` either from the precomputed
``.harvest/graph.json`` (written by finalize/repromote) or by building it
live — these tests pin the builder's semantics and the local collector's
"what is a concept" rules, which must match the S3 side
(``okf_aws.parse_bundle_key``) exactly.
"""

from okf_core.graph_json import build_graph_json, collect_bundle_files


def test_build_graph_json_nodes_and_edges():
    files = {
        "tables/orders": "---\ntitle: orders\ntype: Glue Table\n---\nSee [customers](customers.md).",
        "tables/customers": "---\ntitle: customers\ntype: Glue Table\n---\nrefs [orders](orders.md) and [gone](ghost.md) and [ext](http://x/y.md).",
    }
    g = build_graph_json(files)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert set(nodes) == {"tables/orders", "tables/customers"}
    assert nodes["tables/orders"]["title"] == "orders"
    assert nodes["tables/orders"]["type"] == "Glue Table"

    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("tables/orders", "tables/customers") in edges
    assert ("tables/customers", "tables/orders") in edges
    # Dangling target (ghost) and external link are dropped.
    assert all(t in nodes for _, t in edges)


def test_build_graph_json_tolerates_a_malformed_doc():
    # A doc that fails frontmatter parsing still becomes a node (Unknown type,
    # id as title) — the graph is a view, not a validator.
    g = build_graph_json({"tables/bad": "not: [valid yaml\n---\nbody"})
    assert g["nodes"] == [{"id": "tables/bad", "title": "tables/bad", "type": "Unknown"}]
    assert g["edges"] == []


def test_collect_bundle_files_applies_the_shared_concept_rules(tmp_path):
    # Concepts (must be collected) …
    concept = "---\ntitle: t\ntype: Glue Table\n---\nbody\n"
    for rel in ("tables/orders.md", "datasets/orders.md", "external/d/ds/joins/x.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(concept, encoding="utf-8")
    # … and everything the S3 side (parse_bundle_key) also refuses:
    for rel in (
        "index.md",                      # reserved
        "tables/index.md",               # reserved
        "log.md",                        # reserved
        ".harvest/notes.md",             # dot dir (run state)
        ".metadata/tables/orders.md",    # dot dir (snapshot)
        "large_tool_results/leak.md",    # deepagents scratch leak
        "tables/.hidden.md",             # dot stem
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    files = collect_bundle_files(tmp_path)
    assert set(files) == {"tables/orders", "datasets/orders", "external/d/ds/joins/x"}
    assert files["tables/orders"] == concept


def test_collect_bundle_files_missing_root_is_empty():
    assert collect_bundle_files("/nonexistent/nowhere") == {}

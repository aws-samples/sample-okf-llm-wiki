"""Unit tests for the consumption MCP tool logic (no live AWS)."""

from __future__ import annotations

import pytest

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

from .conftest import BUNDLE_BUCKET, DATASET, DOMAIN, REGISTRY_TABLE
from .fakes import FakeBedrock, FakeS3Vectors


# -- list_domains -----------------------------------------------------------


def test_list_domains_returns_only_domain_items(tools):
    domains = tools.list_domains()
    pairs = sorted((d["data_domain"], d["dataset"]) for d in domains)
    assert pairs == [("ops", "logs"), ("sales", "f1")]
    # The HARVEST#... status item must not leak in.
    assert all(d["data_domain"] for d in domains)


# -- list_directory ---------------------------------------------------------


def test_list_directory_returns_index_content(tools):
    out = tools.list_directory(DOMAIN, DATASET, "tables")
    assert out["content"] is not None
    assert "# Tables" in out["content"]
    assert out["index_key"] == f"okf/{DOMAIN}/{DATASET}/tables/index.md"


def test_list_directory_falls_back_to_prefix_listing(tools):
    # references/ has no index.md -> fall back to listing children.
    out = tools.list_directory(DOMAIN, DATASET, "references")
    assert out["content"] is None
    names = {(e["type"], e["name"]) for e in out["entries"]}
    # joins/ is a child directory.
    assert ("dir", "joins") in names


def test_list_directory_hides_dot_and_reserved_entries(tools):
    # At the dataset root, .harvest/.context dirs and index.md must be hidden,
    # but datasets/, tables/, references/ dirs must show.
    out = tools.list_directory(DOMAIN, DATASET, "")
    # Root has index.md so content is returned; force fallback by deleting it.
    tools.s3.delete_object(Bucket=BUNDLE_BUCKET, Key=f"okf/{DOMAIN}/{DATASET}/index.md")
    out = tools.list_directory(DOMAIN, DATASET, "")
    names = {e["name"] for e in out["entries"]}
    assert ".harvest" not in names
    assert ".context" not in names
    assert "index" not in names
    assert {"tables", "datasets", "references"} <= names


def test_list_directory_rejects_traversal(tools):
    with pytest.raises(ValueError):
        tools.list_directory(DOMAIN, DATASET, "../../etc")


# -- read_page --------------------------------------------------------------


def test_read_page_full(tools):
    out = tools.read_page("tables/races", DOMAIN, DATASET)
    assert out["s3_key"] == f"okf/{DOMAIN}/{DATASET}/tables/races.md"
    assert "# Overview" in out["content"]
    assert out["offset"] == 0
    assert out["returned_lines"] == out["total_lines"]


def test_read_page_pagination_by_lines(tools):
    full = tools.read_page("tables/races", DOMAIN, DATASET)["content"]
    all_lines = full.splitlines()

    page = tools.read_page("tables/races", DOMAIN, DATASET, offset=5, limit=3)
    assert page["content"].splitlines() == all_lines[5:8]
    assert page["returned_lines"] == 3
    assert page["offset"] == 5
    assert page["limit"] == 3
    assert page["total_lines"] == len(all_lines)


def test_read_page_offset_past_end_returns_empty(tools):
    page = tools.read_page("tables/races", DOMAIN, DATASET, offset=10_000, limit=5)
    assert page["content"] == ""
    assert page["returned_lines"] == 0


def test_read_page_limit_none_from_offset(tools):
    full_lines = tools.read_page("tables/races", DOMAIN, DATASET)[
        "content"
    ].splitlines()
    page = tools.read_page("tables/races", DOMAIN, DATASET, offset=3)
    assert page["content"].splitlines() == full_lines[3:]


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "../races",
        "tables/../../../secret",
        "..",
    ],
)
def test_read_page_rejects_path_traversal(tools, bad_id):
    with pytest.raises(ValueError):
        tools.read_page(bad_id, DOMAIN, DATASET)


def test_read_page_negative_offset_rejected(tools):
    with pytest.raises(ValueError):
        tools.read_page("tables/races", DOMAIN, DATASET, offset=-1)


# -- get_backlinks ----------------------------------------------------------


def test_get_backlinks_finds_referencing_docs(tools):
    backlinks = tools.get_backlinks("tables/races", DOMAIN, DATASET)
    by_id = {b["id"]: b for b in backlinks}
    # results.md and the join reference both link to races; index/tables-index
    # also link to races.
    assert "tables/results" in by_id
    assert "references/joins/races__results" in by_id
    # heading of the link in results.md is "Joins".
    assert by_id["tables/results"]["heading"] == "Joins"
    # title comes from frontmatter.
    assert by_id["tables/results"]["title"] == "Results"


def test_get_backlinks_ignores_dot_and_reserved(tools):
    # The .context/source.md doc is dot-prefixed and must never be scanned; add
    # a doc there that links to races and confirm it is not returned.
    tools.s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.context/evil.md",
        Body=b"[races](../tables/races.md)",
    )
    backlinks = tools.get_backlinks("tables/races", DOMAIN, DATASET)
    ids = {b["id"] for b in backlinks}
    assert not any(bid.startswith(".context") for bid in ids)


def test_get_backlinks_empty_for_unreferenced(tools):
    # tables/results is only linked from the join ref; datasets/f1 is linked by
    # nobody.
    assert tools.get_backlinks("datasets/f1", DOMAIN, DATASET) == []


# -- glob -------------------------------------------------------------------


def test_glob_direct_children_single_star(tools):
    out = tools.glob("tables/*", DOMAIN, DATASET)
    ids = {e["concept_id"] for e in out}
    # single "*" stays within a segment: matches the two table docs...
    assert ids == {"tables/races", "tables/results"}
    # ...and does NOT reach the nested references/joins concept.
    assert all(i["data_domain"] == DOMAIN and i["dataset"] == DATASET for i in out)


def test_glob_single_star_does_not_cross_slash(tools):
    # tables/index.md is reserved (excluded); references/joins/... is nested, so
    # a top-level "*" matches nothing (every visible concept is under a dir).
    assert tools.glob("*", DOMAIN, DATASET) == []


def test_glob_double_star_crosses_directories(tools):
    ids = {e["concept_id"] for e in tools.glob("**", DOMAIN, DATASET)}
    assert ids == {
        "datasets/f1",
        "tables/races",
        "tables/results",
        "references/joins/races__results",
    }


def test_glob_double_star_prefix_matches_leaf(tools):
    # "**/races" should match the deep concept regardless of directory depth.
    ids = {e["concept_id"] for e in tools.glob("**/races", DOMAIN, DATASET)}
    assert ids == {"tables/races"}


def test_glob_leaf_substring(tools):
    ids = {e["concept_id"] for e in tools.glob("**/*result*", DOMAIN, DATASET)}
    assert ids == {"tables/results", "references/joins/races__results"}


def test_glob_hides_reserved_and_dot(tools):
    # No pattern should ever surface index.md (reserved) or .harvest/.context.
    for pat in ("**", "**/index", "**/*", ".*/**"):
        ids = {e["concept_id"] for e in tools.glob(pat, DOMAIN, DATASET)}
        assert not any(i.endswith("index") for i in ids)
        assert not any(i.startswith(".") for i in ids)


def test_glob_tolerates_wrapping_slash_and_md_suffix(tools):
    ids = {e["concept_id"] for e in tools.glob("/tables/races.md", DOMAIN, DATASET)}
    assert ids == {"tables/races"}


def test_glob_sorted(tools):
    out = [e["concept_id"] for e in tools.glob("**", DOMAIN, DATASET)]
    assert out == sorted(out)


# -- grep -------------------------------------------------------------------


def test_grep_matches_content_lines(tools):
    out = tools.grep("Races table", DOMAIN, DATASET)
    assert out["match_count"] == 1
    m = out["matches"][0]
    assert m["concept_id"] == "tables/races"
    assert m["line"] == "Races table."
    assert m["line_number"] > 0
    assert out["truncated"] is False


def test_grep_is_case_insensitive_by_default(tools):
    assert tools.grep("races table", DOMAIN, DATASET)["match_count"] == 1
    # case-sensitive: the literal lowercase "races table" is not present.
    assert (
        tools.grep("races table", DOMAIN, DATASET, ignore_case=False)["match_count"]
        == 0
    )


def test_grep_regex_across_multiple_concepts(tools):
    # "Overview" heading appears in several docs.
    out = tools.grep(r"^#\s*Overview", DOMAIN, DATASET)
    cids = {m["concept_id"] for m in out["matches"]}
    assert {"tables/races", "tables/results", "datasets/f1"} <= cids


def test_grep_skips_dot_and_reserved(tools):
    # A secret in .context and content in the reserved index.md must never match.
    tools.s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.context/secret.md",
        Body=b"UNIQUE_SECRET_TOKEN here",
    )
    out = tools.grep("UNIQUE_SECRET_TOKEN", DOMAIN, DATASET)
    assert out["match_count"] == 0


def test_grep_truncates_at_max_results(tools):
    out = tools.grep("line", DOMAIN, DATASET, max_results=3)
    assert out["match_count"] == 3
    assert out["truncated"] is True


def test_grep_invalid_regex_raises(tools):
    with pytest.raises(ValueError):
        tools.grep("[unterminated", DOMAIN, DATASET)


def test_grep_zero_max_results_rejected(tools):
    with pytest.raises(ValueError):
        tools.grep("x", DOMAIN, DATASET, max_results=0)


# -- semantic_search --------------------------------------------------------


def test_semantic_search_builds_filter_and_maps_metadata(aws, config):
    hits = [
        {
            "key": "sales/f1/tables/races",
            "distance": 0.12,
            "metadata": {
                "title": "Races",
                "description": "race rows",
                "s3_key": "okf/sales/f1/tables/races.md",
            },
        }
    ]
    s3v = FakeS3Vectors(hits=hits)
    br = FakeBedrock()
    tools = ConsumptionTools(
        s3=aws["s3"],
        s3vectors=s3v,
        bedrock_runtime=br,
        ddb=aws["table"],
        config=config,
    )

    results = tools.semantic_search(
        "which races happened in 2009",
        data_domain="sales",
        dataset="f1",
        type="Glue Table",
        tags=["racing", "motorsport"],
        top_k=7,
    )

    # embedded the query with Titan.
    assert br.calls, "bedrock invoke_model was not called"

    # built the right query.
    q = s3v.queries[0]
    assert q["vectorBucketName"] == config.vector_bucket
    assert q["indexName"] == config.vector_index
    assert q["topK"] == 7
    assert q["returnMetadata"] is True
    assert q["returnDistance"] is True
    # $and filter over the hierarchy knobs.
    clauses = q["filter"]["$and"]
    assert {"data_domain": {"$eq": "sales"}} in clauses
    assert {"dataset": {"$eq": "f1"}} in clauses
    assert {"type": {"$eq": "Glue Table"}} in clauses
    assert {"tags": {"$in": ["racing", "motorsport"]}} in clauses

    # mapped metadata -> concept_id(=key), title, description, s3_key, distance.
    assert results == [
        {
            "concept_id": "sales/f1/tables/races",
            "title": "Races",
            "description": "race rows",
            "s3_key": "okf/sales/f1/tables/races.md",
            "distance": 0.12,
        }
    ]


def test_semantic_search_no_filter_when_no_constraints(aws, config):
    s3v = FakeS3Vectors(hits=[])
    tools = ConsumptionTools(
        s3=aws["s3"],
        s3vectors=s3v,
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
    )
    tools.semantic_search("anything")
    # No constraints -> query_vectors called without a filter key.
    assert "filter" not in s3v.queries[0]


def test_semantic_search_handles_missing_metadata_keys(aws, config):
    s3v = FakeS3Vectors(hits=[{"key": "sales/f1/tables/x", "distance": 0.5}])
    tools = ConsumptionTools(
        s3=aws["s3"],
        s3vectors=s3v,
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
    )
    results = tools.semantic_search("q")
    assert results[0]["concept_id"] == "sales/f1/tables/x"
    assert results[0]["title"] == ""
    assert results[0]["s3_key"] == ""


# -- config -----------------------------------------------------------------


def test_config_from_env_uses_conventions_var_names():
    env = {
        "OKF_BUNDLE_BUCKET": "b",
        "OKF_VECTOR_BUCKET": "v",
        "OKF_VECTOR_INDEX": "i",
    }
    cfg = ConsumptionConfig.from_env(env)
    assert cfg.bundle_bucket == "b"
    assert cfg.vector_bucket == "v"
    assert cfg.vector_index == "i"
    assert cfg.registry_table == "okf-registry"  # default


# -- DoS guards: grep ReDoS (#21) + semantic_search top_k cap (#13) ----------


def test_grep_rejects_nested_quantifier_pattern(tools):
    # Catastrophic-backtracking shapes are rejected before compile (threat #21).
    for bad in ["(a+)+$", "(a*)*", "(a+)*", "(ab+)+"]:
        with pytest.raises(ValueError, match="nested quantifiers"):
            tools.grep(bad, DOMAIN, DATASET)


def test_grep_rejects_overlong_pattern(tools):
    with pytest.raises(ValueError, match="regex too long"):
        tools.grep("a" * 1001, DOMAIN, DATASET)


def test_grep_allows_normal_patterns(tools):
    # A benign pattern with a single quantifier is fine.
    out = tools.grep(r"races?", DOMAIN, DATASET)
    assert "matches" in out and out["truncated"] in (True, False)


def test_grep_caps_max_results_at_hard_ceiling(tools):
    from consumption_mcp import tools as toolmod

    # Requesting a huge max_results is silently clamped, not honored unbounded.
    out = tools.grep("e", DOMAIN, DATASET, max_results=10_000_000)
    assert len(out["matches"]) <= toolmod._GREP_MAX_RESULTS_CAP


def test_semantic_search_clamps_top_k(aws, config):
    from consumption_mcp import tools as toolmod

    s3v = FakeS3Vectors(hits=[])
    tools = ConsumptionTools(
        s3=aws["s3"],
        s3vectors=s3v,
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
    )
    tools.semantic_search("q", top_k=1000)
    assert s3v.queries[0]["topK"] == toolmod._SEMANTIC_TOP_K_MAX


def test_semantic_search_floors_top_k_at_one(aws, config):
    s3v = FakeS3Vectors(hits=[])
    tools = ConsumptionTools(
        s3=aws["s3"],
        s3vectors=s3v,
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
    )
    tools.semantic_search("q", top_k=0)
    assert s3v.queries[0]["topK"] == 1

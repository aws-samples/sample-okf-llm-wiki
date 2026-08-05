"""Unit tests for the consumption MCP tool logic (no live AWS)."""

from __future__ import annotations

import inspect

import pytest

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

from .conftest import BUNDLE_BUCKET, DATASET, DOMAIN, REGISTRY_TABLE
from .fakes import FakeBedrock, FakeS3Vectors


# -- list_domains -----------------------------------------------------------


def test_list_domains_returns_only_domain_items(tools):
    domains = tools.list_domains()["datasets"]
    pairs = sorted((d["data_domain"], d["dataset"]) for d in domains)
    assert pairs == [("ops", "logs"), ("sales", "f1")]
    # The HARVEST#... status item must not leak in.
    assert all(d["data_domain"] for d in domains)
    # No cross-dataset pairs seeded -> the signal fields are simply absent.
    assert all("cross_references" not in d for d in domains)
    assert all("cross_referenced_by" not in d for d in domains)


def test_list_domains_surfaces_cross_reference_signal(tools, aws):
    # A reindex-derived XREF row: sales/f1's bundle holds pair docs about
    # ops/logs. Both sides must learn about it — the initiating side so it can
    # find its own external/ folder, the referenced side because NOTHING in its
    # own bundle reveals the relationship (the docs live in the other bundle).
    aws["table"].put_item(
        Item={
            "pk": "DOMAIN#ops",
            "sk": "XREF#logs#sales#f1",
            "target_data_domain": "ops",
            "target_dataset": "logs",
            "source_data_domain": "sales",
            "source_dataset": "f1",
            "updated_at": "t",
        }
    )
    by_id = {
        (d["data_domain"], d["dataset"]): d
        for d in tools.list_domains()["datasets"]
    }
    assert by_id[("sales", "f1")]["cross_references"] == ["ops/logs"]
    assert "cross_referenced_by" not in by_id[("sales", "f1")]
    assert by_id[("ops", "logs")]["cross_referenced_by"] == ["sales/f1"]
    assert "cross_references" not in by_id[("ops", "logs")]


def test_list_domains_ignores_malformed_xref_rows(tools, aws):
    aws["table"].put_item(
        Item={"pk": "DOMAIN#ops", "sk": "XREF#logs#sales#f1", "updated_at": "t"}
    )
    domains = tools.list_domains()["datasets"]
    assert all("cross_referenced_by" not in d for d in domains)
    # And the XREF row itself is never mistaken for a dataset mapping.
    pairs = sorted((d["data_domain"], d["dataset"]) for d in domains)
    assert pairs == [("ops", "logs"), ("sales", "f1")]


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


# --- get_bundle_diff ----------------------------------------------------------


def _publish_second_harvest(s3):
    """Enable versioning and publish a second harvest over the seeded bundle.

    The seeded objects (written unversioned) carry VersionId "null" and act as
    version 1; a >1s tick separates the phases because S3/moto LastModified is
    second-granular (real harvests are minutes apart).
    """
    import time

    s3.put_bucket_versioning(
        Bucket=BUNDLE_BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    time.sleep(1.05)
    prefix = f"okf/{DOMAIN}/{DATASET}/"
    s3.delete_object(Bucket=BUNDLE_BUCKET, Key=f"{prefix}tables/results.md")
    s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"{prefix}tables/races.md",
        Body=(
            "---\ntype: Glue Table\ntitle: Races\ndescription: race rows\n"
            "timestamp: t\n---\n\n# Overview\n\nRaces table, REWRITTEN.\n"
        ).encode(),
    )
    s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"{prefix}tables/sprints.md",
        Body=(
            "---\ntype: Glue Table\ntitle: Sprints\ndescription: s\n"
            "timestamp: t\n---\n\n# Overview\n\nSprint races.\n"
        ).encode(),
    )
    import json as _json

    resp = s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"{prefix}.harvest/state.json",
        Body=_json.dumps(
            {
                "status": "complete",
                "data_domain": DOMAIN,
                "dataset": DATASET,
                "tables": ["races", "sprints"],
                "completed_at": "2026-02-01T00:00:00+00:00",
                "table_versions": {},
            }
        ).encode(),
    )
    return resp["VersionId"]


def test_get_bundle_diff_defaults_to_last_harvest(tools, aws):
    v2 = _publish_second_harvest(aws["s3"])
    out = tools.get_bundle_diff(DOMAIN, DATASET)
    assert out["to"]["version_id"] == v2 and out["to"]["current"]
    statuses = {f["key"]: f["status"] for f in out["files"]}
    prefix = f"okf/{DOMAIN}/{DATASET}/"
    assert statuses[f"{prefix}tables/sprints.md"] == "added"
    assert statuses[f"{prefix}tables/results.md"] == "removed"
    assert statuses[f"{prefix}tables/races.md"] == "modified"
    assert out["summary"]["unchanged"] >= 1  # untouched docs counted, not listed
    # The versions header lets the agent self-serve explicit selectors.
    assert [v["version_id"] for v in out["versions"]][0] == v2
    assert len(out["versions"]) <= 5


def test_get_bundle_diff_clamps_max_files_and_flags_truncation(tools, aws):
    from consumption_mcp import tools as toolmod

    _publish_second_harvest(aws["s3"])
    out = tools.get_bundle_diff(DOMAIN, DATASET, max_files=1)
    assert len(out["files"]) == 1 and out["truncated"] is True
    # Server-side cap: an absurd max_files is clamped, not honored.
    out = tools.get_bundle_diff(DOMAIN, DATASET, max_files=10_000_000)
    assert len(out["files"]) <= toolmod._DIFF_MAX_FILES_CAP


def test_get_bundle_diff_unknown_version_raises_value_error(tools, aws):
    _publish_second_harvest(aws["s3"])
    import pytest as _pytest

    with _pytest.raises(ValueError, match="unknown bundle version"):
        tools.get_bundle_diff(DOMAIN, DATASET, to_version="nope")


# -- the docstrings ARE the model-facing tool descriptions ---------------------
#
# chat/tools.py lifts these verbatim as the chat agent's tool descriptions (and
# server.py's MCP wrappers mirror them), so they must read as instructions to a
# MODEL: what the args mean and what the response tells you. Maintainer prose
# (DynamoDB key shapes, boto3 limitations, threat numbers, temp-dir mechanics)
# belongs in `#` comments inside the bodies, where it costs no tokens per request.


def _doc(name: str) -> str:
    return inspect.getdoc(getattr(ConsumptionTools, name)) or ""


MODEL_FACING = (
    "list_domains",
    "list_declared_domains",
    "search_domains",
    "list_directory",
    "read_page",
    "get_backlinks",
    "glob",
    "grep",
    "semantic_search",
    "get_bundle_diff",
)


@pytest.mark.parametrize("name", MODEL_FACING)
def test_docstrings_carry_no_maintainer_only_prose(name):
    doc = _doc(name)
    for leak in (
        "begins_with",  # boto3 resource-Table limitation
        "DOMAIN#",  # DynamoDB key shape
        "XREF#",
        "threat #",  # threat-model numbering
        "tempfile",  # temp-dir download mechanics
        "temp dir",
        "_iter_concepts",  # internal helper names
        "_SEMANTIC_TOP_K_MAX",
        "server.register_tools",  # which surface exposes the tool
        "DELIBERATELY NOT exposed",
        "docs/CONVENTIONS.md",
        ":meth:",  # Sphinx roles don't render for a model
        "/META",  # DynamoDB sort-key shape
    ):
        assert leak not in doc, f"{name} docstring still carries {leak!r}"


def test_list_domains_doc_keeps_both_cross_reference_directions():
    doc = _doc("list_domains")
    # Compressed, but each direction must still say WHERE its pair docs are read.
    # Budget raised (80 -> 110) when pagination joined the contract: the
    # cursor/limit/query semantics must be in the doc or agents cannot page.
    assert len(doc.split()) < 110, f"list_domains doc is {len(doc.split())} words"
    assert "cross_references" in doc and "cross_referenced_by" in doc
    assert "external/<d>/<ds>/" in doc
    assert "<that dataset>/external/<this>/" in doc


def test_get_bundle_diff_doc_keeps_the_selectors_and_the_bounds():
    doc = _doc("get_bundle_diff")
    assert len(doc.split()) < 90, f"get_bundle_diff doc is {len(doc.split())} words"
    assert "BOTH selectors are optional" in doc
    assert "versions" in doc and "newest first" in doc
    assert 'to_version="live"' in doc
    assert "50" in doc and "truncated" in doc
    assert "100 lines" in doc and "diff_truncated" in doc


def test_semantic_search_doc_documents_the_filter_args():
    from okf_core.concept_types import (
        CROSS_DATASET_REFERENCE_TYPE,
        GLUE_DATABASE_TYPE,
        GLUE_TABLE_TYPE,
        REDSHIFT_DATABASE_TYPE,
        REDSHIFT_EXTERNAL_TABLE_TYPE,
        REDSHIFT_TABLE_TYPE,
    )
    from okf_core.domain import DOMAIN_DOC_TYPE

    doc = _doc("semantic_search")
    # `type` is an EXACT match, so a wrong value returns nothing SILENTLY — the
    # model can only avoid that if the vocabulary is spelled out here. Every value
    # is pinned against its source-of-truth constant so a rename can't drift.
    assert "EXACT match" in doc
    assert "silently returns NOTHING" in doc
    for concept_type in (
        GLUE_TABLE_TYPE,
        GLUE_DATABASE_TYPE,
        REDSHIFT_TABLE_TYPE,
        REDSHIFT_EXTERNAL_TABLE_TYPE,
        REDSHIFT_DATABASE_TYPE,
        CROSS_DATASET_REFERENCE_TYPE,
        DOMAIN_DOC_TYPE,
    ):
        assert concept_type in doc, concept_type
    # `Reference` and `Playbook` have no runtime constant (the harvest prompts /
    # okf-authoring skill pin them), so match the backticked literal — a plain
    # substring check for "Reference" would pass on Cross-Dataset Reference alone.
    assert "``Reference``" in doc and "``Playbook``" in doc
    assert "ANY of the given tags" in doc
    # The top_k ceiling is the real constant, not a stale literal.
    from consumption_mcp import tools as toolmod

    assert f"capped server-side at {toolmod._SEMANTIC_TOP_K_MAX}" in doc


def test_grep_doc_documents_both_rejection_rules():
    from consumption_mcp import tools as toolmod

    doc = _doc("grep")
    # Otherwise the model discovers these only by getting an error back.
    assert str(toolmod._GREP_PATTERN_MAX_LEN) in doc
    assert "nests quantifiers" in doc and "(a+)+" in doc


def test_glob_doc_keeps_the_worked_examples():
    doc = _doc("glob")
    for example in ("tables/*", "**/*orders*"):
        assert example in doc
    # The implementation aside ("same scope as get_backlinks") told the model
    # nothing it can act on; the visibility RULE itself stays.
    assert "get_backlinks" not in doc
    assert "index.md" in doc and ".harvest" in doc


def test_read_page_and_get_backlinks_docs_explain_their_responses():
    page = _doc("read_page")
    assert "0-indexed" in page
    assert "total_lines" in page and "returned_lines" in page
    backlinks = _doc("get_backlinks")
    assert "heading" in backlinks


def test_list_domains_pages_and_filters_via_the_entity_index(tools, aws):
    # A GSI-enabled registry with STAMPED rows exercises the paged path
    # (Query, never Scan): the substring filter, the domain filter, cursor
    # continuation, and the enrichment queries.
    import boto3

    from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="okf-registry-gsi",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "entity", "AttributeType": "S"},
            {"AttributeName": "pair", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by-entity",
                "KeySchema": [
                    {"AttributeName": "entity", "KeyType": "HASH"},
                    {"AttributeName": "pair", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    for i in range(1, 6):
        table.put_item(
            Item={
                "pk": "DOMAIN#sales",
                "sk": f"DATASET#ds{i}",
                "entity": "dataset",
                "pair": f"sales/ds{i}",
                "data_domain": "sales",
                "dataset": f"ds{i}",
            }
        )
    table.put_item(
        Item={
            "pk": "DOMAIN#ops",
            "sk": "DATASET#logs",
            "entity": "dataset",
            "pair": "ops/logs",
            "data_domain": "ops",
            "dataset": "logs",
        }
    )
    table.put_item(
        Item={
            "pk": "DOMAIN#sales",
            "sk": "META",
            "entity": "domain",
            "pair": "sales",
            "data_domain": "sales",
            "description": "sales world",
        }
    )
    table.put_item(
        Item={
            "pk": "DOMAIN#ops",
            "sk": "XREF#logs#sales#ds1",
            "entity": "xref",
            "pair": "ops/logs",
            "target_data_domain": "ops",
            "target_dataset": "logs",
            "source_data_domain": "sales",
            "source_dataset": "ds1",
        }
    )
    # The backfill's readiness marker: readers Query the index only once it
    # exists (a partially-stamped registry would Query back a partial catalog).
    table.put_item(Item={"pk": "REGISTRY", "sk": "ENTITY_INDEX_READY"})
    t2 = ConsumptionTools(
        s3=tools.s3,
        s3vectors=tools.s3vectors,
        bedrock_runtime=tools.bedrock_runtime,
        ddb=table,
        config=ConsumptionConfig(
            bundle_bucket=BUNDLE_BUCKET,
            vector_bucket="vb",
            vector_index="vi",
            registry_table="okf-registry-gsi",
        ),
    )

    # Domain filter: base-table partition query, sorted, enriched.
    out = t2.list_domains(domain="sales")
    assert [d["dataset"] for d in out["datasets"]] == [f"ds{i}" for i in range(1, 6)]
    assert out["datasets"][0]["domain_description"] == "sales world"
    assert out["next_cursor"] is None

    # Substring filter over "<domain>/<dataset>".
    out = t2.list_domains(query="OPS/")
    assert [d["dataset"] for d in out["datasets"]] == ["logs"]
    assert out["datasets"][0]["cross_referenced_by"] == ["sales/ds1"]

    # Pagination: each page carries `limit` entries (the DDB queries are
    # Limit-bounded — without that, tiny mapping rows would let one 1MB page
    # return the whole catalog and the cap would never bite) and a cursor;
    # the pages tile the catalog with no overlap.
    seen: list[str] = []
    cursor = None
    pages = 0
    for _ in range(10):
        out = t2.list_domains(limit=2, cursor=cursor)
        assert len(out["datasets"]) <= 2
        pages += 1
        seen.extend(f"{d['data_domain']}/{d['dataset']}" for d in out["datasets"])
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert pages >= 3  # 6 datasets at limit=2 => at least three pages
    assert sorted(seen) == sorted(
        [f"sales/ds{i}" for i in range(1, 6)] + ["ops/logs"]
    )
    assert len(seen) == len(set(seen))  # no page overlap


def test_list_domains_rejects_a_garbled_cursor(tools):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        tools.list_domains(cursor="not-a-cursor")


def test_list_domains_partially_stamped_registry_uses_the_scan(tools, aws):
    # One row already carries the GSI keys (a fresh registration on an
    # un-backfilled registry) but the readiness marker is absent: the index
    # holds a PARTIAL catalog, so readers must keep scanning — trusting a
    # non-empty Query here silently hid every pre-index dataset.
    aws["table"].put_item(
        Item={
            "pk": "DOMAIN#fresh",
            "sk": "DATASET#newds",
            "entity": "dataset",
            "pair": "fresh/newds",
            "data_domain": "fresh",
            "dataset": "newds",
        }
    )
    out = tools.list_domains()
    pairs = {f"{d['data_domain']}/{d['dataset']}" for d in out["datasets"]}
    # BOTH the stamped newcomer and the unstamped legacy rows are visible.
    assert {"fresh/newds", f"{DOMAIN}/{DATASET}", "ops/logs"} <= pairs
    # `query` filters fine on the scan path too (it must not require the GSI).
    out = tools.list_domains(query="ops/")
    assert [d["dataset"] for d in out["datasets"]] == ["logs"]


def test_list_domains_marker_without_index_falls_back_to_scan(tools, aws):
    # The one legitimate post-marker fallback: the marker was stamped before
    # the terraform apply, so the Query fails with "no such index" — the scan
    # still returns the full catalog instead of an error.
    aws["table"].put_item(Item={"pk": "REGISTRY", "sk": "ENTITY_INDEX_READY"})
    out = tools.list_domains()
    pairs = {f"{d['data_domain']}/{d['dataset']}" for d in out["datasets"]}
    assert {f"{DOMAIN}/{DATASET}", "ops/logs"} <= pairs


def test_list_domains_mid_pagination_error_surfaces(tools, config):
    # A throttle (or a cursor replayed against a different query shape)
    # mid-pagination must SURFACE as an error: the old blanket fallback
    # silently re-ran the FULL scan, handing the caller the whole catalog
    # again (pages 1..N-1 duplicated) with next_cursor=null.
    import pytest as _pytest

    from consumption_mcp.tools import ConsumptionTools

    class _Throttle(Exception):
        def __init__(self):
            super().__init__("throttled")
            self.response = {
                "Error": {"Code": "ThrottlingException", "Message": "slow down"}
            }

    class _FakeDdb:
        def get_item(self, **kw):
            key = kw.get("Key") or {}
            if key.get("pk") == "REGISTRY" and key.get("sk") == "ENTITY_INDEX_READY":
                return {"Item": {"pk": "REGISTRY", "sk": "ENTITY_INDEX_READY"}}
            return {}

        def query(self, **kw):
            raise _Throttle()

    t2 = ConsumptionTools(
        s3=tools.s3,
        s3vectors=tools.s3vectors,
        bedrock_runtime=tools.bedrock_runtime,
        ddb=_FakeDdb(),
        config=config,
    )
    with _pytest.raises(_Throttle):
        t2.list_domains(limit=2)


def test_list_domains_cursored_page_survives_a_marker_read_failure(tools, aws):
    # A cursor is PROOF the prior page came from the GSI (the legacy scan
    # never emits one), so a cursored call must take the GSI path without
    # re-consulting the readiness marker: entity_index_ready fails closed to
    # False, so a transient marker read (or a deleted marker) mid-pagination
    # would otherwise reroute page N to the legacy scan — which ignores
    # start_key and hands the caller the whole catalog again with
    # next_cursor=null.
    import boto3

    from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="okf-registry-flaky-marker",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "entity", "AttributeType": "S"},
            {"AttributeName": "pair", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by-entity",
                "KeySchema": [
                    {"AttributeName": "entity", "KeyType": "HASH"},
                    {"AttributeName": "pair", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    for i in range(1, 5):
        table.put_item(
            Item={
                "pk": "DOMAIN#sales",
                "sk": f"DATASET#ds{i}",
                "entity": "dataset",
                "pair": f"sales/ds{i}",
                "data_domain": "sales",
                "dataset": f"ds{i}",
            }
        )
    table.put_item(Item={"pk": "REGISTRY", "sk": "ENTITY_INDEX_READY"})
    cfg = ConsumptionConfig(
        bundle_bucket=BUNDLE_BUCKET,
        vector_bucket="vb",
        vector_index="vi",
        registry_table="okf-registry-flaky-marker",
    )

    def make(ddb_like):
        return ConsumptionTools(
            s3=tools.s3,
            s3vectors=tools.s3vectors,
            bedrock_runtime=tools.bedrock_runtime,
            ddb=ddb_like,
            config=cfg,
        )

    first = make(table).list_domains(limit=2)
    assert first["next_cursor"] is not None

    class _FlakyMarkerTable:
        def get_item(self, **kw):
            raise RuntimeError("transient marker read failure")

        def scan(self, **kw):
            raise AssertionError("a cursored page must never fall back to the scan")

        def query(self, **kw):
            return table.query(**kw)

    out = make(_FlakyMarkerTable()).list_domains(
        limit=2, cursor=first["next_cursor"]
    )
    page1 = {f"{d['data_domain']}/{d['dataset']}" for d in first["datasets"]}
    page2 = {f"{d['data_domain']}/{d['dataset']}" for d in out["datasets"]}
    assert page2 and not (page1 & page2)  # start_key honored — no replay


def test_list_domains_cursored_page_missing_index_error_surfaces(tools, config):
    # Even the one fallback allowed on FIRST pages ("the index does not
    # exist") must surface when a cursor is supplied: the cursor could only
    # have come from a GSI page, so the scan cannot resume it.
    import pytest as _pytest

    from consumption_mcp.tools import ConsumptionTools, _encode_cursor

    class _NoIndex(Exception):
        def __init__(self):
            super().__init__("no index")
            self.response = {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "The table does not have the specified index",
                }
            }

    class _FakeDdb:
        def get_item(self, **kw):
            return {"Item": {"pk": "REGISTRY", "sk": "ENTITY_INDEX_READY"}}

        def query(self, **kw):
            raise _NoIndex()

        def scan(self, **kw):
            raise AssertionError("a cursored page must never fall back to the scan")

    t2 = ConsumptionTools(
        s3=tools.s3,
        s3vectors=tools.s3vectors,
        bedrock_runtime=tools.bedrock_runtime,
        ddb=_FakeDdb(),
        config=config,
    )
    cursor = _encode_cursor({"pk": "DOMAIN#sales", "sk": "DATASET#ds2"})
    with _pytest.raises(_NoIndex):
        t2.list_domains(limit=2, cursor=cursor)

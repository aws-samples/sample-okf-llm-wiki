"""The policy authoring trigger: gates, the lease, persistence, and stamps.

v2 (LLM-judge era): authoring IS completion — the pipeline is gather →
fingerprint-skip → flip → author → persist → stamp ready. moto provides
S3/DynamoDB; the author is an injected callable.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from harvest import ar_build
from okf_aws import ar_policy
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_PENDING_SOURCE_HASH,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_READY,
    policy_doc_key,
    read_policy_doc,
    read_sources_manifest,
    registry_key,
    source_hash,
)

DOMAIN = "sport"
DATASET = "formula_1"
BUCKET = "okf-bundles"
TABLE = "okf-registry"

GOOD_DOC = """\
policies:
  - id: P001
    type: behavioural
    condition: figures are requested from an empty result
    action: never state figures derived from that query
    source: references/usage_guardrails.md
"""


class FakeAuthor:
    """The document-author seam: records every call, returns the scripted doc."""

    def __init__(self, reply=GOOD_DOC):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else self.reply(**kw)


@pytest.fixture()
def aws(monkeypatch):
    """moto S3 + DynamoDB, a seeded bundle, the flag ON, and the env wired."""
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        prefix = f"okf/{DOMAIN}/{DATASET}/"
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}references/usage_guardrails.md",
            Body=b"never sum booked and billed",
        )
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}references/enums/status.md",
            Body=b"-1 means unknown",
        )
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}tables/results.md", Body=b"not a source")
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # The mapping row exists from dataset registration; every stamp is
        # conditioned on it (authoring is always-on — no per-dataset opt-in).
        ddb.put_item(
            TableName=TABLE,
            Item={
                **registry_key(DOMAIN, DATASET),
                "data_domain": {"S": DOMAIN},
                "dataset": {"S": DATASET},
            },
        )
        yield s3, ddb


def _trigger(s3, ddb, *, author=None, force=False):
    return ar_build.maybe_build_policy(
        data_domain=DOMAIN,
        dataset=DATASET,
        registry=(ddb, TABLE),
        s3=s3,
        author=author or FakeAuthor(),
        force=force,
    )


def _row(ddb) -> dict:
    return ddb.get_item(TableName=TABLE, Key=registry_key(DOMAIN, DATASET)).get(
        "Item", {}
    )


def _attr(ddb, name) -> str:
    return (_row(ddb).get(name) or {}).get("S", "")


# --- the flag ------------------------------------------------------------------


def test_disabled_flag_is_a_pure_no_op(monkeypatch):
    # No env beyond the flag, no clients: with the flag off nothing may be
    # touched — passing no seams at all proves it.
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    assert (
        ar_build.maybe_build_policy(data_domain="d", dataset="s")
        == ar_build.OUTCOME_DISABLED
    )


# --- gates ---------------------------------------------------------------------


def test_unregistered_dataset_costs_one_get_item(aws):
    # The mapping row is gone (a dataset delete raced the finalize hook):
    # authoring must not resurrect state — one GetItem, no S3 touch.
    s3, ddb = aws
    ddb.delete_item(TableName=TABLE, Key=registry_key(DOMAIN, DATASET))

    class BrokenS3:
        def __getattr__(self, name):
            raise AssertionError("an unregistered dataset must never touch S3")

    author = FakeAuthor()
    out = ar_build.maybe_build_policy(
        data_domain=DOMAIN, dataset=DATASET,
        registry=(ddb, TABLE), s3=BrokenS3(), author=author,
    )
    assert out == ar_build.OUTCOME_UNREGISTERED
    assert author.calls == []


def test_no_sources_is_a_clean_no_op(aws):
    s3, ddb = aws
    for key in (
        f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        f"okf/{DOMAIN}/{DATASET}/references/enums/status.md",
    ):
        s3.delete_object(Bucket=BUCKET, Key=key)
    assert _trigger(s3, ddb) == ar_build.OUTCOME_NO_SOURCES
    assert _attr(ddb, ATTR_BUILD_STATUS) == ""


def test_unchanged_fingerprint_skips_when_the_document_exists(aws):
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_UNCHANGED
    assert len(author.calls) == 1  # the re-harvest cost zero model calls


def test_forced_trigger_reauthors_at_an_unchanged_fingerprint(aws):
    # The manual Sync's dispatch: same sources, ready row, live document —
    # force re-authors anyway (the authoring model/effort/prompt may have
    # changed, which the fingerprint cannot see).
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert _trigger(s3, ddb, author=author, force=True) == ar_build.OUTCOME_AUTHORED
    assert len(author.calls) == 2
    assert _attr(ddb, ATTR_BUILD_STATUS) == "ready"
    # From scratch, NOT update mode: the prior document is withheld on a
    # forced run — fed back, update mode's "minimally edit" instruction would
    # hand the old document straight back and defeat the re-roll.
    assert author.calls[1]["prior_doc"] == ""
    assert author.calls[1]["prior_manifest"] is None


def test_ready_row_without_its_document_reauthors(aws):
    # The self-heal for datasets stamped ready before v2 (or a lost write):
    # a matching fingerprint is NOT enough — the artifact must exist.
    s3, ddb = aws
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED
    s3.delete_object(Bucket=BUCKET, Key=policy_doc_key(DOMAIN, DATASET))
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert len(author.calls) == 1
    assert read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ) == GOOD_DOC.strip() or read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )


def test_ready_row_with_an_invalid_document_reauthors(aws):
    # THE v3 migration path's last hop: the dispatchers (chat's check gate,
    # the rebuild authority) treat a present-but-unparseable policies.yaml
    # (e.g. a pre-split, type-less document) as needing re-authoring — if the
    # skip gate here checked mere existence, the dataset would ping-pong
    # between "rebuild!" and "unchanged" forever and never re-author.
    s3, ddb = aws
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED
    ar_policy.put_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        doc_text=(
            "policies:\n"
            "  - id: P001\n"  # no `type`: pre-v3 shape, fails the parse
            "    condition: c\n"
            "    action: a\n"
            "    source: references/usage_guardrails.md\n"
        ),
    )
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert len(author.calls) == 1  # re-authored despite the matching fingerprint


def test_building_row_locks_out_a_second_run(aws):
    s3, ddb = aws
    ddb.update_item(
        TableName=TABLE,
        Key=registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_status = :b",
        ExpressionAttributeValues={":b": {"S": BUILD_BUILDING}},
    )
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_LOCKED
    assert author.calls == []


# --- the happy paths -----------------------------------------------------------------


def test_full_authoring_persists_everything_and_stamps_ready(aws):
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED

    (call,) = author.calls
    assert call["prior_doc"] == ""  # first run: nothing to update
    assert call["prior_manifest"] == {}
    assert [rel for rel, _ in call["sources"]] == [
        "references/enums/status.md",
        "references/usage_guardrails.md",
    ]

    assert read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ).startswith("policies:")
    manifest = read_sources_manifest(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert set(manifest["files"]) == {
        "references/enums/status.md",
        "references/usage_guardrails.md",
    }
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    assert _attr(ddb, ATTR_SOURCE_HASH) == fresh
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY


def test_update_run_hands_the_author_the_diff_base(aws):
    s3, ddb = aws
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED

    # The wiki moves; the next run must carry the PRIOR doc, the PRIOR
    # manifest, and a fetch_old that returns the OLD copy — sampled DURING
    # authoring, before persist refreshes the diff base.
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"NEW guardrails text",
    )
    seen: dict = {}

    def author(**kw):
        seen["prior_doc"] = kw["prior_doc"]
        seen["old_copy"] = kw["fetch_old"]("references/usage_guardrails.md")
        return GOOD_DOC

    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert seen["prior_doc"].startswith("policies:")
    assert seen["old_copy"] == b"never sum booked and billed"


def test_stamp_carries_the_gather_time_fingerprint(aws):
    # The wiki mutates while the author runs: the stamp must describe what was
    # AUTHORED FROM (stale on arrival), never the post-mutation state.
    s3, ddb = aws
    gathered = source_hash(s3, BUCKET, DOMAIN, DATASET)

    def author(**kw):
        s3.put_object(
            Bucket=BUCKET,
            Key=f"okf/{DOMAIN}/{DATASET}/references/enums/status.md",
            Body=b"mutated mid-authoring",
        )
        return GOOD_DOC

    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert _attr(ddb, ATTR_SOURCE_HASH) == gathered
    assert _attr(ddb, ATTR_SOURCE_HASH) != source_hash(s3, BUCKET, DOMAIN, DATASET)


# --- failure posture -----------------------------------------------------------------


def test_empty_document_stamps_failed_and_releases_the_lease(aws):
    s3, ddb = aws
    assert _trigger(s3, ddb, author=FakeAuthor(reply="   ")) == ar_build.OUTCOME_NO_RULES
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert _attr(ddb, "ar_build_detail") == "no_rules"
    # Retryable: the lease was released, a later trigger authors normally.
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED


def test_author_exception_stamps_failed_with_the_reason(aws):
    s3, ddb = aws
    out = _trigger(s3, ddb, author=FakeAuthor(reply=RuntimeError("model down")))
    assert out == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert "model down" in _attr(ddb, "ar_build_detail")


def test_pending_hash_is_parked_at_flip_time(aws):
    s3, ddb = aws
    seen: dict = {}

    def author(**kw):
        seen["pending"] = _attr(ddb, ATTR_PENDING_SOURCE_HASH)
        seen["status"] = _attr(ddb, ATTR_BUILD_STATUS)
        return GOOD_DOC

    _trigger(s3, ddb, author=author)
    assert seen["status"] == BUILD_BUILDING
    assert seen["pending"] == _attr(ddb, ATTR_SOURCE_HASH)


# --- the rules-schema sidecar ----------------------------------------------------


COLUMNS_TSV = (
    "table\tcolumn\ttype\tcomment\n"
    "results\traceid\tint\t\n"
    "results\tpoints\tdouble\t\n"
    "races\tyear\tint\t(partition key)\n"
)


def test_authoring_snapshots_the_rules_schema_sidecar(aws):
    s3, ddb = aws
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.metadata/columns.tsv",
        Body=COLUMNS_TSV.encode(),
    )
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    from okf_aws.ar_policy import read_rules_schema

    schema = read_rules_schema(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    # No glue_database on the row -> the dataset id is the database name.
    assert schema == {
        DATASET: {"results": ["points", "raceid"], "races": ["year"]}
    }
    # The author gate received the same schema (rules become validatable).
    assert author.calls[0]["rules_schema"] == schema


def test_no_snapshot_means_no_sidecar_and_a_none_schema(aws):
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    from okf_aws.ar_policy import read_rules_schema

    assert (
        read_rules_schema(s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET)
        is None
    )
    assert author.calls[0]["rules_schema"] is None


def test_sidecar_parse_is_header_driven_not_positional(aws):
    # The multi-database fork's snapshot leads with a `database` column — a
    # positional split would silently read database as table and table as
    # column, building a garbage schema the author gate then trusts.
    s3, ddb = aws
    five_col = (
        "database\ttable\tcolumn\ttype\tcomment\n"
        "f1_db\tresults\traceid\tint\t\n"
        "f1_db\tresults\tpoints\tdouble\t\n"
    )
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.metadata/columns.tsv",
        Body=five_col.encode(),
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED
    from okf_aws.ar_policy import read_rules_schema

    schema = read_rules_schema(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert schema == {DATASET: {"results": ["points", "raceid"]}}


def test_glue_database_attribute_names_the_sidecar_database(aws):
    s3, ddb = aws
    ddb.update_item(
        TableName=TABLE,
        Key=registry_key(DOMAIN, DATASET),
        UpdateExpression="SET glue_database = :g",
        ExpressionAttributeValues={":g": {"S": "F1_Curated"}},
    )
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.metadata/columns.tsv",
        Body=COLUMNS_TSV.encode(),
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED
    from okf_aws.ar_policy import read_rules_schema

    schema = read_rules_schema(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert list(schema) == ["f1_curated"]

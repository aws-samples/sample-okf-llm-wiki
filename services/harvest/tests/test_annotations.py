"""Annotated-mode harvest: entrypoint validation, runner reconcile, DDB write-back."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import harvest.entrypoint as ep
from harvest import annotations as hanno
from harvest import prompts, runner
from okf_core import annotations as anno


# --- annotation prompt contract --------------------------------------------


def test_annotation_prompt_encodes_verify_apply_resolve_contract():
    p = prompts.build_annotation_prompt(
        dataset="orders",
        annotations=[
            {"annotation_id": "a1", "concept_id": "tables/races",
             "quote": "status 9 means refunds", "note": "9 is chargebacks"}
        ],
        results_rel=".harvest/annotation_results.json",
        domain_description="Sales",
        domain_context="ctx",
    )
    low = p.lower()
    # Data is the judge, not the reader's say-so.
    assert "live data" in low
    assert "run_sql" in p
    # The two outcomes + the required verdict file (placeholder must be filled).
    assert "applied" in low and "rejected" in low
    assert ".harvest/annotation_results.json" in p
    assert "{results_rel}" not in p and "{{" not in p
    # The annotation content is threaded in (on disk + inlined).
    assert "9 is chargebacks" in p
    assert "annotations.json" in p

REGION = "us-east-1"
TABLE = "okf-annotations"


# --- entrypoint validation -------------------------------------------------


def test_annotated_mode_requires_user_sub():
    r = ep.start_harvest(
        {"data_domain": "d", "dataset": "x", "mode": "annotated", "annotations": [{}]}
    )
    assert r["status"] == "rejected"
    assert "user_sub" in r["error"]


def test_annotated_mode_requires_nonempty_annotations():
    r = ep.start_harvest(
        {"data_domain": "d", "dataset": "x", "mode": "annotated", "user_sub": "s"}
    )
    assert r["status"] == "rejected"
    assert "annotations" in r["error"]


def test_safe_log_redacts_annotation_bodies():
    # The reader's feedback text must never hit the logs; only a count.
    safe = ep._safe(
        {
            "data_domain": "d",
            "dataset": "x",
            "mode": "annotated",
            "user_sub": "sub-1",
            "annotations": [{"note": "secret feedback"}, {"note": "more"}],
        }
    )
    assert safe["annotations"] == "<2 items>"
    assert "secret feedback" not in json.dumps(safe)
    assert safe["user_sub"] == "sub-1"


# --- DDB write-back (harvest.annotations) ----------------------------------


@pytest.fixture
def anno_table(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
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
        monkeypatch.setenv("OKF_ANNOTATIONS_TABLE", TABLE)
        monkeypatch.setenv("AWS_REGION", REGION)
        yield ddb


def _seed(ddb, *, user_sub, concept_id, annotation_id, status):
    ddb.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": anno.annotation_pk("sales", "orders", user_sub)},
            "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
            "concept_id": {"S": concept_id},
            "annotation_id": {"S": annotation_id},
            "note": {"S": "feedback"},
            "status": {"S": status},
        },
    )


def _get(ddb, user_sub, concept_id, annotation_id):
    return ddb.get_item(
        TableName=TABLE,
        Key={
            "pk": {"S": anno.annotation_pk("sales", "orders", user_sub)},
            "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
        },
    ).get("Item")


def test_resolve_annotation_sets_outcome_comment_and_ttl(anno_table):
    _seed(anno_table, user_sub="s", concept_id="tables/races", annotation_id="a1",
          status=anno.STATUS_IN_REVIEW)
    ct = hanno.build_annotations_client()
    ok = hanno.resolve_annotation(
        ct, data_domain="sales", dataset="orders", user_sub="s",
        concept_id="tables/races", annotation_id="a1",
        outcome="applied", comment="Confirmed: status 9 is chargebacks.",
    )
    assert ok
    item = _get(anno_table, "s", "tables/races", "a1")
    assert item["status"]["S"] == anno.STATUS_RESOLVED
    assert item["outcome"]["S"] == anno.OUTCOME_APPLIED
    assert item["resolution"]["S"].startswith("Confirmed")
    # TTL set only at resolution.
    assert int(item["expires_at"]["N"]) > 0


def test_resolve_coerces_unknown_outcome_to_rejected(anno_table):
    _seed(anno_table, user_sub="s", concept_id="tables/x", annotation_id="a1",
          status=anno.STATUS_IN_REVIEW)
    ct = hanno.build_annotations_client()
    hanno.resolve_annotation(
        ct, data_domain="sales", dataset="orders", user_sub="s",
        concept_id="tables/x", annotation_id="a1", outcome="bogus", comment="x",
    )
    item = _get(anno_table, "s", "tables/x", "a1")
    # An unrecognized agent verdict conservatively becomes rejected, not applied.
    assert item["outcome"]["S"] == anno.OUTCOME_REJECTED


def test_resolve_missing_row_is_noop(anno_table):
    ct = hanno.build_annotations_client()
    ok = hanno.resolve_annotation(
        ct, data_domain="sales", dataset="orders", user_sub="s",
        concept_id="tables/gone", annotation_id="nope", outcome="applied", comment="x",
    )
    assert ok is False  # conditional write fails cleanly, no ghost item created
    assert _get(anno_table, "s", "tables/gone", "nope") is None


def test_revert_to_open_only_flips_in_review(anno_table):
    _seed(anno_table, user_sub="s", concept_id="tables/a", annotation_id="a1",
          status=anno.STATUS_IN_REVIEW)
    _seed(anno_table, user_sub="s", concept_id="tables/b", annotation_id="a2",
          status=anno.STATUS_RESOLVED)
    ct = hanno.build_annotations_client()
    hanno.revert_to_open(ct, data_domain="sales", dataset="orders", user_sub="s",
                         concept_id="tables/a", annotation_id="a1")
    hanno.revert_to_open(ct, data_domain="sales", dataset="orders", user_sub="s",
                         concept_id="tables/b", annotation_id="a2")
    assert _get(anno_table, "s", "tables/a", "a1")["status"]["S"] == anno.STATUS_OPEN
    # A resolved row is NOT reopened.
    assert _get(anno_table, "s", "tables/b", "a2")["status"]["S"] == anno.STATUS_RESOLVED


def test_build_client_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OKF_ANNOTATIONS_TABLE", raising=False)
    assert hanno.build_annotations_client() is None
    # And a resolve against a None client is a safe no-op.
    assert hanno.resolve_annotation(
        None, data_domain="d", dataset="x", user_sub="s",
        concept_id="c", annotation_id="a", outcome="applied", comment="",
    ) is False


# --- runner reconcile (results file -> DDB) --------------------------------


def test_reconcile_resolves_ruled_and_reverts_skipped(anno_table, tmp_path):
    # Two survivors dispatched; the agent's results file rules on one, omits the
    # other. Reconcile must resolve the ruled one and revert the omitted one.
    _seed(anno_table, user_sub="s", concept_id="tables/races", annotation_id="a1",
          status=anno.STATUS_IN_REVIEW)
    _seed(anno_table, user_sub="s", concept_id="tables/results", annotation_id="a2",
          status=anno.STATUS_IN_REVIEW)
    root = tmp_path
    (root / ".harvest").mkdir(parents=True)
    (root / runner.ANNOTATION_RESULTS_REL).write_text(
        json.dumps([
            {"annotation_id": "a1", "concept_id": "tables/races",
             "outcome": "applied", "comment": "Fixed the grain."},
        ])
    )
    survivors = [
        {"annotation_id": "a1", "concept_id": "tables/races"},
        {"annotation_id": "a2", "concept_id": "tables/results"},
    ]
    ct = hanno.build_annotations_client()
    tally = runner._reconcile_annotation_results(
        ct, root, data_domain="sales", dataset="orders", user_sub="s", survivors=survivors
    )
    a1 = _get(anno_table, "s", "tables/races", "a1")
    a2 = _get(anno_table, "s", "tables/results", "a2")
    assert a1["status"]["S"] == anno.STATUS_RESOLVED
    assert a1["outcome"]["S"] == anno.OUTCOME_APPLIED
    # The agent didn't rule on a2 -> it's returned to the open pool, not stranded.
    assert a2["status"]["S"] == anno.STATUS_OPEN
    # The tally reflects what happened (drives the run's status detail).
    assert tally == {"applied": 1, "rejected": 0, "reverted": 1}


def test_reconcile_missing_results_reverts_all(anno_table, tmp_path):
    # No results file (agent crashed before writing) -> every survivor reverts.
    _seed(anno_table, user_sub="s", concept_id="tables/races", annotation_id="a1",
          status=anno.STATUS_IN_REVIEW)
    ct = hanno.build_annotations_client()
    tally = runner._reconcile_annotation_results(
        ct, tmp_path, data_domain="sales", dataset="orders", user_sub="s",
        survivors=[{"annotation_id": "a1", "concept_id": "tables/races"}],
    )
    assert _get(anno_table, "s", "tables/races", "a1")["status"]["S"] == anno.STATUS_OPEN
    assert tally == {"applied": 0, "rejected": 0, "reverted": 1}

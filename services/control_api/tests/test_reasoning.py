"""The Reasoning page endpoints: enrollment (opt-in/delete), status, sync.

The contracts pinned here: enrollment is refused without a complete wiki;
enrolling publishes the first-build event but never builds in-process;
unenrolling is STRICT delete semantics (guardrail -> policy -> artifacts ->
attrs, surfacing a Bedrock failure instead of leaking a policy) and is refused
mid-build; `up_to_date` is the fingerprint gate rendered for humans; and the
manual sync is one event, nothing more.
"""

from __future__ import annotations

import json

import pytest

from conftest import BUCKET, REGISTRY
from control_api import handlers
from okf_aws import ar_policy as ap

DOMAIN, DATASET = "sport", "f1"


class FakeEvents:
    def __init__(self, fail=False):
        self.fail = fail
        self.entries: list[dict] = []

    def put_events(self, *, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 1 if self.fail else 0}


class FakeBedrock:
    def __init__(self, raising=False):
        self.raising = raising
        self.deleted: list[tuple[str, str]] = []

    def delete_guardrail(self, *, guardrailIdentifier):
        if self.raising:
            raise RuntimeError("bedrock down")
        self.deleted.append(("guardrail", guardrailIdentifier))

    def delete_automated_reasoning_policy(self, *, policyArn, force):
        self.deleted.append(("policy", policyArn))


def _seed(cfg, *, wiki=True, sources=True):
    handlers.upsert_domain_mapping(
        cfg.ddb, registry_table=REGISTRY, data_domain=DOMAIN, dataset=DATASET,
        glue_database="db",
    )
    if wiki:
        cfg.s3.put_object(
            Bucket=BUCKET,
            Key=f"okf/{DOMAIN}/{DATASET}/.harvest/state.json",
            Body=json.dumps({"status": "complete"}).encode(),
        )
    if sources:
        cfg.s3.put_object(
            Bucket=BUCKET,
            Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
            Body=b"never sum booked and billed",
        )


def _set(cfg, **attrs):
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"DOMAIN#{DOMAIN}"}, "sk": {"S": f"DATASET#{DATASET}"}},
        UpdateExpression="SET " + ", ".join(f"#{i} = :{i}" for i in range(len(attrs))),
        ExpressionAttributeNames={f"#{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":{i}": v for i, v in enumerate(attrs.values())},
    )


def _enroll(cfg, events=None, bedrock=None, enrolled=True):
    return handlers.set_reasoning_enrollment(
        cfg.ddb, cfg.s3, bedrock or FakeBedrock(), events,
        registry_table=REGISTRY, bucket=BUCKET,
        data_domain=DOMAIN, dataset=DATASET, enrolled=enrolled,
    )


def _status(cfg):
    return handlers.get_reasoning_status(
        cfg.ddb, cfg.s3, registry_table=REGISTRY, bucket=BUCKET,
        data_domain=DOMAIN, dataset=DATASET,
    )


# --- enroll ---------------------------------------------------------------------


def test_enroll_sets_the_flag_and_queues_the_first_build(cfg):
    _seed(cfg)
    events = FakeEvents()
    out = _enroll(cfg, events=events)
    assert out == {"enrolled": True, "queued": True}
    (entry,) = events.entries
    assert entry["DetailType"] == "policy_rebuild"
    assert json.loads(entry["Detail"]) == {
        "data_domain": DOMAIN, "dataset": DATASET, "reason": "enroll",
    }
    assert _status(cfg)["enrolled"] is True


def test_enroll_without_a_wiki_is_refused(cfg):
    # The fail-safe rule: the policy is derived FROM the wiki, so a dataset
    # that was never harvested has nothing to enroll.
    _seed(cfg, wiki=False)
    with pytest.raises(handlers.ApiError) as e:
        _enroll(cfg, events=FakeEvents())
    assert e.value.status == 409 and "harvest" in e.value.message


def test_enroll_when_the_feature_is_off_is_refused(cfg):
    _seed(cfg)
    with pytest.raises(handlers.ApiError) as e:
        _enroll(cfg, events=None)
    assert e.value.status == 409


def test_enroll_unknown_dataset_is_404(cfg):
    with pytest.raises(handlers.ApiError) as e:
        _enroll(cfg, events=FakeEvents())
    assert e.value.status == 404


def test_enroll_is_idempotent_and_requeues(cfg):
    _seed(cfg)
    _enroll(cfg, events=FakeEvents())
    events = FakeEvents()
    out = _enroll(cfg, events=events)
    assert out["enrolled"] is True and len(events.entries) == 1


# --- unenroll (delete semantics) ---------------------------------------------------


def _seed_built(cfg):
    _seed(cfg)
    _set(
        cfg,
        ar_enrolled={"BOOL": True},
        ar_build_status={"S": "ready"},
        ar_policy_arn={"S": "arn:p1"},
        ar_guardrail_id={"S": "g1"},
        ar_source_hash={"S": "h"},
    )
    cfg.s3.put_object(Bucket=BUCKET, Key=f"policy/{DOMAIN}/{DATASET}/ar_rules.md", Body=b"1. x")
    cfg.s3.put_object(Bucket=BUCKET, Key=f"policy/{DOMAIN}/{DATASET}/grounding.json", Body=b"{}")


def test_unenroll_deletes_everything(cfg):
    _seed_built(cfg)
    bedrock = FakeBedrock()
    out = _enroll(cfg, events=FakeEvents(), bedrock=bedrock, enrolled=False)
    assert out == {"enrolled": False, "deleted": True}
    # Guardrail first (it references the policy version), then the policy.
    assert bedrock.deleted == [("guardrail", "g1"), ("policy", "arn:p1")]
    listed = cfg.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"policy/{DOMAIN}/{DATASET}/")
    assert listed.get("KeyCount", 0) == 0
    status = _status(cfg)
    # Delete semantics: indistinguishable from never-enrolled.
    assert status["enrolled"] is False and status["status"] == ""
    assert status["up_to_date"] is None and status["rules"] == []


def test_unenroll_mid_build_is_refused(cfg):
    _seed(cfg)
    _set(cfg, ar_enrolled={"BOOL": True}, ar_build_status={"S": "building"})
    with pytest.raises(handlers.ApiError) as e:
        _enroll(cfg, events=FakeEvents(), enrolled=False)
    assert e.value.status == 409 and "in flight" in e.value.message


def test_unenroll_surfaces_a_bedrock_failure_instead_of_leaking(cfg):
    _seed_built(cfg)
    with pytest.raises(handlers.ApiError) as e:
        _enroll(cfg, events=FakeEvents(), bedrock=FakeBedrock(raising=True), enrolled=False)
    assert e.value.status == 502
    # Nothing was cleared: the row still says enrolled+ready, so a retry works.
    assert _status(cfg)["enrolled"] is True


def test_unenroll_when_not_enrolled_is_a_no_op(cfg):
    _seed(cfg)
    assert _enroll(cfg, events=FakeEvents(), enrolled=False) == {
        "enrolled": False, "deleted": False,
    }


# --- status --------------------------------------------------------------------


def test_status_lists_sources_and_flags_freshness(cfg):
    _seed(cfg)
    fresh = ap.source_hash(cfg.s3, BUCKET, DOMAIN, DATASET)
    _set(
        cfg,
        ar_enrolled={"BOOL": True},
        ar_build_status={"S": "ready"},
        ar_source_hash={"S": fresh},
        ar_built_at={"S": "2026-08-01T10:00:00+00:00"},
        ar_fidelity_coverage={"N": "0.9"},
        ar_fidelity_accuracy={"N": "0.95"},
    )
    ap.put_grounding(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        grounding={"RULE1": {"rule_text": "IF x THEN y",
                              "rule_source_page": "references/usage_guardrails.md"}},
    )
    out = _status(cfg)
    assert out["enrolled"] is True and out["up_to_date"] is True
    assert out["sources"] == ["references/usage_guardrails.md"]
    assert out["rules"] == [{
        "id": "RULE1", "text": "IF x THEN y",
        "source_page": "references/usage_guardrails.md",
    }]
    assert out["built_at"] == "2026-08-01T10:00:00+00:00"
    assert out["fidelity_coverage"] == 0.9

    # The wiki moves -> the same endpoint reports out-of-date (the manual-sync
    # fail-safe's trigger condition).
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"amended",
    )
    assert _status(cfg)["up_to_date"] is False


def test_status_of_an_unharvested_dataset_explains_why(cfg):
    _seed(cfg, wiki=False, sources=False)
    out = _status(cfg)
    assert out["can_enroll"] is False and "harvest" in out["reason"]
    assert out["has_sources"] is False and out["up_to_date"] is None


# --- sync ----------------------------------------------------------------------


def test_sync_queues_one_rebuild(cfg):
    _seed_built(cfg)
    events = FakeEvents()
    out = handlers.trigger_reasoning_sync(
        cfg.ddb, events, registry_table=REGISTRY,
        data_domain=DOMAIN, dataset=DATASET,
    )
    assert out == {"queued": True}
    (entry,) = events.entries
    assert json.loads(entry["Detail"])["reason"] == "manual_sync"


def test_document_returns_the_live_rules_doc(cfg):
    # The viewer's endpoint: the off-mount ar_rules.md verbatim. Deliberately
    # NOT part of get_reasoning_status (that call is polled during builds and
    # this document can be tens of kilobytes).
    _seed_built(cfg)
    out = handlers.get_reasoning_document(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert out == {"exists": True, "text": "1. x"}


def test_document_absent_reads_as_exists_false(cfg):
    _seed(cfg)  # registered, never authored
    out = handlers.get_reasoning_document(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert out == {"exists": False, "text": ""}


def test_sync_is_allowed_from_a_failed_build(cfg):
    # The Reasoning page's "Retry build": a failed row (transient service
    # error, a bad build input) must stay retryable — sync gates ONLY on
    # enrollment, never on build status. Without this, a failed first build
    # strands the dataset (enrolled, so re-enroll is unavailable too).
    _seed(cfg)
    _set(
        cfg,
        ar_enrolled={"BOOL": True},
        ar_build_status={"S": "failed"},
        ar_build_detail={"S": "ValidationException: type not defined"},
    )
    events = FakeEvents()
    out = handlers.trigger_reasoning_sync(
        cfg.ddb, events, registry_table=REGISTRY,
        data_domain=DOMAIN, dataset=DATASET,
    )
    assert out == {"queued": True}
    assert len(events.entries) == 1


def test_sync_requires_enrollment(cfg):
    _seed(cfg)
    with pytest.raises(handlers.ApiError) as e:
        handlers.trigger_reasoning_sync(
            cfg.ddb, FakeEvents(), registry_table=REGISTRY,
            data_domain=DOMAIN, dataset=DATASET,
        )
    assert e.value.status == 409

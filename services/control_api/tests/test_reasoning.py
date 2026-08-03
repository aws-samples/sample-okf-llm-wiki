"""The Reasoning page endpoints: status, manual sync, the document viewer.

The contracts pinned here: policy checks are ALWAYS ON per dataset (no
enrollment routes — the retired opt-in has no successor), sync is refused
without a complete wiki but is otherwise available from EVERY state —
including "never authored", where it is the manual first-authoring trigger
(there is no bulk backfill by design); `up_to_date` is the fingerprint gate
rendered for humans; and the manual sync is one event, nothing more.
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


def _status(cfg):
    return handlers.get_reasoning_status(
        cfg.ddb, cfg.s3, registry_table=REGISTRY, bucket=BUCKET,
        data_domain=DOMAIN, dataset=DATASET,
    )


def _sync(cfg, events=None):
    return handlers.trigger_reasoning_sync(
        cfg.ddb, cfg.s3, events,
        registry_table=REGISTRY, bucket=BUCKET,
        data_domain=DOMAIN, dataset=DATASET,
    )


_POLICY_DOC = """\
policies:
  - id: P001
    type: behavioural
    condition: figures from empty results
    action: never state figures
    source: references/usage_guardrails.md
"""


def _seed_built(cfg):
    _seed(cfg)
    _set(
        cfg,
        ar_build_status={"S": "ready"},
        ar_source_hash={"S": "h"},
    )
    ap.put_policy_doc(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        doc_text=_POLICY_DOC,
    )
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"policy/{DOMAIN}/{DATASET}/sources_manifest.json",
        Body=b"{}",
    )


# --- status --------------------------------------------------------------------


def test_status_lists_sources_and_flags_freshness(cfg):
    _seed(cfg)
    fresh = ap.source_hash(cfg.s3, BUCKET, DOMAIN, DATASET)
    _set(
        cfg,
        ar_build_status={"S": "ready"},
        ar_source_hash={"S": fresh},
        ar_built_at={"S": "2026-08-01T10:00:00+00:00"},
    )
    ap.put_policy_doc(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        doc_text=_POLICY_DOC,
    )
    out = _status(cfg)
    assert out["wiki_ready"] is True and out["up_to_date"] is True
    assert out["sources"] == ["references/usage_guardrails.md"]
    # `type` rides through to the Reasoning page (the per-policy track badge).
    assert out["policies"] == [{
        "id": "P001", "type": "behavioural",
        "condition": "figures from empty results",
        "action": "never state figures",
        "source": "references/usage_guardrails.md",
    }]
    assert out["built_at"] == "2026-08-01T10:00:00+00:00"

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
    assert out["wiki_ready"] is False and "harvest" in out["reason"]
    assert out["has_sources"] is False and out["up_to_date"] is None


def test_status_of_a_never_authored_dataset_reads_as_no_policies_yet(cfg):
    # Always-on but never authored (a dataset predating the feature): the
    # page renders the manual first-authoring CTA off exactly this shape.
    _seed(cfg)
    out = _status(cfg)
    assert out["wiki_ready"] is True and out["status"] == ""
    assert out["up_to_date"] is None and out["policies"] == []
    assert out["has_sources"] is True


def test_status_unknown_dataset_is_404(cfg):
    with pytest.raises(handlers.ApiError) as e:
        _status(cfg)
    assert e.value.status == 404


def test_status_while_building_lists_sources_without_downloading_them(cfg):
    # The UI polls every few seconds during a build; freshness is moot then
    # (the page shows the build state) and the corpus must NOT be downloaded
    # per poll — the source LIST alone feeds the response.
    _seed(cfg)
    _set(
        cfg,
        ar_build_status={"S": "building"},
        ar_source_hash={"S": "stale-or-not-does-not-matter"},
    )

    real_get = cfg.s3.get_object

    def _counting_get(**kwargs):
        key = kwargs.get("Key", "")
        assert not key.endswith("usage_guardrails.md"), (
            "a building-status poll must not download source bodies"
        )
        return real_get(**kwargs)

    cfg.s3.get_object = _counting_get
    try:
        out = _status(cfg)
    finally:
        cfg.s3.get_object = real_get
    assert out["status"] == "building"
    assert out["up_to_date"] is None  # moot mid-build, never a stale verdict
    assert out["sources"] == ["references/usage_guardrails.md"]
    assert out["has_sources"] is True


# --- sync ----------------------------------------------------------------------


def test_sync_queues_one_rebuild(cfg):
    _seed_built(cfg)
    events = FakeEvents()
    out = _sync(cfg, events=events)
    assert out == {"queued": True}
    (entry,) = events.entries
    assert entry["DetailType"] == "policy_rebuild"
    assert json.loads(entry["Detail"]) == {
        "data_domain": DOMAIN, "dataset": DATASET, "reason": "manual_sync",
    }


def test_sync_is_the_manual_first_authoring_for_a_never_authored_dataset(cfg):
    # A dataset predating the feature has a wiki but no policy state; there is
    # no bulk backfill, so THIS is how its first document gets authored.
    _seed(cfg)
    events = FakeEvents()
    assert _sync(cfg, events=events) == {"queued": True}
    assert len(events.entries) == 1


def test_sync_is_allowed_from_a_failed_build(cfg):
    # The Reasoning page's "Retry build": a failed row (transient service
    # error, a bad build input) must stay retryable — sync gates ONLY on the
    # wiki existing, never on build status. Without this, a failed first
    # build strands the dataset.
    _seed(cfg)
    _set(
        cfg,
        ar_build_status={"S": "failed"},
        ar_build_detail={"S": "ValidationException: type not defined"},
    )
    events = FakeEvents()
    assert _sync(cfg, events=events) == {"queued": True}
    assert len(events.entries) == 1


def test_sync_without_a_wiki_is_refused(cfg):
    # The policy is derived FROM the wiki: a dataset that was never harvested
    # has nothing to author from yet.
    _seed(cfg, wiki=False)
    with pytest.raises(handlers.ApiError) as e:
        _sync(cfg, events=FakeEvents())
    assert e.value.status == 409 and "harvest" in e.value.message


def test_sync_when_the_feature_is_off_is_refused(cfg):
    _seed(cfg)
    with pytest.raises(handlers.ApiError) as e:
        _sync(cfg, events=None)
    assert e.value.status == 409


def test_sync_unknown_dataset_is_404(cfg):
    with pytest.raises(handlers.ApiError) as e:
        _sync(cfg, events=FakeEvents())
    assert e.value.status == 404


# --- the document viewer ---------------------------------------------------------


def test_document_returns_the_live_policy_doc(cfg):
    # The viewer's endpoint: the off-mount policies.yaml verbatim. Deliberately
    # NOT part of get_reasoning_status (that call is polled during authoring
    # and this document can be tens of kilobytes).
    _seed_built(cfg)
    out = handlers.get_reasoning_document(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert out == {"exists": True, "text": _POLICY_DOC}


def test_document_absent_reads_as_exists_false(cfg):
    _seed(cfg)  # registered, never authored
    out = handlers.get_reasoning_document(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert out == {"exists": False, "text": ""}

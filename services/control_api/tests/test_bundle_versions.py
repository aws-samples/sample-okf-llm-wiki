"""Bundle versions / diff / repromote endpoints through the full router.

moto backs S3 (versioned bundle bucket) + DynamoDB. The write pattern mirrors a
real pair of harvests (clean_authored_output delete markers included); phases
that must be strictly ordered are separated by a >1s tick because S3 (and moto)
report LastModified at second granularity — see okf_aws's test_s3_versions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from control_api import app, handlers
from okf_aws.s3_bundle import bundle_prefix, state_marker_key
from tests.conftest import BUCKET, FRESHNESS, REGISTRY
from tests.fakes import FakeEvents

DOMAIN = "sport"
DATASET = "formula_1"
PREFIX = bundle_prefix(DOMAIN, DATASET)
MARKER = state_marker_key(DOMAIN, DATASET)


def _event(method: str, path: str, *, body=None, query=None):
    evt: dict = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {"claims": {"sub": "user-1", "email": "u@x.com"}}
            },
        },
    }
    if query:
        evt["queryStringParameters"] = query
    if body is not None:
        evt["body"] = json.dumps(body)
    return evt


def _json(resp):
    return json.loads(resp["body"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc(title: str, body: str) -> str:
    return (
        f"---\ntype: Glue Table\ntitle: {title}\ndescription: d\n"
        f"timestamp: t\n---\n\n{body}\n"
    )


def _mark(s3, status: str, **extra) -> str:
    state = {"status": status, "data_domain": DOMAIN, "dataset": DATASET, **extra}
    return s3.put_object(
        Bucket=BUCKET, Key=MARKER, Body=json.dumps(state).encode()
    )["VersionId"]


def _write_history(s3) -> tuple[str, str]:
    """Enable versioning + publish two harvests. Returns (v1, v2) marker ids."""
    s3.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    _mark(s3, "in_progress", started_at="2026-01-01T00:00:00+00:00")
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}tables/a.md", Body=_doc("A", "v1").encode())
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}tables/b.md", Body=_doc("B", "b").encode())
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}index.md", Body=b"# Index v1\n")
    v1 = _mark(
        s3, "complete", completed_at="2026-01-01T01:00:00+00:00",
        tables=["a", "b"], table_versions={"a": "1"},
    )
    time.sleep(1.05)  # harvest 2 starts strictly later (minutes, in reality)
    _mark(s3, "in_progress", started_at="2026-01-02T00:00:00+00:00")
    for rel in ("tables/a.md", "tables/b.md", "index.md"):
        s3.delete_object(Bucket=BUCKET, Key=f"{PREFIX}{rel}")
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}tables/a.md", Body=_doc("A", "v2").encode())
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}tables/c.md", Body=_doc("C", "c").encode())
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}index.md", Body=b"# Index v2\n")
    v2 = _mark(
        s3, "complete", completed_at="2026-01-02T01:00:00+00:00",
        tables=["a", "c"], table_versions={"a": "2"},
    )
    return v1, v2


def _seed_freshness(ddb, vkey: str, updated_at: str) -> None:
    ddb.put_item(
        TableName=FRESHNESS,
        Item={
            "pk": {"S": f"VEC#{vkey}"},
            "sk": {"S": "SEQ"},
            "last_sequencer": {"S": "0000000000000000FF"},
            "updated_at": {"S": updated_at},
        },
    )


def _status_row(ddb):
    return ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"HARVEST#{DOMAIN}#{DATASET}"}, "sk": {"S": "STATUS"}},
    ).get("Item")


def _mapping_row(ddb):
    """The DOMAIN#/DATASET# mapping item — where the AR policy attrs live.

    A different partition from every other write in the repromote path (which
    all target ``HARVEST#<d>#<ds>``), and ``_write_history`` never creates it.
    """
    return ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"DOMAIN#{DOMAIN}"}, "sk": {"S": f"DATASET#{DATASET}"}},
    ).get("Item")


def _seed_mapping(ddb, **ar_attrs) -> None:
    ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": f"DOMAIN#{DOMAIN}"},
            "sk": {"S": f"DATASET#{DATASET}"},
            "data_domain": {"S": DOMAIN},
            "dataset": {"S": DATASET},
            "glue_database": {"S": DATASET},
            **{k: {"S": v} for k, v in ar_attrs.items()},
        },
    )


def _repromote(aws, version_id: str, *, events=None) -> dict:
    """Call the handler directly — the router owns no ``events`` client yet."""
    return handlers.repromote_bundle(
        aws["s3"],
        aws["ddb"],
        bucket=BUCKET,
        registry_table=REGISTRY,
        data_domain=DOMAIN,
        dataset=DATASET,
        version_id=version_id,
        requested_by="u@x.com",
        events=events,
    )


#: The repromote response is a UI contract (VersionHistory reads copied/deleted,
#: then polls convergence). The accelerator must not perturb it.
_REPROMOTE_KEYS = {
    "status",
    "copied",
    "deleted",
    "target_version_id",
    "new_version_id",
    "converged",
}


# --------------------------------------------------------------------------- #
# versions + diff
# --------------------------------------------------------------------------- #


def test_versions_and_diff_endpoints(cfg, aws):
    v1, v2 = _write_history(aws["s3"])

    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/versions"), cfg)
    assert resp["statusCode"] == 200
    versions = _json(resp)["versions"]
    assert [v["version_id"] for v in versions] == [v2, v1]
    assert versions[0]["current"] is True and versions[1]["current"] is False
    assert versions[1]["tables"] == ["a", "b"]
    assert versions[0]["repromoted_from"] is None

    # Bare diff = "what changed in the last harvest".
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/diff"), cfg)
    assert resp["statusCode"] == 200
    diff = _json(resp)
    assert diff["from"]["version_id"] == v1 and diff["to"]["version_id"] == v2
    assert diff["summary"]["added"] == 1 and diff["summary"]["removed"] == 1
    statuses = {f["key"]: f["status"] for f in diff["files"]}
    assert statuses[f"{PREFIX}tables/c.md"] == "added"
    assert statuses[f"{PREFIX}tables/b.md"] == "removed"

    # Explicit selectors + the live sentinel.
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/diff",
            query={"from": v1, "to": "live"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    assert _json(resp)["to"]["live"] is True

    # Unknown ids are a clean 400, not a 500.
    resp = app.route(
        _event("GET", f"/bundle/{DOMAIN}/{DATASET}/diff", query={"to": "nope"}), cfg
    )
    assert resp["statusCode"] == 400
    assert "unknown bundle version" in _json(resp)["error"]


def test_versions_empty_for_unharvested_dataset(cfg, aws):
    aws["s3"].put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/versions"), cfg)
    assert resp["statusCode"] == 200
    assert _json(resp)["versions"] == []
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/diff"), cfg)
    assert resp["statusCode"] == 400  # nothing published to diff


def test_read_bundle_file_at_version(cfg, aws):
    v1, _v2 = _write_history(aws["s3"])

    # The diff carries per-side version ids; reading the OLD side returns the
    # harvest-1 content even though the live file was rewritten...
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/diff"), cfg)
    files = {f["key"]: f for f in _json(resp)["files"]}
    a = files[f"{PREFIX}tables/a.md"]
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/file",
            query={"key": f"{PREFIX}tables/a.md", "version": a["old_version_id"]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    assert "v1" in _json(resp)["text"]

    # ...and a REMOVED file's old content stays readable under its delete marker.
    b = files[f"{PREFIX}tables/b.md"]
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/file",
            query={"key": f"{PREFIX}tables/b.md", "version": b["old_version_id"]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200

    # index.md is served bundle content -> readable (the version diff shows it);
    # dot-dirs stay rejected.
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/file",
            query={"key": f"{PREFIX}index.md"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/file",
            query={"key": f"{PREFIX}.harvest/state.json"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400

    # An unknown version id is a clean 404.
    resp = app.route(
        _event(
            "GET",
            f"/bundle/{DOMAIN}/{DATASET}/file",
            query={"key": f"{PREFIX}tables/a.md", "version": "no-such-version"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 404


# --------------------------------------------------------------------------- #
# repromote
# --------------------------------------------------------------------------- #


def test_repromote_end_to_end(cfg, aws):
    v1, v2 = _write_history(aws["s3"])
    s3, ddb = aws["s3"], aws["ddb"]
    time.sleep(1.05)  # the repromote happens strictly after harvest 2

    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": v1},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    out = _json(resp)
    assert out["status"] == "complete"
    assert out["copied"] == 3 and out["deleted"] == 1  # a, b, index / c
    assert out["target_version_id"] == v1
    assert out["converged"] is False
    new_vid = out["new_version_id"]

    # Live content is harvest 1's again; c.md is gone.
    body = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}tables/a.md")["Body"].read()
    assert b"v1" in body
    keys = {
        o["Key"]
        for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX).get("Contents", [])
    }
    assert f"{PREFIX}tables/c.md" not in keys and f"{PREFIX}tables/b.md" in keys

    # The head is a NEW version with repromote provenance; history is 3 deep.
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/versions"), cfg)
    versions = _json(resp)["versions"]
    assert [v["version_id"] for v in versions] == [new_vid, v2, v1]
    assert versions[0]["repromoted_from"] == v1
    assert versions[0]["repromoted_by"] == "u@x.com"

    # The lease rode queued -> complete with mode=repromote.
    row = _status_row(ddb)
    assert row["status"]["S"] == "complete" and row["mode"]["S"] == "repromote"

    # The bundle is ready again (fresh complete marker).
    resp = app.route(_event("GET", f"/harvest/{DOMAIN}/{DATASET}"), cfg)
    assert _json(resp)["ready"] is True

    # Convergence: no freshness rows yet -> converging with the full pending set.
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    assert resp["statusCode"] == 200
    status = _json(resp)
    assert status["state"] == "converging"
    assert status["total"] == 3 and status["done"] == 0  # a, b copied + c deleted
    started_at = status["started_at"]

    # Reindex catches up (rows advance past started_at) -> converged.
    for vkey in (
        f"{DOMAIN}/{DATASET}/tables/a",
        f"{DOMAIN}/{DATASET}/tables/b",
        f"{DOMAIN}/{DATASET}/tables/c",
    ):
        _seed_freshness(ddb, vkey, _now_iso())
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    status = _json(resp)
    assert status["state"] == "converged"
    assert status["done"] == 3 and status["pending"] == []
    assert status["started_at"] == started_at

    # Repromoting the current version is a 409; an unknown id a 400.
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": new_vid},
        ),
        cfg,
    )
    assert resp["statusCode"] == 409
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": "nope"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400


def test_repromote_respects_and_takes_over_leases(cfg, aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb = aws["ddb"]

    def _seed_lease(mode: str, started_at: str) -> None:
        ddb.put_item(
            TableName=REGISTRY,
            Item={
                "pk": {"S": f"HARVEST#{DOMAIN}#{DATASET}"},
                "sk": {"S": "STATUS"},
                "status": {"S": "queued"},
                "mode": {"S": mode},
                "started_at": {"S": started_at},
                "updated_at": {"S": started_at},
                "runtime_session_id": {"S": "x"},
            },
        )

    # A live full harvest blocks repromote with the standard 409.
    _seed_lease("full", _now_iso())
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": v1},
        ),
        cfg,
    )
    assert resp["statusCode"] == 409

    # A DEAD repromote (queued, mode=repromote, older than the writer's possible
    # lifetime) is taken over immediately — the one-click-retry path.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    _seed_lease("repromote", stale)
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": v1},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200

    # But a FRESH queued repromote row is a genuinely live run -> still 409.
    _seed_lease("repromote", _now_iso())
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": v1},
        ),
        cfg,
    )
    assert resp["statusCode"] == 409


def test_repromote_status_edge_states(cfg, aws):
    ddb = aws["ddb"]

    # Nothing recorded -> 404.
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    assert resp["statusCode"] == 404

    # A dead repromote lease -> stalled_lease + can_retry (no REPROMOTE item needed).
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": f"HARVEST#{DOMAIN}#{DATASET}"},
            "sk": {"S": "STATUS"},
            "status": {"S": "queued"},
            "mode": {"S": "repromote"},
            "started_at": {"S": stale},
            "updated_at": {"S": stale},
            "runtime_session_id": {"S": "repromote-dead"},
            "repromote_target": {"S": "dead-target-vid"},
        },
    )
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    assert resp["statusCode"] == 200
    status = _json(resp)
    assert status["state"] == "stalled_lease" and status["can_retry"] is True
    # The dead run's target rides along so the UI's retry knows what to re-POST.
    assert status["target_version_id"] == "dead-target-vid"

    # A completed repromote whose events never arrived -> stalled after 10 min.
    ddb.delete_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"HARVEST#{DOMAIN}#{DATASET}"}, "sk": {"S": "STATUS"}},
    )
    old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": f"HARVEST#{DOMAIN}#{DATASET}"},
            "sk": {"S": "REPROMOTE"},
            "started_at": {"S": old},
            "completed_at": {"S": old},
            "target_version_id": {"S": "tv"},
            "new_version_id": {"S": "nv"},
            "requested_by": {"S": "u@x.com"},
            "copied": {"L": [{"S": f"{DOMAIN}/{DATASET}/tables/a"}]},
            "deleted": {"L": []},
            "total": {"N": "1"},
        },
    )
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    status = _json(resp)
    assert status["state"] == "stalled" and status["can_retry"] is True
    assert status["pending"] == [f"{DOMAIN}/{DATASET}/tables/a"]

    # A freshness row that predates the repromote does NOT count as converged.
    _seed_freshness(
        ddb,
        f"{DOMAIN}/{DATASET}/tables/a",
        (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat(),
    )
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    assert _json(resp)["state"] == "stalled"
    # ...but one at/after started_at does.
    _seed_freshness(ddb, f"{DOMAIN}/{DATASET}/tables/a", _now_iso())
    resp = app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/repromote"), cfg)
    status = _json(resp)
    assert status["state"] == "converged" and status["done"] == 1


def test_cancelled_harvest_leaves_no_current_and_allows_restore(cfg, aws):
    # The rollback path CONVENTIONS documents for an interrupted harvest: the
    # cancel leaves the live marker at in_progress (uncommitted working files).
    # The newest published version must NOT be labeled current then — that
    # label made repromote refuse it ("already current"), leaving no way to
    # restore the last good state.
    v1, v2 = _write_history(aws["s3"])
    time.sleep(1.05)
    _mark(aws["s3"], "in_progress", started_at="2026-01-03T00:00:00+00:00")

    versions = _json(
        app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/versions"), cfg)
    )["versions"]
    assert [v["version_id"] for v in versions] == [v2, v1]
    assert all(v["current"] is False for v in versions)

    # Restoring the last good version is now accepted (was a 409).
    resp = app.route(
        _event(
            "POST",
            f"/bundle/{DOMAIN}/{DATASET}/repromote",
            body={"version_id": v2},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    # The repromote finished by writing a fresh complete marker: the bundle is
    # published again and the restored head is current.
    versions = _json(
        app.route(_event("GET", f"/bundle/{DOMAIN}/{DATASET}/versions"), cfg)
    )["versions"]
    assert versions[0]["current"] is True
    assert versions[0]["repromoted_from"] == v2


# --------------------------------------------------------------------------- #
# repromote -> AR policy rebuild accelerator
# --------------------------------------------------------------------------- #
#
# A repromote mutates the live wiki with no finalize, so a policy built off the
# superseded content is now describing the wrong bundle. The accelerator flags
# the mapping row stale + publishes one policy_rebuild event. It is an
# accelerator ONLY: correctness is the fingerprint gate plus the nightly
# reconcile, so every failure mode below must still return a clean repromote.


def test_repromote_flags_policy_stale_and_publishes_event(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb, events = aws["ddb"], FakeEvents()
    _seed_mapping(ddb, ar_build_status="ready", ar_source_hash="abc123")
    time.sleep(1.05)

    out = _repromote(aws, v1, events=events)

    assert out["status"] == "complete"
    assert _mapping_row(ddb)["ar_build_status"]["S"] == "stale"
    # The rule pattern (Source + detail-type) and the consumer's parse both key
    # off these exact strings — assert the whole entry, not just its presence.
    assert len(events.calls) == 1
    assert events.calls[0] == {
        "Entries": [
            {
                "Source": "okf.policy",
                "DetailType": "policy_rebuild",
                "Detail": json.dumps(
                    {
                        "data_domain": DOMAIN,
                        "dataset": DATASET,
                        "reason": "repromote",
                    }
                ),
            }
        ]
    }
    # No EventBusName -> the default bus, which is where the inbound rules live.
    assert "EventBusName" not in events.calls[0]["Entries"][0]


def test_repromote_response_shape_is_unchanged_by_the_accelerator(aws):
    v1, _v2 = _write_history(aws["s3"])
    _seed_mapping(aws["ddb"], ar_build_status="ready")
    time.sleep(1.05)

    out = _repromote(aws, v1, events=FakeEvents())

    assert set(out) == _REPROMOTE_KEYS


def test_repromote_survives_a_raising_events_client(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb, events = aws["ddb"], FakeEvents(raises=True)
    _seed_mapping(ddb, ar_build_status="ready")
    time.sleep(1.05)

    out = _repromote(aws, v1, events=events)

    # Identical outcome: the restore committed, the lease closed complete, and
    # the publish attempt is invisible to the caller.
    assert out["status"] == "complete"
    assert out["copied"] == 3 and out["deleted"] == 1
    assert set(out) == _REPROMOTE_KEYS
    assert _status_row(ddb)["status"]["S"] == "complete"
    assert len(events.calls) == 1  # attempted, then swallowed
    # The stale flag landed BEFORE the failing publish, so the nightly reconcile
    # still sees an invalidated policy.
    assert _mapping_row(ddb)["ar_build_status"]["S"] == "stale"


def test_repromote_survives_a_raising_registry_client(aws):
    v1, _v2 = _write_history(aws["s3"])
    _seed_mapping(aws["ddb"], ar_build_status="ready")
    time.sleep(1.05)

    class BoomOnUpdate:
        """The real ddb for everything the repromote needs; update_item raises.

        ``flag_stale`` is the only update_item the repromote path makes after
        the restore commits (the lease release goes through ``_set_status_row``,
        which swallows its own failures), so this isolates the accelerator.
        """

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def update_item(self, **kwargs):
            raise RuntimeError("registry unreachable")

    events = FakeEvents()
    boom = {"s3": aws["s3"], "ddb": BoomOnUpdate(aws["ddb"])}
    out = _repromote(boom, v1, events=events)

    assert out["status"] == "complete"
    assert set(out) == _REPROMOTE_KEYS
    # One try/except covers both halves: a failed flag abandons the publish too
    # (the reconcile is the backstop), and neither can fail the repromote.
    assert events.calls == []


def test_repromote_does_not_clobber_an_in_flight_build(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb, events = aws["ddb"], FakeEvents()
    # ``building`` IS the per-dataset build lease. Overwriting it with ``stale``
    # would re-arm the conditional flip and let a second build start.
    _seed_mapping(ddb, ar_build_status="building", ar_pending_source_hash="pending1")
    time.sleep(1.05)

    out = _repromote(aws, v1, events=events)

    assert out["status"] == "complete"
    row = _mapping_row(ddb)
    assert row["ar_build_status"]["S"] == "building"
    assert row["ar_pending_source_hash"]["S"] == "pending1"
    # The event still goes out: the in-flight build stamps a pending hash from
    # BEFORE the restore, so it lands stale on arrival and needs the rebuild.
    assert len(events.calls) == 1


def test_repromote_on_a_dataset_with_no_policy_publishes_but_stamps_nothing(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb, events = aws["ddb"], FakeEvents()
    _seed_mapping(ddb)  # registered dataset, never AR-built
    time.sleep(1.05)

    out = _repromote(aws, v1, events=events)

    assert out["status"] == "complete"
    # Never CREATE policy attrs: a phantom status would make the reconcile pick
    # up a dataset that has no policy at all.
    row = _mapping_row(ddb)
    assert not [k for k in row if k.startswith("ar_")]
    assert len(events.calls) == 1  # harmless: the consumer's own gate no-ops


def test_repromote_never_creates_a_missing_mapping_row(aws):
    v1, _v2 = _write_history(aws["s3"])  # deliberately no DOMAIN#/DATASET# row
    ddb = aws["ddb"]
    time.sleep(1.05)

    out = _repromote(aws, v1, events=FakeEvents())

    assert out["status"] == "complete"
    # UpdateItem upserts by default; an unconditional flag would resurrect the
    # mapping of a dataset that was deleted (or never registered).
    assert _mapping_row(ddb) is None


def test_repromote_tolerates_a_rejected_event_entry(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb = aws["ddb"]
    _seed_mapping(ddb, ar_build_status="ready")
    time.sleep(1.05)

    # PutEvents reports per-entry rejections inside a 200 response, so a clean
    # return is not delivery — and neither outcome may reach the caller.
    out = _repromote(aws, v1, events=FakeEvents(fail_entries=True))

    assert out["status"] == "complete"
    assert set(out) == _REPROMOTE_KEYS
    assert _mapping_row(ddb)["ar_build_status"]["S"] == "stale"


def test_repromote_flags_a_degraded_policy_stale_too(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb = aws["ddb"]
    _seed_mapping(ddb, ar_build_status="degraded")
    time.sleep(1.05)

    out = _repromote(aws, v1, events=FakeEvents())

    assert out["status"] == "complete"
    # ``degraded`` still renders verdicts, so it must be invalidated like ready.
    assert _mapping_row(ddb)["ar_build_status"]["S"] == "stale"


def test_repromote_without_an_events_client_is_a_total_no_op(aws):
    v1, _v2 = _write_history(aws["s3"])
    ddb = aws["ddb"]
    _seed_mapping(ddb, ar_build_status="ready")
    time.sleep(1.05)

    out = _repromote(aws, v1)  # the unwired default: policy builds disabled

    assert out["status"] == "complete"
    # No publisher means no rebuild authority listening, so flagging stale would
    # strand the policy unusable with nothing able to restore it.
    assert _mapping_row(ddb)["ar_build_status"]["S"] == "ready"

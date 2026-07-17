"""Annotation CRUD, user isolation, and the pre-flight orphan sweep."""

from __future__ import annotations

import json

import pytest

from control_api import app, handlers
from tests.conftest import ANNOTATIONS, BUCKET


def _event(method, path, *, body=None, query=None, claims=None):
    evt = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": claims
                    if claims is not None
                    else {"sub": "user-1", "email": "u@x.com"}
                }
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


def _seed_doc(cfg, concept_id="tables/races", body="The status 9 means refunds."):
    """Put a bundle doc so the orphan sweep has something to re-anchor against."""
    key = f"okf/sales/orders/{concept_id}.md"
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=(
            f"---\ntype: Glue Table\ntitle: T\ndescription: d\n---\n# Overview\n{body}\n"
        ).encode(),
    )


# --- CRUD + validation -----------------------------------------------------


def test_create_and_list_roundtrip(cfg):
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={
                "concept_id": "tables/races",
                "quote": "status 9 means refunds",
                "note": "9 is actually chargebacks",
            },
        ),
        cfg,
    )
    assert r["statusCode"] == 200
    created = _json(r)
    assert created["status"] == "open"
    assert created["author"] == "u@x.com"  # attribution from the JWT, not the body
    assert created["annotation_id"]

    r = app.route(_event("GET", "/annotations/sales/orders"), cfg)
    items = _json(r)
    assert len(items) == 1
    assert items[0]["note"] == "9 is actually chargebacks"


def test_create_requires_quote_and_note(cfg):
    r = app.route(
        _event("POST", "/annotations/sales/orders", body={"concept_id": "tables/races"}),
        cfg,
    )
    assert r["statusCode"] == 400


def test_create_rejects_bad_concept_id(cfg):
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "../etc/passwd", "quote": "q", "note": "n"},
        ),
        cfg,
    )
    assert r["statusCode"] == 400


def test_unauthenticated_caller_refused(cfg):
    # No sub in the claims -> 401, never a shared partition.
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "q", "note": "n"},
            claims={},
        ),
        cfg,
    )
    assert r["statusCode"] == 401


# --- user isolation --------------------------------------------------------


def test_annotations_are_user_scoped(cfg):
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "q", "note": "alice note"},
            claims={"sub": "alice", "email": "a@x.com"},
        ),
        cfg,
    )
    # Bob sees none of Alice's annotations.
    r = app.route(
        _event(
            "GET",
            "/annotations/sales/orders",
            claims={"sub": "bob", "email": "b@x.com"},
        ),
        cfg,
    )
    assert _json(r) == []
    # Alice still sees her own.
    r = app.route(
        _event(
            "GET",
            "/annotations/sales/orders",
            claims={"sub": "alice", "email": "a@x.com"},
        ),
        cfg,
    )
    assert len(_json(r)) == 1


def test_delete_scoped_and_404_for_stranger(cfg):
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "q", "note": "n"},
            claims={"sub": "alice"},
        ),
        cfg,
    )
    aid = _json(r)["annotation_id"]
    # Bob can't delete Alice's annotation (it's not in his partition) -> 404.
    r = app.route(
        _event(
            "DELETE",
            f"/annotations/sales/orders/{aid}",
            query={"concept": "tables/races"},
            claims={"sub": "bob"},
        ),
        cfg,
    )
    assert r["statusCode"] == 404
    # Alice can.
    r = app.route(
        _event(
            "DELETE",
            f"/annotations/sales/orders/{aid}",
            query={"concept": "tables/races"},
            claims={"sub": "alice"},
        ),
        cfg,
    )
    assert r["statusCode"] == 200 and _json(r)["deleted"] is True


# --- the orphan sweep / annotated harvest trigger --------------------------


def _run(cfg, claims=None):
    return app.route(
        _event("POST", "/harvest/sales/orders/annotations/run", claims=claims), cfg
    )


def test_run_invokes_with_only_live_annotations(cfg):
    _seed_doc(cfg)  # body contains "status 9 means refunds"
    # One grounded (quote present) + one orphan (quote absent).
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "status 9 means refunds",
                  "note": "wrong"},
        ),
        cfg,
    )
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "text that is gone",
                  "note": "orphan"},
        ),
        cfg,
    )
    r = _run(cfg)
    body = _json(r)
    assert body["status"] == "queued"
    assert body["annotations"] == 1 and body["orphaned"] == 1
    # Runtime invoked exactly once, in annotated mode, carrying only the survivor.
    assert len(cfg.agentcore.calls) == 1
    payload = json.loads(cfg.agentcore.calls[0]["payload"].decode())
    assert payload["mode"] == "annotated"
    assert payload["user_sub"] == "user-1"
    assert len(payload["annotations"]) == 1
    assert payload["annotations"][0]["quote"] == "status 9 means refunds"

    # The orphan was auto-resolved with a TTL; the survivor is in_review.
    items = {a["quote"]: a for a in _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))}
    assert items["text that is gone"]["status"] == "resolved"
    assert items["text that is gone"]["outcome"] == "orphaned"
    assert items["status 9 means refunds"]["status"] == "in_review"


def test_run_all_orphaned_skips_invoke_and_completes(cfg):
    _seed_doc(cfg, body="nothing relevant")
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "totally absent", "note": "x"},
        ),
        cfg,
    )
    r = _run(cfg)
    body = _json(r)
    assert body["status"] == "complete" and body["skipped"] is True
    assert body["orphaned"] == 1
    # The whole point: no expensive agent run when nothing can be applied.
    assert len(cfg.agentcore.calls) == 0
    # Lease released — status row is terminal (complete).
    st = handlers.get_harvest_status(
        cfg.s3, cfg.ddb, bucket=cfg.bucket, registry_table=cfg.registry_table,
        data_domain="sales", dataset="orders",
    )
    assert st["status"]["status"] == "complete"


# --- dataset guidance ------------------------------------------------------


def _seed_mapping(cfg, domain="sales", dataset="orders"):
    """Create the DATASET# mapping row so guidance get/set has a row to target."""
    handlers.declare_domain(cfg.ddb, registry_table=cfg.registry_table, data_domain=domain)
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=domain,
        dataset=dataset,
        glue_database=dataset,
    )


def _set_guidance(cfg, text, domain="sales", dataset="orders"):
    return app.route(
        _event("PUT", f"/guidance/{domain}/{dataset}", body={"guidance": text}), cfg
    )


def _get_guidance(cfg, domain="sales", dataset="orders"):
    return _json(app.route(_event("GET", f"/guidance/{domain}/{dataset}"), cfg))


def test_guidance_crud_marks_dirty(cfg):
    _seed_mapping(cfg)
    # Set guidance → it comes back and is DIRTY (never harvested at this version).
    r = _set_guidance(cfg, "  Focus on race results; ignore staging tables.  ")
    assert r["statusCode"] == 200
    saved = _json(r)
    assert saved["guidance"] == "Focus on race results; ignore staging tables."
    assert saved["guidance_dirty"] is True
    got = _get_guidance(cfg)
    assert got["guidance"] == "Focus on race results; ignore staging tables."
    assert got["guidance_dirty"] is True


def test_guidance_set_missing_mapping_is_404(cfg):
    # No DATASET# row → 404 (a stray dataset id).
    r = _set_guidance(cfg, "x", dataset="ghost")
    assert r["statusCode"] == 404


def test_dirty_guidance_forces_run_with_zero_annotations(cfg):
    # The key new behavior: a changed guidance re-harvests even with NO annotations
    # (today's logic would short-circuit and skip the agent).
    _seed_mapping(cfg)
    _set_guidance(cfg, "Decode the status column from the data dictionary.")
    r = _run(cfg)
    body = _json(r)
    assert body["status"] == "queued"
    assert body["annotations"] == 0
    assert body["guidance_applied"] is True
    # The agent WAS invoked, in annotated mode, carrying the guidance (no notes).
    assert len(cfg.agentcore.calls) == 1
    payload = json.loads(cfg.agentcore.calls[0]["payload"].decode())
    assert payload["mode"] == "annotated"
    assert payload["annotations"] == []
    assert payload["dataset_guidance"] == "Decode the status column from the data dictionary."
    assert payload["dataset_guidance_version"]  # the version being applied


def test_clean_guidance_still_skips_when_no_annotations(cfg):
    # Guidance present but NOT dirty (no updated version pending) → still skip.
    _seed_mapping(cfg)
    _set_guidance(cfg, "some guidance")
    # Simulate it already applied: stamp applied_version = current updated_at.
    got = _get_guidance(cfg)
    cfg.ddb.update_item(
        TableName=cfg.registry_table,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
        UpdateExpression="SET guidance_applied_version = :v",
        ExpressionAttributeValues={":v": {"S": got["guidance_updated_at"]}},
    )
    assert _get_guidance(cfg)["guidance_dirty"] is False
    r = _run(cfg)
    body = _json(r)
    assert body["status"] == "complete" and body["skipped"] is True
    assert len(cfg.agentcore.calls) == 0


def test_run_missing_doc_orphans_all(cfg):
    # No S3 doc at all for the concept -> every annotation on it is orphaned.
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/ghost", "quote": "anything", "note": "x"},
        ),
        cfg,
    )
    r = _run(cfg)
    assert _json(r)["skipped"] is True
    assert len(cfg.agentcore.calls) == 0


def test_run_is_user_scoped(cfg):
    # Alice's annotations don't get processed when Bob runs his (empty) batch.
    _seed_doc(cfg)
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "status 9 means refunds",
                  "note": "alice"},
            claims={"sub": "alice"},
        ),
        cfg,
    )
    r = _run(cfg, claims={"sub": "bob"})
    body = _json(r)
    # Bob has nothing open -> skip, and Alice's note is untouched (still open).
    assert body["skipped"] is True and body["annotations"] == 0
    assert len(cfg.agentcore.calls) == 0
    alice = _json(
        app.route(
            _event("GET", "/annotations/sales/orders", claims={"sub": "alice"}), cfg
        )
    )
    assert alice[0]["status"] == "open"


def _status(cfg):
    return handlers.get_harvest_status(
        cfg.s3, cfg.ddb, bucket=cfg.bucket, registry_table=cfg.registry_table,
        data_domain="sales", dataset="orders",
    )["status"]["status"]


def test_run_releases_lease_when_sweep_raises(cfg, monkeypatch):
    # A NON-404 S3 error during the orphan sweep must NOT leave the lease stuck at
    # `queued` (which would wedge the dataset for 8h). The trigger must catch it,
    # mark the row failed, and surface a 5xx.
    _seed_doc(cfg)
    app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "status 9 means refunds",
                  "note": "n"},
        ),
        cfg,
    )

    class _Boom(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "AccessDenied"}}

    def _raise(*a, **k):
        raise _Boom()

    monkeypatch.setattr(cfg.s3, "get_object", _raise)
    r = _run(cfg)
    assert r["statusCode"] >= 500  # surfaced as an error, not a silent wedge
    assert len(cfg.agentcore.calls) == 0
    # Lease released: the row is terminal (failed), so a retrigger is allowed.
    assert _status(cfg) == "failed"


def test_run_reclaims_stranded_in_review_notes(cfg):
    # A note left `in_review` by a prior run that died before finishing must be
    # reclaimed by the next run (the lease proves no run is active), not stranded.
    _seed_doc(cfg)
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "tables/races", "quote": "status 9 means refunds",
                  "note": "n"},
        ),
        cfg,
    )
    aid = _json(r)["annotation_id"]
    # Simulate the straggler: flip it to in_review directly in the table.
    from okf_core import annotations as anno
    cfg.ddb.update_item(
        TableName=ANNOTATIONS,
        Key={
            "pk": {"S": anno.annotation_pk("sales", "orders", "user-1")},
            "sk": {"S": anno.annotation_sk("tables/races", aid)},
        },
        UpdateExpression="SET #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": {"S": anno.STATUS_IN_REVIEW}},
    )
    body = _json(_run(cfg))
    # Reclaimed: it's a live survivor again and the run is dispatched for it.
    assert body["status"] == "queued" and body["annotations"] == 1
    assert len(cfg.agentcore.calls) == 1


def test_run_rejects_too_many_annotations(cfg, monkeypatch):
    # Bounding the payload: more than the per-run cap is a clean 400 up front (no
    # status flips), not an opaque invoke failure the user can never get past.
    monkeypatch.setattr(handlers, "_ANNO_RUN_MAX", 2)
    _seed_doc(cfg, body="alpha beta gamma delta epsilon zeta")
    for word in ("alpha", "beta", "gamma"):
        app.route(
            _event(
                "POST",
                "/annotations/sales/orders",
                body={"concept_id": "tables/races", "quote": word, "note": "n"},
            ),
            cfg,
        )
    r = _run(cfg)
    assert r["statusCode"] == 400
    assert "too many" in _json(r)["error"].lower()
    assert len(cfg.agentcore.calls) == 0
    # No survivor was flipped to in_review (cap check precedes the flip).
    items = _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))
    assert all(a["status"] == "open" for a in items)

"""mode="cross" trigger validation + the annotation run's scope filter.

Route-level (API GW v2 events through app.route) against moto S3/DynamoDB and
the Glue/AgentCore fakes, like test_app_router.py. Pins the Roadmap §5 OSS
contract: target resolution/validation, the payload's `target` block, the
guidance/RI omission on cross runs, and the external/-prefix annotation scope.
"""

from __future__ import annotations

import json

from control_api import app, handlers
from tests.conftest import BUCKET, REGISTRY


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


def _harvest_calls(cfg):
    """Runtime invokes that are actual harvests (the domain-declare and mapping
    setup also invoke the runtime — write_domain_doc / provision — so raw call
    counts would miscount)."""
    out = []
    for c in cfg.agentcore.calls:
        payload = json.loads(c["payload"].decode())
        if payload.get("mode") not in ("write_domain_doc", "provision"):
            out.append(payload)
    return out


def _declare_domain(cfg, domain, description=""):
    r = app.route(
        _event(
            "PUT",
            f"/domain-defs/{domain}",
            body={"description": description or f"{domain} domain", "context": ""},
        ),
        cfg,
    )
    assert r["statusCode"] == 200


def _map_dataset(cfg, domain, dataset):
    # Glue mapping: the dataset name must equal its Glue database (which must
    # exist in the FakeGlue fixture: sales_curated / f1_curated / orders).
    r = app.route(
        _event(
            "PUT",
            f"/domains/{domain}/datasets/{dataset}",
            body={"glue_database": dataset},
        ),
        cfg,
    )
    assert r["statusCode"] == 200


def _mark_ready(cfg, domain, dataset):
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{domain}/{dataset}/.harvest/state.json",
        Body=json.dumps({"status": "complete"}).encode(),
    )


def _setup_pair(cfg, *, ready=("sales/orders", "crm/sales_curated")):
    _declare_domain(cfg, "sales", "Revenue & order pipelines")
    _declare_domain(cfg, "crm", "Customer master data")
    _map_dataset(cfg, "sales", "orders")
    _map_dataset(cfg, "crm", "sales_curated")
    for pair in ready:
        d, ds = pair.split("/")
        _mark_ready(cfg, d, ds)


def _trigger_cross(cfg, *, target_domain="crm", target_dataset="sales_curated"):
    return app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "mode": "cross",
                "target_data_domain": target_domain,
                "target_dataset": target_dataset,
            },
        ),
        cfg,
    )


# --------------------------------------------------------------------------- #
# trigger validation
# --------------------------------------------------------------------------- #


def test_cross_requires_target_fields(cfg):
    _setup_pair(cfg)
    r = app.route(
        _event(
            "POST",
            "/harvest",
            body={"data_domain": "sales", "dataset": "orders", "mode": "cross"},
        ),
        cfg,
    )
    assert r["statusCode"] == 400
    assert "target_data_domain" in _json(r)["error"]
    assert _harvest_calls(cfg) == []


def test_cross_rejects_self_target(cfg):
    _setup_pair(cfg)
    r = _trigger_cross(cfg, target_domain="sales", target_dataset="orders")
    assert r["statusCode"] == 400
    assert "differ" in _json(r)["error"]


def test_cross_unknown_target_404(cfg):
    _setup_pair(cfg)
    r = _trigger_cross(cfg, target_dataset="nope")
    assert r["statusCode"] == 404
    assert _harvest_calls(cfg) == []


def test_cross_non_glue_target_400(cfg):
    _setup_pair(cfg)
    # A redshift-backed mapping (written directly — registration requires a full
    # connection, which is irrelevant to this check).
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "DOMAIN#crm"},
            "sk": {"S": "DATASET#warehouse"},
            "data_domain": {"S": "crm"},
            "dataset": {"S": "warehouse"},
            "source": {
                "M": {
                    "type": {"S": "redshift"},
                    "redshift_database": {"S": "dev"},
                }
            },
        },
    )
    r = _trigger_cross(cfg, target_dataset="warehouse")
    assert r["statusCode"] == 400
    assert "Glue-backed" in _json(r)["error"]


def test_cross_requires_source_bundle_ready(cfg):
    _setup_pair(cfg, ready=("crm/sales_curated",))
    r = _trigger_cross(cfg)
    assert r["statusCode"] == 409
    assert "sales/orders" in _json(r)["error"]
    assert _harvest_calls(cfg) == []


def test_cross_requires_target_bundle_ready(cfg):
    _setup_pair(cfg, ready=("sales/orders",))
    r = _trigger_cross(cfg)
    assert r["statusCode"] == 409
    assert "sales_curated" in _json(r)["error"]
    assert _harvest_calls(cfg) == []


def test_cross_rejects_same_glue_database_under_two_domains(cfg):
    # Glue registration forces dataset == glue_database, so the same dataset
    # name under two domains is the SAME physical data — "cross-referencing" it
    # would verify degenerate self-joins.
    _setup_pair(cfg)
    _map_dataset(cfg, "crm", "orders")  # crm/orders -> Glue db "orders" too
    _mark_ready(cfg, "crm", "orders")
    r = app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "mode": "cross",
                "target_data_domain": "crm",
                "target_dataset": "orders",
            },
        ),
        cfg,
    )
    assert r["statusCode"] == 400
    assert "SAME Glue database" in _json(r)["error"]
    assert _harvest_calls(cfg) == []


# --------------------------------------------------------------------------- #
# the invoke payload + lease row
# --------------------------------------------------------------------------- #


def test_cross_payload_carries_resolved_target_block(cfg):
    _setup_pair(cfg)
    r = _trigger_cross(cfg)
    assert r["statusCode"] == 200
    assert _json(r)["status"] == "queued"

    calls = _harvest_calls(cfg)
    assert len(calls) == 1
    payload = calls[0]
    assert payload["mode"] == "cross"
    target = payload["target"]
    assert target["data_domain"] == "crm"
    assert target["dataset"] == "sales_curated"
    assert target["source"]["type"] == "glue"
    assert target["source"]["glue_database"] == "sales_curated"
    # The target's declared-domain context rides along for the prompt.
    assert target["domain_description"] == "Customer master data"
    # And the source side's own descriptor/domain enrichment is unchanged.
    assert payload["source"]["glue_database"] == "orders"
    assert payload["domain_description"] == "Revenue & order pipelines"

    # The lease row records mode="cross" with a session id (fresh per trigger)
    # and the counterpart the run is against (for the UI's Target field).
    row = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )["Item"]
    assert row["mode"]["S"] == "cross"
    assert row["status"]["S"] == "queued"
    assert row["cross_target"]["S"] == "crm/sales_curated"
    assert row["runtime_session_id"]["S"].startswith("okf-sales-orders-")

    # And the status GET surfaces it.
    st = _json(app.route(_event("GET", "/harvest/sales/orders"), cfg))
    assert st["status"]["cross_target"] == "crm/sales_curated"


def test_cross_payload_omits_guidance_and_ri(cfg):
    _setup_pair(cfg)
    # Dataset guidance saved (and therefore dirty) + RI settings enabled — both
    # ride every other mode but must NOT ride a cross run.
    handlers.set_dataset_guidance(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        guidance="Ignore the staging_* tables.",
    )
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
        UpdateExpression="SET recursive_improvement = :ri",
        ExpressionAttributeValues={
            ":ri": {
                "M": {
                    "enabled": {"BOOL": True},
                    "questions_key": {"S": "benchmark/sales/orders/questions.csv"},
                    "max_iterations": {"N": "3"},
                }
            }
        },
    )
    r = _trigger_cross(cfg)
    assert r["statusCode"] == 200
    payload = _harvest_calls(cfg)[0]
    assert "dataset_guidance" not in payload
    assert "recursive_improvement" not in payload


def test_cross_respects_the_lease_409(cfg):
    _setup_pair(cfg)
    assert _trigger_cross(cfg)["statusCode"] == 200
    r = _trigger_cross(cfg)
    assert r["statusCode"] == 409
    assert len(_harvest_calls(cfg)) == 1


# --------------------------------------------------------------------------- #
# annotation run scope (dataset vs cross)
# --------------------------------------------------------------------------- #

_EXTERNAL_CONCEPT = "external/crm/sales_curated/joins/orders__customers"


def _seed_doc(cfg, concept_id, body):
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/sales/orders/{concept_id}.md",
        Body=(
            f"---\ntype: Reference\ntitle: T\ndescription: d\n---\n{body}\n"
        ).encode(),
    )


def _annotate(cfg, concept_id, quote, note="n"):
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": concept_id, "quote": quote, "note": note},
        ),
        cfg,
    )
    assert r["statusCode"] == 200


def _seed_scoped_annotations(cfg):
    _setup_pair(cfg)
    _seed_doc(cfg, "tables/races", "The grain is one row per race.")
    _seed_doc(cfg, _EXTERNAL_CONCEPT, "Joins on customer_id, 1:N.")
    _annotate(cfg, "tables/races", "one row per race", note="dataset note")
    _annotate(cfg, _EXTERNAL_CONCEPT, "Joins on customer_id", note="cross note")


def _run_annotations(cfg, scope=None, **extra):
    body = {**({"scope": scope} if scope else {}), **extra}
    return app.route(
        _event(
            "POST",
            "/harvest/sales/orders/annotations/run",
            body=body or None,
        ),
        cfg,
    )


def test_annotation_run_without_scope_applies_everything(cfg):
    _seed_scoped_annotations(cfg)
    r = _run_annotations(cfg)
    assert _json(r)["annotations"] == 2


def test_annotation_run_cross_scope_filters_to_external_docs(cfg):
    _seed_scoped_annotations(cfg)
    r = _run_annotations(cfg, scope="cross")
    body = _json(r)
    assert body["annotations"] == 1
    assert body["scope"] == "cross"
    payload = _harvest_calls(cfg)[0]
    assert [a["concept_id"] for a in payload["annotations"]] == [_EXTERNAL_CONCEPT]
    # Cross notes verify against the COUNTERPART's data — the payload widens the
    # run's session policy to its Glue database (derived from the concept ids).
    assert payload["extra_glue_databases"] == ["sales_curated"]
    # The out-of-scope note stays open (untouched) for a dataset-scoped run.
    items = {
        a["concept_id"]: a
        for a in _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))
    }
    assert items["tables/races"]["status"] == "open"
    assert items[_EXTERNAL_CONCEPT]["status"] == "in_review"


def test_annotation_run_dataset_scope_excludes_external_docs(cfg):
    _seed_scoped_annotations(cfg)
    r = _run_annotations(cfg, scope="dataset")
    body = _json(r)
    assert body["annotations"] == 1
    payload = _harvest_calls(cfg)[0]
    assert [a["concept_id"] for a in payload["annotations"]] == ["tables/races"]
    # No cross notes in this run -> no session-policy widening.
    assert "extra_glue_databases" not in payload


def test_scope_filter_reverts_out_of_scope_in_review_stragglers(cfg):
    # An in_review note from a dead prior run must not be stranded by the scope
    # filter: a user who always picks one scope would otherwise never gather it
    # again (open notes carry no TTL). Out-of-scope stragglers revert to open.
    _seed_scoped_annotations(cfg)
    items = _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))
    cross = next(a for a in items if a["concept_id"] == _EXTERNAL_CONCEPT)
    # Simulate the dead prior run: flip the cross note to in_review directly.
    cfg.ddb.update_item(
        TableName="okf-annotations",
        Key={
            "pk": {"S": "ANNO#sales#orders#user-1"},
            "sk": {"S": f"{_EXTERNAL_CONCEPT}#{cross['annotation_id']}"},
        },
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "in_review"}},
    )

    r = _run_annotations(cfg, scope="dataset")
    assert _json(r)["annotations"] == 1  # only the dataset-scoped note ran
    items = {
        a["concept_id"]: a
        for a in _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))
    }
    # The out-of-scope straggler was reverted to open, not silently dropped.
    assert items[_EXTERNAL_CONCEPT]["status"] == "open"


def test_no_run_carries_recursive_improvement(cfg):
    # The RI loop is RETIRED: even a dataset with a legacy saved
    # recursive_improvement map gets a plain harvest payload — the harvester can
    # no longer benchmark (Benchmark Studio is a separate run mode).
    _seed_scoped_annotations(cfg)
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
        UpdateExpression="SET recursive_improvement = :ri",
        ExpressionAttributeValues={
            ":ri": {
                "M": {
                    "enabled": {"BOOL": True},
                    "questions_key": {"S": "benchmark/sales/orders/questions.csv"},
                    "max_iterations": {"N": "3"},
                }
            }
        },
    )
    for scope in ("cross", "dataset"):
        r = _run_annotations(cfg, scope=scope)
        assert _json(r)["annotations"] == 1
        assert "recursive_improvement" not in _harvest_calls(cfg)[-1]
        # Release the lease so the next scope's run isn't refused with a 409
        # (the fake runtime never reports terminal).
        cfg.ddb.update_item(
            TableName=REGISTRY,
            Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": "complete"}},
        )


def test_annotation_run_rejects_unknown_scope(cfg):
    _seed_scoped_annotations(cfg)
    r = _run_annotations(cfg, scope="everything")
    assert r["statusCode"] == 400


def test_general_notes_ride_a_cross_scoped_run(cfg):
    # A `_dataset`-wide note is general feedback, valid for EITHER scope: the
    # picker may send it with a cross run, and it must pass the scope filter
    # (previously it was dataset-only and would be silently dropped). With no
    # pair-doc note in the selection, `cross_target` alone must keep the
    # session-policy widening — else the run couldn't verify anything.
    _seed_scoped_annotations(cfg)
    r = app.route(
        _event(
            "POST",
            "/annotations/sales/orders",
            body={"concept_id": "_dataset", "note": "docs overstate confidence"},
        ),
        cfg,
    )
    gid = _json(r)["annotation_id"]
    r = _run_annotations(
        cfg,
        scope="cross",
        annotation_ids=[gid],
        cross_target="crm/sales_curated",
    )
    body = _json(r)
    assert body["status"] == "queued" and body["annotations"] == 1
    payload = _harvest_calls(cfg)[0]
    assert [a["concept_id"] for a in payload["annotations"]] == ["_dataset"]
    # The pair scope names the target outright — its Glue database is included
    # even though no surviving note's concept id references it.
    assert payload["extra_glue_databases"] == ["sales_curated"]
    # The pair-doc and dataset notes were NOT selected: both stay open.
    items = {
        a["concept_id"]: a
        for a in _json(app.route(_event("GET", "/annotations/sales/orders"), cfg))
    }
    assert items[_EXTERNAL_CONCEPT]["status"] == "open"
    assert items["tables/races"]["status"] == "open"
    assert items["_dataset"]["status"] == "in_review"


def test_cross_target_validation(cfg):
    _seed_scoped_annotations(cfg)
    # cross_target without the cross scope is a client bug — refuse it.
    r = _run_annotations(cfg, scope="dataset", cross_target="crm/sales_curated")
    assert r["statusCode"] == 400
    # Malformed pair strings are refused before any lease/status work.
    for bad in ("no-slash", "a/b/c", "/b", "a/"):
        r = _run_annotations(cfg, scope="cross", cross_target=bad)
        assert r["statusCode"] == 400, bad
    assert len(_harvest_calls(cfg)) == 0
    assert _harvest_calls(cfg) == []


def test_list_domains_surfaces_cross_reference_signal(cfg):
    # The reindex-derived XREF row is what tells the REFERENCED dataset it is
    # cross-referenced at all (the pair docs live only in the initiating
    # bundle). Both directions ride the existing /domains listing.
    _setup_pair(cfg)
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "DOMAIN#crm"},
            "sk": {"S": "XREF#sales_curated#sales#orders"},
            "target_data_domain": {"S": "crm"},
            "target_dataset": {"S": "sales_curated"},
            "source_data_domain": {"S": "sales"},
            "source_dataset": {"S": "orders"},
            "updated_at": {"S": "t"},
        },
    )
    rows = _json(app.route(_event("GET", "/domains"), cfg))
    by_id = {(r["data_domain"], r["dataset"]): r for r in rows}
    # The XREF row must not be listed as a dataset mapping.
    assert sorted(by_id) == [("crm", "sales_curated"), ("sales", "orders")]
    assert by_id[("sales", "orders")]["cross_references"] == ["crm/sales_curated"]
    assert by_id[("crm", "sales_curated")]["cross_referenced_by"] == ["sales/orders"]
    # Absent for datasets with no pairs (no empty-list noise).
    assert "cross_referenced_by" not in by_id[("sales", "orders")]


def test_cross_scope_ignores_dirty_guidance(cfg):
    # Dirty guidance triggers a dataset-scoped run even with zero notes — but a
    # CROSS-scoped run with no cross notes must SKIP (guidance never applies to
    # cross docs), and the payload of a cross-scoped run carries no guidance.
    _setup_pair(cfg)
    _seed_doc(cfg, "tables/races", "The grain is one row per race.")
    handlers.set_dataset_guidance(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        guidance="Decode the status column.",
    )
    r = _run_annotations(cfg, scope="cross")
    body = _json(r)
    assert body["skipped"] is True
    assert _harvest_calls(cfg) == []

    # With a cross note present, the run goes out — still guidance-free.
    _seed_doc(cfg, _EXTERNAL_CONCEPT, "Joins on customer_id, 1:N.")
    _annotate(cfg, _EXTERNAL_CONCEPT, "Joins on customer_id")
    r = _run_annotations(cfg, scope="cross")
    assert _json(r)["annotations"] == 1
    payload = _harvest_calls(cfg)[0]
    assert "dataset_guidance" not in payload

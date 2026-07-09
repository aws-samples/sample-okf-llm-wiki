"""Router tests: full API GW HTTP API v2 (payload 2.0) events through route()."""

from __future__ import annotations

import json

import pytest

from control_api import app
from tests.conftest import BUCKET, REGISTRY


def _event(method: str, path: str, *, body=None, query=None, b64=False, claims=None):
    """Build a minimal API GW v2 payload-format-2.0 event.

    ``claims`` overrides the JWT authorizer claims (verified identity) so tests
    can act as different users; defaults to a single fixed user.
    """
    evt: dict = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            # The JWT authorizer injects verified claims here; handlers trust them.
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
        raw = body if isinstance(body, str) else json.dumps(body)
        if b64:
            import base64

            raw = base64.b64encode(raw.encode()).decode()
            evt["isBase64Encoded"] = True
        evt["body"] = raw
    return evt


def _json(resp):
    assert resp["headers"]["content-type"] == "application/json"
    return json.loads(resp["body"])


def _declare_domain(cfg, domain="sales"):
    """Pre-declare a domain so mapping tests pass the 'must be declared' guard."""
    resp = app.route(
        _event(
            "PUT", f"/domain-defs/{domain}", body={"description": "test", "context": ""}
        ),
        cfg,
    )
    assert resp["statusCode"] == 200


# --------------------------------------------------------------------------- #
# routing basics
# --------------------------------------------------------------------------- #


def test_unknown_path_404(cfg):
    resp = app.route(_event("GET", "/nope"), cfg)
    assert resp["statusCode"] == 404


def test_wrong_method_405(cfg):
    resp = app.route(_event("POST", "/domains"), cfg)
    assert resp["statusCode"] == 405


def test_options_preflight_204(cfg):
    resp = app.route(_event("OPTIONS", "/domains"), cfg)
    assert resp["statusCode"] == 204
    assert resp["headers"]["access-control-allow-origin"] == "*"


def test_cors_headers_on_success(cfg):
    resp = app.route(_event("GET", "/glue/databases"), cfg)
    assert resp["headers"]["access-control-allow-origin"] == "*"


# --------------------------------------------------------------------------- #
# each endpoint via the router
# --------------------------------------------------------------------------- #


def test_get_glue_databases(cfg):
    resp = app.route(_event("GET", "/glue/databases"), cfg)
    assert resp["statusCode"] == 200
    assert _json(resp)[0]["name"] == "sales_curated"


def test_get_domains_empty(cfg):
    resp = app.route(_event("GET", "/domains"), cfg)
    assert _json(resp) == []


def test_put_domain_then_list_and_delete(cfg):
    _declare_domain(cfg, "sales")
    put = app.route(
        _event(
            "PUT", "/domains/sales/datasets/orders", body={"glue_database": "orders"}
        ),
        cfg,
    )
    assert put["statusCode"] == 200
    assert _json(put)["glue_database"] == "orders"

    lst = _json(app.route(_event("GET", "/domains"), cfg))
    assert lst[0]["dataset"] == "orders"

    dele = app.route(_event("DELETE", "/domains/sales/datasets/orders"), cfg)
    assert _json(dele)["deleted"] is True
    assert _json(app.route(_event("GET", "/domains"), cfg)) == []


def test_delete_declared_domain_blocked_while_mapped_409(cfg):
    """A domain with a live dataset mapping cannot be deleted (would orphan assets)."""
    _declare_domain(cfg, "sales")
    put = app.route(
        _event(
            "PUT", "/domains/sales/datasets/orders", body={"glue_database": "orders"}
        ),
        cfg,
    )
    assert put["statusCode"] == 200

    # Attempt to delete the domain declaration while the mapping still exists.
    blocked = app.route(_event("DELETE", "/domain-defs/sales"), cfg)
    assert blocked["statusCode"] == 409
    # The domain declaration must still be present.
    still = app.route(_event("GET", "/domain-defs/sales"), cfg)
    assert still["statusCode"] == 200


def test_delete_declared_domain_succeeds_after_mappings_removed(cfg):
    """Once every mapping is deleted, the domain declaration can be removed."""
    _declare_domain(cfg, "sales")
    app.route(
        _event(
            "PUT", "/domains/sales/datasets/orders", body={"glue_database": "orders"}
        ),
        cfg,
    )
    # Remove the mapping first.
    app.route(_event("DELETE", "/domains/sales/datasets/orders"), cfg)
    # Now the domain declaration deletes cleanly.
    dele = app.route(_event("DELETE", "/domain-defs/sales"), cfg)
    assert dele["statusCode"] == 200
    assert _json(dele)["deleted"] is True
    assert app.route(_event("GET", "/domain-defs/sales"), cfg)["statusCode"] == 404


def test_put_domain_missing_body_field_400(cfg):
    resp = app.route(_event("PUT", "/domains/sales/datasets/orders", body={}), cfg)
    assert resp["statusCode"] == 400


def test_put_domain_with_source_object(cfg):
    # The first-class `source` object is accepted and surfaced on list.
    _declare_domain(cfg, "sales")
    put = app.route(
        _event(
            "PUT",
            "/domains/sales/datasets/orders",
            body={"source": {"type": "glue", "glue_database": "orders"}},
        ),
        cfg,
    )
    assert put["statusCode"] == 200
    assert _json(put)["source"] == {"type": "glue", "glue_database": "orders"}
    lst = _json(app.route(_event("GET", "/domains"), cfg))
    assert lst[0]["source"]["type"] == "glue"


def test_put_domain_unsupported_source_type_400(cfg):
    _declare_domain(cfg, "sales")
    resp = app.route(
        _event(
            "PUT",
            "/domains/sales/datasets/orders",
            body={"source": {"type": "redshift", "glue_database": "orders"}},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    # Nothing persisted.
    assert _json(app.route(_event("GET", "/domains"), cfg)) == []


def test_put_domain_dataset_must_equal_glue_database_400(cfg):
    # Dataset name is the Glue database name; a differing pair is unharvestable.
    resp = app.route(
        _event(
            "PUT",
            "/domains/sales/datasets/orders",
            body={"glue_database": "sales_curated"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    # Nothing persisted.
    assert _json(app.route(_event("GET", "/domains"), cfg)) == []


def test_put_domain_unknown_glue_database_404(cfg):
    _declare_domain(cfg, "sales")
    resp = app.route(
        _event("PUT", "/domains/sales/datasets/ghost", body={"glue_database": "ghost"}),
        cfg,
    )
    assert resp["statusCode"] == 404
    assert _json(app.route(_event("GET", "/domains"), cfg)) == []


def test_credentials_create_list_delete_roundtrip(cfg):
    # Create -> secret returned once.
    created = _json(
        app.route(_event("POST", "/credentials", body={"name": "agent-x"}), cfg)
    )
    assert created["client_secret"]
    cid = created["client_id"]

    # List -> present, no secret.
    lst = _json(app.route(_event("GET", "/credentials"), cfg))
    assert any(c["client_id"] == cid for c in lst)
    assert all("client_secret" not in c for c in lst)

    # Delete -> gone.
    dele = _json(app.route(_event("DELETE", f"/credentials/{cid}"), cfg))
    assert dele["deleted"] is True
    assert _json(app.route(_event("GET", "/credentials"), cfg)) == []


def test_credentials_create_requires_name_400(cfg):
    resp = app.route(_event("POST", "/credentials", body={}), cfg)
    assert resp["statusCode"] == 400


def test_credentials_created_by_comes_from_jwt_not_body(cfg):
    """created_by (the revoke-authorization owner) is the verified JWT identity,
    never a body-supplied value a caller could use to impersonate someone."""
    created = _json(
        app.route(
            _event(
                "POST",
                "/credentials",
                body={"name": "agent-x", "created_by": "attacker@evil.com"},
                claims={"sub": "u2", "email": "real@x.com"},
            ),
            cfg,
        )
    )
    cid = created["client_id"]
    # The persisted owner is the JWT email, not the spoofed body value.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"CRED#{cid}"}, "sk": {"S": "META"}},
    )["Item"]
    assert item["created_by"]["S"] == "real@x.com"


def test_credentials_delete_unknown_client_id_404(cfg):
    """Deleting an id this API never vended (e.g. the public SPA client id) 404s
    and never reaches Cognito — can't be used to delete the login client."""
    resp = app.route(_event("DELETE", "/credentials/the-spa-web-client-id"), cfg)
    assert resp["statusCode"] == 404
    assert cfg.cognito.delete_calls == []


def test_credentials_delete_cross_user_forbidden_403(cfg):
    """Alice creates a credential; Bob cannot revoke it via the router."""
    created = _json(
        app.route(
            _event(
                "POST",
                "/credentials",
                body={"name": "alice-agent"},
                claims={"sub": "a", "email": "alice@x.com"},
            ),
            cfg,
        )
    )
    cid = created["client_id"]

    bob = app.route(
        _event(
            "DELETE",
            f"/credentials/{cid}",
            claims={"sub": "b", "email": "bob@x.com"},
        ),
        cfg,
    )
    assert bob["statusCode"] == 403
    assert cid in cfg.cognito.clients  # still live

    alice = app.route(
        _event(
            "DELETE",
            f"/credentials/{cid}",
            claims={"sub": "a", "email": "alice@x.com"},
        ),
        cfg,
    )
    assert alice["statusCode"] == 200
    assert cid not in cfg.cognito.clients


def test_harvest_double_trigger_returns_409(cfg):
    """The router surfaces the per-dataset lease conflict as a 409."""
    first = app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    assert first["statusCode"] == 200
    second = app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    assert second["statusCode"] == 409


def test_credentials_create_500_when_not_configured(aws, glue, agentcore, cognito):
    # Pool/scope unset -> vending disabled -> 500 (not a crash).
    cfg = app.Config(
        bucket=BUCKET,
        registry_table=REGISTRY,
        freshness_table="okf-freshness",
        harvest_runtime_arn="",
        s3=aws["s3"],
        ddb=aws["ddb"],
        glue=glue,
        agentcore=agentcore,
        cognito=cognito,
        user_pool_id="",
        mcp_scope="",
    )
    resp = app.route(_event("POST", "/credentials", body={"name": "x"}), cfg)
    assert resp["statusCode"] == 500


def test_context_presign_and_list_and_delete(cfg):
    pre = app.route(
        _event(
            "POST",
            "/context/sales/orders/presign",
            body={"filename": "spec.pdf", "content_type": "application/pdf"},
        ),
        cfg,
    )
    body = _json(pre)
    assert body["key"] == "okf/sales/orders/.context/spec.pdf"

    # Simulate the browser having uploaded, then list + delete via the API.
    cfg.s3.put_object(Bucket=BUCKET, Key=body["key"], Body=b"data")
    lst = _json(app.route(_event("GET", "/context/sales/orders"), cfg))
    assert lst[0]["filename"] == "spec.pdf"

    dele = app.route(_event("DELETE", "/context/sales/orders/spec.pdf"), cfg)
    assert _json(dele)["deleted"] is True
    assert _json(app.route(_event("GET", "/context/sales/orders"), cfg)) == []


def test_presign_route_not_captured_as_filename(cfg):
    """POST .../presign hits the presign handler, not the {filename} delete route."""
    resp = app.route(
        _event("POST", "/context/sales/orders/presign", body={"filename": "a.txt"}),
        cfg,
    )
    assert resp["statusCode"] == 200
    assert _json(resp)["key"].endswith("/.context/a.txt")


def test_post_harvest_queues(cfg, agentcore):
    resp = app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    assert resp["statusCode"] == 200
    assert _json(resp)["status"] == "queued"
    # Full harvest -> fresh per-trigger session id (readable prefix, valid length).
    sid = agentcore.calls[-1]["runtimeSessionId"]
    assert sid.startswith("okf-sales-orders-")
    assert 33 <= len(sid) <= 256


def test_post_harvest_missing_field_400(cfg, agentcore):
    resp = app.route(_event("POST", "/harvest", body={"dataset": "orders"}), cfg)
    assert resp["statusCode"] == 400
    assert agentcore.calls == []


def test_post_harvest_omits_model_effort_by_default(cfg, agentcore):
    # No model/effort in the body -> not in the payload (runtime uses env default).
    app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    payload = agentcore.last_payload()
    assert "model" not in payload and "effort" not in payload


def test_post_harvest_forwards_valid_model_effort(cfg, agentcore):
    resp = app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "model": "openai.gpt-5.5",
                "effort": "high",
            },
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    payload = agentcore.last_payload()
    assert payload["model"] == "openai.gpt-5.5"
    assert payload["effort"] == "high"


def test_post_harvest_defaults_effort_when_only_model_given(cfg, agentcore):
    app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "model": "openai.gpt-5.5",
            },
        ),
        cfg,
    )
    # Catalog default effort for gpt-5.5 is xhigh.
    assert agentcore.last_payload()["effort"] == "xhigh"


def test_post_harvest_unknown_model_400(cfg, agentcore):
    resp = app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "model": "anthropic.made-up",
            },
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    assert agentcore.calls == []  # never invoked


def test_post_harvest_effort_not_offered_400(cfg, agentcore):
    # "max" is valid for Claude but NOT offered for gpt-5.5 -> reject.
    resp = app.route(
        _event(
            "POST",
            "/harvest",
            body={
                "data_domain": "sales",
                "dataset": "orders",
                "model": "openai.gpt-5.5",
                "effort": "max",
            },
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    assert agentcore.calls == []


def test_post_harvest_effort_without_model_400(cfg, agentcore):
    resp = app.route(
        _event(
            "POST",
            "/harvest",
            body={"data_domain": "sales", "dataset": "orders", "effort": "high"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    assert agentcore.calls == []


def test_post_harvest_unknown_dataset_404_before_invoke(cfg, agentcore):
    # A dataset with no same-named Glue database fails fast with a 404 and never
    # reaches the runtime (this is the european_football vs european_football_2
    # mismatch, caught at trigger time instead of inside the async job).
    resp = app.route(
        _event(
            "POST",
            "/harvest",
            body={"data_domain": "sport", "dataset": "european_football"},
        ),
        cfg,
    )
    assert resp["statusCode"] == 404
    assert agentcore.calls == []


def test_post_harvest_no_runtime_arn_500(aws, glue, agentcore):
    cfg = app.Config(
        bucket=BUCKET,
        registry_table=REGISTRY,
        freshness_table="okf-freshness",
        harvest_runtime_arn="",
        s3=aws["s3"],
        ddb=aws["ddb"],
        glue=glue,
        agentcore=agentcore,
    )
    resp = app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    assert resp["statusCode"] == 500


def test_get_harvest_status(cfg, agentcore):
    app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    resp = app.route(_event("GET", "/harvest/sales/orders"), cfg)
    body = _json(resp)
    assert body["status"]["status"] == "queued"
    assert body["ready"] is False
    # A queued row hasn't been stamped with model/effort yet (the runtime sets
    # them at `running`), so they come back empty (None, like other absent attrs).
    assert not body["status"]["model"]
    assert not body["status"]["effort"]


def test_get_harvest_status_surfaces_model_effort(cfg):
    # A running row stamped by the runtime carries the resolved model/effort.
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "HARVEST#sales#orders"},
            "sk": {"S": "STATUS"},
            "status": {"S": "running"},
            "mode": {"S": "full"},
            "model": {"S": "openai.gpt-5.5"},
            "effort": {"S": "xhigh"},
        },
    )
    body = _json(app.route(_event("GET", "/harvest/sales/orders"), cfg))
    assert body["status"]["model"] == "openai.gpt-5.5"
    assert body["status"]["effort"] == "xhigh"


def test_post_harvest_cancel_stops_and_frees(cfg, agentcore):
    """POST /harvest/{d}/{ds}/cancel stops the session and marks the row cancelled."""
    app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    resp = app.route(_event("POST", "/harvest/sales/orders/cancel"), cfg)
    assert resp["statusCode"] == 200
    body = _json(resp)
    assert body["cancelled"] is True
    assert body["status"] == "cancelled"
    assert len(agentcore.stop_calls) == 1

    # The status route now reflects the cancelled state.
    st = _json(app.route(_event("GET", "/harvest/sales/orders"), cfg))
    assert st["status"]["status"] == "cancelled"


def test_post_harvest_cancel_no_harvest_404(cfg):
    resp = app.route(_event("POST", "/harvest/sales/orders/cancel"), cfg)
    assert resp["statusCode"] == 404


def test_cancel_route_not_captured_by_status_route(cfg, agentcore):
    """POST .../cancel hits the cancel handler, not the GET status route with
    dataset='orders' (route ordering + method must both resolve correctly)."""
    app.route(
        _event("POST", "/harvest", body={"data_domain": "sales", "dataset": "orders"}),
        cfg,
    )
    # A GET on the cancel path is a real path but wrong method -> 405, proving
    # the cancel template matched (not swallowed as a {dataset} segment).
    resp = app.route(_event("GET", "/harvest/sales/orders/cancel"), cfg)
    assert resp["statusCode"] == 405


def test_get_harvest_events_route(cfg):
    """GET /harvest/{d}/{ds}/events returns the parsed feed for the run."""
    from tests.conftest import HARVEST_LOG_GROUP

    # Seed a running status row with a session id + a couple of step lines.
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "HARVEST#sales#orders"},
            "sk": {"S": "STATUS"},
            "status": {"S": "running"},
            "runtime_session_id": {"S": "sid-r"},
        },
    )
    line = "OKF_STEP " + json.dumps(
        {
            "ts": "t",
            "session_id": "sid-r",
            "seq": 1,
            "kind": "tool_call",
            "tool": "ls",
            "label": "Listing files",
            "agent": "main",
        }
    )
    cfg.logs._events[HARVEST_LOG_GROUP] = [{"message": line, "timestamp": 0}]

    resp = app.route(_event("GET", "/harvest/sales/orders/events"), cfg)
    assert resp["statusCode"] == 200
    body = _json(resp)
    assert body["events"][0]["label"] == "Listing files"
    assert body["next"] == 1
    assert body["done"] is False


def test_get_harvest_events_since_query_param(cfg):
    resp = app.route(
        _event("GET", "/harvest/sales/orders/events", query={"since": "5"}), cfg
    )
    # No status row -> empty, and the cursor echoes back so the client doesn't rewind.
    assert resp["statusCode"] == 200
    assert _json(resp)["next"] == 5


def test_events_route_not_captured_by_status_route(cfg):
    """/events must match the events route, not GET status with dataset='orders'."""
    resp = app.route(_event("POST", "/harvest/sales/orders/events"), cfg)
    # Real path, wrong method -> 405 proves the events template matched.
    assert resp["statusCode"] == 405


def test_bundle_list_file_and_graph_routes(cfg):
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/tables/orders.md",
        Body=b"---\ntitle: orders\ntype: Glue Table\n---\n[c](customers.md)",
    )
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/tables/customers.md",
        Body=b"---\ntitle: customers\ntype: Glue Table\n---\nx",
    )

    lst = _json(app.route(_event("GET", "/bundle/sales/orders"), cfg))
    assert {f["concept_id"] for f in lst} == {"tables/orders", "tables/customers"}

    f = _json(
        app.route(
            _event(
                "GET",
                "/bundle/sales/orders/file",
                query={"key": "okf/sales/orders/tables/orders.md"},
            ),
            cfg,
        )
    )
    assert "customers.md" in f["text"]

    g = _json(app.route(_event("GET", "/bundle/sales/orders/graph"), cfg))
    assert ("tables/orders", "tables/customers") in {
        (e["source"], e["target"]) for e in g["edges"]
    }


def test_bundle_file_requires_key_query(cfg):
    resp = app.route(_event("GET", "/bundle/sales/orders/file"), cfg)
    assert resp["statusCode"] == 400


def test_graph_and_file_not_captured_as_dataset(cfg):
    """/bundle/{d}/{ds}/graph must match the graph route, not list with ds='...'."""
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/tables/x.md",
        Body=b"---\ntitle: x\ntype: Glue Table\n---\n",
    )
    resp = app.route(_event("GET", "/bundle/sales/orders/graph"), cfg)
    assert resp["statusCode"] == 200
    assert "nodes" in _json(resp)


def test_url_encoded_path_params(cfg):
    resp = app.route(_event("DELETE", "/context/sales/orders/my%20file.txt"), cfg)
    # No such object, but delete is idempotent -> 200 with the decoded name.
    assert resp["statusCode"] == 200


def test_base64_body_decoded(cfg):
    _declare_domain(cfg, "sales")
    resp = app.route(
        _event(
            "PUT",
            "/domains/sales/datasets/orders",
            body={"glue_database": "orders"},
            b64=True,
        ),
        cfg,
    )
    assert resp["statusCode"] == 200


def test_invalid_json_body_400(cfg):
    resp = app.route(
        _event("PUT", "/domains/sales/datasets/orders", body="{not json"), cfg
    )
    assert resp["statusCode"] == 400

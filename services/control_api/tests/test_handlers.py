"""Unit tests for each Control API handler's core logic."""

from __future__ import annotations

import json

import pytest

from control_api import handlers
from control_api.handlers import ApiError
from okf_core.session import runtime_session_id
from tests.conftest import (
    BUCKET,
    FRESHNESS,
    HARVEST_ARN,
    HARVEST_LOG_GROUP,
    REGISTRY,
)
from tests.fakes import FakeGlue, FakeLogs

# --------------------------------------------------------------------------- #
# Glue databases
# --------------------------------------------------------------------------- #


def test_list_glue_databases_shape(glue):
    out = handlers.list_glue_databases(glue)
    assert out == [
        {"name": "sales_curated", "description": "sales"},
        {"name": "f1_curated", "description": None},
        {"name": "orders", "description": "orders"},
    ]


def test_list_glue_databases_paginates():
    g = FakeGlue([{"Name": f"db{i}"} for i in range(5)], page_size=2)
    out = handlers.list_glue_databases(g)
    assert [d["name"] for d in out] == ["db0", "db1", "db2", "db3", "db4"]


def test_assert_glue_database_exists_ok(glue):
    # Present database (even with no tables) is accepted, no raise.
    handlers.assert_glue_database_exists(glue, "sales_curated")


def test_assert_glue_database_exists_missing_404(glue):
    with pytest.raises(ApiError) as ei:
        handlers.assert_glue_database_exists(glue, "does_not_exist")
    assert ei.value.status == 404


# --------------------------------------------------------------------------- #
# Domain registry
# --------------------------------------------------------------------------- #


def test_upsert_and_list_domains(cfg):
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="sales_curated",
    )
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="racing",
        dataset="f1",
        glue_database="f1_curated",
    )
    # A non-domain item must not leak into list_domains.
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )

    rows = handlers.list_domains(cfg.ddb, registry_table=REGISTRY)
    by_ds = {r["dataset"]: r for r in rows}
    assert set(by_ds) == {"orders", "f1"}
    assert by_ds["orders"]["glue_database"] == "sales_curated"
    assert by_ds["orders"]["data_domain"] == "sales"
    assert by_ds["orders"]["created_at"]  # stamped
    # First-class source descriptor is stored and surfaced.
    assert by_ds["orders"]["source"] == {
        "type": "glue",
        "glue_database": "sales_curated",
    }


def test_upsert_writes_nested_source_object(cfg):
    result = handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="orders",
    )
    assert result["source"] == {"type": "glue", "glue_database": "orders"}
    # The raw item carries both the nested source map AND the flat back-compat attr.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
    )["Item"]
    assert item["source"]["M"]["type"]["S"] == "glue"
    assert item["source"]["M"]["glue_database"]["S"] == "orders"
    assert item["glue_database"]["S"] == "orders"  # back-compat mirror


def test_upsert_redshift_source_stores_generic_map_no_glue_mirror(cfg):
    # A redshift source is stored generically (its config keys under source.M) and
    # writes NO flat glue_database mirror (it isn't reached by the glue-event path).
    result = handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders_analytics",
        source={"type": "redshift", "redshift_database": "warehouse"},
    )
    assert result["source"] == {"type": "redshift", "redshift_database": "warehouse"}
    assert result["glue_database"] is None
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders_analytics"}},
    )["Item"]
    assert item["source"]["M"]["type"]["S"] == "redshift"
    assert item["source"]["M"]["redshift_database"]["S"] == "warehouse"
    assert "glue_database" not in item  # no mirror for a non-glue source


def test_assert_source_registrable_redshift_allows_distinct_dataset_name(cfg):
    # Redshift: dataset name is independent of the database; no equality rule, no
    # live probe. (A glue source would require dataset == glue_database.)
    handlers.assert_source_registrable(
        cfg.glue,
        dataset="orders_analytics",
        source={"type": "redshift", "redshift_database": "warehouse"},
    )  # does not raise


def test_assert_source_registrable_glue_requires_equality(cfg):
    with pytest.raises(handlers.SourceError, match="dataset must equal glue_database"):
        handlers.assert_source_registrable(
            cfg.glue,
            dataset="orders",
            source={"type": "glue", "glue_database": "different_db"},
        )


def test_list_redshift_targets_merges_clusters_and_workgroups(cfg):
    targets = handlers.list_redshift_targets(cfg.redshift, cfg.redshift_serverless)
    kinds = {(t["kind"], t["id"]) for t in targets}
    assert ("cluster", "prod-cluster") in kinds
    assert ("workgroup", "analytics-wg") in kinds


def test_list_redshift_databases_for_a_cluster(cfg):
    dbs = handlers.list_redshift_databases(
        cfg.redshift_data,
        cluster_identifier="prod-cluster",
        secret_arn="arn:aws:secretsmanager:eu-west-1:1:secret:x",
    )
    assert dbs == ["dev", "reporting"]


def test_list_redshift_databases_requires_target_and_secret(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.list_redshift_databases(cfg.redshift_data, secret_arn="s")
    assert ei.value.status == 400
    with pytest.raises(ApiError) as ei2:
        handlers.list_redshift_databases(
            cfg.redshift_data, workgroup_name="analytics-wg"
        )
    assert ei2.value.status == 400


def test_list_redshift_databases_unreachable_target_is_400(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.list_redshift_databases(
            cfg.redshift_data, cluster_identifier="ghost", secret_arn="s"
        )
    assert ei.value.status == 400


def test_list_domains_derives_source_for_legacy_rows(cfg):
    # A pre-`source` row (flat glue_database only) still yields a source object.
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "DOMAIN#sales"},
            "sk": {"S": "DATASET#legacy"},
            "data_domain": {"S": "sales"},
            "dataset": {"S": "legacy"},
            "glue_database": {"S": "legacy"},
            "created_at": {"S": "2026-01-01T00:00:00Z"},
        },
    )
    rows = handlers.list_domains(cfg.ddb, registry_table=REGISTRY)
    legacy = next(r for r in rows if r["dataset"] == "legacy")
    assert legacy["source"] == {"type": "glue", "glue_database": "legacy"}


def test_upsert_overwrites_and_stores_exact_keys(cfg):
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="old_db",
    )
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="new_db",
    )
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
    )["Item"]
    assert item["glue_database"]["S"] == "new_db"


def test_provision_dataset_dirs_invokes_runtime(cfg, agentcore):
    res = handlers.provision_dataset_dirs(
        agentcore,
        runtime_arn=HARVEST_ARN,
        data_domain="sport",
        dataset="spider2_ipl",
    )
    assert res == {"provisioned": True}
    call = agentcore.calls[-1]
    assert call["agentRuntimeArn"] == HARVEST_ARN
    # Deterministic (non-unique) session id — provisioning shares the dataset's
    # session, unlike a full harvest which uses a fresh one.
    assert call["runtimeSessionId"] == runtime_session_id("sport", "spider2_ipl")
    assert json.loads(call["payload"].decode()) == {
        "mode": "provision",
        "data_domain": "sport",
        "dataset": "spider2_ipl",
    }


def test_provision_dataset_dirs_no_runtime_is_noop(cfg, agentcore):
    res = handlers.provision_dataset_dirs(
        agentcore, runtime_arn="", data_domain="sport", dataset="spider2_ipl"
    )
    assert res["provisioned"] is False
    assert agentcore.calls == []  # never invoked


def test_provision_dataset_dirs_swallows_invoke_failure(cfg):
    class BoomAgentCore:
        calls: list = []

        def invoke_agent_runtime(self, **kwargs):
            raise RuntimeError("runtime unreachable")

    # A provisioning failure must never propagate (it can't block the mapping).
    res = handlers.provision_dataset_dirs(
        BoomAgentCore(), runtime_arn=HARVEST_ARN, data_domain="sport", dataset="x"
    )
    assert res["provisioned"] is False
    assert res["reason"] == "RuntimeError"


def test_delete_domain_mapping(cfg):
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="sales_curated",
    )
    handlers.delete_domain_mapping(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders"
    )
    assert handlers.list_domains(cfg.ddb, registry_table=REGISTRY) == []


def test_delete_domain_mapping_purges_bundle_and_freshness(cfg):
    """DELETE must remove the S3 bundle objects, freshness rows, and harvest
    status row — not just the mapping (the orphaned-bundle correctness bug)."""
    dd, ds = "sport", "european_football"
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain=dd,
        dataset=ds,
        glue_database="european_football",
    )
    # Seed a realistic bundle: concept docs, index, and dot-prefixed dirs.
    keys = [
        f"okf/{dd}/{ds}/index.md",
        f"okf/{dd}/{ds}/tables/player.md",
        f"okf/{dd}/{ds}/tables/match.md",
        f"okf/{dd}/{ds}/.harvest/state.json",
        f"okf/{dd}/{ds}/.context/notes.txt",
    ]
    for k in keys:
        cfg.s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")
    # A bundle for a DIFFERENT dataset must survive (prefix isolation).
    other = f"okf/{dd}/formula_1/tables/races.md"
    cfg.s3.put_object(Bucket=BUCKET, Key=other, Body=b"x")
    # Seed freshness rows this dataset owns + one belonging to another dataset.
    cfg.ddb.put_item(
        TableName=FRESHNESS,
        Item={
            "pk": {"S": f"TABLE#{dd}#{ds}#player"},
            "sk": {"S": "VERSION"},
            "version_id": {"S": "v1"},
        },
    )
    cfg.ddb.put_item(
        TableName=FRESHNESS,
        Item={
            "pk": {"S": f"VEC#{dd}/{ds}/tables/player"},
            "sk": {"S": "SEQ"},
            "last_sequencer": {"S": "0001"},
        },
    )
    cfg.ddb.put_item(
        TableName=FRESHNESS,
        Item={
            "pk": {"S": f"TABLE#{dd}#formula_1#races"},
            "sk": {"S": "VERSION"},
            "version_id": {"S": "v9"},
        },
    )
    # Seed the harvest status row.
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": f"HARVEST#{dd}#{ds}"},
            "sk": {"S": "STATUS"},
            "status": {"S": "complete"},
        },
    )

    res = handlers.delete_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain=dd,
        dataset=ds,
        s3=cfg.s3,
        bundle_bucket=BUCKET,
        freshness_table=FRESHNESS,
    )
    assert res["deleted"] is True
    assert res["purged_bundle_objects"] == len(keys)
    assert res["purged_freshness_rows"] == 2  # the two owned by this dataset

    # Mapping gone.
    assert handlers.list_domains(cfg.ddb, registry_table=REGISTRY) == []
    # This dataset's bundle objects gone; the other dataset's survives.
    remaining = cfg.s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    assert [o["Key"] for o in remaining] == [other]
    # Harvest status row gone.
    assert "Item" not in cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"HARVEST#{dd}#{ds}"}, "sk": {"S": "STATUS"}},
    )
    # Owned freshness rows gone; the other dataset's row survives.
    assert "Item" not in cfg.ddb.get_item(
        TableName=FRESHNESS,
        Key={"pk": {"S": f"TABLE#{dd}#{ds}#player"}, "sk": {"S": "VERSION"}},
    )
    assert "Item" in cfg.ddb.get_item(
        TableName=FRESHNESS,
        Key={"pk": {"S": f"TABLE#{dd}#formula_1#races"}, "sk": {"S": "VERSION"}},
    )


def test_delete_domain_mapping_idempotent_when_nothing_exists(cfg):
    """Deleting an absent dataset is a clean no-op (no bundle, no rows)."""
    res = handlers.delete_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="ghost",
        dataset="none",
        s3=cfg.s3,
        bundle_bucket=BUCKET,
        freshness_table=FRESHNESS,
    )
    assert res == {
        "deleted": True,
        "data_domain": "ghost",
        "dataset": "none",
        "purged_bundle_objects": 0,
        "purged_freshness_rows": 0,
    }


# --------------------------------------------------------------------------- #
# MCP machine credentials
# --------------------------------------------------------------------------- #


def test_create_credential_returns_secret_once_and_configures_client(cfg, cognito):
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    res = handlers.create_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        mcp_scope=MCP_SCOPE,
        registry_table=REGISTRY,
        name="analytics-agent",
        created_by="admin@example.com",
    )
    # Secret returned exactly here, client_id present.
    assert res["client_secret"]
    assert res["client_id"]
    assert res["name"] == "analytics-agent"

    # The Cognito client was created as client_credentials-only with the MCP scope.
    call = cognito.create_calls[-1]
    assert call["UserPoolId"] == USER_POOL_ID
    assert call["GenerateSecret"] is True
    assert call["AllowedOAuthFlows"] == ["client_credentials"]
    assert call["AllowedOAuthScopes"] == [MCP_SCOPE]

    # Metadata persisted, but NEVER the secret.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": f"CRED#{res['client_id']}"}, "sk": {"S": "META"}},
    )["Item"]
    assert item["name"]["S"] == "analytics-agent"
    assert item["created_by"]["S"] == "admin@example.com"
    assert "secret" not in item and "client_secret" not in item


def test_list_credentials_metadata_only(cfg):
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    for n in ("agent-a", "agent-b"):
        handlers.create_credential(
            cfg.cognito,
            cfg.ddb,
            user_pool_id=USER_POOL_ID,
            mcp_scope=MCP_SCOPE,
            registry_table=REGISTRY,
            name=n,
        )
    rows = handlers.list_credentials(cfg.ddb, registry_table=REGISTRY)
    assert {r["name"] for r in rows} == {"agent-a", "agent-b"}
    assert all("client_id" in r and "client_secret" not in r for r in rows)


def test_delete_credential_removes_client_and_row(cfg, cognito):
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    res = handlers.create_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        mcp_scope=MCP_SCOPE,
        registry_table=REGISTRY,
        name="tmp",
    )
    cid = res["client_id"]
    handlers.delete_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        registry_table=REGISTRY,
        client_id=cid,
    )
    assert cid not in cognito.clients
    assert handlers.list_credentials(cfg.ddb, registry_table=REGISTRY) == []


def test_delete_credential_unknown_client_404_and_no_cognito_call(cfg, cognito):
    from tests.conftest import USER_POOL_ID

    # A client_id with no CRED# registry row was NOT vended by this API. It must
    # be refused with a 404 and MUST NOT reach Cognito — otherwise a caller could
    # delete an arbitrary user-pool app client (e.g. the public SPA login client,
    # whose id is shipped in the UI bundle) and brick the console.
    with pytest.raises(ApiError) as ei:
        handlers.delete_credential(
            cfg.cognito,
            cfg.ddb,
            user_pool_id=USER_POOL_ID,
            registry_table=REGISTRY,
            client_id="the-spa-web-client-id",
        )
    assert ei.value.status == 404
    assert cognito.delete_calls == []  # never touched Cognito


def test_delete_credential_requires_matching_row_before_cognito(cfg, cognito):
    """Even a client that exists in Cognito is not deletable without a CRED# row."""
    from tests.conftest import USER_POOL_ID

    # Seed a Cognito client directly (as if it were some OTHER app client), with
    # no corresponding registry row.
    cognito.clients["external-client"] = {"ClientId": "external-client"}
    with pytest.raises(ApiError) as ei:
        handlers.delete_credential(
            cfg.cognito,
            cfg.ddb,
            user_pool_id=USER_POOL_ID,
            registry_table=REGISTRY,
            client_id="external-client",
        )
    assert ei.value.status == 404
    assert "external-client" in cognito.clients  # untouched
    assert cognito.delete_calls == []


def test_delete_credential_enforces_owner(cfg, cognito):
    """A user can only revoke a credential they created (created_by match)."""
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    res = handlers.create_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        mcp_scope=MCP_SCOPE,
        registry_table=REGISTRY,
        name="alice-agent",
        created_by="alice@x.com",
    )
    cid = res["client_id"]

    # Bob may not revoke Alice's credential.
    with pytest.raises(ApiError) as ei:
        handlers.delete_credential(
            cfg.cognito,
            cfg.ddb,
            user_pool_id=USER_POOL_ID,
            registry_table=REGISTRY,
            client_id=cid,
            caller="bob@x.com",
        )
    assert ei.value.status == 403
    assert cid in cognito.clients  # not revoked
    assert cognito.delete_calls == []

    # Alice can.
    ok = handlers.delete_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        registry_table=REGISTRY,
        client_id=cid,
        caller="alice@x.com",
    )
    assert ok["deleted"] is True
    assert cid not in cognito.clients


def test_delete_credential_no_caller_skips_owner_check(cfg, cognito):
    """When no caller identity is supplied (e.g. local/no-authorizer), the owner
    check is skipped but the CRED#-row requirement still holds."""
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    res = handlers.create_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        mcp_scope=MCP_SCOPE,
        registry_table=REGISTRY,
        name="svc",
        created_by="someone@x.com",
    )
    cid = res["client_id"]
    ok = handlers.delete_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=USER_POOL_ID,
        registry_table=REGISTRY,
        client_id=cid,  # caller=None
    )
    assert ok["deleted"] is True
    assert cid not in cognito.clients


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, "bad\x01name"])
def test_create_credential_rejects_bad_name(cfg, bad):
    from tests.conftest import MCP_SCOPE, USER_POOL_ID

    with pytest.raises(ApiError):
        handlers.create_credential(
            cfg.cognito,
            cfg.ddb,
            user_pool_id=USER_POOL_ID,
            mcp_scope=MCP_SCOPE,
            registry_table=REGISTRY,
            name=bad,
        )


# --------------------------------------------------------------------------- #
# .context/ docs
# --------------------------------------------------------------------------- #


def test_presign_context_upload_key_and_url(cfg):
    res = handlers.presign_context_upload(
        cfg.s3,
        bucket=BUCKET,
        data_domain="sales",
        dataset="orders",
        filename="notes.pdf",
        content_type="application/pdf",
    )
    assert res["key"] == "okf/sales/orders/.context/notes.pdf"
    assert res["url"].startswith("https://")
    assert res["expires_in"] == handlers.PRESIGN_EXPIRY_SECONDS
    # Presigned POST: fields carry the signed policy, and the size cap is exposed.
    assert isinstance(res["fields"], dict) and res["fields"]
    assert res["max_bytes"] == handlers.CONTEXT_UPLOAD_MAX_BYTES


def test_presign_enforces_content_length_range(cfg):
    # The signed policy must carry a content-length-range condition capping size
    # (threat #42) so S3 rejects an oversized body regardless of the client.
    import base64
    import json as _json

    res = handlers.presign_context_upload(
        cfg.s3,
        bucket=BUCKET,
        data_domain="sales",
        dataset="orders",
        filename="big.csv",
        content_type="text/csv",
    )
    policy = _json.loads(base64.b64decode(res["fields"]["policy"]))
    ranges = [
        c
        for c in policy["conditions"]
        if isinstance(c, list) and c and c[0] == "content-length-range"
    ]
    assert ranges, "presigned POST policy missing content-length-range"
    assert ranges[0][2] == handlers.CONTEXT_UPLOAD_MAX_BYTES
    # The object key is pinned in the signed fields (client can't relocate it).
    assert res["fields"]["key"] == "okf/sales/orders/.context/big.csv"


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", "", "..", "x\x00y"])
def test_presign_rejects_bad_filename(cfg, bad):
    with pytest.raises(ApiError) as ei:
        handlers.presign_context_upload(
            cfg.s3,
            bucket=BUCKET,
            data_domain="sales",
            dataset="orders",
            filename=bad,
            content_type=None,
        )
    assert ei.value.status == 400


def test_list_and_delete_context_docs(cfg):
    # Put two context docs + one bundle concept that must NOT appear.
    cfg.s3.put_object(
        Bucket=BUCKET, Key="okf/sales/orders/.context/spec.md", Body=b"spec"
    )
    cfg.s3.put_object(
        Bucket=BUCKET, Key="okf/sales/orders/.context/erd.png", Body=b"png"
    )
    cfg.s3.put_object(
        Bucket=BUCKET, Key="okf/sales/orders/tables/orders.md", Body=b"# orders"
    )

    docs = handlers.list_context_docs(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    names = sorted(d["filename"] for d in docs)
    assert names == ["erd.png", "spec.md"]
    assert all(d["key"].startswith("okf/sales/orders/.context/") for d in docs)

    handlers.delete_context_doc(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders", filename="erd.png"
    )
    docs2 = handlers.list_context_docs(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert [d["filename"] for d in docs2] == ["spec.md"]


# --------------------------------------------------------------------------- #
# Harvest trigger + status
# --------------------------------------------------------------------------- #


def test_trigger_harvest_invokes_runtime_and_writes_status(cfg, agentcore):
    res = handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert res == {"status": "queued", "data_domain": "sales", "dataset": "orders"}

    # AgentCore call shape. A FULL harvest uses a FRESH (unique) session id per
    # trigger, so we check the shape/length + readable prefix, not an exact match
    # — and that the status row records the SAME id that was sent.
    call = agentcore.calls[-1]
    assert call["agentRuntimeArn"] == HARVEST_ARN
    sent_session = call["runtimeSessionId"]
    assert sent_session.startswith("okf-sales-orders-")
    assert 33 <= len(sent_session) <= 256
    assert json.loads(call["payload"].decode()) == {
        "data_domain": "sales",
        "dataset": "orders",
        "mode": "full",
    }

    # Status row records the exact session id that was invoked.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )["Item"]
    assert item["status"]["S"] == "queued"
    assert item["mode"]["S"] == "full"
    assert item["runtime_session_id"]["S"] == sent_session
    assert item["started_at"]["S"]


def test_trigger_harvest_incremental_includes_changed_table(cfg, agentcore):
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="incremental",
        changed_table="customers",
    )
    assert agentcore.last_payload() == {
        "data_domain": "sales",
        "dataset": "orders",
        "mode": "incremental",
        "changed_table": "customers",
    }


def test_trigger_harvest_incremental_requires_changed_table(cfg, agentcore):
    with pytest.raises(ApiError) as ei:
        handlers.trigger_harvest(
            agentcore,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="sales",
            dataset="orders",
            mode="incremental",
        )
    assert ei.value.status == 400
    assert agentcore.calls == []  # never invoked the runtime


def test_trigger_harvest_threads_source_descriptor_from_mapping(cfg, agentcore):
    # A registered mapping's source descriptor rides the invocation payload so the
    # runtime dispatches on the source type (Phase D).
    handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        glue_database="orders",
    )
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert agentcore.last_payload()["source"] == {
        "type": "glue",
        "glue_database": "orders",
    }


def test_trigger_harvest_omits_source_when_no_mapping(cfg, agentcore):
    # No mapping row -> no source key (the runtime back-compat-defaults to glue).
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert "source" not in agentcore.last_payload()


def test_trigger_harvest_rejects_concurrent_same_dataset_409(cfg, agentcore):
    """A second trigger while one is queued/running is refused (per-dataset lease).

    Without this, a double-click starts two full harvests on the same bundle
    directory — one rm -rf's the tree while the other writes, corrupting the
    published bundle and the commit marker.
    """
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert len(agentcore.calls) == 1

    with pytest.raises(ApiError) as ei:
        handlers.trigger_harvest(
            agentcore,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="sales",
            dataset="orders",
            mode="full",
        )
    assert ei.value.status == 409
    # The second trigger never invoked the runtime.
    assert len(agentcore.calls) == 1


def test_trigger_harvest_allowed_again_after_terminal_state(cfg, agentcore):
    """Once a prior harvest reaches a terminal state, a fresh trigger is allowed."""
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    # Simulate the agent finishing.
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
        UpdateExpression="SET #s = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": {"S": "complete"}},
    )
    # A re-harvest is now allowed.
    res = handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert res["status"] == "queued"
    assert len(agentcore.calls) == 2


def test_trigger_harvest_takes_over_stale_lease(cfg, agentcore):
    """A lease older than the 8h AgentCore session cap is dead -> re-acquirable.

    Without stale-takeover a job whose terminal status write was lost would wedge
    the dataset on 409 forever.
    """
    from datetime import datetime, timezone, timedelta

    stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "HARVEST#sales#orders"},
            "sk": {"S": "STATUS"},
            "status": {"S": "running"},  # but started 9h ago
            "mode": {"S": "full"},
            "started_at": {"S": stale},
        },
    )
    res = handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert res["status"] == "queued"
    assert len(agentcore.calls) == 1


def test_trigger_harvest_fresh_running_lease_blocks_409(cfg, agentcore):
    """A recently-started running lease is NOT stale -> concurrent trigger 409s."""
    from datetime import datetime, timezone, timedelta

    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "HARVEST#sales#orders"},
            "sk": {"S": "STATUS"},
            "status": {"S": "running"},
            "mode": {"S": "full"},
            "started_at": {"S": fresh},
        },
    )
    with pytest.raises(ApiError) as ei:
        handlers.trigger_harvest(
            agentcore,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="sales",
            dataset="orders",
            mode="full",
        )
    assert ei.value.status == 409
    assert agentcore.calls == []


def test_trigger_harvest_writes_status_before_invoke(cfg, agentcore):
    """The status row exists before the runtime is invoked (no untracked harvest).

    We assert the lease row is present the moment the invoke fires by having the
    fake capture it — here we simply confirm the row is queued after a normal
    trigger, and (below) that a failing invoke releases the lease.
    """
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )["Item"]
    assert item["status"]["S"] == "queued"


def test_trigger_harvest_releases_lease_when_invoke_fails(cfg):
    """A failed invoke marks the row failed so a retry isn't blocked by our lease."""

    class BoomAgentCore:
        def __init__(self):
            self.calls = []

        def invoke_agent_runtime(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("runtime unavailable")

    boom = BoomAgentCore()
    with pytest.raises(RuntimeError):
        handlers.trigger_harvest(
            boom,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="sales",
            dataset="orders",
            mode="full",
        )
    # Lease released: the row is 'failed', not stuck 'queued'.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )["Item"]
    assert item["status"]["S"] == "failed"

    # And a subsequent trigger (with a working runtime) is allowed through.
    from tests.fakes import FakeAgentCore

    good = FakeAgentCore()
    res = handlers.trigger_harvest(
        good,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert res["status"] == "queued"
    assert len(good.calls) == 1


def test_get_harvest_status_ready_reflects_commit_marker(cfg, agentcore):
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    # No marker yet -> not ready.
    st = handlers.get_harvest_status(
        cfg.s3,
        cfg.ddb,
        bucket=BUCKET,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
    )
    assert st["status"]["status"] == "queued"
    assert st["ready"] is False

    # Write the commit marker -> ready.
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/.harvest/state.json",
        Body=json.dumps({"status": "complete"}).encode(),
    )
    st2 = handlers.get_harvest_status(
        cfg.s3,
        cfg.ddb,
        bucket=BUCKET,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
    )
    assert st2["ready"] is True


def test_get_harvest_status_no_row(cfg):
    st = handlers.get_harvest_status(
        cfg.s3,
        cfg.ddb,
        bucket=BUCKET,
        registry_table=REGISTRY,
        data_domain="none",
        dataset="none",
    )
    assert st["status"] == {}
    assert st["ready"] is False


# --------------------------------------------------------------------------- #
# Cancel harvest
# --------------------------------------------------------------------------- #


def _read_status(cfg, pk="HARVEST#sales#orders"):
    return cfg.ddb.get_item(
        TableName=REGISTRY, Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}}
    )["Item"]


def test_cancel_harvest_stops_session_and_frees_lease(cfg, agentcore):
    """A queued/running harvest is cancelled: the session is stopped, the row
    flips to `cancelled`, and (because that's terminal) a retrigger is allowed."""
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    sent_session = agentcore.calls[-1]["runtimeSessionId"]

    res = handlers.cancel_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
    )
    assert res["cancelled"] is True
    assert res["stopped_session"] is True
    assert res["status"] == "cancelled"

    # Stopped the EXACT session the trigger invoked (the fresh per-trigger id).
    assert len(agentcore.stop_calls) == 1
    stop = agentcore.stop_calls[-1]
    assert stop["runtimeSessionId"] == sent_session
    assert stop["agentRuntimeArn"] == HARVEST_ARN

    # Row is terminal `cancelled`.
    assert _read_status(cfg)["status"]["S"] == "cancelled"

    # Lease is free: a new trigger for the same dataset is allowed (not 409).
    res2 = handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    assert res2["status"] == "queued"


def test_cancel_harvest_running_status(cfg, agentcore):
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    # Simulate the runner having flipped queued -> running.
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
        UpdateExpression="SET #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": {"S": "running"}},
    )
    res = handlers.cancel_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
    )
    assert res["cancelled"] is True
    assert _read_status(cfg)["status"]["S"] == "cancelled"


def test_cancel_harvest_no_row_404(cfg, agentcore):
    with pytest.raises(ApiError) as ei:
        handlers.cancel_harvest(
            agentcore,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="none",
            dataset="none",
        )
    assert ei.value.status == 404
    assert agentcore.stop_calls == []  # never tried to stop a nonexistent session


def test_cancel_harvest_terminal_status_409(cfg, agentcore):
    """A completed harvest can't be cancelled — 409, and no session stop."""
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
        UpdateExpression="SET #s = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": {"S": "complete"}},
    )
    with pytest.raises(ApiError) as ei:
        handlers.cancel_harvest(
            agentcore,
            cfg.ddb,
            registry_table=REGISTRY,
            runtime_arn=HARVEST_ARN,
            data_domain="sales",
            dataset="orders",
        )
    assert ei.value.status == 409
    assert agentcore.stop_calls == []


def test_cancel_harvest_stop_failure_still_frees_lease(cfg):
    """If StopRuntimeSession raises, we still mark the row cancelled (best-effort
    stop): the lease must never be wedged by a dead/unreachable session."""
    from tests.fakes import FakeAgentCore

    class StopBoom(FakeAgentCore):
        def stop_runtime_session(self, **kwargs):
            self.stop_calls.append(kwargs)
            raise RuntimeError("session gone")

    ac = StopBoom()
    handlers.trigger_harvest(
        ac,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )
    res = handlers.cancel_harvest(
        ac,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
    )
    assert res["cancelled"] is True
    assert res["stopped_session"] is False
    assert res["stop_error"] == "RuntimeError"
    assert _read_status(cfg)["status"]["S"] == "cancelled"


def test_cancel_harvest_loses_race_to_terminal_write(cfg, agentcore, monkeypatch):
    """If the runner writes a terminal state between our read and our conditional
    write, cancel does NOT clobber it — it reports the real finished status."""
    handlers.trigger_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
        mode="full",
    )

    # Wrap update_item so that the instant before cancel's conditional write, a
    # concurrent `complete` lands — making the condition (queued/running) fail.
    real_update = cfg.ddb.update_item
    state = {"raced": False}

    def racing_update(**kwargs):
        cond = kwargs.get("ConditionExpression", "")
        if (
            "cancelled" in str(kwargs.get("ExpressionAttributeValues", {}))
            and not state["raced"]
        ):
            state["raced"] = True
            real_update(
                TableName=REGISTRY,
                Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
                UpdateExpression="SET #s = :c",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":c": {"S": "complete"}},
            )
        return real_update(**kwargs)

    monkeypatch.setattr(cfg.ddb, "update_item", racing_update)

    res = handlers.cancel_harvest(
        agentcore,
        cfg.ddb,
        registry_table=REGISTRY,
        runtime_arn=HARVEST_ARN,
        data_domain="sales",
        dataset="orders",
    )
    assert res["cancelled"] is False
    assert res["status"] == "complete"
    assert _read_status(cfg)["status"]["S"] == "complete"


# --------------------------------------------------------------------------- #
# Harvest live step feed (get_harvest_events)
# --------------------------------------------------------------------------- #


def _seed_status(cfg, *, session_id, status="running", started_at=""):
    """Write a HARVEST STATUS row with a session id (the feed correlation key)."""
    item = {
        "pk": {"S": "HARVEST#sales#orders"},
        "sk": {"S": "STATUS"},
        "status": {"S": status},
        "runtime_session_id": {"S": session_id},
    }
    if started_at:
        item["started_at"] = {"S": started_at}
    cfg.ddb.put_item(TableName=REGISTRY, Item=item)


def _step_line(session_id, seq, kind, *, ts_ms=None, **extra):
    """Build one OKF_STEP <json> CloudWatch message the runtime would emit.

    ``ts_ms`` is the CloudWatch event timestamp (defaults to seq for simplicity in
    tests that don't care about the scan window).
    """
    rec = {
        "ts": "2026-07-07T00:00:00Z",
        "session_id": session_id,
        "seq": seq,
        "kind": kind,
    }
    rec.update(extra)
    return {
        "message": f"OKF_STEP {json.dumps(rec, separators=(',', ':'))}",
        "timestamp": seq if ts_ms is None else ts_ms,
    }


def test_get_harvest_events_returns_parsed_steps(cfg):
    sid = "okf-sales-orders-abc"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="Planning", agent="main"),
                _step_line(
                    sid,
                    2,
                    "tool_call",
                    tool="run_sql",
                    label="Running an Athena query",
                    agent="main",
                ),
                _step_line(sid, 3, "tool_result", ok=True, agent="main"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert [e["seq"] for e in res["events"]] == [1, 2, 3]
    assert res["events"][1]["tool"] == "run_sql"
    assert res["events"][2]["ok"] is True
    assert res["next"] == 3
    assert res["done"] is False


def test_get_harvest_events_preserves_agent_full_markdown(cfg):
    """An agent event's `full` markdown survives parsing so the UI can render it
    in a modal; a short event without `full` stays lean."""
    sid = "sid-full"
    _seed_status(cfg, session_id=sid)
    md = "Plan:\n\n- a\n- b"
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="Plan: - a - b", full=md),
                _step_line(sid, 2, "agent", label="Short one"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"][0]["full"] == md
    assert "full" not in res["events"][1]


def test_get_harvest_events_since_cursor_filters(cfg):
    sid = "sid-1"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="a"),
                _step_line(sid, 2, "agent", label="b"),
                _step_line(sid, 3, "agent", label="c"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=2,
    )
    assert [e["seq"] for e in res["events"]] == [3]
    assert res["next"] == 3


def test_get_harvest_events_done_on_terminal_status(cfg):
    sid = "sid-2"
    _seed_status(cfg, session_id=sid, status="complete")
    logs = FakeLogs({HARVEST_LOG_GROUP: [_step_line(sid, 1, "agent", label="done")]})
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["done"] is True
    assert [e["seq"] for e in res["events"]] == [1]


def test_get_harvest_events_ignores_other_sessions(cfg):
    """A step line from a DIFFERENT session (a prior run) is never returned."""
    sid = "sid-current"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="mine"),
                _step_line("sid-OTHER", 2, "agent", label="theirs"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    # Only the current session's line survives (the fake's substring filter would
    # match on "OKF_STEP" alone, so the handler's per-line session guard matters).
    assert [e["label"] for e in res["events"]] == ["mine"]


def test_get_harvest_events_no_session_yet_is_empty(cfg):
    """A queued harvest with no session id yet yields an empty feed, not an error."""
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": "HARVEST#sales#orders"},
            "sk": {"S": "STATUS"},
            "status": {"S": "queued"},
        },
    )
    logs = FakeLogs({HARVEST_LOG_GROUP: []})
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"] == []
    assert res["next"] == 0


def test_get_harvest_events_no_log_group_is_empty(cfg):
    _seed_status(cfg, session_id="sid")
    res = handlers.get_harvest_events(
        FakeLogs(),
        cfg.ddb,
        registry_table=REGISTRY,
        log_group="",
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"] == []


def test_get_harvest_events_paginates(cfg):
    sid = "sid-page"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, i, "agent", label=f"s{i}") for i in range(1, 8)
            ]
        }
    )
    logs.page_size = 3  # force 3 pages
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert [e["seq"] for e in res["events"]] == [1, 2, 3, 4, 5, 6, 7]
    assert len(logs.calls) >= 3  # walked multiple pages


def test_get_harvest_events_malformed_lines_skipped(cfg):
    sid = "sid-bad"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                {"message": f"OKF_STEP not-json {sid}", "timestamp": 1},
                _step_line(sid, 5, "agent", label="good"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert [e["seq"] for e in res["events"]] == [5]


def test_get_harvest_events_swallows_logs_failure(cfg):
    """If FilterLogEvents raises, the feed returns empty (never 500s)."""
    sid = "sid-boom"
    _seed_status(cfg, session_id=sid)

    class BoomLogs(FakeLogs):
        def filter_log_events(self, **kwargs):
            raise RuntimeError("logs unavailable")

    res = handlers.get_harvest_events(
        BoomLogs(),
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"] == []
    assert res["next"] == 0


def test_get_harvest_events_preserves_call_id(cfg):
    """call_id (the tool-call<->result correlation key) survives parsing so the
    UI can pair them into one row."""
    sid = "sid-callid"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(
                    sid,
                    1,
                    "tool_call",
                    tool="run_sql",
                    label="Running an Athena query",
                    call_id="rid-9",
                ),
                _step_line(sid, 2, "tool_result", ok=True, call_id="rid-9"),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"][0]["call_id"] == "rid-9"
    assert res["events"][1]["call_id"] == "rid-9"


def test_get_harvest_events_preserves_subagent_fields(cfg):
    """Sub-agent fleet fields (phase/batch/sub_id/subagent_type) survive parsing
    so the UI can render idle->active->done squares."""
    sid = "sid-fleet"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(
                    sid,
                    1,
                    "subagent",
                    phase="start",
                    batch="call_1",
                    sub_id="ptc_a",
                    subagent_type="reviewer",
                    label="reviewer: v",
                ),
                _step_line(
                    sid, 2, "subagent", phase="complete", batch="call_1", sub_id="ptc_a"
                ),
                _step_line(
                    sid, 3, "subagent", phase="error", batch="call_1", sub_id="ptc_b"
                ),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    evs = res["events"]
    assert (
        evs[0]["phase"] == "start"
        and evs[0]["batch"] == "call_1"
        and evs[0]["subagent_type"] == "reviewer"
    )
    assert evs[1]["phase"] == "complete" and evs[1]["sub_id"] == "ptc_a"
    assert evs[2]["phase"] == "error" and evs[2]["sub_id"] == "ptc_b"


def test_get_harvest_events_preserves_usage_snapshot(cfg):
    """The cumulative token-usage object on a kind=usage event survives parsing
    so the UI can show a running total."""
    sid = "sid-usage"
    _seed_status(cfg, session_id=sid)
    usage = {
        "input": 1500,
        "output": 300,
        "cache_read": 900,
        "cache_write": 100,
        "total": 1800,
    }
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "usage", usage=usage),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
    )
    assert res["events"][0]["kind"] == "usage"
    assert res["events"][0]["usage"] == usage


def test_get_harvest_events_preserves_benchmark_progress_fields(cfg):
    """A kind=benchmark_progress event keeps its phase + current/total counters so
    the UI can render a live progress row."""
    sid = "sid-bench"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(
                    sid,
                    1,
                    "benchmark_progress",
                    label="Benchmark round 1/5 — solving 30/66",
                    phase="solving",
                    iteration=0,
                    max_iterations=5,
                    current=30,
                    total=66,
                ),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs, cfg.ddb, registry_table=REGISTRY, log_group=HARVEST_LOG_GROUP,
        data_domain="sales", dataset="orders", since=0,
    )
    ev = res["events"][0]
    assert ev["kind"] == "benchmark_progress"
    assert ev["phase"] == "solving"
    assert ev["current"] == 30 and ev["total"] == 66
    assert ev["iteration"] == 0 and ev["max_iterations"] == 5
    assert ev["label"].startswith("Benchmark round 1/5")


def test_get_harvest_events_preserves_benchmark_kpi_fields(cfg):
    """A kind=benchmark round-summary keeps its KPI fields + improvements list."""
    sid = "sid-bench-kpi"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(
                    sid, 1, "benchmark",
                    label="Benchmark round 2/5 done — EX 0.71",
                    phase="done", iteration=1, max_iterations=5,
                    ex_score=0.71, judge_accuracy=0.83,
                    passed=34, failed=14, discarded=2, graded=48,
                    target_met=False, has_review=True,
                    improvements=["document that status is an int code"],
                ),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs, cfg.ddb, registry_table=REGISTRY, log_group=HARVEST_LOG_GROUP,
        data_domain="sales", dataset="orders", since=0,
    )
    ev = res["events"][0]
    assert ev["kind"] == "benchmark"
    assert ev["ex_score"] == 0.71 and ev["judge_accuracy"] == 0.83
    assert ev["passed"] == 34 and ev["graded"] == 48
    assert ev["target_met"] is False
    assert ev["has_review"] is True
    assert ev["improvements"] == ["document that status is an int code"]


def test_get_harvest_events_returns_next_ts_high_water(cfg):
    """The response echoes the highest CloudWatch event ts as next_ts."""
    sid = "sid-ts"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="a", ts_ms=1000),
                _step_line(sid, 2, "agent", label="b", ts_ms=2500),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
        since_ts=0,
    )
    assert res["next_ts"] == 2500
    assert [e["seq"] for e in res["events"]] == [1, 2]


def test_get_harvest_events_since_ts_bounds_scan_window(cfg):
    """A live poll passes startTime = since_ts - overlap, so old events are
    excluded server-side (not just filtered by seq)."""
    sid = "sid-win"
    _seed_status(cfg, session_id=sid)
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="old", ts_ms=1000),
                _step_line(sid, 2, "agent", label="new", ts_ms=100000),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=1,
        since_ts=100000,
    )
    # startTime = 100000 - 5000 overlap = 95000, so only the ts=100000 event is
    # even scanned; the ts=1000 event is outside the window.
    assert logs.calls[0]["startTime"] == 95000
    assert [e["label"] for e in res["events"]] == ["new"]


def test_get_harvest_events_first_load_backfills_from_started_at(cfg):
    """On first load (since_ts=0), startTime is derived from the run's started_at
    so a viewer who opens the page mid-run gets the WHOLE current run."""
    sid = "sid-first"
    # started_at well before every step's ts, so nothing is clipped.
    _seed_status(cfg, session_id=sid, started_at="2026-07-07T00:00:00+00:00")
    base = 1783382400000  # 2026-07-07T00:00:00Z in ms
    logs = FakeLogs(
        {
            HARVEST_LOG_GROUP: [
                _step_line(sid, 1, "agent", label="first", ts_ms=base + 1000),
                _step_line(sid, 2, "agent", label="second", ts_ms=base + 2000),
            ]
        }
    )
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
        since_ts=0,
    )
    # startTime floored at started_at (base), so both mid-run steps come back.
    assert logs.calls[0]["startTime"] == base
    assert [e["label"] for e in res["events"]] == ["first", "second"]


def test_get_harvest_events_no_started_at_scans_all(cfg):
    """If started_at is absent/unparseable, the first poll omits startTime (scans
    all) — the safe pre-optimization behavior, never clips the run."""
    sid = "sid-nostart"
    _seed_status(cfg, session_id=sid)  # no started_at
    logs = FakeLogs({HARVEST_LOG_GROUP: [_step_line(sid, 1, "agent", label="x")]})
    res = handlers.get_harvest_events(
        logs,
        cfg.ddb,
        registry_table=REGISTRY,
        log_group=HARVEST_LOG_GROUP,
        data_domain="sales",
        dataset="orders",
        since=0,
        since_ts=0,
    )
    assert "startTime" not in logs.calls[0]
    assert [e["seq"] for e in res["events"]] == [1]


# --------------------------------------------------------------------------- #
# Bundle browsing
# --------------------------------------------------------------------------- #


def _seed_bundle(s3):
    s3.put_object(Bucket=BUCKET, Key="okf/sales/orders/index.md", Body=b"idx")
    s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/datasets/orders.md",
        Body=b"---\ntitle: Orders DB\ntype: Glue Database\n---\nbody",
    )
    s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/tables/orders.md",
        Body=b"---\ntitle: orders\ntype: Glue Table\n---\nSee [customers](customers.md).",
    )
    s3.put_object(
        Bucket=BUCKET,
        Key="okf/sales/orders/tables/customers.md",
        Body=b"---\ntitle: customers\ntype: Glue Table\n---\nrefs [orders](orders.md) and [ext](http://x/y.md).",
    )
    # Non-concepts that must be skipped.
    s3.put_object(Bucket=BUCKET, Key="okf/sales/orders/.context/spec.md", Body=b"x")
    s3.put_object(Bucket=BUCKET, Key="okf/sales/orders/.harvest/state.json", Body=b"{}")


def test_list_bundle_files_only_concepts(cfg):
    _seed_bundle(cfg.s3)
    files = handlers.list_bundle_files(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    ids = sorted(f["concept_id"] for f in files)
    assert ids == ["datasets/orders", "tables/customers", "tables/orders"]
    # index.md, .context/, .harvest/ all excluded.
    assert all(not f["key"].endswith("index.md") for f in files)


def test_read_bundle_file_ok(cfg):
    _seed_bundle(cfg.s3)
    res = handlers.read_bundle_file(
        cfg.s3,
        bucket=BUCKET,
        data_domain="sales",
        dataset="orders",
        key="okf/sales/orders/tables/orders.md",
    )
    assert "See [customers]" in res["text"]
    assert res["key"] == "okf/sales/orders/tables/orders.md"


def test_read_bundle_file_rejects_key_outside_bundle(cfg):
    _seed_bundle(cfg.s3)
    # A .context upload is not a concept -> rejected.
    with pytest.raises(ApiError) as ei:
        handlers.read_bundle_file(
            cfg.s3,
            bucket=BUCKET,
            data_domain="sales",
            dataset="orders",
            key="okf/sales/orders/.context/spec.md",
        )
    assert ei.value.status == 400


def test_read_bundle_file_rejects_other_dataset(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.read_bundle_file(
            cfg.s3,
            bucket=BUCKET,
            data_domain="sales",
            dataset="orders",
            key="okf/other/ds/tables/x.md",
        )
    assert ei.value.status == 400


def test_read_bundle_file_missing_object_404(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.read_bundle_file(
            cfg.s3,
            bucket=BUCKET,
            data_domain="sales",
            dataset="orders",
            key="okf/sales/orders/tables/ghost.md",
        )
    assert ei.value.status == 404


def test_build_graph_json_nodes_and_edges():
    files = {
        "tables/orders": "---\ntitle: orders\ntype: Glue Table\n---\nSee [customers](customers.md).",
        "tables/customers": "---\ntitle: customers\ntype: Glue Table\n---\nrefs [orders](orders.md) and [gone](ghost.md) and [ext](http://x/y.md).",
    }
    g = handlers.build_graph_json(files)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert set(nodes) == {"tables/orders", "tables/customers"}
    assert nodes["tables/orders"]["title"] == "orders"
    assert nodes["tables/orders"]["type"] == "Glue Table"

    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("tables/orders", "tables/customers") in edges
    assert ("tables/customers", "tables/orders") in edges
    # Dangling target (ghost) and external link are dropped.
    assert all(t in nodes for _, t in edges)


def test_bundle_graph_from_s3(cfg):
    _seed_bundle(cfg.s3)
    g = handlers.bundle_graph(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    ids = {n["id"] for n in g["nodes"]}
    assert {"datasets/orders", "tables/orders", "tables/customers"} == ids
    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("tables/orders", "tables/customers") in edges

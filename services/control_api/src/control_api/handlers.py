"""Pure endpoint handlers for the Control API.

Every function here takes its boto3 clients (and config) as arguments so it can
be exercised with moto (S3/DynamoDB/Glue) and small fakes (bedrock-agentcore)
and never touches live AWS or ``os.environ``. The thin router in ``app`` builds
the real clients from env and forwards the parsed path/body/query.

The S3 layout, DynamoDB item shapes, env-var names, and the harvest payload all
come from ``docs/CONVENTIONS.md`` and are reused via ``okf_aws`` / ``okf_core``
rather than re-encoded here.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datetime import timedelta

from okf_aws import bundle_prefix, is_bundle_ready, parse_bundle_key, state_marker_key
from okf_core.domain import DOMAIN_DATASET
from okf_core.links import extract_links_with_headings
from okf_core.session import HARVEST_LEASE_STALE_SECONDS, runtime_session_id
from okf_core.sources import (
    SourceError,
    build_glue_source,
    normalize_source,
    source_glue_database,
)

# Boto3's URL expiry for context-upload presigns. Long enough for a browser
# upload of a source doc, short enough that a leaked URL ages out.
PRESIGN_EXPIRY_SECONDS = 900

# Max size for a .context/ upload (threat #42: an oversized upload blows up the
# harvest Claude context / cost). Enforced by S3 itself via the presigned-POST
# ``content-length-range`` condition, so a client cannot bypass it by editing the
# request — S3 rejects the PUT with EntityTooLarge. 20 MiB is generous for a
# source doc (PDF/markdown/CSV) while bounding the worst case.
CONTEXT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024

# ``.context/`` holds user-uploaded source docs (CONVENTIONS.md S3 layout). It is
# a dot-prefixed dir so it is NOT a concept and is never embedded.
_CONTEXT_DIRNAME = ".context"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    """A handler-level error carrying the HTTP status to surface to the client.

    Handlers raise this for expected 4xx conditions (bad key, missing field);
    the router turns it into a JSON error body. Unexpected exceptions become a
    500 in the router.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _require(body: dict[str, Any] | None, field: str) -> Any:
    if not isinstance(body, dict) or body.get(field) in (None, ""):
        raise ApiError(400, f"missing required field: {field}")
    return body[field]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Glue databases
# --------------------------------------------------------------------------- #


def list_glue_databases(glue) -> list[dict[str, Any]]:
    """List every Glue database as ``[{name, description}]`` (paginated).

    Feeds the UI's "pick a Glue database to map to a dataset" dropdown. Glue
    pages at 100 databases per call via ``NextToken``.
    """
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["NextToken"] = token
        resp = glue.get_databases(**kwargs)
        for db in resp.get("DatabaseList", []):
            out.append({"name": db.get("Name"), "description": db.get("Description")})
        token = resp.get("NextToken")
        if not token:
            break
    return out


def assert_glue_database_exists(glue, database: str) -> None:
    """Raise ``ApiError(404)`` unless a Glue database of this name exists.

    The harvest runtime resolves a *dataset* to a Glue database of the SAME name
    (CONVENTIONS.md: the invocation payload carries only ``data_domain``/
    ``dataset`` and the runtime does ``GetTables(DatabaseName=dataset)``). We
    front-run that exact call here so a typo or a not-yet-loaded database
    surfaces as an immediate 404 at registration / trigger time instead of a
    deep ``EntityNotFoundException`` inside a background harvest job.

    ``GetTables`` (not ``GetDatabase``) is used deliberately: it mirrors the
    runtime's call and needs no IAM action beyond the ``glue:GetTables`` the
    Control API already holds. An existing-but-empty database is allowed (tables
    may be loaded later); only a missing database is rejected.
    """
    try:
        glue.get_tables(DatabaseName=database)
    except Exception as e:  # noqa: BLE001 - map a missing database to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "EntityNotFoundException":
            raise ApiError(404, f"no such Glue database: {database!r}") from e
        raise


# --------------------------------------------------------------------------- #
# Domain -> dataset registry (okf-registry)
# --------------------------------------------------------------------------- #


def list_domains(ddb, *, registry_table: str) -> list[dict[str, Any]]:
    """All domain->dataset mappings: registry items with ``pk`` begins_with DOMAIN#
    AND ``sk`` begins_with DATASET# (tightened so declared-domain META rows aren't
    leaked into the mapping list).

    Uses Scan with a ``begins_with`` filter because the registry is tiny (a
    handful of dataset mappings for the demo) and there is no GSI on the item
    type. Returns the raw mapping attrs the UI needs.
    """
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "FilterExpression": "begins_with(pk, :d) AND begins_with(sk, :ds)",
        "ExpressionAttributeValues": {
            ":d": {"S": "DOMAIN#"},
            ":ds": {"S": "DATASET#"},
        },
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            items.append(
                {
                    "data_domain": _s(item.get("data_domain")),
                    "dataset": _s(item.get("dataset")),
                    # First-class source descriptor ({type, ...config}); derived
                    # from the flat glue_database for legacy rows.
                    "source": _source_from_item(item),
                    # Kept for back-compat with existing UI/readers.
                    "glue_database": _s(item.get("glue_database")),
                    "created_at": _s(item.get("created_at")),
                }
            )
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return items


# --------------------------------------------------------------------------- #
# Declared domains (first-class domain entities with description + context)
# --------------------------------------------------------------------------- #


def declare_domain(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    description: str = "",
    context: str = "",
) -> dict[str, Any]:
    """Create or update a declared domain (``DOMAIN#<d> / META``).

    A domain must be declared before any Glue database can be mapped into it
    (the mapping guard in ``_r_upsert_domain`` verifies this). The item shape:
    ``pk=DOMAIN#<data_domain>``, ``sk=META``, attrs ``{data_domain, description,
    context, created_at, updated_at}``. ``created_at`` is preserved on update;
    ``updated_at`` is always refreshed.
    """
    now = _now_iso()
    # Preserve created_at on updates (conditional SET if absent).
    ddb.update_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": "META"}},
        UpdateExpression=(
            "SET data_domain = :dd, description = :desc, #ctx = :ctx, "
            "updated_at = :now"
            " , created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeNames={"#ctx": "context"},
        ExpressionAttributeValues={
            ":dd": {"S": data_domain},
            ":desc": {"S": description},
            ":ctx": {"S": context},
            ":now": {"S": now},
        },
    )
    return {
        "data_domain": data_domain,
        "description": description,
        "context": context,
    }


def get_domain(ddb, *, registry_table: str, data_domain: str) -> dict[str, Any] | None:
    """Return the declared-domain META item, or None if not declared."""
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": "META"}},
    )
    item = resp.get("Item")
    if not item:
        return None
    return {
        "data_domain": _s(item.get("data_domain")) or data_domain,
        "description": _s(item.get("description")) or "",
        "context": _s(item.get("context")) or "",
        "created_at": _s(item.get("created_at")) or "",
        "updated_at": _s(item.get("updated_at")) or "",
    }


def list_declared_domains(ddb, *, registry_table: str) -> list[dict[str, Any]]:
    """All declared domains (``sk = META`` under ``DOMAIN#*``).

    Also auto-backfills: any ``DOMAIN#<d>`` partition that has DATASET# mappings
    but no META row gets a stub declaration with empty description/context.
    """
    meta_items: list[dict[str, Any]] = []
    # Track domains that have a META row vs those that only have mappings.
    declared: set[str] = set()
    mapping_domains: set[str] = set()

    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "FilterExpression": "begins_with(pk, :d)",
        "ExpressionAttributeValues": {":d": {"S": "DOMAIN#"}},
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            sk = _s(item.get("sk")) or ""
            domain = _s(item.get("data_domain")) or ""
            if not domain:
                # Derive from pk if data_domain attr is missing (legacy).
                pk = _s(item.get("pk")) or ""
                domain = pk.removeprefix("DOMAIN#")
            if sk == "META":
                declared.add(domain)
                meta_items.append(
                    {
                        "data_domain": domain,
                        "description": _s(item.get("description")) or "",
                        "context": _s(item.get("context")) or "",
                        "created_at": _s(item.get("created_at")) or "",
                        "updated_at": _s(item.get("updated_at")) or "",
                    }
                )
            elif sk.startswith("DATASET#"):
                mapping_domains.add(domain)
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start

    # Auto-backfill: create stub META for domains that only have mappings.
    for domain in sorted(mapping_domains - declared):
        declare_domain(ddb, registry_table=registry_table, data_domain=domain)
        meta_items.append(
            {
                "data_domain": domain,
                "description": "",
                "context": "",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
        )

    meta_items.sort(key=lambda x: x.get("data_domain", ""))
    return meta_items


def delete_declared_domain(
    ddb, *, registry_table: str, data_domain: str
) -> dict[str, Any]:
    """Delete a declared domain. Blocks (409) if any datasets are still mapped.

    The caller must remove all mappings first (``DELETE /domains/{d}/datasets/{ds}``
    per dataset) before the domain declaration itself can be deleted.
    """
    # Check for live mappings under this domain. Use a QUERY on the partition
    # (not a scan): the mapping rows share the domain's pk and carry
    # ``sk begins_with "DATASET#"``, so a Query with that key condition returns
    # exactly this domain's mappings and nothing else.
    #
    # Do NOT add a ``Limit`` here. On a ``scan`` with a FilterExpression,
    # ``Limit`` caps items examined BEFORE the filter runs, so ``Limit=1`` reads
    # one arbitrary item (almost never the mapping) and wrongly reports "no
    # mappings" — which let a domain be deleted while datasets were still mapped,
    # orphaning the bundle + vectors. The Query key condition avoids that, and
    # the partition is tiny (a handful of datasets), so reading it fully is cheap
    # and unambiguous.
    resp = ddb.query(
        TableName=registry_table,
        KeyConditionExpression="pk = :pk AND begins_with(sk, :ds)",
        ExpressionAttributeValues={
            ":pk": {"S": f"DOMAIN#{data_domain}"},
            ":ds": {"S": "DATASET#"},
        },
    )
    if resp.get("Items"):
        raise ApiError(
            409,
            f"cannot delete domain '{data_domain}': datasets are still mapped to it"
            " — delete all mappings first",
        )
    ddb.delete_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": "META"}},
    )
    return {"deleted": True, "data_domain": data_domain}


def assert_domain_declared(ddb, *, registry_table: str, data_domain: str) -> None:
    """Raise ``ApiError(400)`` unless the domain has been declared (META exists).

    Called by the mapping upsert guard so a mapping cannot be created for a
    domain that hasn't been pre-declared.
    """
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": "META"}},
        ProjectionExpression="pk",
    )
    if not resp.get("Item"):
        raise ApiError(
            400,
            f"domain '{data_domain}' has not been declared — create it first via "
            "PUT /domain-defs/{domain}",
        )


def upsert_domain_mapping(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    glue_database: str,
) -> dict[str, Any]:
    """Create/replace the DOMAIN#<domain> / DATASET#<dataset> registry item.

    Item shape (CONVENTIONS.md): attrs ``data_domain, dataset, source,
    glue_database, created_at``. ``source`` is the first-class, future-extensible
    source descriptor — a nested map ``{type, ...config}`` (see
    ``okf_core.sources``); today the only type is ``glue``. The flat
    ``glue_database`` attribute is ALSO written for back-compat: the harvest
    payload and the incremental scan (which filters on ``glue_database``) still
    read it directly, so no consumer needs to change in lockstep. We PutItem
    (full overwrite) since the mapping is small and PUT matches the REST verb.
    """
    source = build_glue_source(glue_database)
    item = {
        "pk": {"S": f"DOMAIN#{data_domain}"},
        "sk": {"S": f"DATASET#{dataset}"},
        "data_domain": {"S": data_domain},
        "dataset": {"S": dataset},
        "source": {
            "M": {
                "type": {"S": source["type"]},
                "glue_database": {"S": source["glue_database"]},
            }
        },
        "glue_database": {"S": glue_database},  # back-compat mirror of source config
        "created_at": {"S": _now_iso()},
    }
    ddb.put_item(TableName=registry_table, Item=item)
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "source": source,
        "glue_database": glue_database,
    }


def provision_dataset_dirs(
    agentcore,
    *,
    runtime_arn: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Ask the harvest runtime to create the dataset's bundle dirs via the mount.

    Called right after a dataset mapping is created. A presigned ``.context/``
    upload PUTs straight to S3 (bypassing the mount); if that PUT is the first
    thing to touch the dataset prefix, S3 Files auto-creates the parent dirs
    owned by root — an identity the mount's access point (forced to uid 1000)
    can't later write into, wedging the first full harvest at ``mark_in_progress``
    with EACCES. Provisioning the dirs THROUGH the mount here (uid 1000) means the
    upload lands inside an already-writable tree.

    Best-effort and non-fatal: only the harvest runtime holds the mount, so if it
    is unreachable we still return the mapping — the operator can re-trigger, and
    the failure is contained to this call. Idempotent (the runtime's mkdirs is
    exist_ok). Returns a small status dict for logging; never raises.
    """
    if not runtime_arn:
        return {"provisioned": False, "reason": "no harvest runtime configured"}
    payload = {"mode": "provision", "data_domain": data_domain, "dataset": dataset}
    session_id = runtime_session_id(data_domain, dataset)
    try:
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            qualifier="DEFAULT",
        )
        return {"provisioned": True}
    except Exception as e:  # noqa: BLE001 - provisioning must never fail the mapping
        import logging

        logging.getLogger("control_api").warning(
            "dataset dir provisioning failed for %s/%s: %s",
            data_domain,
            dataset,
            type(e).__name__,
        )
        return {"provisioned": False, "reason": type(e).__name__}


def write_domain_doc(
    agentcore,
    *,
    runtime_arn: str,
    data_domain: str,
    description: str,
    context: str,
) -> dict[str, Any]:
    """Ask the harvest runtime to write the domain's concept doc through the mount.

    CRITICAL ownership rule: the doc lives at ``<mount>/<domain>/_domain/overview.md``
    which means the ``<mount>/<domain>/`` directory is created BY the mount's uid-1000
    identity. A raw ``put_object`` from the Lambda would materialise that dir as
    root-owned, poisoning ALL datasets under the domain (the exact bug
    ``provision_dataset_dirs`` prevents per-dataset). So this is always delegated to
    the harvest runtime (which holds the S3 Files mount), just like provisioning.

    Best-effort and non-fatal: a failure to write the doc only means the domain
    won't be semantically searchable until the next declare/update call succeeds.
    """
    if not runtime_arn:
        return {"written": False, "reason": "no harvest runtime configured"}
    payload = {
        "mode": "write_domain_doc",
        "data_domain": data_domain,
        "description": description,
        "context": context,
    }
    session_id = runtime_session_id(data_domain, DOMAIN_DATASET)
    try:
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            qualifier="DEFAULT",
        )
        return {"written": True}
    except Exception as e:  # noqa: BLE001 - domain-doc write must never fail the API call
        import logging

        logging.getLogger("control_api").warning(
            "domain doc write failed for %s: %s",
            data_domain,
            type(e).__name__,
        )
        return {"written": False, "reason": type(e).__name__}


def delete_domain_doc(
    s3,
    *,
    bundle_bucket: str,
    data_domain: str,
) -> dict[str, Any]:
    """Delete a domain's concept doc from S3 (cascades to vector via reindex).

    Called when a domain declaration is deleted. The Object Deleted event flows
    through the reindex pipeline and ``DeleteVectors`` the domain's vector.
    """
    from okf_aws import domain_doc_key

    key = domain_doc_key(data_domain)
    try:
        s3.delete_object(Bucket=bundle_bucket, Key=key)
        return {"deleted_key": key}
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        import logging

        logging.getLogger("control_api").warning(
            "domain doc delete failed for %s: %s",
            data_domain,
            type(e).__name__,
        )
        return {"deleted_key": None, "error": type(e).__name__}


def delete_domain_mapping(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    s3=None,
    bundle_bucket: str | None = None,
    freshness_table: str | None = None,
) -> dict[str, Any]:
    """Delete a dataset and ALL state it owns. Idempotent throughout.

    A dataset owns more than its registry pointer, so deleting only the mapping
    (as this used to) orphans the authored bundle, its derived vectors, and the
    freshness/harvest bookkeeping. We purge, in an order safe against a partial
    failure (each step is retryable and independently idempotent):

    1. **Bundle objects** in S3 under ``okf/<domain>/<dataset>/`` — the OKF
       markdown and everything under it (``index.md``, ``.harvest/``,
       ``.context/``). Deleting the ``.md`` objects ALSO cleans the derived
       **S3 Vectors** entries for free: each ``Object Deleted`` event flows
       through the reindex pipeline, which ``DeleteVectors`` by key. (The Control
       API has no s3vectors permissions by design; the cascade owns that.)
    2. **Freshness rows** in the freshness table: the per-table ``TABLE#.../VERSION``
       rows and the reindex dedup ``VEC#.../SEQ`` markers for this dataset.
    3. **Harvest status** row (``HARVEST#.../STATUS``) and the **mapping**
       (``DOMAIN#/DATASET#``) in the registry — deleted LAST so that if an
       earlier step fails and the request is retried, the dataset is still
       resolvable/visible rather than half-gone.

    ``s3``/``bundle_bucket``/``freshness_table`` are optional so existing callers
    and tests that only exercise the registry keep working; when omitted, the
    corresponding purge step is skipped (and reported in the result).
    """
    purged_objects = 0
    purged_freshness = 0

    # 1. Bundle objects (+ cascade to vectors via Object-Deleted events).
    if s3 is not None and bundle_bucket:
        prefix = bundle_prefix(data_domain, dataset)
        batch: list[dict[str, str]] = []
        for key in _iter_bundle_keys(s3, bucket=bundle_bucket, prefix=prefix):
            batch.append({"Key": key})
            if len(batch) == 1000:  # DeleteObjects hard limit
                s3.delete_objects(Bucket=bundle_bucket, Delete={"Objects": batch})
                purged_objects += len(batch)
                batch = []
        if batch:
            s3.delete_objects(Bucket=bundle_bucket, Delete={"Objects": batch})
            purged_objects += len(batch)

    # 2. Freshness rows: TABLE#<d>#<ds>#* / VERSION and VEC#<d>/<ds>/* / SEQ.
    if freshness_table:
        purged_freshness = _delete_freshness_rows(
            ddb, freshness_table, data_domain, dataset
        )

    # 3. Harvest status row, then the mapping (mapping last).
    ddb.delete_item(
        TableName=registry_table,
        Key={"pk": {"S": f"HARVEST#{data_domain}#{dataset}"}, "sk": {"S": "STATUS"}},
    )
    ddb.delete_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": f"DATASET#{dataset}"}},
    )
    return {
        "deleted": True,
        "data_domain": data_domain,
        "dataset": dataset,
        "purged_bundle_objects": purged_objects,
        "purged_freshness_rows": purged_freshness,
    }


def _delete_freshness_rows(
    ddb_resource_or_client, freshness_table: str, data_domain: str, dataset: str
) -> int:
    """Delete every freshness row a dataset owns. Returns the count deleted.

    Two pk shapes belong to a dataset (docs/CONVENTIONS.md):
      * ``TABLE#<domain>#<dataset>#<table>`` (sk ``VERSION``) — the incremental
        path's stored Glue table version, and
      * ``VEC#<domain>/<dataset>/<concept_id>`` (sk ``SEQ``) — the reindex
        worker's per-vector sequencer dedup marker.
    Neither is a queryable key prefix on its own (pk is the full partition key),
    so we Scan with a FilterExpression on the two prefixes. The freshness table
    is small (one row per table + one per concept doc) so a filtered Scan is
    cheap and simplest; batch the deletes.

    Accepts the low-level client (``.scan``/``.delete_item`` with typed keys) —
    the shape the router passes as ``cfg.ddb``.
    """
    table_prefix = f"TABLE#{data_domain}#{dataset}#"
    vec_prefix = f"VEC#{data_domain}/{dataset}/"
    deleted = 0
    # Mirrors the ``begins_with(pk, ...)`` scan pattern already used by
    # list_domains/list_credentials in this file (``pk`` is not a DynamoDB
    # reserved word, so no attribute-name alias is needed). No ProjectionExpression:
    # the table is tiny and we only need pk/sk, which every item carries.
    scan_kwargs: dict[str, Any] = {
        "TableName": freshness_table,
        "FilterExpression": "begins_with(pk, :t) OR begins_with(pk, :v)",
        "ExpressionAttributeValues": {
            ":t": {"S": table_prefix},
            ":v": {"S": vec_prefix},
        },
    }
    while True:
        resp = ddb_resource_or_client.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            ddb_resource_or_client.delete_item(
                TableName=freshness_table,
                Key={"pk": item["pk"], "sk": item["sk"]},
            )
            deleted += 1
        token = resp.get("LastEvaluatedKey")
        if not token:
            break
        scan_kwargs["ExclusiveStartKey"] = token
    return deleted


# --------------------------------------------------------------------------- #
# MCP machine credentials (Cognito M2M app clients)
# --------------------------------------------------------------------------- #
#
# An app/agent that wants to call the consumption MCP server needs a
# non-interactive credential. We vend one Cognito user-pool app client per
# credential, configured for the OAuth2 client_credentials grant and granted the
# shared MCP scope (``okf-mcp/invoke``) that the AgentCore JWT authorizer trusts.
# The holder exchanges client_id/client_secret at the Cognito token endpoint for
# a bearer token; no per-credential infra change is needed (the authorizer trusts
# the scope, not a client allowlist). We record lightweight metadata in the
# registry (``CRED#<client_id>`` / ``META``) so the UI can list/revoke — the
# secret is returned ONCE at creation and never stored.

_CRED_NAME_MAX = 64


def _validate_credential_name(name: str) -> str:
    """A human label for the credential. Kept to a safe, bounded charset.

    Not security-sensitive (the client_id/secret are the real credential), but we
    reject control chars / overlong values so it renders cleanly in the UI and
    the Cognito ClientName (which has its own charset limits).
    """
    if not name or not name.strip():
        raise ApiError(400, "credential name must not be empty")
    name = name.strip()
    if len(name) > _CRED_NAME_MAX:
        raise ApiError(400, f"credential name too long (max {_CRED_NAME_MAX})")
    if any(ord(c) < 0x20 for c in name):
        raise ApiError(400, "credential name contains control characters")
    return name


def create_credential(
    cognito,
    ddb,
    *,
    user_pool_id: str,
    mcp_scope: str,
    registry_table: str,
    name: str,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Create a Cognito M2M app client for MCP access; return the secret ONCE.

    The client is client_credentials-only (no interactive flows), granted the
    single MCP scope. We persist metadata (name, client_id, created_by/at) but
    NEVER the secret — it exists only in this response. Callers must copy it now.
    """
    name = _validate_credential_name(name)
    resp = cognito.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName=name,
        GenerateSecret=True,
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthScopes=[mcp_scope],
        # Machine tokens are short-lived; the holder re-fetches from the token
        # endpoint. No refresh tokens exist for client_credentials.
        AccessTokenValidity=60,
        TokenValidityUnits={"AccessToken": "minutes"},
    )
    client = resp["UserPoolClient"]
    client_id = client["ClientId"]
    client_secret = client.get("ClientSecret")
    created_at = _now_iso()

    item: dict[str, Any] = {
        "pk": {"S": f"CRED#{client_id}"},
        "sk": {"S": "META"},
        "name": {"S": name},
        "client_id": {"S": client_id},
        "created_at": {"S": created_at},
    }
    if created_by:
        item["created_by"] = {"S": created_by}
    ddb.put_item(TableName=registry_table, Item=item)

    return {
        "name": name,
        "client_id": client_id,
        "client_secret": client_secret,  # shown ONCE; never persisted
        "created_at": created_at,
    }


def list_credentials(ddb, *, registry_table: str) -> list[dict[str, Any]]:
    """List vended MCP credentials (metadata only; never the secret).

    Scan for ``pk`` begins_with ``CRED#`` — same tiny-registry pattern as
    ``list_domains``.
    """
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "FilterExpression": "begins_with(pk, :c)",
        "ExpressionAttributeValues": {":c": {"S": "CRED#"}},
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            items.append(
                {
                    "name": _s(item.get("name")),
                    "client_id": _s(item.get("client_id")),
                    "created_at": _s(item.get("created_at")),
                    "created_by": _s(item.get("created_by")),
                }
            )
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    items.sort(key=lambda c: c.get("created_at") or "")
    return items


def delete_credential(
    cognito,
    ddb,
    *,
    user_pool_id: str,
    registry_table: str,
    client_id: str,
    caller: str | None = None,
) -> dict[str, Any]:
    """Revoke a credential: delete the Cognito app client + its registry row.

    Deleting the app client immediately invalidates its client_credentials (the
    token endpoint rejects it; already-issued tokens age out within their short
    TTL). Idempotent on re-run: a missing Cognito client is treated as
    already-revoked.

    SECURITY: the client_id is caller-supplied, so it must NOT be treated as an
    authorization boundary. Before touching Cognito we require a matching
    ``CRED#<client_id>/META`` registry row — i.e. this API actually vended it —
    so a caller can never delete an arbitrary user-pool app client (e.g. the
    public SPA login client, whose id is shipped in the UI bundle) and brick the
    console. When ``caller`` is given, we also require it to match the row's
    ``created_by`` so one user can't revoke another user's credential
    (self-serve model: you can only revoke what you created).

    NOTE: a row with NO ``created_by`` (an ownerless credential — only possible
    for one created via a no-authorizer path, since the router always stamps the
    owner from the verified JWT in production) is revocable by any authenticated
    caller. This is acceptable under the self-serve model: the anti-brick
    ``CRED#``-row requirement still holds, and every production-vended credential
    carries an owner. To make ownerless rows admin-only, gate on a group claim.
    """
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"CRED#{client_id}"}, "sk": {"S": "META"}},
    )
    item = resp.get("Item")
    if not item:
        # Not a credential this API vended (or already revoked). Refuse rather
        # than fall through to delete an arbitrary Cognito client.
        raise ApiError(404, f"no such credential: {client_id}")
    if caller is not None:
        owner = _s(item.get("created_by"))
        if owner and owner != caller:
            raise ApiError(403, "you can only revoke credentials you created")

    try:
        cognito.delete_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
    except Exception as e:  # noqa: BLE001 - a missing client is already-revoked
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("ResourceNotFoundException",):
            raise
    ddb.delete_item(
        TableName=registry_table,
        Key={"pk": {"S": f"CRED#{client_id}"}, "sk": {"S": "META"}},
    )
    return {"deleted": True, "client_id": client_id}


# --------------------------------------------------------------------------- #
# .context/ source docs (S3)
# --------------------------------------------------------------------------- #


def _context_prefix(data_domain: str, dataset: str) -> str:
    return f"{bundle_prefix(data_domain, dataset)}{_CONTEXT_DIRNAME}/"


def _validate_filename(filename: str) -> str:
    """Reject path traversal / nesting so an upload can't escape .context/.

    A context filename is a single flat segment (no ``/``, no ``..``, no leading
    dot) — it lands directly under ``okf/<d>/<ds>/.context/``.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise ApiError(400, f"invalid filename: {filename!r}")
    if filename in (".", "..") or "\x00" in filename:
        raise ApiError(400, f"invalid filename: {filename!r}")
    return filename


def list_context_docs(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> list[dict[str, Any]]:
    """List user-uploaded source docs under ``.context/`` as ``[{filename, key, size}]``."""
    prefix = _context_prefix(data_domain, dataset)
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            # Skip the "directory" placeholder key if one exists.
            if key == prefix:
                continue
            out.append(
                {
                    "filename": key[len(prefix) :],
                    "key": key,
                    "size": obj.get("Size"),
                }
            )
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return out


def presign_context_upload(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    filename: str,
    content_type: str | None,
) -> dict[str, Any]:
    """Return a presigned POST for uploading a source doc into ``.context/``.

    The browser POSTs the file bytes straight to S3 (via multipart form), keeping
    large uploads off the Lambda. Two things are enforced server-side so the
    client cannot bypass them:

    * **Exact key** — pinned from the validated filename (client cannot choose an
      arbitrary location).
    * **Size cap** — a ``content-length-range`` condition (0..CONTEXT_UPLOAD_MAX_
      BYTES) that S3 itself enforces, rejecting an oversized body with
      ``EntityTooLarge`` (threat #42). A PUT presign cannot express this — only
      the POST policy can — which is why this vends a presigned POST.

    Returns ``{url, fields, key, max_bytes, expires_in}``: the caller builds a
    multipart form of ``fields`` + the file and POSTs it to ``url``.
    """
    filename = _validate_filename(filename)
    key = f"{_context_prefix(data_domain, dataset)}{filename}"
    conditions: list[Any] = [
        ["content-length-range", 0, CONTEXT_UPLOAD_MAX_BYTES],
    ]
    fields: dict[str, Any] = {}
    if content_type:
        fields["Content-Type"] = content_type
        conditions.append({"Content-Type": content_type})
    presigned = s3.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )
    return {
        "url": presigned["url"],
        "fields": presigned["fields"],
        "key": key,
        "max_bytes": CONTEXT_UPLOAD_MAX_BYTES,
        "expires_in": PRESIGN_EXPIRY_SECONDS,
    }


def delete_context_doc(
    s3, *, bucket: str, data_domain: str, dataset: str, filename: str
) -> dict[str, Any]:
    """Delete a single ``.context/`` source doc. Idempotent (S3 delete is)."""
    filename = _validate_filename(filename)
    key = f"{_context_prefix(data_domain, dataset)}{filename}"
    s3.delete_object(Bucket=bucket, Key=key)
    return {"deleted": True, "key": key}


# --------------------------------------------------------------------------- #
# Harvest control (AgentCore invoke + status row)
# --------------------------------------------------------------------------- #


def acquire_harvest_lease(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    mode: str,
    session_id: str,
    detail: str | None = None,
) -> bool:
    """Try to take the per-dataset harvest lease (the ``HARVEST#.../STATUS`` row).

    Returns True if the lease was acquired (the row was written as ``queued``),
    False if a harvest for this dataset is already in flight. The write lands
    (conditional PutItem) only when ANY of:

    * there is no status row yet, OR
    * the last harvest reached a terminal state (not queued/running), OR
    * the in-flight lease is STALE — ``started_at`` older than
      ``HARVEST_LEASE_STALE_SECONDS`` (an AgentCore session can't outlive 8h, so
      such a row is a dead job whose terminal status write was lost; taking it
      over lets the dataset recover instead of wedging on 409 forever).

    This is the SINGLE choke point every harvest trigger (Control API AND the
    incremental orchestrator / reconcile) must go through so concurrent harvests
    of one dataset can never race on the shared bundle directory.
    """
    now = _now_iso()
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
    ).isoformat()
    item: dict[str, Any] = {
        "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
        "sk": {"S": "STATUS"},
        "status": {"S": "queued"},
        "mode": {"S": mode},
        "started_at": {"S": now},
        "updated_at": {"S": now},
        "runtime_session_id": {"S": session_id},
    }
    if detail is not None:
        item["detail"] = {"S": detail}
    try:
        ddb.put_item(
            TableName=registry_table,
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(pk) "
                "OR NOT (#s = :queued OR #s = :running) "
                "OR started_at < :stale"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
                ":stale": {"S": stale_cutoff},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition means "already leased"
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return False
        raise


def trigger_harvest(
    agentcore,
    ddb,
    *,
    registry_table: str,
    runtime_arn: str,
    data_domain: str,
    dataset: str,
    mode: str = "full",
    changed_table: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Invoke the harvest AgentCore runtime and write a ``queued`` status row.

    The payload and ``runtimeSessionId`` shape are frozen by CONVENTIONS.md:
    the session id is a deterministic per-dataset id (one session per dataset;
    AgentCore requires 33-256 chars, so we can't use a bare "domain__dataset")
    and the payload carries ``data_domain/dataset/mode`` plus ``changed_table``
    for incremental.

    Concurrency + ordering: we write the ``HARVEST#<d>#<ds> / STATUS`` = queued
    row FIRST, with a ConditionExpression that rejects the write when a harvest
    for this dataset is already ``queued`` or ``running``. This makes the row a
    per-dataset LEASE:

    * Two triggers for the same dataset (a double-click, or a full trigger while
      one is in flight) can no longer both start and race on the shared bundle
      directory — the second gets a 409. Full harvests do ``clean_authored_output``
      (an rm -rf of the dataset root) then a non-atomic finalize, so a concurrent
      pair would corrupt the published bundle.
    * The row always exists before the async job is invoked, so ``GET /harvest``
      never reports "not started" for a job that is actually running (the prior
      invoke-then-write order left an untracked harvest on a write failure).

    If the invoke fails after the lease is taken, we release it (mark the row
    ``failed``) so the operator's retry isn't blocked, then re-raise.
    """
    payload: dict[str, Any] = {
        "data_domain": data_domain,
        "dataset": dataset,
        "mode": mode,
    }
    # Per-harvest model/effort override (already validated against the catalog in
    # the route adapter). Omitted -> the runtime uses its deploy-time env default.
    if model:
        payload["model"] = model
    if effort:
        payload["effort"] = effort
    # Enrich the payload with the declared domain's description/context so the
    # harvester can produce domain-aware authoring. Best-effort: a missing META
    # row (e.g. a legacy mapping with no declaration) simply omits the context.
    domain_meta = get_domain(
        ddb, registry_table=registry_table, data_domain=data_domain
    )
    if domain_meta:
        if domain_meta.get("description"):
            payload["domain_description"] = domain_meta["description"]
        if domain_meta.get("context"):
            payload["domain_context"] = domain_meta["context"]

    if mode == "incremental":
        if not changed_table:
            raise ApiError(400, "incremental mode requires 'changed_table'")
        payload["changed_table"] = changed_table
        # Incremental keeps per-dataset affinity (deterministic session).
        session_id = runtime_session_id(data_domain, dataset)
    else:
        # A full harvest is one-shot: use a FRESH session per trigger so it gets
        # a new microVM (with a clean S3 Files mount) instead of reattaching to a
        # warm/stale one from a prior run.
        session_id = runtime_session_id(
            data_domain, dataset, unique_token=uuid.uuid4().hex
        )

    pk = f"HARVEST#{data_domain}#{dataset}"
    # Acquire the per-dataset lease before invoking. Rejected (409) if a harvest
    # for this dataset is already in flight, so concurrent runs can't race on the
    # shared bundle directory.
    if not acquire_harvest_lease(
        ddb,
        registry_table=registry_table,
        data_domain=data_domain,
        dataset=dataset,
        mode=mode,
        session_id=session_id,
    ):
        raise ApiError(
            409,
            f"a harvest for {data_domain}/{dataset} is already queued or running",
        )

    # Lease held: invoke the runtime. On failure, release the lease (mark failed)
    # so a retry is not permanently blocked by our own queued row.
    try:
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            qualifier="DEFAULT",
        )
    except Exception as e:  # noqa: BLE001 - release lease, then surface the error
        try:
            ddb.update_item(
                TableName=registry_table,
                Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}},
                UpdateExpression="SET #s = :f, updated_at = :u, detail = :d",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":f": {"S": "failed"},
                    ":u": {"S": _now_iso()},
                    ":d": {"S": f"harvest invoke failed: {type(e).__name__}"},
                },
            )
        except Exception:  # noqa: BLE001 - best-effort lease release
            pass
        raise

    return {"status": "queued", "data_domain": data_domain, "dataset": dataset}


def get_harvest_status(
    s3,
    ddb,
    *,
    bucket: str,
    registry_table: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Read the harvest status row + the S3 commit marker (``ready`` boolean).

    ``ready`` reflects the source of truth for consumability (CONVENTIONS.md):
    the bundle is only ready when ``.harvest/state.json`` exists with
    ``status == complete``. The DynamoDB row is the live progress signal; the S3
    marker is the durable commit. They can disagree briefly (e.g. status row is
    ``running`` while no marker yet) and the UI shows both.
    """
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"HARVEST#{data_domain}#{dataset}"}, "sk": {"S": "STATUS"}},
    )
    item = resp.get("Item")
    status: dict[str, Any] = {}
    if item:
        status = {
            "status": _s(item.get("status")),
            "mode": _s(item.get("mode")),
            "started_at": _s(item.get("started_at")),
            "updated_at": _s(item.get("updated_at")),
            "detail": _s(item.get("detail")),
            "runtime_session_id": _s(item.get("runtime_session_id")),
            # The resolved LLM the runtime actually used (stamped at `running`);
            # empty until the runtime advances past `queued`.
            "model": _s(item.get("model")),
            "effort": _s(item.get("effort")),
        }
    ready = is_bundle_ready(s3, bucket, data_domain, dataset)
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "status": status,
        "ready": ready,
    }


# --------------------------------------------------------------------------- #
# Harvest live step feed (read back from the runtime's CloudWatch logs)
# --------------------------------------------------------------------------- #

# Must match harvest.steps.STEP_MARKER (the frozen line token the harvest runtime
# writes). Duplicated here rather than imported so the Control API has no harvest
# dependency; a mismatch would silently return no events, so it's called out in
# docs/CONVENTIONS.md alongside the harvest status shape.
_STEP_MARKER = "OKF_STEP"

# Cap events returned per poll so a long run's backlog can't produce an unbounded
# response; the client keeps polling with the advanced cursor to drain the rest.
_STEP_PAGE_LIMIT = 500


# Overlap window (ms) subtracted from the timestamp cursor on each live poll, so
# a slightly out-of-order CloudWatch ingestion near the boundary isn't missed.
# The ``seq`` filter dedups the resulting re-scan, so the overlap is free.
_FEED_OVERLAP_MS = 5000


def _iso_to_ms(iso: str) -> int | None:
    """Epoch millis for an ISO-8601 timestamp, or None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_harvest_events(
    logs,
    ddb,
    *,
    registry_table: str,
    log_group: str,
    data_domain: str,
    dataset: str,
    since: int = 0,
    since_ts: int = 0,
) -> dict[str, Any]:
    """Return the live step feed for a run, read from the runtime's CloudWatch logs.

    The harvest runtime narrates each step as an ``OKF_STEP <json>`` stdout line
    (see ``harvest.steps``); AgentCore ships stdout to the runtime's CloudWatch
    log group. We reuse THAT existing store — no new event storage. Correlation:
    the run's ``runtime_session_id`` is on the DynamoDB STATUS row and stamped on
    every step line, so we filter by it and never intermix a prior run's events.

    Two cursors, both echoed back for the next poll:

    * ``since`` — the highest ``seq`` the client has; we return ``seq > since``
      and echo the new high-water ``seq`` as ``next``. This is the EXACT dedup.
    * ``since_ts`` — the highest CloudWatch event timestamp (epoch ms) the client
      has seen; it bounds ``FilterLogEvents``' ``startTime`` so each live poll
      scans only a recent window instead of the whole run (returned as
      ``next_ts``). On the FIRST poll (``since_ts == 0``) the floor is the run's
      ``started_at`` so a viewer who opens the page mid-run gets the WHOLE current
      run's history (bounded — one run is a page or two), not just steps from the
      moment they loaded. ``started_at`` is set at ``queued`` time, before any log
      line, so it never clips the run; for incremental harvests (which reuse a
      deterministic session id) it also excludes a prior run's steps.

    Degrades gracefully: if the log group isn't configured or the run has no
    session id yet, returns an empty batch rather than erroring — the feed is an
    enhancement layered on the durable status.
    """
    # Read the STATUS row for the correlation id, terminal flag, and run start.
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"HARVEST#{data_domain}#{dataset}"}, "sk": {"S": "STATUS"}},
    )
    item = resp.get("Item") or {}
    session_id = _s(item.get("runtime_session_id")) or ""
    status = _s(item.get("status")) or ""
    started_at = _s(item.get("started_at")) or ""
    done = status in ("complete", "failed", "cancelled")

    empty = {
        "data_domain": data_domain,
        "dataset": dataset,
        "events": [],
        "next": since,
        "next_ts": since_ts,
        "done": done,
    }
    if logs is None or not log_group or not session_id:
        return empty

    # Bound the scan window (startTime). Subsequent polls scan from the last seen
    # event ts (minus a small overlap); the first poll scans from the run start so
    # a mid-run page load backfills the full current run. None => scan all (only if
    # started_at is missing/unparseable — the safe pre-optimization behavior).
    if since_ts > 0:
        start_time_ms: int | None = max(0, since_ts - _FEED_OVERLAP_MS)
    else:
        start_time_ms = _iso_to_ms(started_at)

    # CloudWatch filter pattern: match only OUR step lines for THIS session. Both
    # terms are quoted substrings ANDed together; cheap server-side pre-filter so
    # we page over just this run's step lines.
    pattern = f'"{_STEP_MARKER}" "{session_id}"'
    events: list[dict[str, Any]] = []
    high = since
    high_ts = since_ts
    try:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "logGroupName": log_group,
                "filterPattern": pattern,
                # interleaved=True merges streams by timestamp (default in v2 API).
            }
            if start_time_ms is not None:
                kwargs["startTime"] = start_time_ms
            if token:
                kwargs["nextToken"] = token
            page = logs.filter_log_events(**kwargs)
            for ev in page.get("events", []):
                parsed = _parse_step_line(ev.get("message", ""), session_id=session_id)
                if parsed is None:
                    continue
                seq = parsed.get("seq")
                if not isinstance(seq, int) or seq <= since:
                    continue
                events.append(parsed)
                if seq > high:
                    high = seq
                # Track the CloudWatch event timestamp (not the app-stamped ts) so
                # the next poll's startTime floor matches the source of truth.
                ev_ts = ev.get("timestamp")
                if isinstance(ev_ts, int) and ev_ts > high_ts:
                    high_ts = ev_ts
                if len(events) >= _STEP_PAGE_LIMIT:
                    break
            token = page.get("nextToken")
            if not token or len(events) >= _STEP_PAGE_LIMIT:
                break
    except Exception as e:  # noqa: BLE001 - the feed must not 500 if logs are unreadable
        import logging

        logging.getLogger("control_api").warning(
            "harvest events read failed for %s/%s: %s",
            data_domain,
            dataset,
            type(e).__name__,
        )
        return empty

    # Order by seq (streams can interleave) and dedup (a line can be delivered
    # more than once across pages / retries).
    events.sort(key=lambda e: e["seq"])
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for e in events:
        if e["seq"] in seen:
            continue
        seen.add(e["seq"])
        deduped.append(e)

    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "events": deduped,
        "next": high,
        "next_ts": high_ts,
        "done": done,
    }


def _parse_step_line(message: str, *, session_id: str) -> dict[str, Any] | None:
    """Parse one ``OKF_STEP <json>`` log line into an event dict, or None.

    Defensive: a line that isn't our marker, isn't valid JSON, or belongs to a
    different session (a substring filter match can be coincidental) is dropped.
    Returns only the UI-relevant fields so the response stays small.
    """
    if not message or _STEP_MARKER not in message:
        return None
    idx = message.find(_STEP_MARKER)
    payload_str = message[idx + len(_STEP_MARKER) :].strip()
    try:
        rec = json.loads(payload_str)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict):
        return None
    # Guard against a coincidental substring match on a different session.
    if session_id and rec.get("session_id") not in (session_id, "", None):
        return None
    seq = rec.get("seq")
    if not isinstance(seq, int):
        return None
    out: dict[str, Any] = {
        "seq": seq,
        "kind": rec.get("kind") or "",
        "label": rec.get("label") or "",
        "ts": rec.get("ts") or "",
    }
    if "tool" in rec:
        out["tool"] = rec.get("tool")
    if "ok" in rec:
        out["ok"] = bool(rec.get("ok"))
    # Full agent-message markdown (agent events only) — the UI renders it in a
    # modal when the truncated one-liner is expanded.
    if rec.get("full"):
        out["full"] = rec.get("full")
    # Correlation key pairing a tool_call with its tool_result (the UI folds them
    # into one row). Present on tool events only.
    if rec.get("call_id"):
        out["call_id"] = rec.get("call_id")
    # Sub-agent fleet fields (KIND_SUBAGENT): phase = start|complete|error,
    # batch groups a fan-out (the eval id), sub_id is the per-square id.
    for k in ("phase", "batch", "sub_id", "subagent_type"):
        if rec.get(k):
            out[k] = rec.get(k)
    # Running token-usage snapshot (kind="usage"): cumulative counts for the whole
    # run. Passed through verbatim as a dict so the UI can show a running total.
    if isinstance(rec.get("usage"), dict):
        out["usage"] = rec["usage"]
    return out


def cancel_harvest(
    agentcore,
    ddb,
    *,
    registry_table: str,
    runtime_arn: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Cancel an in-flight harvest: stop the AgentCore session and free the lease.

    Only ``queued``/``running`` harvests are cancellable — a terminal row
    (``complete``/``failed``/``cancelled``) is a 409 no-op. We:

    1. Read the ``HARVEST#<d>#<ds> / STATUS`` row for its ``runtime_session_id``
       (persisted at lease time — for a full harvest this is the fresh per-trigger
       UUID session, so we stop the EXACT microVM running the job).
    2. Best-effort ``StopRuntimeSession`` on that session. Non-fatal: if the stop
       call fails (e.g. the session already ended), we still free the lease so the
       dataset isn't wedged. The status row is the source of truth for the lease,
       not the live session.
    3. Flip the row to ``cancelled`` with a **conditional** update (status still
       ``queued``/``running``). If the runner concurrently wrote a terminal state
       (``complete``/``failed``) in the meantime, the condition fails and we report
       that actual state rather than clobbering it — the harvest already finished.

    ``cancelled`` is a terminal status, so it satisfies the lease-free predicate
    (``NOT (status IN (queued, running))``) and a retry is immediately allowed.
    """
    pk = f"HARVEST#{data_domain}#{dataset}"
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}},
    )
    item = resp.get("Item")
    if not item:
        raise ApiError(404, f"no harvest found for {data_domain}/{dataset}")
    current = _s(item.get("status")) or ""
    if current not in ("queued", "running"):
        raise ApiError(
            409,
            f"harvest for {data_domain}/{dataset} is not in progress "
            f"(status={current!r})",
        )
    session_id = _s(item.get("runtime_session_id")) or ""

    # Stop the runtime session that's executing the job. Best-effort: a failure
    # here must not block freeing the lease (the session may already be gone).
    stopped = False
    stop_error: str | None = None
    if session_id and runtime_arn:
        try:
            agentcore.stop_runtime_session(
                runtimeSessionId=session_id,
                agentRuntimeArn=runtime_arn,
                qualifier="DEFAULT",
            )
            stopped = True
        except Exception as e:  # noqa: BLE001 - proceed to free the lease regardless
            import logging

            stop_error = type(e).__name__
            logging.getLogger("control_api").warning(
                "StopRuntimeSession failed for %s/%s: %s",
                data_domain,
                dataset,
                stop_error,
            )

    # Flip to `cancelled`, but only if the harvest is STILL in flight. A
    # ConditionalCheckFailed means the runner reached a terminal state first.
    try:
        ddb.update_item(
            TableName=registry_table,
            Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}},
            UpdateExpression="SET #s = :c, updated_at = :u, detail = :d",
            ConditionExpression="#s = :queued OR #s = :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": {"S": "cancelled"},
                ":u": {"S": _now_iso()},
                ":d": {"S": "cancelled by operator"},
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
            },
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            # The harvest finished (or was already cancelled) between our read and
            # write. Report the real, freshly-read status instead of overwriting.
            latest = ddb.get_item(
                TableName=registry_table,
                Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}},
            ).get("Item", {})
            return {
                "data_domain": data_domain,
                "dataset": dataset,
                "status": _s(latest.get("status")) or current,
                "cancelled": False,
                "stopped_session": stopped,
                "detail": "harvest reached a terminal state before cancel",
            }
        raise

    result: dict[str, Any] = {
        "data_domain": data_domain,
        "dataset": dataset,
        "status": "cancelled",
        "cancelled": True,
        "stopped_session": stopped,
    }
    if stop_error:
        result["stop_error"] = stop_error
    return result


# --------------------------------------------------------------------------- #
# Bundle browsing (S3)
# --------------------------------------------------------------------------- #


def _iter_bundle_keys(s3, *, bucket: str, prefix: str):
    """Yield every S3 object key under a bundle prefix (paginated)."""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            yield obj["Key"]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break


def list_bundle_files(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> list[dict[str, Any]]:
    """List concept docs in a bundle as ``[{concept_id, key}]``.

    Reuses ``okf_aws.parse_bundle_key`` so we apply the exact same "what is a
    concept" rules as the reindex worker: ``.md`` only, and skip ``index.md`` /
    ``log.md`` / anything under a dot-prefixed dir (``.context``/``.harvest``).
    """
    prefix = bundle_prefix(data_domain, dataset)
    out: list[dict[str, Any]] = []
    for key in _iter_bundle_keys(s3, bucket=bucket, prefix=prefix):
        loc = parse_bundle_key(key)
        if loc is None:
            continue
        out.append({"concept_id": loc.concept_id, "key": key})
    return out


def _validate_bundle_key(key: str, *, data_domain: str, dataset: str) -> str:
    """Ensure ``key`` is a real ``.md`` concept under this dataset's prefix.

    Guards the "read one file" endpoint so a caller cannot pass an arbitrary key
    (e.g. another dataset's ``.context/`` upload or ``../`` traversal) and read
    it back. We accept only keys that parse as a concept in *this* bundle.
    """
    loc = parse_bundle_key(key)
    prefix = bundle_prefix(data_domain, dataset)
    if loc is None or not key.startswith(prefix):
        raise ApiError(400, f"key is not a concept under this bundle: {key!r}")
    if loc.data_domain != data_domain or loc.dataset != dataset:
        raise ApiError(400, f"key is not a concept under this bundle: {key!r}")
    return key


def read_bundle_file(
    s3, *, bucket: str, data_domain: str, dataset: str, key: str
) -> dict[str, Any]:
    """Return one bundle ``.md`` file's raw text after validating the key."""
    key = _validate_bundle_key(key, data_domain=data_domain, dataset=dataset)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - map missing object to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise ApiError(404, f"no such bundle file: {key}") from e
        raise
    text = obj["Body"].read().decode("utf-8")
    return {"key": key, "text": text}


def build_graph_json(files: dict[str, str]) -> dict[str, Any]:
    """Build ``{nodes, edges}`` link-graph JSON for the UI from concept docs.

    ``files`` maps concept id (e.g. ``tables/races``) -> raw markdown text. We
    materialize the docs into a temp dir preserving structure, then reuse
    ``okf_core.links.extract_links_with_headings`` (the exact resolver the
    harvest agent and viewer use) so link resolution is identical everywhere.
    Edges whose target is not itself a known concept are dropped.

    * nodes: ``{id, title, type}`` (title/type from YAML frontmatter, best effort)
    * edges: ``{source, target}`` for each resolved intra-bundle link
    """
    from okf_core.document import OKFDocument, OKFDocumentError

    node_ids = set(files.keys())
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Write each concept doc to <root>/<concept_id>.md, creating parent dirs.
        for concept_id, text in files.items():
            path = root / f"{concept_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        for concept_id in sorted(files):
            text = files[concept_id]
            title = concept_id
            type_ = "Unknown"
            body = text
            try:
                doc = OKFDocument.parse(text)
                fm = doc.frontmatter or {}
                title = str(fm.get("title") or concept_id)
                type_ = str(fm.get("type") or "Unknown")
                body = doc.body or ""
            except (OKFDocumentError, Exception) as e:  # noqa: BLE001 - tolerate malformed docs
                del e  # keep title/type defaults; a bad doc still becomes a node
            nodes.append({"id": concept_id, "title": title, "type": type_})

            doc_dir = (root / f"{concept_id}.md").parent
            for link in extract_links_with_headings(body, doc_dir, root):
                if link.target in node_ids:
                    edges.append({"source": concept_id, "target": link.target})

    return {"nodes": nodes, "edges": edges}


def bundle_graph(s3, *, bucket: str, data_domain: str, dataset: str) -> dict[str, Any]:
    """Download the bundle's concept docs and return link-graph JSON for the UI."""
    prefix = bundle_prefix(data_domain, dataset)
    files: dict[str, str] = {}
    for key in _iter_bundle_keys(s3, bucket=bucket, prefix=prefix):
        loc = parse_bundle_key(key)
        if loc is None:
            continue
        obj = s3.get_object(Bucket=bucket, Key=key)
        files[loc.concept_id] = obj["Body"].read().decode("utf-8")
    return build_graph_json(files)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _s(attr: dict[str, Any] | None) -> str | None:
    """Extract the string value from a DynamoDB ``{"S": ...}`` attribute."""
    if not attr:
        return None
    return attr.get("S")


def _source_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Read a mapping's ``source`` object, tolerating legacy (flat) rows.

    New rows carry ``source = {"M": {"type": {"S": ...}, ...}}``; pre-``source``
    rows carry only a flat top-level ``glue_database``. ``normalize_source``
    reconciles both into one ``{type, ...config}`` dict so the UI/readers see a
    single shape regardless of when the row was written.
    """
    raw = item.get("source")
    source_dict: dict[str, Any] | None = None
    if isinstance(raw, dict) and isinstance(raw.get("M"), dict):
        m = raw["M"]
        source_dict = {k: _s(v) for k, v in m.items() if _s(v) is not None}
    return normalize_source(source_dict, glue_database=_s(item.get("glue_database")))

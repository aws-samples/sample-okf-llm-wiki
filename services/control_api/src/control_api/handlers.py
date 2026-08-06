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
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datetime import timedelta

log = logging.getLogger(__name__)

from okf_aws import ar_policy, registry_entity
from okf_aws import (
    bundle_marker_status,
    bundle_prefix,
    is_bundle_ready,
    parse_bundle_key,
    state_marker_key,
)
from okf_aws import s3_versions
from okf_core import annotations as anno
from okf_core import chat_threads as ct
from okf_core import guidance as gd
from okf_core import policy_rebuild
from okf_core.domain import DOMAIN_DATASET
from okf_core.links import extract_links_with_headings
from okf_core.paths import is_external_concept_id, parse_concept_id
from okf_core.session import HARVEST_LEASE_STALE_SECONDS, runtime_session_id
from okf_core.sources import (
    REDSHIFT_CLUSTER_KEY,
    REDSHIFT_DATABASE_KEY,
    REDSHIFT_SECRET_ARN_KEY,
    REDSHIFT_WORKGROUP_KEY,
    SOURCE_TYPE_GLUE,
    SOURCE_TYPE_REDSHIFT,
    SourceError,
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

# The recursive-improvement benchmark CSV (question,gold_sql) lives OFF the okf/
# mount prefix — under a sibling ``benchmark/<domain>/<dataset>/`` prefix in the
# same bucket. This is deliberate and load-bearing: the harvest S3 Files mount is
# rooted at ``okf/``, so anything there is readable by the supervisor/authoring
# agents. Keeping gold under ``benchmark/`` (NOT ``okf/``) makes it invisible to
# every LLM role's file tools — the runner fetches it via GetObject into the
# benchmark tool's memory. See docs/CONVENTIONS.md.
_BENCHMARK_PREFIX = "benchmark/"


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


def assert_source_registrable(glue, *, dataset: str, source: dict[str, Any]) -> None:
    """Per-source registration guards for ``PUT /domains/{d}/datasets/{ds}``.

    * **glue** — the dataset name MUST equal the Glue database name (the harvest
      runtime and the incremental path resolve a dataset to a Glue database of the
      SAME name), and the database must exist (front-run ``GetTables`` → 404 on a
      typo). This preserves the historical Glue contract.
    * **redshift** — the dataset name is the OKF dataset id and is INDEPENDENT of
      the ``redshift_database`` (a Redshift database holds many schemas/datasets),
      so there is no equality rule. The mapping must be COMPLETE, though: the
      self-describing connection (a ``cluster_identifier`` or ``workgroup_name``,
      plus the ``secret_arn`` that authenticates to it) is required here, because
      a db-only descriptor can't be harvested and would otherwise fail deep in the
      async run instead of 400ing at this boundary. We deliberately do NOT probe
      the connection live (that would run ``ListDatabases`` on every PUT); an
      unreachable target surfaces when the first harvest runs.

    Raises :class:`SourceError` (mapped to 400 by the route) on a rule violation;
    :class:`ApiError` (404) if a named Glue database doesn't exist.
    """
    source_type = source.get("type")
    if source_type == SOURCE_TYPE_GLUE:
        glue_database = source_glue_database(source)
        if dataset != glue_database:
            raise SourceError(
                f"dataset must equal glue_database (got dataset={dataset!r}, "
                f"glue_database={glue_database!r})"
            )
        assert_glue_database_exists(glue, glue_database)
    elif source_type == SOURCE_TYPE_REDSHIFT:
        if not source.get(REDSHIFT_DATABASE_KEY):
            raise SourceError("redshift source requires a non-empty redshift_database")
        # Fail fast on an unharvestable mapping: the harvest connects entirely
        # from the descriptor, so registration requires the full connection.
        if not (source.get(REDSHIFT_CLUSTER_KEY) or source.get(REDSHIFT_WORKGROUP_KEY)):
            raise SourceError(
                "redshift source requires a cluster_identifier or workgroup_name "
                "(the harvest connects from the mapping's descriptor)"
            )
        if not source.get(REDSHIFT_SECRET_ARN_KEY):
            raise SourceError(
                "redshift source requires a secret_arn (the Secrets Manager "
                "secret that authenticates to the cluster/workgroup)"
            )
        # No equality rule and no live probe (see docstring).
    # Unknown types were already rejected by normalize_source.


def list_redshift_targets(redshift, redshift_serverless) -> list[dict[str, Any]]:
    """List every Redshift compute target the account can harvest from.

    Returns ``[{kind, id, database?}]`` where ``kind`` is ``"cluster"`` (a
    provisioned cluster) or ``"workgroup"`` (a Serverless workgroup) and ``id`` is
    the identifier the mapping/harvest connects by. ``database`` is the cluster's
    default DB when Redshift reports one (a hint for the UI). Both calls are pure
    IAM control-plane reads (no DB connection), so this feeds the UI's cluster
    picker without any credentials. Paginated. Best-effort per API: if one of the
    two services errors (e.g. not enabled in-region, or ``var.enable_redshift``
    is off so the role has no grants), its list is simply empty — but the error
    is LOGGED so an IAM/throttling problem doesn't masquerade as "no clusters".
    """
    out: list[dict[str, Any]] = []

    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Marker": token} if token else {}
        try:
            resp = redshift.describe_clusters(**kwargs)
        except Exception:  # noqa: BLE001 - a disabled/absent service is fine
            logging.getLogger("control_api").warning(
                "describe_clusters failed (enable_redshift off, or a real "
                "IAM/availability error?); returning no provisioned clusters",
                exc_info=True,
            )
            break
        for c in resp.get("Clusters", []):
            out.append(
                {
                    "kind": "cluster",
                    "id": c.get("ClusterIdentifier"),
                    "database": c.get("DBName"),
                }
            )
        token = resp.get("Marker")
        if not token:
            break

    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        try:
            resp = redshift_serverless.list_workgroups(**kwargs)
        except Exception:  # noqa: BLE001
            logging.getLogger("control_api").warning(
                "list_workgroups failed (enable_redshift off, or a real "
                "IAM/availability error?); returning no Serverless workgroups",
                exc_info=True,
            )
            break
        for w in resp.get("workgroups", []):
            out.append({"kind": "workgroup", "id": w.get("workgroupName")})
        token = resp.get("nextToken")
        if not token:
            break

    return out


def list_redshift_databases(
    redshift_data,
    *,
    cluster_identifier: str | None = None,
    workgroup_name: str | None = None,
    secret_arn: str | None = None,
    database: str | None = None,
) -> list[str]:
    """List database names on one Redshift target via the Data API.

    Unlike :func:`list_redshift_targets`, this DOES connect (``ListDatabases`` runs
    against the target), so it needs the target identifier + a Secrets Manager
    secret to authenticate. Feeds the UI's database picker once a cluster/workgroup
    is chosen. Requires exactly one of ``cluster_identifier`` / ``workgroup_name``.

    ``database`` is the BOOTSTRAP database ``ListDatabases`` connects to first —
    for a provisioned cluster pass its ``DBName`` (returned as the ``database``
    hint by :func:`list_redshift_targets`), since a cluster created with a custom
    initial database may have no ``dev`` at all. Falls back to the conventional
    ``dev`` (always present on Serverless).
    """
    if not (cluster_identifier or workgroup_name):
        raise ApiError(400, "cluster or workgroup is required")
    if not secret_arn:
        raise ApiError(400, "secret_arn is required to list databases")
    kwargs: dict[str, Any] = {"Database": database or "dev", "SecretArn": secret_arn}
    if cluster_identifier:
        kwargs["ClusterIdentifier"] = cluster_identifier
    else:
        kwargs["WorkgroupName"] = workgroup_name
    names: list[str] = []
    token: str | None = None
    while True:
        if token:
            kwargs["NextToken"] = token
        try:
            resp = redshift_data.list_databases(**kwargs)
        except Exception as e:  # noqa: BLE001 - surface a clean 400 to the UI
            raise ApiError(400, f"could not list Redshift databases: {e}") from e
        names.extend(resp.get("Databases", []) or [])
        token = resp.get("NextToken")
        if not token:
            break
    return names


# --------------------------------------------------------------------------- #
# Domain -> dataset registry (okf-registry)
# --------------------------------------------------------------------------- #


def _registry_entity_rows(ddb, registry_table: str) -> list[dict[str, Any]]:
    """All dataset-mapping + XREF rows, via the by-entity GSI when TRUSTABLE.

    Two small Queries (``entity="dataset"``, ``entity="xref"``) instead of a
    full-table Scan. A GSI only contains rows stamped with its keys, so it is
    consulted ONLY once the backfill's readiness marker says every row is
    stamped (``okf_aws.registry_entity``) — on a partially-stamped registry a
    result-shape heuristic would return the FRESH rows and silently hide every
    pre-index dataset. No marker (or an index read error — e.g. the marker
    stamped before the terraform apply) → the original filtered Scan, which is
    always correct, just table-sized.
    """
    if registry_entity.entity_index_ready(ddb, registry_table):
        try:
            rows: list[dict[str, Any]] = []
            for entity in (
                registry_entity.ENTITY_DATASET,
                registry_entity.ENTITY_XREF,
            ):
                rows.extend(
                    registry_entity.query_entity_rows(ddb, registry_table, entity)
                )
            return rows
        except Exception:  # noqa: BLE001 - marker without index => scan below
            log.info("by-entity index unavailable — falling back to the scan")
    rows = []
    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "FilterExpression": (
            "begins_with(pk, :d) AND (begins_with(sk, :ds) OR begins_with(sk, :x))"
        ),
        "ExpressionAttributeValues": {
            ":d": {"S": "DOMAIN#"},
            ":ds": {"S": "DATASET#"},
            ":x": {"S": "XREF#"},
        },
    }
    while True:
        resp = ddb.scan(**kwargs)
        rows.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return rows


def list_domains(ddb, *, registry_table: str) -> list[dict[str, Any]]:
    """All domain->dataset mappings: registry items with ``pk`` begins_with DOMAIN#
    AND ``sk`` begins_with DATASET# or XREF# (declared-domain META rows are still
    excluded from the mapping list).

    Enumerates via the ``by-entity`` GSI (Query — a Scan reads the WHOLE
    table, harvest/report rows included, so its cost grows with usage) with
    the legacy filtered Scan as the fallback for deployments whose rows were
    never stamped into the index (scripts/backfill_registry_entity.py).
    Returns the raw mapping attrs the UI needs, plus the CROSS-DATASET
    reference signal: ``cross_references`` (datasets this one holds
    ``external/`` pair docs for) and ``cross_referenced_by`` (datasets whose
    bundle holds pair docs about this one), from reindex-derived ``XREF#``
    rows (``entity="xref"`` — see docs/CONVENTIONS.md).
    """
    items: list[dict[str, Any]] = []
    references: dict[tuple[str, str], set[str]] = {}
    referenced_by: dict[tuple[str, str], set[str]] = {}
    for item in _registry_entity_rows(ddb, registry_table):
            if (_s(item.get("sk")) or "").startswith("XREF#"):
                src = (
                    _s(item.get("source_data_domain")),
                    _s(item.get("source_dataset")),
                )
                tgt = (
                    _s(item.get("target_data_domain")),
                    _s(item.get("target_dataset")),
                )
                if not all(src) or not all(tgt):
                    continue  # malformed row — ignore rather than half-report
                references.setdefault(src, set()).add(f"{tgt[0]}/{tgt[1]}")
                referenced_by.setdefault(tgt, set()).add(f"{src[0]}/{src[1]}")
                continue
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
                    # Dataset-level authoring guidance (shared; steers every
                    # harvest). Surfaced so the UI can show it + whether it's
                    # pending a re-harvest (dirty).
                    **_guidance_fields(item),
                }
            )
    for m in items:
        pair_key = (m["data_domain"], m["dataset"])
        if pair_key in references:
            m["cross_references"] = sorted(references[pair_key])
        if pair_key in referenced_by:
            m["cross_referenced_by"] = sorted(referenced_by[pair_key])
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
            # by-entity GSI keys (CONVENTIONS.md "Registry entity index").
            " , entity = :ent, #pr = :pair"
        ),
        ExpressionAttributeNames={"#ctx": "context", "#pr": "pair"},
        ExpressionAttributeValues={
            ":dd": {"S": data_domain},
            ":desc": {"S": description},
            ":ctx": {"S": context},
            ":now": {"S": now},
            ":ent": {"S": registry_entity.ENTITY_DOMAIN},
            ":pair": {"S": registry_entity.entity_pair(data_domain)},
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
    source: dict[str, Any] | None = None,
    glue_database: str | None = None,
) -> dict[str, Any]:
    """Create/replace the DOMAIN#<domain> / DATASET#<dataset> registry item.

    Item shape (CONVENTIONS.md): attrs ``data_domain, dataset, source,
    created_at`` (+ a flat ``glue_database`` mirror for glue sources only).
    ``source`` is the first-class source descriptor — a nested map
    ``{type, ...config}`` (see ``okf_core.sources``); its config keys are stored
    generically so a new source type adds no per-key code here. For a GLUE source
    we ALSO write the flat top-level ``glue_database`` attribute for back-compat:
    the incremental scan (``iter_dataset_mappings``) filters on it. A non-glue
    source writes no such mirror — it isn't reached by the glue-event path. We
    PutItem (full overwrite) since the mapping is small and PUT matches the verb.

    Pass EITHER a ``source`` descriptor or the convenience ``glue_database`` (lifted
    into a glue source); ``normalize_source`` reconciles both into one shape.
    """
    source = normalize_source(source, glue_database=glue_database)
    # Store every source-config key as a nested string map, source-generically.
    source_map = {k: {"S": str(v)} for k, v in source.items()}
    item: dict[str, Any] = {
        "pk": {"S": f"DOMAIN#{data_domain}"},
        "sk": {"S": f"DATASET#{dataset}"},
        # by-entity GSI keys (CONVENTIONS.md "Registry entity index") — what
        # lets listings Query instead of Scan.
        "entity": {"S": registry_entity.ENTITY_DATASET},
        "pair": {"S": registry_entity.entity_pair(data_domain, dataset)},
        "data_domain": {"S": data_domain},
        "dataset": {"S": dataset},
        "source": {"M": source_map},
        "created_at": {"S": _now_iso()},
    }
    # Back-compat mirror: only a glue source carries the flat glue_database that the
    # incremental (aws.glue-event) path reads. Other source types omit it.
    glue_db = source_glue_database(source)
    if glue_db:
        item["glue_database"] = {"S": glue_db}
    ddb.put_item(TableName=registry_table, Item=item)
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "source": source,
        "glue_database": glue_db,
    }


# --------------------------------------------------------------------------- #
# Dataset guidance (shared authoring instructions on the DATASET# mapping row)
# --------------------------------------------------------------------------- #


def _guidance_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the guidance attrs + derived ``guidance_dirty`` from a mapping item."""
    text = _s(item.get(gd.ATTR_TEXT)) or ""
    updated_at = _s(item.get(gd.ATTR_UPDATED_AT)) or ""
    applied_version = _s(item.get(gd.ATTR_APPLIED_VERSION)) or ""
    return {
        "guidance": text,
        "guidance_updated_at": updated_at,
        "guidance_applied_version": applied_version,
        # Pending a re-harvest to take effect (edited or never applied).
        "guidance_dirty": gd.is_dirty(text, updated_at, applied_version),
    }


def get_dataset_guidance(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> dict[str, Any]:
    """Return the dataset's guidance + dirty state. 404 if the mapping is missing."""
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": f"DATASET#{dataset}"}},
    )
    item = resp.get("Item")
    if not item:
        raise ApiError(404, f"no such dataset: {data_domain}/{dataset}")
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        **_guidance_fields(item),
    }


def get_dataset_source(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> dict[str, Any] | None:
    """The dataset's normalized source descriptor (``{type, ...config}``), or None.

    Read from the mapping's ``source`` attr (tolerating legacy flat ``glue_database``
    rows via ``_source_from_item``), so ``trigger_harvest`` / the annotation run can
    thread it into the invocation payload and the runtime dispatches on ``type``
    instead of assuming a Glue database named by the dataset. Best-effort: a missing
    mapping returns None (the runtime then defaults to a glue source by dataset name).
    """
    resp = ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": f"DATASET#{dataset}"}},
    )
    item = resp.get("Item")
    if not item:
        return None
    try:
        return _source_from_item(item)
    except SourceError:
        return None


def set_dataset_guidance(
    ddb, *, registry_table: str, data_domain: str, dataset: str, guidance: str
) -> dict[str, Any]:
    """Set/clear the dataset's guidance, bumping ``guidance_updated_at``.

    Conditioned on the mapping existing (a stray dataset id is a clean 404). The
    text is trimmed + capped (okf_core.guidance.normalize). ``updated_at`` always
    moves forward, so the guidance goes DIRTY (``applied_version`` no longer
    matches) — the next annotation-run/harvest picks it up. We do NOT touch
    ``applied_version`` here; only a successful harvest advances that.
    """
    text = gd.normalize(guidance)
    now = _now_iso()
    try:
        ddb.update_item(
            TableName=registry_table,
            Key={
                "pk": {"S": f"DOMAIN#{data_domain}"},
                "sk": {"S": f"DATASET#{dataset}"},
            },
            UpdateExpression="SET #g = :g, #gu = :now",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames={
                "#g": gd.ATTR_TEXT,
                "#gu": gd.ATTR_UPDATED_AT,
            },
            ExpressionAttributeValues={":g": {"S": text}, ":now": {"S": now}},
        )
    except Exception as e:  # noqa: BLE001 - map a missing mapping to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiError(404, f"no such dataset: {data_domain}/{dataset}") from e
        raise
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "guidance": text,
        "guidance_updated_at": now,
        "guidance_applied_version": "",  # cleared relationship; recomputed on read
        "guidance_dirty": gd.is_dirty(text, now, ""),
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
       **Benchmark objects** under ``benchmark/<domain>/<dataset>/`` go with
       them — the gold-carrying ``questions.csv`` and every report artifact
       must not outlive the dataset (a re-registered same-named dataset would
       otherwise inherit the previous owner's gold and reports).
    2. **Freshness rows** in the freshness table: the per-table ``TABLE#.../VERSION``
       rows and the reindex dedup ``VEC#.../SEQ`` markers for this dataset.
    3. **Harvest status + REPORT# rows** (the whole ``HARVEST#<d>#<ds>``
       partition) and the **mapping** (``DOMAIN#/DATASET#``) in the registry —
       deleted LAST so that if an earlier step fails and the request is
       retried, the dataset is still resolvable/visible rather than half-gone.

    ``s3``/``bundle_bucket``/``freshness_table`` are optional so existing callers
    and tests that only exercise the registry keep working; when omitted, the
    corresponding purge step is skipped (and reported in the result).

    Refused while a FRESH guardrails author runs (the same 409 gate as the
    lease acquirers): an author finishing mid-delete would re-materialize
    ``policy/<d>/<ds>/`` objects after the purge. Deleting takes no lease, so
    it calls the gate directly.
    """
    refuse_if_guardrails_building(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    )
    purged_objects = 0
    purged_freshness = 0

    # 1. Bundle + benchmark + AR policy artifacts (+ cascade to vectors via
    #    Object-Deleted events for the .md keys). policy/<d>/<ds>/ holds the
    #    derived ar_rules.md + grounding.json — off-mount, like benchmark/.
    #    benchmark/ carries GOLD, so it purges ALL versions (the bucket is
    #    versioned; plain deletes leave gold readable as noncurrent versions).
    if s3 is not None and bundle_bucket:
        for prefix in (
            bundle_prefix(data_domain, dataset),
            ar_policy.policy_prefix(data_domain, dataset),
        ):
            purged_objects += _purge_s3_prefix(s3, bucket=bundle_bucket, prefix=prefix)
        purged_objects += _purge_s3_prefix_versions(
            s3, bucket=bundle_bucket, prefix=_benchmark_prefix(data_domain, dataset)
        )

    # 2. Freshness rows: TABLE#<d>#<ds>#* / VERSION and VEC#<d>/<ds>/* / SEQ.
    if freshness_table:
        purged_freshness = _delete_freshness_rows(
            ddb, freshness_table, data_domain, dataset
        )

    # 3. Benchmark REPORT# rows, the harvest status row, then the mapping
    #    (mapping last).
    purged_reports = _delete_report_rows(ddb, registry_table, data_domain, dataset)
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
        "purged_report_rows": purged_reports,
    }


def _checked_delete_objects(s3, *, bucket: str, objects: list[dict[str, str]]) -> int:
    """One DeleteObjects call; returns how many S3 CONFIRMED deleted.

    ``delete_objects`` reports per-key failures IN-BAND — HTTP 200 with an
    ``Errors`` list (e.g. AccessDenied on a version-targeted delete) — so a
    blind ``+= len(batch)`` counts objects that are still there. Any error
    must raise: the delete handler drops the registry/REPORT# rows AFTER the
    purge, and proceeding past a failed purge leaves the S3 artifacts alive
    (readable, and resurrected by a re-register) while the API reports
    success. Message bounded to the first few Code/Key pairs — a purge can
    carry a thousand keys.
    """
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    errors = resp.get("Errors") or []
    if errors:
        sample = "; ".join(
            f"{e.get('Code', '?')} on {e.get('Key', '?')}" for e in errors[:3]
        )
        raise ApiError(
            502,
            f"S3 purge failed for {len(errors)} object(s) under s3://{bucket}: "
            f"{sample}",
        )
    return len(resp.get("Deleted") or [])


def _purge_s3_prefix(s3, *, bucket: str, prefix: str) -> int:
    """Batch-delete every object under ``prefix``; returns the count deleted."""
    deleted = 0
    batch: list[dict[str, str]] = []
    for key in _iter_bundle_keys(s3, bucket=bucket, prefix=prefix):
        batch.append({"Key": key})
        if len(batch) == 1000:  # DeleteObjects hard limit
            deleted += _checked_delete_objects(s3, bucket=bucket, objects=batch)
            batch = []
    if batch:
        deleted += _checked_delete_objects(s3, bucket=bucket, objects=batch)
    return deleted


def _purge_s3_prefix_versions(s3, *, bucket: str, prefix: str) -> int:
    """Delete EVERY version and delete marker under ``prefix``; returns the count.

    For gold-carrying prefixes (``benchmark/…``) in the VERSIONED bundle
    bucket: a plain delete only stacks a delete marker and the objects stay
    readable as noncurrent versions — "delete" must mean purge there.
    """
    deleted = 0
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_object_versions(**kwargs)
        batch = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for group in ("Versions", "DeleteMarkers")
            for v in resp.get(group, [])
        ]
        for i in range(0, len(batch), 1000):  # DeleteObjects hard limit
            chunk = batch[i : i + 1000]
            deleted += _checked_delete_objects(s3, bucket=bucket, objects=chunk)
        if not resp.get("IsTruncated") or not resp.get("NextKeyMarker"):
            return deleted
        kwargs["KeyMarker"] = resp["NextKeyMarker"]
        if resp.get("NextVersionIdMarker"):
            kwargs["VersionIdMarker"] = resp["NextVersionIdMarker"]


def _delete_report_rows(
    ddb, registry_table: str, data_domain: str, dataset: str
) -> int:
    """Delete every benchmark ``REPORT#`` row for a dataset; returns the count.

    Same partition as the harvest status row (``HARVEST#<d>#<ds>``), so this is
    one Query on the sk prefix + per-row deletes. Without it a deleted dataset's
    reports (KPIs, gold-derived config) survived — and re-registering the same
    names resurrected them in the new owner's report list.
    """
    from okf_core import benchmark_report as br

    deleted = 0
    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :skp)",
        "ExpressionAttributeValues": {
            ":pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
            ":skp": {"S": br.report_sk_query_prefix()},
        },
    }
    while True:
        resp = ddb.query(**kwargs)
        for item in resp.get("Items", []):
            ddb.delete_item(
                TableName=registry_table,
                Key={"pk": item["pk"], "sk": item["sk"]},
            )
            deleted += 1
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return deleted


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


# The uploaded benchmark CSV can't be larger than the presign cap; guard the parse
# so a malformed multi-MB upload can't be read fully into the Lambda for nothing.
_BENCHMARK_CSV_MAX_BYTES = CONTEXT_UPLOAD_MAX_BYTES

# Inline cap for benchmark artifacts (report.json / traces.json) served through
# the Lambda. A synchronous Lambda response tops out at 6 MB, and a multi-run
# traces.json (EVERY attempt: questions × checks × runs) legitimately exceeds
# that — so anything past this cap is served as a short-lived presigned S3 GET
# instead of being streamed (which would die in the platform as an opaque 502).
_BENCHMARK_INLINE_MAX_BYTES = 4 * 1024 * 1024
_BENCHMARK_PRESIGN_EXPIRY_SECONDS = 300


# --------------------------------------------------------------------------- #
# Recursive-improvement benchmark CSV (S3, OFF the okf/ mount prefix)
# --------------------------------------------------------------------------- #


def _benchmark_prefix(data_domain: str, dataset: str) -> str:
    """S3 prefix for a dataset's benchmark inputs — a sibling of ``okf/``.

    Off the mount on purpose (see ``_BENCHMARK_PREFIX``): gold SQL must not be
    reachable by any harvest LLM role's file tools.
    """
    return f"{_BENCHMARK_PREFIX}{data_domain}/{dataset}/"


def benchmark_questions_key(data_domain: str, dataset: str) -> str:
    """The canonical S3 key of a dataset's benchmark ``questions.csv``."""
    return f"{_benchmark_prefix(data_domain, dataset)}questions.csv"


def presign_benchmark_upload(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    content_type: str | None,
) -> dict[str, Any]:
    """Presigned POST for the ``question,gold_sql`` CSV, pinned OFF the okf/ mount.

    Mirrors :func:`presign_context_upload` (20 MiB cap, server-enforced key) but
    targets the off-mount ``benchmark/<domain>/<dataset>/questions.csv`` key. The
    key is a single fixed filename (one active question set per dataset), so no
    client-supplied filename is accepted — the gold set can't be scattered.
    """
    key = benchmark_questions_key(data_domain, dataset)
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


def inspect_benchmark_questions(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> dict[str, Any]:
    """Fetch + parse the uploaded question set and report the extracted count.

    Uses the SAME parser the harvest runtime uses
    (``okf_core.benchmark_questions.load_questions``), so the ``count`` reported
    to the UI is exactly what a harvest would benchmark. Returns:

    * ``{"uploaded": False}`` when no CSV has been uploaded yet (a clean "upload
      one" state, not an error).
    * ``{"uploaded": True, "valid": False, "error": "..."}`` when the CSV is
      present but malformed (missing a required column, unparseable) — the
      user-facing format-validation feedback.
    * ``{"uploaded": True, "valid": True, "count", "total_in_csv", "dropped",
      "capped", "max_questions"}`` when it parses — ``count`` is the number of
      questions that will be benchmarked (after skipping blank rows and applying
      the 100-row cap), ``capped`` true iff rows were dropped by the cap.
    """
    from okf_core.benchmark_questions import (
        MAX_QUESTIONS,
        BenchmarkCSVError,
        load_questions,
    )

    key = benchmark_questions_key(data_domain, dataset)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - a missing object is "not uploaded yet"
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return {"uploaded": False, "key": key}
        # Other S3 errors are surfaced (misconfig, perms) rather than masked.
        raise ApiError(502, f"could not read benchmark CSV: {e}") from e

    raw = obj["Body"].read(_BENCHMARK_CSV_MAX_BYTES + 1)
    if len(raw) > _BENCHMARK_CSV_MAX_BYTES:
        return {
            "uploaded": True,
            "valid": False,
            "key": key,
            "error": f"CSV exceeds the {_BENCHMARK_CSV_MAX_BYTES // (1024 * 1024)} MiB limit",
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "uploaded": True,
            "valid": False,
            "key": key,
            "error": "CSV is not valid UTF-8 text",
        }

    try:
        loaded = load_questions(text)
    except BenchmarkCSVError as e:
        return {"uploaded": True, "valid": False, "key": key, "error": str(e)}

    if not loaded.questions:
        return {
            "uploaded": True,
            "valid": False,
            "key": key,
            "error": (
                "no valid rows — every row is missing a question or has no gold "
                "cell (gold_sql / expected_behavior)"
            ),
        }

    return {
        "uploaded": True,
        "valid": True,
        "key": key,
        "count": len(loaded.questions),
        "total_in_csv": loaded.total_in_csv,
        "dropped": loaded.dropped,
        "capped": loaded.dropped > 0,
        "max_questions": MAX_QUESTIONS,
        # Per-check participation over the KEPT questions — what a run of that
        # check would actually grade ("sql: 62, behavior: 25").
        "check_counts": loaded.check_counts,
    }


def _read_benchmark_artifact(
    s3, *, bucket: str, key: str, what: str, missing: str
) -> tuple[dict[str, Any] | None, str | None]:
    """GET + parse one off-mount benchmark JSON artifact (report or traces).

    Returns ``(doc, None)`` for an artifact small enough to ride the Lambda
    response, or ``(None, presigned_url)`` for a large one — the UI follows the
    short-lived S3 URL instead. Same trust boundary: the URL is vended only
    inside a Cognito-authed response and expires in minutes. A completed run's
    report must NEVER become unreadable because it grew (it used to 502 forever
    past a hard cap — after hours of paid solving). A missing object is a clean
    404 (an older run, or a run that never persisted); every other S3/decode
    failure is a 502.
    """
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - a missing object is a clean 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise ApiError(404, missing) from e
        raise ApiError(502, f"could not read benchmark {what}: {e}") from e

    def _presign() -> tuple[None, str]:
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=_BENCHMARK_PRESIGN_EXPIRY_SECONDS,
            )
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"could not presign benchmark {what}: {e}") from e
        return None, url

    if int(obj.get("ContentLength") or 0) > _BENCHMARK_INLINE_MAX_BYTES:
        return _presign()
    raw = obj["Body"].read(_BENCHMARK_INLINE_MAX_BYTES + 1)
    if len(raw) > _BENCHMARK_INLINE_MAX_BYTES:
        # ContentLength was absent or lied — degrade to the URL, never a 502.
        return _presign()
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError) as e:
        raise ApiError(502, f"benchmark {what} is not valid JSON: {e}") from e


# --------------------------------------------------------------------------- #
# Benchmark Studio: standalone runs + persisted reports
# --------------------------------------------------------------------------- #
#
# A benchmark run is NOT a harvest: it writes nothing to the bundle, takes no
# harvest lease (concurrent with harvests and with other runs — independent
# reports; Athena workgroup concurrency is the only shared resource), and its
# lifecycle lives on its own REPORT# row. The Control API mints the report id,
# writes the QUEUED row (config summary, flat scalars), and invokes the runtime
# with mode="benchmark"; the runtime owns everything after that (running →
# complete/failed, progress stamps, the S3 report artifacts). See
# okf_core.benchmark_report for the shared key/field shapes and
# docs/BENCHMARK_GUIDE.md for the product story.

_REPORT_APPLY_MAX = 50  # annotations per apply call (payload-bound guard)


def _report_pk(data_domain: str, dataset: str) -> str:
    return f"HARVEST#{data_domain}#{dataset}"


def _invoke_ack(resp: Any) -> dict[str, Any] | None:
    """Parse an ``invoke_agent_runtime`` ack body (best-effort, never raises).

    The harvest entrypoint answers SYNCHRONOUSLY with a small JSON ack —
    ``{"status": "accepted" | "rejected", ...}`` — inside the response's
    streaming body. A payload the runtime rejects otherwise looks exactly like
    success (the API itself returns 200), leaving the queued row to rot until
    the stale escape. Returns None when there is no parseable ack.
    """
    try:
        body = (resp or {}).get("response")
        if body is None:
            return None
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8")
        ack = json.loads(body)
        return ack if isinstance(ack, dict) else None
    except Exception:  # noqa: BLE001 - the ack is advisory; absence is not an error
        return None


def _fail_report_row(
    ddb, *, registry_table: str, data_domain: str, dataset: str,
    report_id: str, detail: str,
) -> None:
    """Best-effort flip of a REPORT# row to failed (start-path cleanup only)."""
    from okf_core import benchmark_report as br

    try:
        ddb.update_item(
            TableName=registry_table,
            Key={
                "pk": {"S": _report_pk(data_domain, dataset)},
                "sk": {"S": br.report_sk(report_id)},
            },
            UpdateExpression="SET #s = :s, detail = :d, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": br.STATUS_FAILED},
                ":d": {"S": detail[:1024]},
                ":u": {"S": _now_iso()},
            },
        )
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def _plain_attr(av: dict[str, Any] | None) -> Any:
    """One DynamoDB attribute value → a plain scalar (the row is flat by contract)."""
    if not isinstance(av, dict):
        return None
    if "S" in av:
        return av["S"]
    if "BOOL" in av:
        return bool(av["BOOL"])
    if "N" in av:
        raw = av["N"]
        try:
            return int(raw) if "." not in raw and "e" not in raw.lower() else float(raw)
        except ValueError:
            return float(raw)
    return None


def _report_row_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    """A REPORT# row → the wire dict (every non-key attribute, plainly typed)."""
    out: dict[str, Any] = {}
    for key, av in item.items():
        if key in ("pk", "sk"):
            continue
        out[key] = _plain_attr(av)
    return out


def start_benchmark_run(
    agentcore,
    ddb,
    s3,
    *,
    registry_table: str,
    runtime_arn: str,
    bucket: str,
    data_domain: str,
    dataset: str,
    checks: Any,
    runs: Any,
    solver_model: str | None,
    solver_effort: str | None,
    judge_model: str | None,
    judge_effort: str | None,
    version_id: str = "",
    requested_by: str = "",
    behavior_live_sql: bool = False,
) -> dict[str, Any]:
    """Validate the run config, write the QUEUED report row, invoke the runtime.

    Validation is the trust boundary (models were already catalog-validated in
    the route adapter): checks/runs normalize via ``okf_core.benchmark_report``;
    the question CSV must exist, parse, and carry at least one participant for
    an enabled check (fail HERE with a 400 the user can act on, not minutes
    later on the row); a pinned ``version_id`` must name a known complete
    marker. On invoke failure the row flips to ``failed`` so the list never
    shows a phantom queued run.
    """
    from okf_core import benchmark_report as br
    from okf_core.benchmark_questions import BenchmarkCSVError, load_questions

    try:
        checks = br.validate_checks(checks)
        runs = br.coerce_runs(runs)
    except br.BenchmarkRunConfigError as e:
        raise ApiError(400, str(e)) from e

    # Registration preflight: an unregistered dataset must fail HERE with a
    # 404, not minutes later on the row when the runtime tries to snapshot.
    if not ddb.get_item(
        TableName=registry_table,
        Key={"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": f"DATASET#{dataset}"}},
    ).get("Item"):
        raise ApiError(404, f"no such dataset: {data_domain}/{dataset}")

    # The question set: present, parseable, and USABLE for the enabled checks.
    questions_key = benchmark_questions_key(data_domain, dataset)
    try:
        obj = s3.get_object(Bucket=bucket, Key=questions_key)
        raw = obj["Body"].read(_BENCHMARK_CSV_MAX_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - a missing CSV is a user-fixable 400
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise ApiError(
                400, "no question set uploaded — upload the CSV first"
            ) from e
        raise ApiError(502, f"could not read the question set: {e}") from e
    if len(raw) > _BENCHMARK_CSV_MAX_BYTES:
        # Same gate as inspect: reject rather than silently truncate at the cap.
        raise ApiError(
            400,
            f"CSV exceeds the {_BENCHMARK_CSV_MAX_BYTES // (1024 * 1024)} MiB limit",
        )
    # Pin the exact object version graded (the bundle bucket is versioned):
    # the payload carries it so a re-upload between start and the runtime's
    # fetch can't swap the question set. "null" = written while unversioned.
    questions_version_id = str(obj.get("VersionId") or "")
    if questions_version_id.lower() == "null":
        questions_version_id = ""
    try:
        loaded = load_questions(raw.decode("utf-8"))
    except (BenchmarkCSVError, UnicodeDecodeError) as e:
        raise ApiError(400, f"invalid question set: {e}") from e
    enabled_counts = {c: loaded.check_counts.get(c, 0) for c in checks}
    if not any(enabled_counts.values()):
        raise ApiError(
            400,
            "no question participates in any enabled check "
            f"(per-check counts: {loaded.check_counts})",
        )

    version_id = (version_id or "").strip()
    if version_id:
        markers = s3_versions.list_complete_markers(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        )
        if not any(m.version_id == version_id for m in markers):
            raise ApiError(400, f"unknown bundle version: {version_id}")

    # Resolve the source BEFORE writing the queued row — a DDB read throwing
    # after the PutItem would orphan a queued row no runtime will ever touch.
    source = get_dataset_source(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    )

    now = _now_iso()
    report_id = br.new_report_id(
        now_compact=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
        token=uuid.uuid4().hex[:8],
    )
    session_id = runtime_session_id(data_domain, dataset, unique_token=report_id)

    # The QUEUED index row — flat scalars only (structure lives in the S3 JSON).
    item: dict[str, Any] = {
        "pk": {"S": _report_pk(data_domain, dataset)},
        "sk": {"S": br.report_sk(report_id)},
        "report_id": {"S": report_id},
        "status": {"S": br.STATUS_QUEUED},
        "created_at": {"S": now},
        "updated_at": {"S": now},
        "checks": {"S": ",".join(checks)},
        "runs": {"N": str(runs)},
        "version_id": {"S": version_id},
        "question_count": {"N": str(len(loaded.questions))},
        "runtime_session_id": {"S": session_id},
        "agg_status": {"S": br.AGG_IDLE},
    }
    for check, count in enabled_counts.items():
        item[f"count_{check}"] = {"N": str(count)}
    if solver_model:
        item["solver_model"] = {"S": solver_model}
    if solver_effort:
        item["solver_effort"] = {"S": solver_effort}
    if judge_model:
        item["judge_model"] = {"S": judge_model}
    if judge_effort:
        item["judge_effort"] = {"S": judge_effort}
    if behavior_live_sql:
        # Config summary the list renders as a badge; also a comparability
        # marker — scores taken with and without live SQL measure different
        # things. Written only when ON (absent == the classic wiki-only solver).
        item["behavior_live_sql"] = {"BOOL": True}
    if requested_by:
        item["requested_by"] = {"S": requested_by}
    ddb.put_item(
        TableName=registry_table,
        Item=item,
        ConditionExpression="attribute_not_exists(sk)",
    )

    payload: dict[str, Any] = {
        "data_domain": data_domain,
        "dataset": dataset,
        "mode": "benchmark",
        "report_id": report_id,
        br.FIELD_CHECKS: checks,
        br.FIELD_RUNS: runs,
        br.FIELD_VERSION_ID: version_id,
        br.FIELD_QUESTIONS_KEY: questions_key,
    }
    if questions_version_id:
        payload[br.FIELD_QUESTIONS_VERSION_ID] = questions_version_id
    if solver_model:
        payload[br.FIELD_SOLVER_MODEL] = solver_model
    if solver_effort:
        payload[br.FIELD_SOLVER_EFFORT] = solver_effort
    if judge_model:
        payload[br.FIELD_JUDGE_MODEL] = judge_model
    if judge_effort:
        payload[br.FIELD_JUDGE_EFFORT] = judge_effort
    if behavior_live_sql:
        payload[br.FIELD_BEHAVIOR_LIVE_SQL] = True
    if source:
        payload["source"] = source

    try:
        ack = _invoke_ack(
            agentcore.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                payload=json.dumps(payload).encode(),
                qualifier="DEFAULT",
            )
        )
    except Exception as e:  # noqa: BLE001 - flip the row so no phantom queued run
        _fail_report_row(
            ddb, registry_table=registry_table, data_domain=data_domain,
            dataset=dataset, report_id=report_id, detail=f"invoke failed: {e}",
        )
        raise ApiError(502, f"could not start the benchmark run: {e}") from e
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        # The invoke API returned 200, but the runtime's synchronous ack says
        # it will never run the payload — without reading it the queued row
        # would sit untouched until the stale escape.
        err = str(ack.get("error") or "the runtime rejected the payload")
        _fail_report_row(
            ddb, registry_table=registry_table, data_domain=data_domain,
            dataset=dataset, report_id=report_id,
            detail=f"runtime rejected: {err}",
        )
        raise ApiError(502, f"the runtime rejected the benchmark run: {err}")

    return {
        "report_id": report_id,
        "status": br.STATUS_QUEUED,
        "data_domain": data_domain,
        "dataset": dataset,
        "checks": checks,
        "runs": runs,
        "question_count": len(loaded.questions),
        "check_counts": enabled_counts,
    }


def list_benchmark_reports(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> dict[str, Any]:
    """Every report row for the dataset, newest first.

    Report ids are time-prefixed, so the sk RANGE ordering IS chronological —
    one Query, ``ScanIndexForward=False``, no GSI needed.
    """
    from okf_core import benchmark_report as br

    kwargs: dict[str, Any] = {
        "TableName": registry_table,
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :skp)",
        "ExpressionAttributeValues": {
            ":pk": {"S": _report_pk(data_domain, dataset)},
            ":skp": {"S": br.report_sk_query_prefix()},
        },
        "ScanIndexForward": False,
    }
    reports: list[dict[str, Any]] = []
    while True:
        resp = ddb.query(**kwargs)
        reports.extend(_report_row_to_dict(item) for item in resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return {"data_domain": data_domain, "dataset": dataset, "reports": reports}


def _get_report_row(
    ddb, *, registry_table: str, data_domain: str, dataset: str, report_id: str
) -> dict[str, Any]:
    from okf_core import benchmark_report as br

    if not br.is_valid_report_id(report_id):
        raise ApiError(400, f"invalid report id: {report_id!r}")
    resp = ddb.get_item(
        TableName=registry_table,
        Key={
            "pk": {"S": _report_pk(data_domain, dataset)},
            "sk": {"S": br.report_sk(report_id)},
        },
    )
    item = resp.get("Item")
    if not item:
        raise ApiError(404, "no such benchmark report")
    return item


def _report_row_is_stale(item: dict[str, Any]) -> bool:
    """True when the row's last heartbeat predates the lease-stale cutoff.

    ``updated_at`` is stamped by every runtime progress tick (and on start),
    so it is the liveness signal; ``started_at``/``created_at`` are fallbacks
    for a row that died before its first tick. Mirrors the harvest lease's
    staleness escape — an AgentCore session can't outlive 8h, so an "active"
    row untouched for longer is a dead job, not one worth waiting for.
    """
    ts = (
        _s(item.get("updated_at"))
        or _s(item.get("started_at"))
        or _s(item.get("created_at"))
    )
    if not ts:
        return True  # no heartbeat at all — nothing to wait for
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
    ).isoformat()
    return ts < cutoff


def get_benchmark_report(
    s3,
    ddb,
    *,
    bucket: str,
    registry_table: str,
    data_domain: str,
    dataset: str,
    report_id: str,
) -> dict[str, Any]:
    """One report: the index row + (when persisted) the full S3 document.

    ``report`` is None while the run is still queued/running or after a failure
    — the row's ``status``/``detail`` tell that story; the JSON appears with
    ``complete``. A report too large to ride the Lambda response comes back as
    ``report_url`` (a short-lived presigned GET) instead — the api client
    follows it transparently.
    """
    from okf_core import benchmark_report as br

    item = _get_report_row(
        ddb, registry_table=registry_table, data_domain=data_domain,
        dataset=dataset, report_id=report_id,
    )
    report_doc = None
    report_url = None
    try:
        report_doc, report_url = _read_benchmark_artifact(
            s3,
            bucket=bucket,
            key=br.report_key(data_domain, dataset, report_id),
            what="report",
            missing="no report document",
        )
    except ApiError as e:
        if e.status != 404:
            raise
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "row": _report_row_to_dict(item),
        "report": report_doc,
        "report_url": report_url,
    }


def get_benchmark_report_traces(
    s3, *, bucket: str, data_domain: str, dataset: str, report_id: str
) -> dict[str, Any]:
    """The report's solver-traces document (large; the UI fetches it lazily).

    Past the inline cap the response is ``{report_id, traces_url}`` (presigned
    GET) instead of the document — traces are the artifact that most routinely
    outgrows the Lambda response (every attempt, questions × checks × runs).
    """
    from okf_core import benchmark_report as br

    if not br.is_valid_report_id(report_id):
        raise ApiError(400, f"invalid report id: {report_id!r}")
    doc, url = _read_benchmark_artifact(
        s3,
        bucket=bucket,
        key=br.traces_key(data_domain, dataset, report_id),
        what="solver traces",
        missing="no solver traces for that report",
    )
    if url:
        return {"report_id": report_id, "traces_url": url}
    return doc


def delete_benchmark_report(
    s3,
    ddb,
    *,
    bucket: str,
    registry_table: str,
    data_domain: str,
    dataset: str,
    report_id: str,
) -> dict[str, Any]:
    """Delete one report: every S3 object under its prefix + the index row.

    Refused (409) while the run (or an annotation aggregation) is genuinely
    active — the runtime would recreate row attrs and orphan artifacts
    mid-write; cancel is "let it finish, then delete". A row stuck in an
    active state whose last heartbeat is older than the lease-stale cutoff is
    treated as DEAD and deletable (mirrors the harvest lease escape: an
    AgentCore session can't outlive 8h, so such a row is a killed job whose
    terminal write was lost — without this escape it would be an undeletable
    zombie polled forever). The runtime side can't resurrect it either:
    ``update_report_row`` is conditional on the row existing. History is
    otherwise unbounded by decision (no TTL), so this is the only way a report
    leaves the list.
    """
    from okf_core import benchmark_report as br

    item = _get_report_row(
        ddb, registry_table=registry_table, data_domain=data_domain,
        dataset=dataset, report_id=report_id,
    )
    status = _s(item.get("status"))
    stale = _report_row_is_stale(item)
    if status in (br.STATUS_QUEUED, br.STATUS_RUNNING) and not stale:
        raise ApiError(409, f"report is {status}; wait for it to finish")
    if _s(item.get("agg_status")) == br.AGG_RUNNING and not stale:
        raise ApiError(
            409, "an annotation aggregation is running; wait for it to finish"
        )

    # ALL versions: the bundle bucket is versioned, and a plain delete_object
    # only stacks a delete marker — the gold-carrying artifacts would stay
    # readable as noncurrent versions. Deletion means PURGE (CONVENTIONS.md).
    deleted = _purge_s3_prefix_versions(
        s3, bucket=bucket, prefix=br.report_prefix(data_domain, dataset, report_id)
    )
    ddb.delete_item(
        TableName=registry_table,
        Key={
            "pk": {"S": _report_pk(data_domain, dataset)},
            "sk": {"S": br.report_sk(report_id)},
        },
    )
    return {"deleted": True, "report_id": report_id, "objects_deleted": deleted}


def start_annotation_aggregation(
    agentcore,
    ddb,
    *,
    registry_table: str,
    runtime_arn: str,
    data_domain: str,
    dataset: str,
    report_id: str,
) -> dict[str, Any]:
    """Kick the aggregator (mode=aggregate_annotations) for a COMPLETE report.

    409 unless the report is complete and no aggregation is genuinely running —
    an ``agg_status=running`` whose heartbeat predates the lease-stale cutoff
    is a dead aggregator (killed container, lost terminal write) and is
    retryable, mirroring the delete route's escape. The flip to running is
    CONDITIONAL on that same predicate so two concurrent POSTs can't both
    start an aggregator. The aggregator inherits the report's judge model
    (stored in the report's config) — deliberately no model choice here.
    """
    from okf_core import benchmark_report as br

    item = _get_report_row(
        ddb, registry_table=registry_table, data_domain=data_domain,
        dataset=dataset, report_id=report_id,
    )
    if _s(item.get("status")) != br.STATUS_COMPLETE:
        raise ApiError(409, "the report is not complete yet")
    if _s(item.get("agg_status")) == br.AGG_RUNNING and not _report_row_is_stale(item):
        raise ApiError(409, "an aggregation is already running for this report")

    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
    ).isoformat()
    try:
        ddb.update_item(
            TableName=registry_table,
            Key={
                "pk": {"S": _report_pk(data_domain, dataset)},
                "sk": {"S": br.report_sk(report_id)},
            },
            # agg_detail cleared so a retry doesn't show a previous failure.
            UpdateExpression="SET agg_status = :s, agg_detail = :empty, updated_at = :u",
            # Closes the check-then-set race: re-assert "not already running
            # unless stale" atomically at the flip itself.
            ConditionExpression="agg_status <> :s OR updated_at < :cutoff",
            ExpressionAttributeValues={
                ":s": {"S": br.AGG_RUNNING},
                ":u": {"S": _now_iso()},
                ":empty": {"S": ""},
                ":cutoff": {"S": stale_cutoff},
            },
        )
    except Exception as e:  # noqa: BLE001 - a lost race is a clean 409
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiError(
                409, "an aggregation is already running for this report"
            ) from e
        raise
    session_id = runtime_session_id(
        data_domain, dataset, unique_token=f"agg-{report_id}-{uuid.uuid4().hex[:8]}"
    )
    payload = {
        "data_domain": data_domain,
        "dataset": dataset,
        "mode": "aggregate_annotations",
        "report_id": report_id,
    }

    def _revert(detail: str) -> None:
        try:
            ddb.update_item(
                TableName=registry_table,
                Key={
                    "pk": {"S": _report_pk(data_domain, dataset)},
                    "sk": {"S": br.report_sk(report_id)},
                },
                UpdateExpression=(
                    "SET agg_status = :s, agg_detail = :d, updated_at = :u"
                ),
                ExpressionAttributeValues={
                    ":s": {"S": br.AGG_FAILED},
                    ":d": {"S": detail[:1024]},
                    ":u": {"S": _now_iso()},
                },
            )
        except Exception:  # noqa: BLE001 - best-effort revert
            pass

    try:
        ack = _invoke_ack(
            agentcore.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                payload=json.dumps(payload).encode(),
                qualifier="DEFAULT",
            )
        )
    except Exception as e:  # noqa: BLE001 - revert so the button isn't wedged
        _revert(f"invoke failed: {e}")
        raise ApiError(502, f"could not start the aggregation: {e}") from e
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        err = str(ack.get("error") or "the runtime rejected the payload")
        _revert(f"runtime rejected: {err}")
        raise ApiError(502, f"the runtime rejected the aggregation: {err}")
    return {"report_id": report_id, "agg_status": br.AGG_RUNNING}


def apply_report_annotations(
    ddb,
    *,
    registry_table: str,
    annotations_table: str,
    data_domain: str,
    dataset: str,
    report_id: str,
    user_sub: str,
    author: str,
    annotations: Any,
) -> dict[str, Any]:
    """Batch-create the user's SELECTED final annotations from a report.

    ``annotations`` is ``[{note, concept_id?}, ...]`` — the human-reviewed
    subset of the aggregated set (possibly edited). Items are created under the
    ACTING user's sub with ``submitted_via="benchmark"``; a normal
    annotation-harvest then folds them into the wiki (zero new harvest
    machinery). The de-identification boundary moved from machine to human: the
    user saw every note before it exists. ``report_id`` must name an existing
    report row (404 otherwise — the route carries it, so a stale/foreign id
    must not mint annotations) and is stamped on each created annotation for
    provenance.
    """
    user_sub = _require_user_sub(user_sub)
    # Validates the id shape (400) and the row's existence (404).
    _get_report_row(
        ddb, registry_table=registry_table, data_domain=data_domain,
        dataset=dataset, report_id=report_id,
    )
    if not isinstance(annotations, list) or not annotations:
        raise ApiError(400, "annotations must be a non-empty list of {note, concept_id}")
    if len(annotations) > _REPORT_APPLY_MAX:
        raise ApiError(
            400, f"too many annotations in one apply (max {_REPORT_APPLY_MAX})"
        )

    # Validate EVERYTHING before writing ANYTHING (mirroring create_annotation's
    # own checks, including parse_concept_id). create_annotation 400s on a bad
    # entry, and a mid-batch 400 used to leave the earlier annotations silently
    # committed with their ids never returned — the natural retry then filed
    # duplicates the follow-on annotation harvest would apply twice.
    normalized: list[tuple[str, str]] = []
    for entry in annotations:
        if not isinstance(entry, dict):
            raise ApiError(400, "each annotation must be an object with a note")
        note = str(entry.get("note") or "").strip()
        if not note:
            raise ApiError(400, "each annotation needs a non-empty note")
        concept_id = str(entry.get("concept_id") or "").strip().strip("/")
        if not concept_id:
            concept_id = anno.DATASET_WIDE_CONCEPT
        else:
            try:
                parse_concept_id(concept_id)
            except ValueError as e:
                raise ApiError(400, f"invalid concept_id: {concept_id!r}") from e
        normalized.append((note, concept_id))

    created: list[dict[str, Any]] = []
    for note, concept_id in normalized:
        created.append(
            create_annotation(
                ddb,
                annotations_table=annotations_table,
                data_domain=data_domain,
                dataset=dataset,
                user_sub=user_sub,
                author=author,
                concept_id=concept_id,
                note=note,
                submitted_via=anno.SUBMITTED_VIA_BENCHMARK,
                report_id=report_id,
            )
        )
    return {"created": created, "count": len(created)}


# --------------------------------------------------------------------------- #
# Harvest control (AgentCore invoke + status row)
# --------------------------------------------------------------------------- #


def harvest_lease_held(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> bool:
    """Whether the per-dataset harvest lease is currently held (and fresh).

    The read-side mirror of the ACQUIRERS' conditionals — the same active
    statuses and ``HARVEST_LEASE_STALE_SECONDS`` escape as
    :func:`acquire_harvest_lease`, AND :func:`acquire_repromote_lease`'s
    extra takeover clause (a ``mode=repromote`` row still ``queued`` after
    ``REPROMOTE_LEASE_STALE_SECONDS`` is a dead 30s-capped Lambda, stealable
    immediately). Without that second escape a dead repromote would block
    the Reasoning Sync for the full 8h even though a repromote retry could
    take the lease right now. For callers that must NOT start work while a
    harvest runs but take no lease of their own. Fail-open on read errors:
    this gate is a courtesy; the building lease still serializes the authors
    themselves.
    """
    try:
        item = (
            ddb.get_item(
                TableName=registry_table,
                Key={
                    "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                    "sk": {"S": "STATUS"},
                },
            ).get("Item")
            or {}
        )
        status = _s(item.get("status"))
        if status not in ("queued", "running"):
            return False
        stale_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
        ).isoformat()
        started = _s(item.get("started_at"))
        if not started or started < stale_cutoff:
            return False
        # The repromote twin (constants defined near acquire_repromote_lease).
        if _s(item.get("mode")) == REPROMOTE_MODE and status == "queued":
            repromote_cutoff = (
                datetime.now(timezone.utc)
                - timedelta(seconds=REPROMOTE_LEASE_STALE_SECONDS)
            ).isoformat()
            if started < repromote_cutoff:
                return False
        return True
    except Exception:  # noqa: BLE001 - fail-open (see docstring)
        log.warning("harvest lease read failed (treating as free)", exc_info=True)
        return False


def refuse_if_guardrails_building(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> None:
    """409 when the dataset's policy author currently holds the build lock.

    The harvest status row goes terminal at the bundle commit, so an
    in-flight guardrails authoring run is invisible to the harvest lease —
    but it is still reading the just-committed wiki, so bundle-WRITING work
    (harvest/annotation/cross starts, repromotes, dataset deletion) waits it
    out. Freshness escape + fail-open live in
    ``okf_aws.ar_policy.build_lock_active``. Called INSIDE
    :func:`acquire_harvest_lease` / :func:`acquire_repromote_lease` — the
    choke points every bundle-writing start already goes through — so a new
    trigger path can never forget the gate (CONVENTIONS.md "Harvest status");
    only non-lease writers (dataset deletion) call it directly.
    """
    if ar_policy.build_lock_active(
        ddb, registry_table, data_domain=data_domain, dataset=dataset
    ):
        raise ApiError(
            409,
            f"guardrails are being authored for {data_domain}/{dataset}; "
            "retry when the build finishes",
        )


def acquire_harvest_lease(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    mode: str,
    session_id: str,
    detail: str | None = None,
    cross_target: str | None = None,
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
    of one dataset can never race on the shared bundle directory. The
    guardrails build lock is checked HERE for the same reason — a trigger
    path that exists at all gets the gate for free (raises ApiError 409; the
    incremental orchestrator's resource-API twin does its own check to keep
    its distinct ``skipped_guardrails_building`` action).
    """
    refuse_if_guardrails_building(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    )
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
    if cross_target is not None:
        # A cross-dataset discovery run records its counterpart so the status
        # surface can show WHO the run is against (mirrors `repromote_target`).
        item["cross_target"] = {"S": cross_target}
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


def resolve_cross_target(
    ddb,
    s3,
    glue,
    *,
    registry_table: str,
    bucket: str,
    data_domain: str,
    dataset: str,
    source: dict[str, Any] | None,
    target_data_domain: str,
    target_dataset: str,
) -> dict[str, Any]:
    """Validate a cross-mode target and build the payload's ``target`` block.

    The trust boundary for ``mode="cross"`` (Roadmap §5, OSS flat-trust mode).
    Enforced here, before any lease is taken:

    * the target is not the dataset itself (400);
    * the target is a REGISTERED dataset mapping (404) — the runtime builds a
      source from this block, so an unregistered name must fail fast;
    * both sides are GLUE-backed (400) — v1's verification path is qualified
      Athena SQL spanning Glue databases; cross-source pairs (Redshift↔Glue)
      have no common engine to verify against;
    * the two sides resolve to DIFFERENT Glue databases (400) — registration
      forces a glue dataset's name to equal its database, so the same dataset
      name under two domains is the same physical data; "cross-referencing" a
      database against itself would verify degenerate self-joins;
    * the target's Glue database exists (404, same probe as the source side);
    * BOTH bundles are ready (409): the run snapshots the target's published
      wiki at start (read-only — nothing is ever written into the target's
      bundle) — a mid-write or never-harvested bundle on either side has
      nothing coherent to reference.

    Returns ``{data_domain, dataset, source, domain_description?,
    domain_context?}`` — the resolved block the runtime consumes verbatim.
    """
    if target_data_domain == data_domain and target_dataset == dataset:
        raise ApiError(400, "cross mode target must differ from the dataset itself")

    x_db = source_glue_database(source) if source is not None else dataset
    if not x_db:
        raise ApiError(
            400,
            "cross-dataset harvest supports Glue-backed datasets only "
            f"({data_domain}/{dataset} is not glue-backed)",
        )

    target_source = get_dataset_source(
        ddb,
        registry_table=registry_table,
        data_domain=target_data_domain,
        dataset=target_dataset,
    )
    if target_source is None:
        raise ApiError(
            404, f"no such dataset: {target_data_domain}/{target_dataset}"
        )
    target_db = source_glue_database(target_source)
    if not target_db:
        raise ApiError(
            400,
            "cross-dataset harvest supports Glue-backed datasets only "
            f"({target_data_domain}/{target_dataset} is not glue-backed)",
        )
    if target_db == x_db:
        raise ApiError(
            400,
            f"cross target {target_data_domain}/{target_dataset} maps the SAME "
            f"Glue database as {data_domain}/{dataset} ({x_db!r}) — a dataset "
            "cannot cross-reference its own data",
        )
    assert_glue_database_exists(glue, target_db)

    # Both bundles must be published (complete marker): the target's wiki is the
    # run's discovery surface, and this dataset's docs are the context the cross
    # docs complement.
    if not is_bundle_ready(s3, bucket, data_domain, dataset):
        raise ApiError(
            409,
            f"{data_domain}/{dataset} has no published bundle yet — run a full "
            "harvest before a cross-dataset one",
        )
    if not is_bundle_ready(s3, bucket, target_data_domain, target_dataset):
        raise ApiError(
            409,
            f"target {target_data_domain}/{target_dataset} has no published "
            "bundle yet — harvest it before cross-referencing it",
        )

    target: dict[str, Any] = {
        "data_domain": target_data_domain,
        "dataset": target_dataset,
        "source": target_source,
    }
    target_domain_meta = get_domain(
        ddb, registry_table=registry_table, data_domain=target_data_domain
    )
    if target_domain_meta:
        if target_domain_meta.get("description"):
            target["domain_description"] = target_domain_meta["description"]
        if target_domain_meta.get("context"):
            target["domain_context"] = target_domain_meta["context"]
    return target


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
    cross_target: dict[str, Any] | None = None,
    model: str | None = None,
    effort: str | None = None,
    subagent_model: str | None = None,
    subagent_effort: str | None = None,
    reviewer_model: str | None = None,
    reviewer_effort: str | None = None,
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
    # First-class source descriptor ({type, ...config}) so the runtime dispatches on
    # the source type instead of assuming a Glue database named by the dataset.
    # Best-effort: a missing/legacy mapping omits it and the runtime defaults to a
    # glue source by dataset name (back-compat).
    source = get_dataset_source(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    )
    if source:
        payload["source"] = source
    # Per-harvest model/effort overrides (already validated against the catalog in
    # the route adapter): the supervisor pair, and the separate sub-agent pair
    # (authors/reviewers/benchmark). Omitted -> the runtime uses its deploy-time
    # env default (supervisor) / the supervisor's config (sub-agents).
    if model:
        payload["model"] = model
    if effort:
        payload["effort"] = effort
    if subagent_model:
        payload["subagent_model"] = subagent_model
    if subagent_effort:
        payload["subagent_effort"] = subagent_effort
    if reviewer_model:
        payload["reviewer_model"] = reviewer_model
    if reviewer_effort:
        payload["reviewer_effort"] = reviewer_effort
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

    # Dataset-level guidance (shared authoring instructions) — steers this harvest
    # and, on success, the runner stamps guidance_applied_version so it clears
    # dirty. Passed with its version so the stamp records exactly what was applied.
    # DELIBERATELY omitted on a cross run: guidance is dataset-scoped authoring
    # steering, and cross docs are shared verbatim with another dataset — one
    # side's operator instructions must not silently shape the pair's docs.
    if mode != "cross":
        try:
            g = get_dataset_guidance(
                ddb, registry_table=registry_table,
                data_domain=data_domain, dataset=dataset,
            )
            if g.get("guidance"):
                payload["dataset_guidance"] = g["guidance"]
                payload["dataset_guidance_version"] = g["guidance_updated_at"]
        except ApiError:
            pass  # no mapping row yet (shouldn't happen at harvest time) — omit


    if mode == "incremental":
        if not changed_table:
            raise ApiError(400, "incremental mode requires 'changed_table'")
        payload["changed_table"] = changed_table
        # Incremental keeps per-dataset affinity (deterministic session).
        session_id = runtime_session_id(data_domain, dataset)
    else:
        if mode == "cross":
            # The route adapter resolved + validated the target (see
            # resolve_cross_target); the runtime consumes the block verbatim.
            if not cross_target:
                raise ApiError(400, "cross mode requires 'target_dataset'")
            payload["target"] = cross_target
        # A full (or cross) harvest is one-shot: use a FRESH session per trigger
        # so it gets a new microVM (with a clean S3 Files mount) instead of
        # reattaching to a warm/stale one from a prior run.
        session_id = runtime_session_id(
            data_domain, dataset, unique_token=uuid.uuid4().hex
        )

    pk = f"HARVEST#{data_domain}#{dataset}"
    # Acquire the per-dataset lease before invoking. Rejected (409) if a harvest
    # for this dataset is already in flight, so concurrent runs can't race on the
    # shared bundle directory. A live guardrails authoring run blocks too (the
    # gate lives inside the lease acquisition).
    if not acquire_harvest_lease(
        ddb,
        registry_table=registry_table,
        data_domain=data_domain,
        dataset=dataset,
        mode=mode,
        session_id=session_id,
        cross_target=(
            f"{cross_target['data_domain']}/{cross_target['dataset']}"
            if mode == "cross" and cross_target
            else None
        ),
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
            # empty until the runtime advances past `queued`. The subagent pair
            # is empty when no separate sub-agent override was chosen (the
            # sub-agents then ran on the supervisor's config).
            "model": _s(item.get("model")),
            "effort": _s(item.get("effort")),
            "subagent_model": _s(item.get("subagent_model")),
            "subagent_effort": _s(item.get("subagent_effort")),
            "reviewer_model": _s(item.get("reviewer_model")),
            "reviewer_effort": _s(item.get("reviewer_effort")),
            # Cross-dataset discovery runs record their counterpart
            # ("<domain>/<dataset>", stamped at lease time) so the UI can show
            # WHO the run is against, not just the mode.
            "cross_target": _s(item.get("cross_target")),
        }
    ready = is_bundle_ready(s3, bucket, data_domain, dataset)
    # A live post-harvest guardrails authoring run (the mapping row's
    # `building` flip, fresh) — the harvest row is terminal by then, so this
    # is the UI's only signal that the follow-on step is running (and why a
    # new harvest/repromote would 409 right now). Knowably false while the
    # run itself is still in flight (the build only starts post-terminal, and
    # a build in progress at start would have 409'd the lease), so the poll
    # skips the extra GetItem for the whole — potentially hours-long — run.
    in_flight = status.get("status") in ("queued", "running")
    guardrails_building = (
        False
        if in_flight
        else ar_policy.build_lock_active(
            ddb, registry_table, data_domain=data_domain, dataset=dataset
        )
    )
    return {
        "data_domain": data_domain,
        "dataset": dataset,
        "status": status,
        "ready": ready,
        "guardrails_building": guardrails_building,
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
    # Bounded failure snippet, present on a failed tool_result or an errored
    # subagent event only — the UI surfaces it on the failed row/square.
    if rec.get("error"):
        out["error"] = rec.get("error")
    # Full agent-message markdown (agent events) or a sub-agent dispatch's
    # full brief (task tool_call / subagent start events) — the UI renders it
    # in a modal / the fleet drill-in's Input tab.
    if rec.get("full"):
        out["full"] = rec.get("full")
    # A sub-agent dispatch's final answer (task tool_result / subagent
    # complete events), bounded by the emitter — the fleet drill-in's Output
    # tab.
    if rec.get("result"):
        out["result"] = rec.get("result")
    # Correlation key pairing a tool_call with its tool_result (the UI folds them
    # into one row). Present on tool events only.
    if rec.get("call_id"):
        out["call_id"] = rec.get("call_id")
    # Sub-agent fleet fields (KIND_SUBAGENT): phase = start|complete|error,
    # plus `update` (a mid-flight I/O patch carrying `full`/`result` for an
    # existing square); batch groups a fan-out (the eval id), sub_id is the
    # per-square id.
    for k in ("phase", "batch", "sub_id", "subagent_type"):
        if rec.get(k):
            out[k] = rec.get(k)
    # Running token-usage snapshot (kind="usage"): cumulative counts for the whole
    # run. Passed through verbatim as a dict so the UI can show a running total.
    if isinstance(rec.get("usage"), dict):
        out["usage"] = rec["usage"]
    # The lint gate's structured report (successful lint_bundle tool_result
    # only, bounded by the emitter) — the UI badges the feed row with its
    # error/warning counts and renders the findings in a modal on click.
    if isinstance(rec.get("lint"), dict):
        out["lint"] = rec["lint"]
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
    """Ensure ``key`` is served bundle content under this dataset's prefix.

    Guards the "read one file" endpoint so a caller cannot pass an arbitrary key
    (another dataset's file, a ``.context/`` upload, ``../`` traversal) and read
    it back. Any non-dot ``.md`` under the prefix qualifies — INCLUDING
    ``index.md``/``log.md``, which are published content (the version-diff UI
    reads them) even though they are not concepts; dot-prefixed dirs
    (``.context``/``.harvest``/``.metadata``) remain authoring state and are
    rejected (a ``..`` segment starts with a dot, so traversal is covered too).
    """
    prefix = bundle_prefix(data_domain, dataset)
    if key.startswith(prefix) and key.endswith(".md"):
        parts = key[len(prefix) :].split("/")
        if all(p and not p.startswith(".") for p in parts):
            return key
    raise ApiError(400, f"key is not bundle content under this bundle: {key!r}")


def read_bundle_file(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    key: str,
    version_id: str = "",
) -> dict[str, Any]:
    """Return one bundle ``.md`` file's raw text after validating the key.

    ``version_id`` (optional) reads a specific S3 object version — how the
    version-diff UI fetches both sides of a file for the rendered rich view;
    it also works under a delete marker (a removed file's old content stays
    readable). Empty = the live latest, exactly as before.
    """
    key = _validate_bundle_key(key, data_domain=data_domain, dataset=dataset)
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        obj = s3.get_object(**kwargs)
    except Exception as e:  # noqa: BLE001 - map missing object/version to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound", "NoSuchVersion", "InvalidArgument"):
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
# Bundle versions, diff & repromote (S3 object-version history)
# --------------------------------------------------------------------------- #
#
# A bundle version = one `complete` write of `.harvest/state.json`; identity is
# that marker object's S3 VersionId (see okf_aws.s3_versions + CONVENTIONS.md).
# Repromote rewrites the live prefix to equal a chosen version via CopyObject
# with source VersionIds — append-only (new head, fresh complete marker carrying
# repromoted_from/by) — and the untouched reindex pipeline re-converges the
# vector index from the resulting object events. The status GET reads
# convergence off the freshness table's VEC# rows, which reindex advances only
# AFTER the vector work succeeds.

REPROMOTE_MODE = "repromote"

# A repromote runs synchronously inside this 30s-capped Lambda, so — unlike a
# real harvest, which legitimately runs for hours (HARVEST_LEASE_STALE_SECONDS
# = 8h) — a repromote row still `queued` after this window is provably dead.
# The lease takeover clause + the status GET's stalled_lease/can_retry both key
# off it, giving the UI one-click retry instead of an 8h wedge.
REPROMOTE_LEASE_STALE_SECONDS = 120

# EventBridge->SQS->reindex normally converges in seconds; past this we stop
# claiming "converging" and surface `stalled` (an event may have been dropped —
# the honest recovery is re-running the idempotent repromote).
REPROMOTE_STALL_SECONDS = 600

# Convergence compares this Lambda's clock (repromote started_at) against the
# reindex Lambda's clock (freshness updated_at); a small epsilon absorbs skew.
_CONVERGE_EPSILON = timedelta(seconds=2)


def list_bundle_versions(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> dict[str, Any]:
    """The bundle's published versions, newest first (empty list = never harvested)."""
    markers = s3_versions.list_complete_markers(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    return {"versions": [{**m.descriptor(), "tables": m.tables} for m in markers]}


def get_bundle_diff(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    from_version: str = "",
    to_version: str = "",
) -> dict[str, Any]:
    """Diff two versions (defaults: previous -> current; `to=live` = working state)."""
    try:
        return s3_versions.bundle_diff(
            s3,
            bucket=bucket,
            data_domain=data_domain,
            dataset=dataset,
            from_version=from_version,
            to_version=to_version,
        )
    except ValueError as e:
        raise ApiError(400, str(e)) from e


def acquire_repromote_lease(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    detail: str,
    target_version_id: str = "",
) -> str | None:
    """Take the harvest lease for a repromote. Returns ``started_at`` or None.

    Same conditional PutItem as :func:`acquire_harvest_lease` — repromote rides
    the existing ``queued -> complete|failed`` lifecycle (no new status value) —
    plus ONE extra takeover clause: a prior ``mode=repromote`` row still
    ``queued`` after :data:`REPROMOTE_LEASE_STALE_SECONDS` is a dead run (the
    writer is a 30s-capped Lambda), so a retry may steal it immediately instead
    of waiting out the 8h harvest staleness. Harvest rows are unaffected.
    The guardrails build lock gates here too (inside the choke point, like
    :func:`acquire_harvest_lease`) — the restore rewrites the wiki a live
    author is reading.
    """
    refuse_if_guardrails_building(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    )
    now = _now_iso()
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
    ).isoformat()
    repromote_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=REPROMOTE_LEASE_STALE_SECONDS)
    ).isoformat()
    try:
        ddb.put_item(
            TableName=registry_table,
            Item={
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": "STATUS"},
                "status": {"S": "queued"},
                "mode": {"S": REPROMOTE_MODE},
                "started_at": {"S": now},
                "updated_at": {"S": now},
                "runtime_session_id": {"S": f"repromote-{uuid.uuid4().hex}"},
                "detail": {"S": detail[:1024]},
                # Persisted so a DEAD repromote's one-click retry knows which
                # version to re-POST (the status GET echoes it on stalled_lease).
                "repromote_target": {"S": target_version_id},
            },
            ConditionExpression=(
                "attribute_not_exists(pk) "
                "OR NOT (#s = :queued OR #s = :running) "
                "OR started_at < :stale "
                "OR (#m = :rmode AND #s = :queued AND started_at < :rstale)"
            ),
            ExpressionAttributeNames={"#s": "status", "#m": "mode"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
                ":stale": {"S": stale_cutoff},
                ":rmode": {"S": REPROMOTE_MODE},
                ":rstale": {"S": repromote_cutoff},
            },
        )
        return now
    except Exception as e:  # noqa: BLE001 - a lost condition means "already leased"
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return None
        raise


#: Publisher breadcrumb on the ``policy_rebuild`` detail. Only ever reaches the
#: consumer's log — the rebuild pipeline is the same whatever moved the wiki —
#: but it makes a bare detail classifiable when read off the queue.
_POLICY_REBUILD_REASON = "repromote"


def _signal_policy_rebuild(
    ddb,
    events,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Nudge the AR rebuild authority after a wiki mutation no harvest covered.

    A repromote replaces the live bundle without a finalize, so any Automated
    Reasoning policy built off the superseded content now describes the wrong
    wiki. This flags the dataset's registry row ``stale`` (which is what the
    check-time usability gate reads) and publishes one ``policy_rebuild`` event
    so the incremental service starts the repair in minutes instead of at the
    next nightly reconcile.

    Purely an accelerator, hence total: it never raises and never blocks. The
    fingerprint gate makes a policy built off older sources unusable rather than
    wrong, and the nightly hash-verify rebuilds it regardless, so a swallowed
    failure here costs freshness, never truth. A dataset with no AR attrs flags
    nothing (the conditional write must never CREATE row state), but the event
    still publishes — for a dataset restored to a version that predates its
    policy document (or that never had one), this is the AUTOMATIC first
    authoring: the rebuild authority sees no usable document and dispatches.

    ``events`` doubles as the accelerator's structural off switch — handlers read
    no environment, so an unconfigured publisher is how "policy builds are off"
    reaches this code, and None then skips BOTH halves: with no rebuild authority
    listening there is nothing to accelerate, and the row must not be moved out
    of ``ready`` with no one able to move it back.

    Returns a small status dict (what actually happened, for logging and for
    tests to assert on); never raises. Deliberately NOT surfaced in the
    repromote response: the restore's outcome does not depend on it.
    """
    if events is None:
        return {"flagged": False, "published": False, "reason": "no events client"}
    flagged = False
    published = False
    try:
        flagged = ar_policy.flag_stale(
            ddb, registry_table, data_domain=data_domain, dataset=dataset
        )
        resp = events.put_events(
            Entries=[
                {
                    "Source": policy_rebuild.EVENT_SOURCE,
                    "DetailType": policy_rebuild.DETAIL_TYPE_POLICY_REBUILD,
                    "Detail": json.dumps(
                        policy_rebuild.build_detail(
                            data_domain, dataset, reason=_POLICY_REBUILD_REASON
                        )
                    ),
                }
            ]
        )
        # PutEvents reports per-entry rejections INSIDE a 200 response, so a
        # clean return is not delivery.
        failed = (resp or {}).get("FailedEntryCount", 0)
        published = not failed
        if failed:
            import logging

            logging.getLogger("control_api").warning(
                "policy_rebuild event rejected for %s/%s: %s",
                data_domain,
                dataset,
                (resp.get("Entries") or [{}])[0].get("ErrorCode", ""),
            )
    except Exception as e:  # noqa: BLE001 - the accelerator must never fail a repromote
        import logging

        logging.getLogger("control_api").warning(
            "policy rebuild signal failed for %s/%s: %s",
            data_domain,
            dataset,
            type(e).__name__,
        )
        return {"flagged": flagged, "published": published, "reason": type(e).__name__}
    return {"flagged": flagged, "published": published}


# --------------------------------------------------------------------------- #
# Reasoning (AR policy) — the UI's Reasoning page
# --------------------------------------------------------------------------- #
#
# Policy documents are ALWAYS ON per dataset (the v1-era `ar_enrolled` opt-in
# is retired — the LLM-judge engine has no per-account policy cap to budget):
# any dataset with a committed wiki authors automatically after each harvest
# and re-authors on wiki changes. Pre-existing datasets are deliberately NOT
# backfilled in bulk — their first document comes from the Sync button here,
# their next harvest/increment, or a repromote.


def _reasoning_row(ddb, registry_table: str, data_domain: str, dataset: str) -> dict:
    """The raw mapping row, or ApiError(404) — reasoning needs a registered dataset."""
    item = ddb.get_item(
        TableName=registry_table,
        Key={
            "pk": {"S": f"DOMAIN#{data_domain}"},
            "sk": {"S": f"DATASET#{dataset}"},
        },
    ).get("Item")
    if not item:
        raise ApiError(404, f"no such dataset: {data_domain}/{dataset}")
    return item


def _row_str(item: dict[str, Any], name: str) -> str:
    return str((item.get(name) or {}).get("S") or "")


def _publish_policy_rebuild(
    events, *, data_domain: str, dataset: str, reason: str, force: bool = False
) -> bool:
    """Publish one ``policy_rebuild`` event; True when accepted. Never raises.

    The rebuild authority's conditional ``building`` flip makes duplicates
    harmless, and the nightly reconcile makes a lost event a freshness delay,
    never a correctness problem — so publishing is always best-effort.
    ``force`` bypasses the authority's rebuild-iff-changed skip (manual Sync
    only — see ``okf_core.policy_rebuild.FIELD_FORCE``).
    """
    if events is None:
        return False
    try:
        resp = events.put_events(
            Entries=[
                {
                    "Source": policy_rebuild.EVENT_SOURCE,
                    "DetailType": policy_rebuild.DETAIL_TYPE_POLICY_REBUILD,
                    "Detail": json.dumps(
                        policy_rebuild.build_detail(
                            data_domain, dataset, reason=reason, force=force
                        )
                    ),
                }
            ]
        )
        return not (resp or {}).get("FailedEntryCount", 0)
    except Exception:  # noqa: BLE001 - the nightly reconcile is the safety net
        import logging

        logging.getLogger("control_api").warning(
            "policy_rebuild publish failed for %s/%s", data_domain, dataset,
            exc_info=True,
        )
        return False


# How long a TERMINAL harvest status row may coexist with a bundle commit
# marker still reading `in_progress` before that pair means a dead rewrite.
# The runner writes its terminal status straight to DynamoDB while the
# marker's own overwrite is still riding the S3 Files mount's write-back
# cache (observed ~62s late — see the flush-wait comment in
# harvest/runner.py), so for a minute after every SUCCESSFUL harvest the
# terminal-row + in_progress-marker combination is the normal flush lag, not
# a run that died. Sized well past the observed lag; the cost of waiting is
# only that a genuinely dead rewrite reads "rewriting" a few minutes longer.
WIKI_DEAD_REWRITE_GRACE_SECONDS = 600


def _harvest_terminal_settled(
    ddb, *, registry_table: str, data_domain: str, dataset: str
) -> bool:
    """Whether the harvest status row went terminal beyond the flush grace window.

    The dead-rewrite verdict may only fire once this is True: a terminal row
    younger than ``WIKI_DEAD_REWRITE_GRACE_SECONDS`` is a successful run whose
    marker overwrite is still flushing through the S3 mount. A missing row or
    timestamp settles immediately — there is no recent terminal write whose
    flush could still be in flight. Fail-open on read errors, mirroring
    :func:`harvest_lease_held`: an unreadable row cannot veto the verdict.
    """
    try:
        item = (
            ddb.get_item(
                TableName=registry_table,
                Key={
                    "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                    "sk": {"S": "STATUS"},
                },
            ).get("Item")
            or {}
        )
    except Exception:  # noqa: BLE001 - fail-open (see docstring)
        log.warning("harvest status read failed (treating as settled)", exc_info=True)
        return True
    ts = _s(item.get("updated_at")) or _s(item.get("started_at"))
    if not ts:
        return True
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=WIKI_DEAD_REWRITE_GRACE_SECONDS)
    ).isoformat()
    return ts < cutoff


def get_reasoning_status(
    ddb,
    s3,
    *,
    registry_table: str,
    bucket: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Everything the Reasoning page renders, in one call.

    ``up_to_date`` is the user-facing face of the fingerprint gate: the stored
    ``ar_source_hash`` (what the document was authored from) against a hash
    computed from the LIVE wiki right now — except while a build is RUNNING,
    where it is ``None`` (the verdict is moot mid-build and computing it
    would download the whole source corpus on every few-second poll).
    ``sources`` is the live source-file set — what an authoring run started
    today would read — and ``policies`` is the authored document's entries
    (empty until the first authoring run completes), each individually
    trackable by its stable id.
    """
    item = _reasoning_row(ddb, registry_table, data_domain, dataset)
    status = _row_str(item, ar_policy.ATTR_BUILD_STATUS)
    stored_hash = _row_str(item, ar_policy.ATTR_SOURCE_HASH)

    # Read the marker's STATUS, not just the ready boolean: a harvest mid-run
    # overwrites the marker with `in_progress` (mark_in_progress), and that
    # is NOT "no wiki yet" — the previous wiki's authored guardrails still
    # exist and must stay on screen; only the freshness verdict is moot (the
    # sources are being rewritten under us, so a hash would be noise).
    # "Rewriting" additionally requires a LIVE harvest lease: a failed or
    # cancelled run leaves the marker at `in_progress` FOREVER (nothing
    # restores it on the failure path), and without the cross-check the page
    # would promise an auto re-author that is never coming. "Dead" further
    # requires the terminal status to have SETTLED past the mount-flush grace
    # window: every successful harvest spends ~a minute with a terminal row
    # and an `in_progress` marker (the marker overwrite lags through the S3
    # mount), and calling that dead flashed a false "run a new harvest" after
    # each success — see WIKI_DEAD_REWRITE_GRACE_SECONDS.
    marker = bundle_marker_status(s3, bucket, data_domain, dataset)
    wiki_ready = marker == "complete"
    wiki_dead_rewrite = (
        marker == "in_progress"
        and not harvest_lease_held(
            ddb, registry_table=registry_table, data_domain=data_domain,
            dataset=dataset,
        )
        and _harvest_terminal_settled(
            ddb, registry_table=registry_table, data_domain=data_domain,
            dataset=dataset,
        )
    )
    wiki_rewriting = marker == "in_progress" and not wiki_dead_rewrite
    building = status == ar_policy.BUILD_BUILDING
    if building or wiki_rewriting:
        # The UI polls every few seconds while a build runs, and the response
        # only needs the source PATHS then (freshness is moot mid-build — the
        # page shows the build state, not the fingerprint verdict). LIST-only
        # instead of downloading the whole source corpus per poll.
        source_paths = ar_policy.list_source_paths(s3, bucket, data_domain, dataset)
        fresh_hash = None
    else:
        sources = ar_policy.gather_sources(s3, bucket, data_domain, dataset)
        source_paths = [rel for rel, _content in sources]
        fresh_hash = ar_policy.hash_sources(sources)

    up_to_date: bool | None = None
    # No verdict mid-build/mid-rewrite — and none against a DEAD rewrite's
    # half-written bundle either (its hash is noise, not a freshness signal).
    if stored_hash and not building and not wiki_rewriting and not wiki_dead_rewrite:
        up_to_date = bool(fresh_hash) and fresh_hash == stored_hash

    policies: list[dict[str, Any]] = []
    if status:
        from okf_core import policy_doc as pdoc

        doc = ar_policy.read_policy_doc(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        )
        if doc is not None:
            try:
                policies = pdoc.parse_policies(doc)
            except pdoc.PolicyDocError:
                policies = []  # a bad artifact reads as "no policies yet"

    return {
        "data_domain": data_domain,
        "dataset": dataset,
        # A wiki must exist first (the policy is derived FROM it); until then
        # the page explains instead of offering a dead Sync. An empty
        # `status` with a ready wiki means "never authored" — the page offers
        # Sync as the manual first authoring (no bulk backfill exists by
        # design), and a wiki without policy-source files can sync but would
        # produce no rules — the page says so instead of showing a dead state.
        "wiki_ready": wiki_ready,
        # A harvest mid-write is its own state, never "no wiki yet".
        "wiki_rewriting": wiki_rewriting,
        "reason": (
            None
            if wiki_ready
            else (
                "a harvest is rewriting this wiki — guardrails re-author "
                "automatically when it commits"
                if wiki_rewriting
                else (
                    "the last harvest did not complete — run a new harvest "
                    "(or restore a version) to publish the wiki"
                    if wiki_dead_rewrite
                    else "no wiki yet — run a harvest first"
                )
            )
        ),
        "has_sources": bool(source_paths),
        "status": status,
        "built_at": _row_str(item, "ar_built_at"),
        "build_detail": _row_str(item, "ar_build_detail"),
        "up_to_date": up_to_date,
        "sources": source_paths,
        "policies": policies,
    }


def trigger_reasoning_sync(
    ddb,
    s3,
    events,
    *,
    registry_table: str,
    bucket: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """The Reasoning page's manual trigger: author now, unconditionally.

    Doubles as the FIRST authoring for a pre-existing dataset (there is no
    bulk backfill by design — a dataset that predates the feature authors on
    its first Sync, harvest, or repromote) and as the operator's re-author
    button. The event carries ``force``: the rebuild authority skips its
    rebuild-iff-changed check, because a manual Sync's sources may be
    unchanged while the AUTHORING moved on (a new model, effort, or prompt) —
    without force, Sync on a ready dataset acknowledged "queued" and then
    silently did nothing (live 2026-08-03). An in-flight build still wins
    (the building lease is honored). Refused while no complete wiki exists:
    the policy is derived from the bundle, so there is nothing to author
    from yet. Also refused while the HARVEST lease is held — the reverse of
    the build-lock gate on harvest starts: an author dispatched mid-harvest
    would gather sources from a bundle being wiped and rewritten under it
    (and its `building` flip would then 409 the operator's other work). The
    finished harvest authors on its own anyway, so nothing is lost by
    refusing.
    """
    _reasoning_row(ddb, registry_table, data_domain, dataset)  # 404 if unknown
    if events is None:
        raise ApiError(409, "reasoning is not enabled on this deployment")
    # Lease check FIRST: mid-harvest the commit marker reads in_progress, so
    # the bundle-ready check would misreport a rewrite as "no wiki yet".
    if harvest_lease_held(
        ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
    ):
        raise ApiError(
            409,
            f"a harvest for {data_domain}/{dataset} is in flight; it authors "
            "the guardrails itself when it finishes",
        )
    if not is_bundle_ready(s3, bucket, data_domain, dataset):
        raise ApiError(409, "no wiki yet — run a harvest first")
    queued = _publish_policy_rebuild(
        events,
        data_domain=data_domain,
        dataset=dataset,
        reason="manual_sync",
        force=True,
    )
    return {"queued": queued}


def get_reasoning_document(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """The dataset's ``policies.yaml`` — the authored policy document.

    A separate endpoint (not a ``get_reasoning_status`` field) because the
    status call is polled every few seconds during authoring and this
    document can be tens of kilobytes: it is fetched only when the user opens
    the viewer. The live file is version-faithful by construction (authoring
    rewrites it), so no version parameter exists. Absent (never authored)
    reads as ``exists: false`` rather than an error: the page decides how to
    render that.
    """
    text = ar_policy.read_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    return {"exists": text is not None, "text": text or ""}


def repromote_bundle(
    s3,
    ddb,
    *,
    bucket: str,
    registry_table: str,
    data_domain: str,
    dataset: str,
    version_id: str,
    requested_by: str,
    events=None,
) -> dict[str, Any]:
    """Make an older bundle version the new head. Synchronous; seconds.

    Flow: validate the target -> take the harvest lease (mode=repromote; a
    concurrent harvest gets its usual 409, concurrent incremental events degrade
    to skipped_locked) -> write an ``in_progress`` marker so ``is_bundle_ready``
    honestly reports not-ready during the copy window -> restore the snapshot
    (CopyObject + delete markers; append-only, idempotent) -> write a FRESH
    ``complete`` marker carrying ``repromoted_from``/``repromoted_by`` -> record
    the touched vector keys in the REPROMOTE registry item (the convergence
    manifest; deleted keys are unlistable after the fact, so this must be
    captured at write time) -> release the lease as ``complete``.

    Deliberately does NOT touch the freshness table's Glue-version rows:
    repromote is a content rollback, not a pin — the next genuine catalog
    change may legitimately harvest over it (documented in CONVENTIONS.md and
    the UI confirm dialog). Failure mid-write releases the lease as ``failed``
    and leaves the marker ``in_progress`` — the same posture as a crashed
    harvest; the recovery is retrying (idempotent) or re-harvesting.

    ``events`` is the EventBridge publisher for the AR policy-rebuild
    accelerator (:func:`_signal_policy_rebuild`); None turns the accelerator off
    entirely. It fires once the restore is committed and never affects the
    outcome, so a repromote behaves identically with it wired or not.
    """
    markers = s3_versions.list_complete_markers(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    if not markers:
        raise ApiError(404, f"no published versions for {data_domain}/{dataset}")
    target = next((m for m in markers if m.version_id == version_id), None)
    if target is None:
        raise ApiError(400, f"unknown bundle version: {version_id}")
    if target.is_current:
        raise ApiError(409, "that version is already current")

    label = target.completed_at or target.version_id[:8]
    started_at = acquire_repromote_lease(
        ddb,
        registry_table=registry_table,
        data_domain=data_domain,
        dataset=dataset,
        detail=f"repromote to {label}",
        target_version_id=target.version_id,
    )
    if started_at is None:
        raise ApiError(
            409,
            f"a harvest for {data_domain}/{dataset} is already queued or running",
        )

    marker_key = state_marker_key(data_domain, dataset)
    try:
        s3.put_object(
            Bucket=bucket,
            Key=marker_key,
            Body=(
                json.dumps(
                    {
                        "status": "in_progress",
                        "data_domain": data_domain,
                        "dataset": dataset,
                        "started_at": started_at,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
        snapshot = s3_versions.snapshot_at(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset, marker=target
        )
        live = s3_versions.live_snapshot(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        )
        copied, deleted = s3_versions.restore_snapshot(
            s3,
            bucket=bucket,
            data_domain=data_domain,
            dataset=dataset,
            snapshot=snapshot,
            live=live,
        )
        completed_at = _now_iso()
        put = s3.put_object(
            Bucket=bucket,
            Key=marker_key,
            Body=(
                json.dumps(
                    {
                        "status": "complete",
                        "data_domain": data_domain,
                        "dataset": dataset,
                        "tables": target.tables,
                        "completed_at": completed_at,
                        "table_versions": target.table_versions,
                        "repromoted_from": target.version_id,
                        "repromoted_by": requested_by or "",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
        new_version_id = put.get("VersionId", "")
        # index.md/log.md copies never index (reserved files), so they are not
        # part of the convergence set.
        copied_vkeys = [
            loc.vector_key for k in copied if (loc := parse_bundle_key(k)) is not None
        ]
        deleted_vkeys = [
            loc.vector_key for k in deleted if (loc := parse_bundle_key(k)) is not None
        ]
        ddb.put_item(
            TableName=registry_table,
            Item={
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": "REPROMOTE"},
                "started_at": {"S": started_at},
                "completed_at": {"S": completed_at},
                "target_version_id": {"S": target.version_id},
                "new_version_id": {"S": new_version_id},
                "requested_by": {"S": requested_by or ""},
                "copied": {"L": [{"S": k} for k in copied_vkeys]},
                "deleted": {"L": [{"S": k} for k in deleted_vkeys]},
                "total": {"N": str(len(copied_vkeys) + len(deleted_vkeys))},
            },
        )
        _set_status_row(
            ddb,
            registry_table=registry_table,
            data_domain=data_domain,
            dataset=dataset,
            status="complete",
            detail=f"repromoted to {label}",
        )
        # Signal only from here: with the copy done AND the fresh ``complete``
        # marker written, "the live wiki is now the restored version" is durably
        # true. Vector convergence is a LATER, separately polled concept — the
        # rebuild must not wait on it (this is a 30s-capped Lambda).
        _signal_policy_rebuild(
            ddb,
            events,
            registry_table=registry_table,
            data_domain=data_domain,
            dataset=dataset,
        )
        return {
            "status": "complete",
            "copied": len(copied),
            "deleted": len(deleted),
            "target_version_id": target.version_id,
            "new_version_id": new_version_id,
            "converged": False,
        }
    except ValueError as e:  # oversized restore refused — release + 400
        _set_status_row(
            ddb,
            registry_table=registry_table,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"repromote failed: {e}",
        )
        raise ApiError(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 - release the lease, surface a 502
        _set_status_row(
            ddb,
            registry_table=registry_table,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"repromote failed: {type(e).__name__}",
        )
        raise ApiError(502, f"repromote failed: {e}") from e


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def get_repromote_status(
    ddb,
    *,
    registry_table: str,
    freshness_table: str,
    data_domain: str,
    dataset: str,
) -> dict[str, Any]:
    """Convergence poll for the last repromote (+ dead-lease detection).

    The product-level "repromote is done" is the vector index serving the
    promoted content: a touched key is converged when its ``VEC#<key>``
    freshness row's ``updated_at`` (advanced by reindex only AFTER the vector
    work succeeds) is >= the repromote's ``started_at`` minus a clock-skew
    epsilon. Also detects a repromote that died mid-write (status row stuck
    ``queued`` with ``mode=repromote`` past the writer's possible lifetime) and
    answers ``stalled_lease`` + ``can_retry`` so the UI offers one-click retry —
    the retry POST steals the dead lease via the takeover clause.
    """
    pk = f"HARVEST#{data_domain}#{dataset}"
    now = datetime.now(timezone.utc)

    status_item = ddb.get_item(
        TableName=registry_table, Key={"pk": {"S": pk}, "sk": {"S": "STATUS"}}
    ).get("Item")
    if (
        status_item
        and _s(status_item.get("mode")) == REPROMOTE_MODE
        and _s(status_item.get("status")) == "queued"
    ):
        started_raw = _s(status_item.get("started_at")) or ""
        started = _parse_iso(started_raw)
        if started and (now - started).total_seconds() > REPROMOTE_LEASE_STALE_SECONDS:
            return {
                "state": "stalled_lease",
                "can_retry": True,
                "started_at": started_raw,
                "target_version_id": _s(status_item.get("repromote_target")) or "",
                "detail": "a repromote died mid-write; retry to take over its lease",
            }
        return {"state": "running", "can_retry": False, "started_at": started_raw}

    rep = ddb.get_item(
        TableName=registry_table, Key={"pk": {"S": pk}, "sk": {"S": "REPROMOTE"}}
    ).get("Item")
    if not rep:
        raise ApiError(404, f"no repromote recorded for {data_domain}/{dataset}")

    started_at = _s(rep.get("started_at")) or ""
    copied = [v.get("S", "") for v in rep.get("copied", {}).get("L", [])]
    deleted = [v.get("S", "") for v in rep.get("deleted", {}).get("L", [])]
    keys = [k for k in copied + deleted if k]
    started_dt = _parse_iso(started_at)
    cutoff = (started_dt - _CONVERGE_EPSILON).isoformat() if started_dt else started_at

    # Freshness rows for every touched key (BatchGetItem pages at 100). Rows
    # left in UnprocessedKeys after the retries count as pending — honest
    # "don't know yet" beats claiming convergence.
    pending: list[str] = []
    for i in range(0, len(keys), 100):
        batch = keys[i : i + 100]
        request: dict[str, Any] = {
            freshness_table: {
                "Keys": [
                    {"pk": {"S": f"VEC#{k}"}, "sk": {"S": "SEQ"}} for k in batch
                ],
                "ConsistentRead": True,
            }
        }
        rows: dict[str, str] = {}
        for _ in range(3):
            resp = ddb.batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(freshness_table, []):
                rows[_s(item.get("pk")) or ""] = _s(item.get("updated_at")) or ""
            request = resp.get("UnprocessedKeys") or {}
            if not request.get(freshness_table):
                break
        for k in batch:
            if rows.get(f"VEC#{k}", "") < cutoff:
                pending.append(k)

    if not pending:
        state = "converged"
    elif started_dt and (now - started_dt).total_seconds() > REPROMOTE_STALL_SECONDS:
        state = "stalled"
    else:
        state = "converging"
    return {
        "state": state,
        "can_retry": state == "stalled",
        "done": len(keys) - len(pending),
        "total": len(keys),
        "pending": pending[:20],
        "started_at": started_at,
        "completed_at": _s(rep.get("completed_at")),
        "target_version_id": _s(rep.get("target_version_id")),
        "new_version_id": _s(rep.get("new_version_id")),
        "requested_by": _s(rep.get("requested_by")),
    }


# --------------------------------------------------------------------------- #
# Wiki annotations (user-scoped feedback -> annotation-mode re-harvest)
# --------------------------------------------------------------------------- #
#
# Item shape + the orphan/quote-match invariants live in okf_core.annotations
# (imported as ``anno``). Isolation is STRUCTURAL: the partition key embeds the
# caller's immutable Cognito ``sub``, so a Query can only return that user's own
# annotations. Every handler here therefore takes ``user_sub`` and refuses to run
# without it — a missing subject must never collapse users into a shared partition.

# Length caps so one annotation can't bloat the item (DynamoDB 400 KB item cap)
# or the harvest payload. Generous for real feedback; a hard boundary against abuse.
_ANNO_QUOTE_MAX = 2000
_ANNO_CONTEXT_MAX = 200  # prefix / suffix each
_ANNO_NOTE_MAX = 4000

# Cap the number of live annotations one run sends to the agent. Each carries a
# quote (<=2 KB) + note (<=4 KB) + context, and the whole set is JSON-encoded into
# ONE InvokeAgentRuntime payload; an unbounded set could exceed the payload byte
# limit and fail the invoke unrecoverably (the user could never apply them). 100
# is far above any real single review and bounds the worst case well under the limit.
_ANNO_RUN_MAX = 100


def _require_user_sub(user_sub: str | None) -> str:
    """The verified caller subject, or a 401 — never fall through to no scope."""
    if not user_sub:
        raise ApiError(401, "authenticated user required for annotations")
    return user_sub


def _int_or_none(value: Any) -> int | None:
    """Parse a DynamoDB numeric string to int, tolerating corruption.

    A stored ``block_line`` that isn't a parseable int (data corruption / a future
    writer bug) must NOT 500 the whole list read — one bad row shouldn't break the
    sidebar. Returns None on anything non-integer.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _annotation_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Deserialize a DynamoDB annotation item to the UI/JSON shape."""
    return {
        "annotation_id": _s(item.get("annotation_id")),
        "concept_id": _s(item.get("concept_id")),
        "author": _s(item.get("author")),
        "quote": _s(item.get("quote")),
        "prefix": _s(item.get("prefix")),
        "suffix": _s(item.get("suffix")),
        # Tolerate a corrupt N so one bad row can't 500 the whole list read.
        "block_line": _int_or_none(item.get("block_line", {}).get("N")),
        "note": _s(item.get("note")),
        # "ui" (default) or "agent" — the chat agent filing on the user's behalf.
        "submitted_via": _s(item.get("submitted_via")) or anno.SUBMITTED_VIA_UI,
        # Benchmark provenance: which report the note was applied from.
        "report_id": _s(item.get("report_id")),
        "status": _s(item.get("status")),
        "outcome": _s(item.get("outcome")),
        "resolution": _s(item.get("resolution")),
        "created_at": _s(item.get("created_at")),
        "updated_at": _s(item.get("updated_at")),
    }


def create_annotation(
    ddb,
    *,
    annotations_table: str,
    data_domain: str,
    dataset: str,
    user_sub: str | None,
    author: str | None,
    concept_id: str,
    quote: str = "",
    note: str = "",
    prefix: str = "",
    suffix: str = "",
    block_line: int | None = None,
    submitted_via: str = "",
    report_id: str = "",
) -> dict[str, Any]:
    """Persist one open annotation, scoped to the caller.

    ``quote`` is the selected passage (a TextQuoteSelector anchor); ``prefix`` /
    ``suffix`` are the minimal disambiguating context the UI captured; ``note`` is
    the user's feedback. ``author`` is the human-facing label for display only —
    ISOLATION is via ``user_sub`` in the partition key, never ``author``.

    ``quote`` is OPTIONAL: an empty quote is an UNANCHORED note — page-level
    general feedback, or dataset-level when ``concept_id`` is the
    ``anno.DATASET_WIDE_CONCEPT`` sentinel. Unanchored notes skip quote
    re-anchoring in the harvest sweep (they orphan only if the doc vanishes;
    dataset-wide never). ``submitted_via`` records provenance ("ui" default,
    "agent" when the chat agent files on the user's behalf); ``report_id``
    (benchmark provenance only) records which report the note came from.
    """
    user_sub = _require_user_sub(user_sub)
    if not concept_id:
        raise ApiError(400, "missing required field: concept_id")
    # A concept id is a slash path of validated segments (never contains '#',
    # which is our sk delimiter). Reject anything else at the boundary.
    try:
        parse_concept_id(concept_id)
    except ValueError as e:
        raise ApiError(400, f"invalid concept_id: {concept_id!r}") from e
    quote = (quote or "").strip()
    note = (note or "").strip()
    if not note:
        raise ApiError(400, "missing required field: note")
    if submitted_via not in (
        "",
        anno.SUBMITTED_VIA_UI,
        anno.SUBMITTED_VIA_AGENT,
        anno.SUBMITTED_VIA_BENCHMARK,
    ):
        raise ApiError(400, f"invalid submitted_via: {submitted_via!r}")

    annotation_id = uuid.uuid4().hex
    now = _now_iso()
    item: dict[str, Any] = {
        "pk": {"S": anno.annotation_pk(data_domain, dataset, user_sub)},
        "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
        "data_domain": {"S": data_domain},
        "dataset": {"S": dataset},
        "concept_id": {"S": concept_id},
        "annotation_id": {"S": annotation_id},
        "quote": {"S": quote[:_ANNO_QUOTE_MAX]},
        "note": {"S": note[:_ANNO_NOTE_MAX]},
        "status": {"S": anno.STATUS_OPEN},
        "submitted_via": {"S": submitted_via or anno.SUBMITTED_VIA_UI},
        "created_at": {"S": now},
        "updated_at": {"S": now},
    }
    if author:
        item["author"] = {"S": author}
    if prefix:
        item["prefix"] = {"S": prefix[:_ANNO_CONTEXT_MAX]}
    if suffix:
        item["suffix"] = {"S": suffix[:_ANNO_CONTEXT_MAX]}
    if block_line is not None:
        item["block_line"] = {"N": str(int(block_line))}
    if report_id:
        item["report_id"] = {"S": report_id}
    ddb.put_item(TableName=annotations_table, Item=item)
    return _annotation_to_dict(item)


def _query_user_annotations(
    ddb,
    *,
    annotations_table: str,
    data_domain: str,
    dataset: str,
    user_sub: str,
    concept_id: str | None = None,
) -> list[dict[str, Any]]:
    """Raw DynamoDB items for the caller's annotations in this dataset.

    Single-partition Query on the user-scoped pk (optionally narrowed to one
    concept via a ``begins_with`` on the sk). Returns raw items so callers that
    need to UpdateItem (the orphan sweep) keep the keys.
    """
    pk = anno.annotation_pk(data_domain, dataset, user_sub)
    kwargs: dict[str, Any] = {
        "TableName": annotations_table,
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": {"S": pk}},
    }
    if concept_id is not None:
        kwargs["KeyConditionExpression"] = "pk = :pk AND begins_with(sk, :skp)"
        kwargs["ExpressionAttributeValues"][":skp"] = {
            "S": anno.concept_sk_prefix(concept_id)
        }
    items: list[dict[str, Any]] = []
    while True:
        resp = ddb.query(**kwargs)
        items.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return items


def list_annotations(
    ddb,
    *,
    annotations_table: str,
    data_domain: str,
    dataset: str,
    user_sub: str | None,
    concept_id: str | None = None,
) -> list[dict[str, Any]]:
    """List the caller's annotations (optionally for one concept), newest first."""
    user_sub = _require_user_sub(user_sub)
    items = _query_user_annotations(
        ddb,
        annotations_table=annotations_table,
        data_domain=data_domain,
        dataset=dataset,
        user_sub=user_sub,
        concept_id=concept_id,
    )
    out = [_annotation_to_dict(it) for it in items]
    out.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return out


def delete_annotation(
    ddb,
    *,
    annotations_table: str,
    data_domain: str,
    dataset: str,
    user_sub: str | None,
    concept_id: str,
    annotation_id: str,
) -> dict[str, Any]:
    """Delete one of the caller's annotations.

    Conditioned on the item existing so a delete of someone else's id (which
    can't be in this caller's partition anyway) or a stale id is a clean 404, not
    a silent no-op.
    """
    user_sub = _require_user_sub(user_sub)
    key = {
        "pk": {"S": anno.annotation_pk(data_domain, dataset, user_sub)},
        "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
    }
    try:
        ddb.delete_item(
            TableName=annotations_table,
            Key=key,
            ConditionExpression="attribute_exists(pk)",
        )
    except Exception as e:  # noqa: BLE001 - map a missing item to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiError(404, f"no such annotation: {annotation_id}") from e
        raise
    return {"deleted": True, "annotation_id": annotation_id}


def _resolve_annotation(
    ddb,
    *,
    annotations_table: str,
    pk: str,
    sk: str,
    outcome: str,
    resolution: str,
    now_iso: str,
    expires_at: int,
) -> None:
    """Flip one annotation to resolved with an outcome + comment + 7-day TTL.

    Best-effort per item (a single failed write must not abort the whole sweep):
    the caller logs and continues. ``expires_at`` (epoch seconds) is set ONLY
    here — an open annotation never carries it, so only resolved rows expire.
    """
    try:
        ddb.update_item(
            TableName=annotations_table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression=(
                "SET #s = :s, outcome = :o, resolution = :r, "
                "updated_at = :u, expires_at = :e"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": anno.STATUS_RESOLVED},
                ":o": {"S": outcome},
                ":r": {"S": (resolution or "")[:_ANNO_NOTE_MAX]},
                ":u": {"S": now_iso},
                ":e": {"N": str(expires_at)},
            },
        )
    except Exception:  # noqa: BLE001 - one bad write shouldn't fail the batch
        import logging

        logging.getLogger("control_api").warning(
            "failed to resolve annotation %s (continuing)", sk, exc_info=True
        )


def _set_status_row(
    ddb,
    *,
    registry_table: str,
    data_domain: str,
    dataset: str,
    status: str,
    detail: str,
) -> None:
    """Terminal-set the harvest STATUS row from the Control API (skip/abort paths).

    Used both when the orphan sweep resolves EVERY annotation (nothing to invoke →
    ``complete``) and to RELEASE the lease if the pre-flight itself fails
    (``failed``). Flip is guarded to in-flight so a raced cancel/terminal write
    wins. Best-effort: a failed status write never masks the caller's own outcome.
    """
    try:
        ddb.update_item(
            TableName=registry_table,
            Key={
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": "STATUS"},
            },
            UpdateExpression="SET #s = :s, updated_at = :u, detail = :d",
            ConditionExpression="attribute_not_exists(pk) OR #s = :queued OR #s = :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":u": {"S": _now_iso()},
                ":d": {"S": detail[:1024]},
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
            },
        )
    except Exception:  # noqa: BLE001 - status is best-effort, lease still frees
        import logging

        logging.getLogger("control_api").warning(
            "failed to close status row for %s/%s (continuing)",
            data_domain,
            dataset,
            exc_info=True,
        )


def _set_annotation_status(
    ddb, *, annotations_table: str, pk: str, sk: str, status: str, now_iso: str
) -> bool:
    """Set one annotation's ``status`` (+ updated_at). Best-effort; returns success.

    Used to flip survivors to ``in_review`` and to revert them to ``open``. Never
    raises — a single failed write is logged and skipped so a batch keeps moving.
    """
    if not (pk and sk):
        return False
    try:
        ddb.update_item(
            TableName=annotations_table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="SET #s = :s, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": status}, ":u": {"S": now_iso}},
        )
        return True
    except Exception:  # noqa: BLE001 - best-effort per item
        import logging

        logging.getLogger("control_api").warning(
            "failed to set annotation %s -> %s (continuing)", sk, status, exc_info=True
        )
        return False


def trigger_annotation_harvest(
    agentcore,
    ddb,
    s3,
    *,
    registry_table: str,
    annotations_table: str,
    bucket: str,
    runtime_arn: str,
    data_domain: str,
    dataset: str,
    user_sub: str | None,
    domain_meta: dict[str, Any] | None = None,
    scope: str | None = None,
    annotation_ids: list[str] | None = None,
    cross_target: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    subagent_model: str | None = None,
    subagent_effort: str | None = None,
    reviewer_model: str | None = None,
    reviewer_effort: str | None = None,
) -> dict[str, Any]:
    """Run the caller's open annotations through an annotation-mode re-harvest.

    ``model``/``effort`` (+ the ``subagent_*``/``reviewer_*`` pairs) mirror
    ``trigger_harvest``'s per-harvest override knobs — same three scopes
    (supervisor / sub-agents / reviewer), same fallback when omitted (the
    runtime's deploy-time default). Applying annotations is a harvest like any
    other (``mode="annotated"``); giving it no way to carry an override meant
    every apply silently ran on ``OKF_HARVEST_MODEL``/``OKF_HARVEST_EFFORT``
    regardless of what the operator had picked for full harvests of this
    dataset — a bug, not a deliberate restriction.

    ``scope`` narrows WHICH annotations the run applies when the bundle carries
    cross-dataset docs (an ``external/`` subtree): ``"dataset"`` = only notes on
    the dataset's own docs, ``"cross"`` = only notes on ``external/`` docs.
    None (the default, and the only case when no ``external/`` exists) applies
    everything. A ``"cross"``-scoped run also IGNORES dataset guidance — cross
    docs are authored without it by design (see ``mode="cross"``), so a dirty
    guidance neither triggers nor rides a cross-scoped run.

    ``annotation_ids`` narrows the run further to an EXPLICIT selection (the
    UI's annotation picker — partial application). None applies every in-scope
    note; a list applies only the listed ids. An empty list is valid: with a
    dirty guidance the run still fires guidance-only, otherwise it short-
    circuits like "nothing to apply".

    ``cross_target`` (``"<domain>/<dataset>"``, cross scope only) names the
    pair the UI's per-target scope was set to. It is merged into the run's
    ``extra_glue_databases`` so the session policy covers the target even when
    the selection carries no pair-doc note (e.g. only ``_dataset``-wide general
    notes) — without it such a run could not verify anything against the
    counterpart's data.

    Ordering is the whole point:

    1. **Take the per-dataset lease first** (mode ``annotated``) so the bundle
       can't change under the sweep and no full/incremental run races us. A held
       lease -> 409.
    2. **Orphan sweep** — for each open annotation, load its target doc from S3
       and try to re-anchor the quote (``anno.is_orphaned``). A note whose passage
       is gone (doc dropped, or quote no longer present) is auto-resolved
       ``orphaned`` with a 7-day TTL — the agent never sees it.
    3. **Branch**:
       * every open note orphaned (or none open) -> nothing to apply: close the
         status row ``complete`` and DON'T invoke the runtime (the payoff of doing
         this in the pre-flight, not inside an expensive agent run).
       * some survive -> flip them ``in_review`` and invoke the runtime with only
         the live notes. On invoke failure, release the lease AND revert
         ``in_review`` -> ``open`` so the feedback isn't silently lost.
    """
    user_sub = _require_user_sub(user_sub)
    if not runtime_arn:
        raise ApiError(500, "OKF_HARVEST_RUNTIME_ARN not configured")

    # Fresh session per run (like a full harvest, NOT the incremental affinity id):
    # a one-shot job wants a new microVM with a clean S3 Files mount rather than
    # reattaching to a warm/stale one left by a prior incremental run.
    session_id = runtime_session_id(
        data_domain, dataset, unique_token=uuid.uuid4().hex
    )
    if not acquire_harvest_lease(
        ddb,
        registry_table=registry_table,
        data_domain=data_domain,
        dataset=dataset,
        mode="annotated",
        session_id=session_id,
    ):
        raise ApiError(
            409,
            f"a harvest for {data_domain}/{dataset} is already queued or running",
        )

    # EVERYTHING after the lease is taken runs under try/except: if the pre-flight
    # (Query, S3 reads, sweep) or the invoke raises, we MUST release the lease
    # (mark the row failed) or the dataset wedges at `queued` for the 8h stale
    # window. `flipped` tracks survivors moved to in_review so we can revert them.
    now = _now_iso()
    expires_at = int(
        (datetime.now(timezone.utc) + timedelta(seconds=anno.HISTORY_TTL_SECONDS)).timestamp()
    )
    flipped: list[tuple[str, str]] = []
    try:
        # Dataset guidance (shared): a DIRTY guidance (edited since the last
        # successful harvest) is on its own reason to run — even with zero live
        # annotations. We load it here so both the short-circuit decision and the
        # invoke payload see the same value/version. A missing mapping row (no
        # guidance ever set) is not an error here — treat it as empty guidance.
        try:
            guidance = get_dataset_guidance(
                ddb,
                registry_table=registry_table,
                data_domain=data_domain,
                dataset=dataset,
            )
        except ApiError:
            guidance = {"guidance": "", "guidance_updated_at": "", "guidance_dirty": False}
        # A cross-scoped run never applies (or is triggered by) dataset guidance:
        # cross docs are authored guidance-free by design.
        if scope == "cross":
            guidance = {"guidance": "", "guidance_updated_at": "", "guidance_dirty": False}
        guidance_dirty = bool(guidance.get("guidance_dirty"))

        # We reclaim BOTH open and in_review notes. An in_review note here is a
        # straggler from a prior run that died between flipping it and finishing
        # (the lease we now hold proves no run is currently active), so it's safe —
        # and necessary — to re-process it, else it would be stranded forever (an
        # open-only query would never see it again).
        actionable = [
            it
            for it in _query_user_annotations(
                ddb,
                annotations_table=annotations_table,
                data_domain=data_domain,
                dataset=dataset,
                user_sub=user_sub,
            )
            if _s(it.get("status")) in (anno.STATUS_OPEN, anno.STATUS_IN_REVIEW)
        ]
        # Scope filter: notes on external/ (cross-dataset) docs vs the dataset's
        # own docs. `_dataset`-WIDE notes are general feedback — valid steering
        # for EITHER scope — so they pass both filters; whether one rides a
        # given run is the picker's annotation_ids decision. An out-of-scope
        # OPEN note stays open for a later run of the other scope — but an
        # out-of-scope IN_REVIEW note is a straggler from a dead prior run (the
        # lease we hold proves nothing is active), and simply dropping it would
        # strand it forever: a user who always picks one scope would never
        # gather it again. Revert those to open here, exactly as the reclaim
        # invariant promises.
        if scope in ("cross", "dataset"):
            want_external = scope == "cross"
            in_scope: list[dict[str, Any]] = []
            for it in actionable:
                cid = _s(it.get("concept_id")) or ""
                if (
                    cid == anno.DATASET_WIDE_CONCEPT
                    or is_external_concept_id(cid) == want_external
                ):
                    in_scope.append(it)
                elif _s(it.get("status")) == anno.STATUS_IN_REVIEW:
                    _set_annotation_status(
                        ddb,
                        annotations_table=annotations_table,
                        pk=_s(it.get("pk")),
                        sk=_s(it.get("sk")),
                        status=anno.STATUS_OPEN,
                        now_iso=now,
                    )
            actionable = in_scope

        # Explicit id selection (the UI's annotation picker). An unselected OPEN
        # note simply stays open for a later run; an unselected IN_REVIEW note
        # is a straggler from a dead prior run (the lease we hold proves nothing
        # is active) and reverts to open — same stranding argument as the scope
        # filter above, else a user who never ticks it would strand it forever.
        if annotation_ids is not None:
            wanted = set(annotation_ids)
            selected: list[dict[str, Any]] = []
            for it in actionable:
                if (_s(it.get("annotation_id")) or "") in wanted:
                    selected.append(it)
                elif _s(it.get("status")) == anno.STATUS_IN_REVIEW:
                    _set_annotation_status(
                        ddb,
                        annotations_table=annotations_table,
                        pk=_s(it.get("pk")),
                        sk=_s(it.get("sk")),
                        status=anno.STATUS_OPEN,
                        now_iso=now,
                    )
            actionable = selected

        # Cache each concept doc's body so N annotations on one page cost one GET.
        body_cache: dict[str, str | None] = {}

        def _load_body(concept_id: str) -> str | None:
            if concept_id in body_cache:
                return body_cache[concept_id]
            key = f"{bundle_prefix(data_domain, dataset)}{concept_id}.md"
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                body_cache[concept_id] = obj["Body"].read().decode("utf-8")
            except Exception as e:  # noqa: BLE001 - a MISSING doc -> orphan (None)
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "404", "NotFound"):
                    body_cache[concept_id] = None
                else:
                    # A non-404 S3 error (throttle, AccessDenied) is NOT "the doc
                    # is gone" — re-raise so the outer handler releases the lease
                    # rather than silently orphaning a note whose doc we couldn't read.
                    raise
            return body_cache[concept_id]

        survivors: list[dict[str, Any]] = []
        orphaned = 0
        for it in actionable:
            concept_id = _s(it.get("concept_id")) or ""
            quote = _s(it.get("quote")) or ""
            # Dataset-wide notes anchor to the dataset itself, not a doc: the
            # sweep only runs on a registered dataset, so they never orphan.
            if concept_id == anno.DATASET_WIDE_CONCEPT:
                survivors.append(it)
                continue
            body = _load_body(concept_id)
            if anno.is_orphaned(body, quote):
                _resolve_annotation(
                    ddb,
                    annotations_table=annotations_table,
                    pk=_s(it.get("pk")),
                    sk=_s(it.get("sk")),
                    outcome=anno.OUTCOME_ORPHANED,
                    resolution=anno.ORPHAN_RESOLUTION_MESSAGE,
                    now_iso=now,
                    expires_at=expires_at,
                )
                orphaned += 1
            else:
                survivors.append(it)

        # Nothing live to act on AND no pending guidance change: close the run out
        # here, skip the agent entirely (the payoff of the pre-flight). But if the
        # guidance is DIRTY, we DO run even with zero survivors — applying the
        # updated instructions to the bundle is the whole point of this path now.
        if not survivors and not guidance_dirty:
            detail = (
                f"No live annotations to apply — {orphaned} auto-resolved as orphaned."
                if orphaned
                else "No open annotations to apply."
            )
            _set_status_row(
                ddb,
                registry_table=registry_table,
                data_domain=data_domain,
                dataset=dataset,
                status="complete",
                detail=detail,
            )
            return {
                "status": "complete",
                "skipped": True,
                "data_domain": data_domain,
                "dataset": dataset,
                "annotations": 0,
                "orphaned": orphaned,
                **({"scope": scope} if scope else {}),
            }

        # Bound the payload: the whole survivor set is JSON-encoded into ONE invoke
        # payload, so an unbounded set could exceed the byte limit and fail
        # unrecoverably. Refuse up front (before flipping any status) with a clear
        # 400 so the caller can delete/prune rather than hit an opaque invoke error.
        if len(survivors) > _ANNO_RUN_MAX:
            _set_status_row(
                ddb,
                registry_table=registry_table,
                data_domain=data_domain,
                dataset=dataset,
                status="failed",
                detail=f"too many open annotations ({len(survivors)} > {_ANNO_RUN_MAX})",
            )
            raise ApiError(
                400,
                f"too many open annotations to apply in one run "
                f"({len(survivors)}; max {_ANNO_RUN_MAX}). Delete some and retry.",
            )

        # Mark survivors in_review so a second run can't double-process them. Track
        # what we flipped so the failure path can revert. (A death BETWEEN this loop
        # and the invoke leaves them in_review, but the next run reclaims in_review
        # notes above — so they're never permanently stranded.)
        for it in survivors:
            pk, sk = _s(it.get("pk")), _s(it.get("sk"))
            if _set_annotation_status(
                ddb,
                annotations_table=annotations_table,
                pk=pk,
                sk=sk,
                status=anno.STATUS_IN_REVIEW,
                now_iso=now,
            ):
                flipped.append((pk, sk))

        # The runner writes these through the mount, prompts the agent to assess +
        # apply, then reconciles the agent's verdicts back to DDB (UpdateItem). It
        # needs pk/sk-reconstruction data: user_sub + concept_id + annotation_id.
        payload_annotations = [
            {
                "annotation_id": _s(it.get("annotation_id")),
                "concept_id": _s(it.get("concept_id")),
                "quote": _s(it.get("quote")),
                "prefix": _s(it.get("prefix")) or "",
                "suffix": _s(it.get("suffix")) or "",
                "block_line": _int_or_none(it.get("block_line", {}).get("N")),
                "note": _s(it.get("note")),
            }
            for it in survivors
        ]
        payload: dict[str, Any] = {
            "data_domain": data_domain,
            "dataset": dataset,
            "mode": "annotated",
            "user_sub": user_sub,
            "annotations": payload_annotations,
        }
        # Per-harvest model/effort overrides — same three scopes as a full
        # harvest (supervisor, sub-agents, reviewer). Omitted -> the runtime
        # falls back to its deploy-time env default (supervisor) / the
        # supervisor's config (sub-agents) / the sub-agents' config (reviewer).
        if model:
            payload["model"] = model
        if effort:
            payload["effort"] = effort
        if subagent_model:
            payload["subagent_model"] = subagent_model
        if subagent_effort:
            payload["subagent_effort"] = subagent_effort
        if reviewer_model:
            payload["reviewer_model"] = reviewer_model
        if reviewer_effort:
            payload["reviewer_effort"] = reviewer_effort
        # Source descriptor so the annotation re-harvest reads the right backend.
        source = get_dataset_source(
            ddb, registry_table=registry_table, data_domain=data_domain, dataset=dataset
        )
        if source:
            payload["source"] = source
        if domain_meta:
            if domain_meta.get("description"):
                payload["domain_description"] = domain_meta["description"]
            if domain_meta.get("context"):
                payload["domain_context"] = domain_meta["context"]

        # Carry the dataset guidance + its version so the runner steers the apply
        # AND, on success, stamps guidance_applied_version to clear dirty.
        if guidance.get("guidance"):
            payload["dataset_guidance"] = guidance["guidance"]
            payload["dataset_guidance_version"] = guidance["guidance_updated_at"]

        # Notes on external/ docs make cross-dataset claims — the agent can only
        # CONFIRM or REFUTE them with qualified SQL against the counterpart's
        # database, which the run's scoped session policy must be widened to
        # (else every check is AccessDenied and the note gets a confident but
        # false rejection). Derive the counterpart set from the surviving notes'
        # concept ids and thread the glue databases through.
        extra_dbs: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        # The UI's per-target scope names the pair outright — include it even if
        # no surviving note's concept id references it (general-note-only runs).
        if cross_target and "/" in cross_target:
            td, tds = cross_target.split("/", 1)
            if td and tds:
                seen_pairs.add((td, tds))
        for it in survivors:
            cid = _s(it.get("concept_id")) or ""
            parts = cid.split("/")
            if len(parts) >= 4 and parts[0] == "external":
                seen_pairs.add((parts[1], parts[2]))
        for td, tds in sorted(seen_pairs):
            t_source = get_dataset_source(
                ddb, registry_table=registry_table, data_domain=td, dataset=tds
            )
            t_db = source_glue_database(t_source) if t_source else None
            if t_db and t_db not in extra_dbs:
                extra_dbs.append(t_db)
        if extra_dbs:
            payload["extra_glue_databases"] = extra_dbs

        agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            qualifier="DEFAULT",
        )
    except ApiError:
        # Already status-set + surfaced with the right code (e.g. the run-cap 400);
        # any survivors were not yet flipped, so nothing to revert. Re-raise as-is.
        raise
    except Exception as e:  # noqa: BLE001 - release lease + revert notes, then raise
        _set_status_row(
            ddb,
            registry_table=registry_table,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"annotation harvest failed: {type(e).__name__}",
        )
        # Revert in_review -> open so no feedback is stranded by a failed run.
        for rpk, rsk in flipped:
            _set_annotation_status(
                ddb,
                annotations_table=annotations_table,
                pk=rpk,
                sk=rsk,
                status=anno.STATUS_OPEN,
                now_iso=_now_iso(),
            )
        raise ApiError(502, f"annotation harvest could not be started: {type(e).__name__}")

    return {
        "status": "queued",
        "data_domain": data_domain,
        "dataset": dataset,
        "annotations": len(survivors),
        "orphaned": orphaned,
        # Whether this run was carrying a pending guidance change (so the UI can
        # say "applying updated guidance" even on a zero-annotation run).
        "guidance_applied": guidance_dirty,
        **({"scope": scope} if scope else {}),
    }


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


# --------------------------------------------------------------------------- #
# Chat conversations (the per-user sidebar list) — okf-chat index table
# --------------------------------------------------------------------------- #
#
# The chat RUNTIME writes these rows (create/touch per turn); the Control API
# only reads/renames/deletes them for the UI. Isolation is structural: the pk
# embeds the caller's Cognito sub (CHAT#<sub>), so a Query can only ever return
# the caller's own conversations. Delete also PURGES the LangGraph checkpoint via
# the DynamoDBSaver so a deleted conversation leaves no state behind.


def _thread_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Deserialize an okf-chat index item to the UI/JSON shape.

    ``thread_id`` is recovered from the sk (``THREAD#<thread_id>``) so the client
    gets back the id it sends as the AG-UI threadId.
    """
    sk = _s(item.get("sk")) or ""
    thread_id = sk[len("THREAD#") :] if sk.startswith("THREAD#") else sk
    out = {
        "thread_id": thread_id,
        "title": _s(item.get("title")),
        "model": _s(item.get("model")),
        "effort": _s(item.get("effort")),
        "created_at": _s(item.get("created_at")),
        "updated_at": _s(item.get("updated_at")),
    }
    dd, ds = _s(item.get("data_domain")), _s(item.get("dataset"))
    if dd and ds:
        out["dataset_scope"] = {"data_domain": dd, "dataset": ds}
    return out


def list_chat_threads(
    ddb,
    *,
    threads_table: str,
    user_sub: str | None,
) -> dict[str, Any]:
    """The caller's conversations, newest-updated first.

    Single-partition Query on ``CHAT#<sub>``; a missing sub is a 401 (never fall
    through to an unscoped scan). Deleted rows carry an ``expires_at`` and are
    reaped by TTL, but TTL is eventually-consistent, so we also skip any row whose
    ``expires_at`` is already set (a just-deleted conversation shouldn't reappear).

    Paginates on ``LastEvaluatedKey``: a Query returns at most 1 MB per page, so a
    caller with a large history would otherwise silently get only the first page
    (and the UI's client-side search would never see the rest). Loop to the end so
    the returned list is always the complete conversation set.
    """
    user_sub = _require_user_sub(user_sub)
    kwargs: dict[str, Any] = {
        "TableName": threads_table,
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": {"S": ct.thread_pk(user_sub)}},
    }
    items: list[dict[str, Any]] = []
    while True:
        resp = ddb.query(**kwargs)
        items.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    threads = [_thread_to_dict(it) for it in items if "expires_at" not in it]
    # Newest activity first; rows without updated_at sort last.
    threads.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    return {"threads": threads}


def rename_chat_thread(
    ddb,
    *,
    threads_table: str,
    user_sub: str | None,
    thread_id: str,
    title: str,
) -> dict[str, Any]:
    """Rename one of the caller's conversations.

    Conditioned on the row existing (within the caller's partition), so renaming a
    stale/foreign id is a clean 404. Empty title is a 400.
    """
    user_sub = _require_user_sub(user_sub)
    title = (title or "").strip()
    if not title:
        raise ApiError(400, "missing required field: title")
    key = {
        "pk": {"S": ct.thread_pk(user_sub)},
        "sk": {"S": ct.thread_sk(thread_id)},
    }
    try:
        ddb.update_item(
            TableName=threads_table,
            Key=key,
            UpdateExpression="SET #t = :t, updated_at = :u",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames={"#t": "title"},
            ExpressionAttributeValues={
                ":t": {"S": title[: ct.TITLE_MAX]},
                ":u": {"S": _now_iso()},
            },
        )
    except Exception as e:  # noqa: BLE001 - map a missing item to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiError(404, f"no such conversation: {thread_id}") from e
        raise
    return {"thread_id": thread_id, "title": title[: ct.TITLE_MAX]}


def _purge_chat_checkpoints(
    ddb, *, checkpoint_table: str, namespaced_thread_id: str
) -> None:
    """Best-effort delete of a conversation's LangGraph checkpoint items.

    The DynamoDBSaver stores a conversation as items whose PK is
    ``CHECKPOINT_<thread_id>`` (metadata) and ``WRITES_<thread_id>#<ns>#<ckpt>``
    (pending writes), all sharing our sub-namespaced ``<sub>:<thread_id>`` as the
    thread id. We Query each PK and BatchWrite the deletes. Done directly on
    DynamoDB (not via the saver) so the Control API stays free of the langgraph
    dependency. This is best-effort: an unreachable checkpoint (no index row) is
    TTL-reaped anyway, so a purge failure must not fail the user-visible delete.
    """
    # (query?, PK-or-prefix) pairs. CHECKPOINT is an EXACT PK (the ckpt id lives in
    # the SK) → a keyed Query. WRITES PKs carry a ``#<ns>#<ckpt>`` suffix, so match
    # by prefix via a Scan — but the prefix MUST include the trailing ``#``
    # delimiter, else deleting thread ``c1`` also matches ``c10#…`` and purges a
    # DIFFERENT conversation's pending writes (thread ids are client-supplied, so
    # one being a prefix of another is reachable).
    targets = (
        (True, f"CHECKPOINT_{namespaced_thread_id}"),
        (False, f"WRITES_{namespaced_thread_id}#"),
    )
    for is_exact, pk in targets:
        try:
            if is_exact:
                resp = ddb.query(
                    TableName=checkpoint_table,
                    KeyConditionExpression="PK = :pk",
                    ExpressionAttributeValues={":pk": {"S": pk}},
                    ProjectionExpression="PK, SK",
                )
                items = resp.get("Items", [])
            else:
                resp = ddb.scan(
                    TableName=checkpoint_table,
                    FilterExpression="begins_with(PK, :p)",
                    ExpressionAttributeValues={":p": {"S": pk}},
                    ProjectionExpression="PK, SK",
                )
                items = resp.get("Items", [])
            for it in items:
                ddb.delete_item(
                    TableName=checkpoint_table,
                    Key={"PK": it["PK"], "SK": it["SK"]},
                )
        except Exception:  # noqa: BLE001 - best-effort; index row already gone
            import logging

            logging.getLogger("control_api").warning(
                "chat checkpoint purge failed for %s (non-fatal)",
                namespaced_thread_id,
                exc_info=True,
            )


def delete_chat_thread(
    ddb,
    *,
    threads_table: str,
    checkpoint_table: str,
    user_sub: str | None,
    thread_id: str,
) -> dict[str, Any]:
    """Delete one of the caller's conversations: index row + checkpoint state.

    Removes the index row (conditioned on existence -> 404 for a stale/foreign
    id), then PURGES the LangGraph checkpoint items for the sub-namespaced thread
    id. The purge is best-effort — the index row is the user-visible source of
    truth, and an orphaned checkpoint is unreachable + TTL-reaped anyway.
    """
    user_sub = _require_user_sub(user_sub)
    key = {
        "pk": {"S": ct.thread_pk(user_sub)},
        "sk": {"S": ct.thread_sk(thread_id)},
    }
    try:
        ddb.delete_item(
            TableName=threads_table,
            Key=key,
            ConditionExpression="attribute_exists(pk)",
        )
    except Exception as e:  # noqa: BLE001 - map a missing item to 404
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiError(404, f"no such conversation: {thread_id}") from e
        raise
    # The chat runtime namespaces the checkpoint thread id with the user's sub.
    _purge_chat_checkpoints(
        ddb,
        checkpoint_table=checkpoint_table,
        namespaced_thread_id=f"{user_sub}:{thread_id}",
    )
    return {"deleted": True, "thread_id": thread_id}

"""HTTP API router + Lambda entry point for the Control API.

Behind an API Gateway HTTP API (v2, **payload format 2.0**) with a Cognito JWT
authorizer. The authorizer enforces auth before we run, so the Lambda trusts
``requestContext.authorizer.jwt.claims`` and does not re-verify tokens.

Design (OKF_DESIGN §2): a *single* Lambda with a tiny internal router. We match
on ``(method, path)`` from ``requestContext.http.method`` + ``rawPath`` against a
small table of path templates (``/domains/{domain}/datasets/{dataset}``), pull
out the path params ourselves, and dispatch to a pure handler in
``control_api.handlers``. Doing our own matching (rather than relying on API GW's
per-route ``pathParameters``) keeps the router fully unit-testable with a raw
event and independent of how the routes happen to be declared in Terraform.

Response shape is always
``{"statusCode": int, "headers": {...}, "body": json.dumps(...)}`` with CORS
headers, per the service contract.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from control_api import handlers
from control_api.handlers import ApiError

# CORS: the SPA is served from CloudFront on a different origin, so every
# response (including errors) carries permissive CORS headers. API GW's own CORS
# config handles the preflight OPTIONS; this covers the actual responses.
CORS_HEADERS = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "authorization,content-type",
    "access-control-allow-methods": "GET,PUT,POST,DELETE,OPTIONS",
}


def _response(status: int, payload: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(CORS_HEADERS),
        "body": json.dumps(payload),
    }


# --------------------------------------------------------------------------- #
# Config resolved from env (CONVENTIONS.md env-var names)
# --------------------------------------------------------------------------- #


class Config:
    """Env-derived config + boto3 client factories, injected into routing.

    Tests construct this directly with fakes/moto clients; the Lambda builds it
    from ``os.environ`` via :meth:`from_env`. Clients are created eagerly here
    because a single Lambda invocation touches at most a couple of services and
    cold-start client creation is cheap relative to the network calls.
    """

    def __init__(
        self,
        *,
        bucket: str,
        registry_table: str,
        freshness_table: str,
        harvest_runtime_arn: str,
        s3,
        ddb,
        glue,
        agentcore,
        cognito=None,
        user_pool_id: str = "",
        mcp_scope: str = "",
        logs=None,
        harvest_log_group: str = "",
        harvest_model_catalog=None,
        annotations_table: str = "okf-annotations",
        chat_threads_table: str = "okf-chat",
        chat_checkpoint_table: str = "okf-chat-checkpoints",
    ):
        self.bucket = bucket
        self.registry_table = registry_table
        self.freshness_table = freshness_table
        self.harvest_runtime_arn = harvest_runtime_arn
        # User-scoped wiki annotations (feedback awaiting an annotation harvest).
        self.annotations_table = annotations_table
        # Chat conversation index (sidebar list) + the LangGraph checkpoint table
        # (purged on delete). The chat RUNTIME owns the writes; the Control API
        # reads/renames/deletes for the UI.
        self.chat_threads_table = chat_threads_table
        self.chat_checkpoint_table = chat_checkpoint_table
        self.s3 = s3
        self.ddb = ddb
        self.glue = glue
        self.agentcore = agentcore
        # Cognito M2M credential vending (create/delete user-pool app clients).
        self.cognito = cognito
        self.user_pool_id = user_pool_id
        self.mcp_scope = mcp_scope
        # CloudWatch Logs client + the harvest runtime's log group — used to read
        # back the live step feed (OKF_STEP lines) the harvest runtime emits. When
        # unset, the feed endpoint returns an empty batch (feature degrades off).
        self.logs = logs
        self.harvest_log_group = harvest_log_group
        # The allowed (model, effort) catalog the UI's picker offers; used to
        # VALIDATE a per-harvest model/effort selection before it reaches the
        # runtime + Bedrock. Defaults to okf_core's built-in catalog when the
        # env is unset. Parsed once at Config build time (see from_env).
        from okf_core import harvest_models

        self.harvest_model_catalog = (
            harvest_model_catalog
            if harvest_model_catalog is not None
            else harvest_models.DEFAULT_CATALOG
        )

    @classmethod
    def from_env(cls) -> "Config":
        import boto3
        from botocore.config import Config as BotoConfig

        from okf_core import harvest_models

        region = os.environ.get("AWS_REGION", "us-east-1")
        # The bundle bucket enforces default SSE-KMS (aws:kms). S3 rejects any
        # KMS-encrypted PUT whose presigned URL was signed with SigV2 with
        # "requests specifying SSE-KMS require AWS Signature Version 4". Pin the
        # S3 client to s3v4 so generate_presigned_url() produces a SigV4 URL the
        # browser's PUT can use. (Regional endpoints also require v4.)
        s3 = boto3.client(
            "s3",
            region_name=region,
            config=BotoConfig(signature_version="s3v4"),
        )
        return cls(
            bucket=os.environ["OKF_BUNDLE_BUCKET"],
            registry_table=os.environ.get("OKF_REGISTRY_TABLE", "okf-registry"),
            freshness_table=os.environ.get("OKF_FRESHNESS_TABLE", "okf-freshness"),
            harvest_runtime_arn=os.environ.get("OKF_HARVEST_RUNTIME_ARN", ""),
            s3=s3,
            ddb=boto3.client("dynamodb", region_name=region),
            glue=boto3.client("glue", region_name=region),
            agentcore=boto3.client("bedrock-agentcore", region_name=region),
            cognito=boto3.client("cognito-idp", region_name=region),
            user_pool_id=os.environ.get("OKF_USER_POOL_ID", ""),
            mcp_scope=os.environ.get("OKF_MCP_SCOPE", ""),
            logs=boto3.client("logs", region_name=region),
            harvest_log_group=os.environ.get("OKF_HARVEST_LOG_GROUP", ""),
            harvest_model_catalog=harvest_models.parse_catalog(
                os.environ.get("OKF_HARVEST_MODEL_CATALOG")
            ),
            annotations_table=os.environ.get(
                "OKF_ANNOTATIONS_TABLE", "okf-annotations"
            ),
            chat_threads_table=os.environ.get("OKF_CHAT_THREADS_TABLE", "okf-chat"),
            chat_checkpoint_table=os.environ.get(
                "OKF_CHAT_CHECKPOINT_TABLE", "okf-chat-checkpoints"
            ),
        )


# --------------------------------------------------------------------------- #
# Route table
# --------------------------------------------------------------------------- #
#
# Each entry is (method, template, handler-adapter). Templates use ``{name}``
# segments; the matcher extracts them into a ``params`` dict. Handlers are
# adapters ``(cfg, params, body, query, caller) -> (status, payload)`` where
# ``caller`` is the verified identity from the JWT authorizer's claims (email or
# sub) — used to attribute/authorize per-user actions (e.g. credential vending).

@dataclass(frozen=True)
class Caller:
    """The verified caller identity from the JWT authorizer.

    Two facets, both trustworthy (they come from the validated token, not the
    request body). ``ident`` is the human-facing label (email, falling back to
    sub) used for ATTRIBUTION — a credential's ``created_by``, an annotation's
    ``author``. ``sub`` is the immutable, opaque Cognito subject used for
    ISOLATION — it's baked into the annotation partition key, so it must never
    change under a user and must be ``#``-delimiter-safe (a UUID is both). Kept
    separate because attribution wants the readable value while isolation needs
    the stable one.
    """

    ident: str | None = None
    sub: str | None = None

    def __bool__(self) -> bool:  # truthiness == "we have an identity"
        return bool(self.ident or self.sub)

    def __str__(self) -> str:  # so existing ``caller``-as-str uses keep working
        return self.ident or self.sub or ""


RouteFn = Callable[
    [Config, dict[str, str], dict[str, Any] | None, dict[str, str], Caller],
    tuple[int, Any],
]


def _caller_identity(event: dict[str, Any]) -> Caller:
    """The verified caller identity (ident + sub) from the JWT authorizer claims.

    The authorizer validates the token before we run and injects the decoded
    claims at ``requestContext.authorizer.jwt.claims``. ``ident`` prefers
    ``email`` (what the UI shows) and falls back to ``sub``; ``sub`` is captured
    separately for user-scoped partition keys. This is the ONLY trustworthy
    caller identity — never trust an identity carried in the request body, which
    any caller can set.
    """
    # Use `or {}` at every level: a key can be PRESENT with a null value (e.g.
    # ``authorizer: null``), where ``.get(k, {})`` would return None and the next
    # ``.get`` would raise AttributeError -> a 500 instead of treating the caller
    # as anonymous. `or {}` coerces both missing and null to an empty dict.
    rc = event.get("requestContext") or {}
    authz = rc.get("authorizer") or {}
    jwt = authz.get("jwt") or {}
    claims = jwt.get("claims") or {}
    if not isinstance(claims, dict):
        return Caller()
    sub = claims.get("sub") or None
    ident = claims.get("email") or sub
    return Caller(ident=ident or None, sub=sub)


def _r_list_glue(cfg, params, body, query, caller):
    return 200, handlers.list_glue_databases(cfg.glue)


def _r_list_domains(cfg, params, body, query, caller):
    return 200, handlers.list_domains(cfg.ddb, registry_table=cfg.registry_table)


# -- Declared domain CRUD --------------------------------------------------


def _r_list_declared_domains(cfg, params, body, query, caller):
    return 200, handlers.list_declared_domains(
        cfg.ddb, registry_table=cfg.registry_table
    )


def _r_get_declared_domain(cfg, params, body, query, caller):
    domain = handlers.get_domain(
        cfg.ddb, registry_table=cfg.registry_table, data_domain=params["domain"]
    )
    if domain is None:
        raise ApiError(404, f"domain '{params['domain']}' not declared")
    return 200, domain


def _r_upsert_declared_domain(cfg, params, body, query, caller):
    body = body or {}
    description = (body.get("description") or "").strip()
    context = (body.get("context") or "").strip()
    result = handlers.declare_domain(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        description=description,
        context=context,
    )
    # Write the domain's concept doc THROUGH the harvest mount so the domain is
    # semantically searchable via the normal reindex pipeline. Best-effort.
    handlers.write_domain_doc(
        cfg.agentcore,
        runtime_arn=cfg.harvest_runtime_arn,
        data_domain=params["domain"],
        description=description,
        context=context,
    )
    return 200, result


def _r_delete_declared_domain(cfg, params, body, query, caller):
    result = handlers.delete_declared_domain(
        cfg.ddb, registry_table=cfg.registry_table, data_domain=params["domain"]
    )
    # Clean up the domain doc from S3 (cascades to vector via reindex).
    handlers.delete_domain_doc(
        cfg.s3, bundle_bucket=cfg.bucket, data_domain=params["domain"]
    )
    return 200, result


# -- Domain->dataset mappings -----------------------------------------------


def _r_upsert_domain(cfg, params, body, query, caller):
    # Accept the first-class `source` object ({type, glue_database}) or the
    # legacy flat `glue_database` (the only-supported source today is glue).
    # normalize_source validates the type and rejects anything unsupported.
    body = body or {}
    try:
        source = handlers.normalize_source(
            body.get("source"), glue_database=body.get("glue_database")
        )
    except handlers.SourceError as e:
        raise ApiError(400, str(e)) from e
    glue_database = handlers.source_glue_database(source)
    dataset = params["dataset"]
    # The dataset name IS the Glue database name: the harvest runtime queries
    # Glue by the dataset name directly (CONVENTIONS.md frozen payload), so a
    # mapping where they differ is unharvestable. Reject it at the boundary.
    if dataset != glue_database:
        raise ApiError(
            400,
            f"dataset must equal glue_database (got dataset={dataset!r}, "
            f"glue_database={glue_database!r})",
        )
    # A mapping must select from a PRE-DECLARED domain.
    handlers.assert_domain_declared(
        cfg.ddb, registry_table=cfg.registry_table, data_domain=params["domain"]
    )
    handlers.assert_glue_database_exists(cfg.glue, glue_database)
    result = handlers.upsert_domain_mapping(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=dataset,
        glue_database=glue_database,
    )
    # Pre-create the dataset's bundle dirs THROUGH the harvest mount (uid 1000)
    # so a later presigned .context/ upload (which PUTs straight to S3, bypassing
    # the mount) doesn't materialize the dataset root as a root-owned dir the
    # mount can't write into — which would wedge the first full harvest. Best-
    # effort: a provisioning failure never blocks the mapping.
    handlers.provision_dataset_dirs(
        cfg.agentcore,
        runtime_arn=cfg.harvest_runtime_arn,
        data_domain=params["domain"],
        dataset=dataset,
    )
    return 200, result


def _r_delete_domain(cfg, params, body, query, caller):
    # Deletes the mapping AND everything the dataset owns: the S3 bundle (which
    # cascades to vector cleanup via Object-Deleted -> reindex), the freshness
    # rows, and the harvest status row. See handlers.delete_domain_mapping.
    return 200, handlers.delete_domain_mapping(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        s3=cfg.s3,
        bundle_bucket=cfg.bucket,
        freshness_table=cfg.freshness_table,
    )


def _r_list_credentials(cfg, params, body, query, caller):
    return 200, handlers.list_credentials(cfg.ddb, registry_table=cfg.registry_table)


def _r_create_credential(cfg, params, body, query, caller):
    if not cfg.cognito or not cfg.user_pool_id or not cfg.mcp_scope:
        raise ApiError(500, "credential vending not configured (pool/scope)")
    name = handlers._require(body, "name")
    # created_by is the OWNER of the credential and is used to authorize a later
    # revoke, so it MUST come from the verified JWT identity — never the request
    # body (which any caller can set to impersonate another owner). Fall back to
    # a body value only when there is no authorizer identity (e.g. local tests).
    created_by = caller.ident or (body or {}).get("created_by")
    return 200, handlers.create_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=cfg.user_pool_id,
        mcp_scope=cfg.mcp_scope,
        registry_table=cfg.registry_table,
        name=name,
        created_by=created_by,
    )


def _r_delete_credential(cfg, params, body, query, caller):
    if not cfg.cognito or not cfg.user_pool_id:
        raise ApiError(500, "credential vending not configured (pool)")
    # Pass the verified caller so the handler can enforce that a credential is
    # only revocable by the user who created it (and only if this API vended it).
    # ``created_by`` was stamped from ``caller.ident`` (email), so compare on that.
    return 200, handlers.delete_credential(
        cfg.cognito,
        cfg.ddb,
        user_pool_id=cfg.user_pool_id,
        registry_table=cfg.registry_table,
        client_id=params["client_id"],
        caller=caller.ident,
    )


def _r_list_context(cfg, params, body, query, caller):
    return 200, handlers.list_context_docs(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_presign_context(cfg, params, body, query, caller):
    filename = handlers._require(body, "filename")
    content_type = (body or {}).get("content_type")
    return 200, handlers.presign_context_upload(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
        filename=filename,
        content_type=content_type,
    )


def _r_delete_context(cfg, params, body, query, caller):
    return 200, handlers.delete_context_doc(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
        filename=params["filename"],
    )


def _r_trigger_harvest(cfg, params, body, query, caller):
    body = body or {}
    data_domain = handlers._require(body, "data_domain")
    dataset = handlers._require(body, "dataset")
    if not cfg.harvest_runtime_arn:
        raise ApiError(500, "OKF_HARVEST_RUNTIME_ARN not configured")
    # Per-harvest model/effort selection (from the UI picker). Both optional: when
    # a request omits `model`, the runtime falls back to its deploy-time default
    # env var, so we only validate/forward when a model was chosen. This is the
    # TRUST BOUNDARY — the value reaches bedrock:InvokeModel, so validate it
    # against the catalog here (the runtime deliberately does not allow-list it).
    model = effort = None
    if body.get("model"):
        from okf_core import harvest_models

        try:
            model, effort = harvest_models.validate_model_effort(
                cfg.harvest_model_catalog, body.get("model"), body.get("effort")
            )
        except harvest_models.ModelCatalogError as e:
            raise ApiError(400, str(e)) from e
    elif body.get("effort"):
        # effort without model is ambiguous (which model's scale?) — reject rather
        # than silently applying it to the default model.
        raise ApiError(400, "'effort' requires 'model' to be specified")
    # The runtime resolves the dataset to a same-named Glue database; verify it
    # exists now so a typo fails fast with a 404 here instead of an
    # EntityNotFoundException deep in the async harvest job.
    handlers.assert_glue_database_exists(cfg.glue, dataset)
    return 200, handlers.trigger_harvest(
        cfg.agentcore,
        cfg.ddb,
        registry_table=cfg.registry_table,
        runtime_arn=cfg.harvest_runtime_arn,
        data_domain=data_domain,
        dataset=dataset,
        mode=body.get("mode") or "full",
        changed_table=body.get("changed_table"),
        model=model,
        effort=effort,
    )


def _r_trigger_annotation_harvest(cfg, params, body, query, caller):
    if not cfg.harvest_runtime_arn:
        raise ApiError(500, "OKF_HARVEST_RUNTIME_ARN not configured")
    # Enrich with the declared domain's description/context (same as a full
    # harvest) so the agent applies annotations domain-aware.
    domain_meta = handlers.get_domain(
        cfg.ddb, registry_table=cfg.registry_table, data_domain=params["domain"]
    )
    return 200, handlers.trigger_annotation_harvest(
        cfg.agentcore,
        cfg.ddb,
        cfg.s3,
        registry_table=cfg.registry_table,
        annotations_table=cfg.annotations_table,
        bucket=cfg.bucket,
        runtime_arn=cfg.harvest_runtime_arn,
        data_domain=params["domain"],
        dataset=params["dataset"],
        user_sub=caller.sub,
        domain_meta=domain_meta,
    )


# -- Annotations (user-scoped feedback on concept docs) ---------------------


def _r_list_annotations(cfg, params, body, query, caller):
    return 200, handlers.list_annotations(
        cfg.ddb,
        annotations_table=cfg.annotations_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        user_sub=caller.sub,
        # Optional ?concept=<id> narrows to one page's annotations.
        concept_id=query.get("concept"),
    )


def _r_create_annotation(cfg, params, body, query, caller):
    body = body or {}
    block_line = body.get("block_line")
    try:
        block_line = int(block_line) if block_line is not None else None
    except (TypeError, ValueError):
        block_line = None
    return 200, handlers.create_annotation(
        cfg.ddb,
        annotations_table=cfg.annotations_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        user_sub=caller.sub,
        author=caller.ident,
        concept_id=handlers._require(body, "concept_id"),
        quote=handlers._require(body, "quote"),
        note=handlers._require(body, "note"),
        prefix=body.get("prefix") or "",
        suffix=body.get("suffix") or "",
        block_line=block_line,
    )


def _r_delete_annotation(cfg, params, body, query, caller):
    # concept_id is a slash-delimited path (``tables/races``), so it can't be a
    # single-segment path param — it rides in ?concept=<id>. Together with the
    # {annotation_id} path param it reconstructs the sk within the caller's pk.
    concept_id = query.get("concept")
    if not concept_id:
        raise ApiError(400, "missing required query param: concept")
    return 200, handlers.delete_annotation(
        cfg.ddb,
        annotations_table=cfg.annotations_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        user_sub=caller.sub,
        concept_id=concept_id,
        annotation_id=params["annotation_id"],
    )


# -- Dataset guidance (shared authoring instructions) -----------------------


def _r_get_dataset_guidance(cfg, params, body, query, caller):
    return 200, handlers.get_dataset_guidance(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_set_dataset_guidance(cfg, params, body, query, caller):
    body = body or {}
    # `guidance` may be an empty string (clearing) — so accept it explicitly
    # rather than via _require, which rejects falsy values.
    return 200, handlers.set_dataset_guidance(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        guidance=body.get("guidance", ""),
    )


# -- Recursive-improvement benchmark (settings + off-mount CSV upload) -------


def _r_presign_benchmark(cfg, params, body, query, caller):
    content_type = (body or {}).get("content_type")
    return 200, handlers.presign_benchmark_upload(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
        content_type=content_type,
    )


def _r_inspect_benchmark(cfg, params, body, query, caller):
    return 200, handlers.inspect_benchmark_questions(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_get_ri_settings(cfg, params, body, query, caller):
    return 200, handlers.get_dataset_ri_settings(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_set_ri_settings(cfg, params, body, query, caller):
    body = body or {}
    # The whole recursive_improvement block is validated/clamped in the handler
    # (the trust boundary). Accept it under either the nested key or the bare body.
    settings = body.get("recursive_improvement", body)
    return 200, handlers.set_dataset_ri_settings(
        cfg.ddb,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
        settings=settings,
    )


# -- Chat conversations (per-user sidebar list) -----------------------------


def _r_list_chat_threads(cfg, params, body, query, caller):
    return 200, handlers.list_chat_threads(
        cfg.ddb,
        threads_table=cfg.chat_threads_table,
        user_sub=caller.sub,
    )


def _r_rename_chat_thread(cfg, params, body, query, caller):
    body = body or {}
    return 200, handlers.rename_chat_thread(
        cfg.ddb,
        threads_table=cfg.chat_threads_table,
        user_sub=caller.sub,
        thread_id=params["thread_id"],
        title=handlers._require(body, "title"),
    )


def _r_delete_chat_thread(cfg, params, body, query, caller):
    return 200, handlers.delete_chat_thread(
        cfg.ddb,
        threads_table=cfg.chat_threads_table,
        checkpoint_table=cfg.chat_checkpoint_table,
        user_sub=caller.sub,
        thread_id=params["thread_id"],
    )


def _r_cancel_harvest(cfg, params, body, query, caller):
    if not cfg.harvest_runtime_arn:
        raise ApiError(500, "OKF_HARVEST_RUNTIME_ARN not configured")
    return 200, handlers.cancel_harvest(
        cfg.agentcore,
        cfg.ddb,
        registry_table=cfg.registry_table,
        runtime_arn=cfg.harvest_runtime_arn,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_get_harvest_events(cfg, params, body, query, caller):
    # Two client cursors, both echoed back: ``since`` = highest seq seen (exact
    # dedup), ``since_ts`` = highest CloudWatch event ts in ms (bounds the scan
    # window). Both default to 0 = "first load" (server backfills from run start).
    def _int(name: str) -> int:
        try:
            return int(query.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    return 200, handlers.get_harvest_events(
        cfg.logs,
        cfg.ddb,
        registry_table=cfg.registry_table,
        log_group=cfg.harvest_log_group,
        data_domain=params["domain"],
        dataset=params["dataset"],
        since=_int("since"),
        since_ts=_int("since_ts"),
    )


def _r_get_harvest(cfg, params, body, query, caller):
    return 200, handlers.get_harvest_status(
        cfg.s3,
        cfg.ddb,
        bucket=cfg.bucket,
        registry_table=cfg.registry_table,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_list_bundle(cfg, params, body, query, caller):
    return 200, handlers.list_bundle_files(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


def _r_bundle_file(cfg, params, body, query, caller):
    key = query.get("key")
    if not key:
        raise ApiError(400, "missing required query param: key")
    return 200, handlers.read_bundle_file(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
        key=key,
    )


def _r_bundle_graph(cfg, params, body, query, caller):
    return 200, handlers.bundle_graph(
        cfg.s3,
        bucket=cfg.bucket,
        data_domain=params["domain"],
        dataset=params["dataset"],
    )


# Order matters: more specific templates (``/bundle/{d}/{ds}/graph``) must come
# before catch-alls with the same prefix so ``graph``/``file`` are not captured
# as a dataset segment. We list the fixed-suffix routes first.
_ROUTES: list[tuple[str, str, RouteFn]] = [
    ("GET", "/glue/databases", _r_list_glue),
    ("GET", "/domain-defs", _r_list_declared_domains),
    ("GET", "/domain-defs/{domain}", _r_get_declared_domain),
    ("PUT", "/domain-defs/{domain}", _r_upsert_declared_domain),
    ("DELETE", "/domain-defs/{domain}", _r_delete_declared_domain),
    ("GET", "/domains", _r_list_domains),
    ("PUT", "/domains/{domain}/datasets/{dataset}", _r_upsert_domain),
    ("DELETE", "/domains/{domain}/datasets/{dataset}", _r_delete_domain),
    ("GET", "/credentials", _r_list_credentials),
    ("POST", "/credentials", _r_create_credential),
    ("DELETE", "/credentials/{client_id}", _r_delete_credential),
    ("POST", "/context/{domain}/{dataset}/presign", _r_presign_context),
    ("DELETE", "/context/{domain}/{dataset}/{filename}", _r_delete_context),
    ("GET", "/context/{domain}/{dataset}", _r_list_context),
    ("POST", "/harvest", _r_trigger_harvest),
    ("POST", "/harvest/{domain}/{dataset}/annotations/run", _r_trigger_annotation_harvest),
    ("POST", "/harvest/{domain}/{dataset}/cancel", _r_cancel_harvest),
    ("GET", "/harvest/{domain}/{dataset}/events", _r_get_harvest_events),
    ("GET", "/harvest/{domain}/{dataset}", _r_get_harvest),
    # Annotations: fixed-suffix + method disambiguate; concept id rides in
    # ?concept= (it has slashes, so can't be a path segment).
    ("GET", "/annotations/{domain}/{dataset}", _r_list_annotations),
    ("POST", "/annotations/{domain}/{dataset}", _r_create_annotation),
    ("DELETE", "/annotations/{domain}/{dataset}/{annotation_id}", _r_delete_annotation),
    # Dataset guidance (shared authoring instructions). PUT (not PATCH) for the
    # same API Gateway CORS reason as the chat rename above.
    ("GET", "/guidance/{domain}/{dataset}", _r_get_dataset_guidance),
    ("PUT", "/guidance/{domain}/{dataset}", _r_set_dataset_guidance),
    # Recursive-improvement benchmark: off-mount CSV presign + saved settings +
    # parsed question-count (fixed /questions suffix disambiguates from settings).
    ("POST", "/benchmark/{domain}/{dataset}/presign", _r_presign_benchmark),
    ("GET", "/benchmark/{domain}/{dataset}/questions", _r_inspect_benchmark),
    ("GET", "/benchmark/{domain}/{dataset}", _r_get_ri_settings),
    ("PUT", "/benchmark/{domain}/{dataset}", _r_set_ri_settings),
    # Chat conversations (per-user sidebar list). thread_id is a single opaque
    # segment (a UUID the SPA generates), so it's a clean path param. Rename is
    # PUT (not PATCH) because the API Gateway CORS allow_methods + the CORS_HEADERS
    # above enumerate GET/PUT/POST/DELETE/OPTIONS — PATCH would fail preflight.
    ("GET", "/chat/threads", _r_list_chat_threads),
    ("PUT", "/chat/threads/{thread_id}", _r_rename_chat_thread),
    ("DELETE", "/chat/threads/{thread_id}", _r_delete_chat_thread),
    ("GET", "/bundle/{domain}/{dataset}/graph", _r_bundle_graph),
    ("GET", "/bundle/{domain}/{dataset}/file", _r_bundle_file),
    ("GET", "/bundle/{domain}/{dataset}", _r_list_bundle),
]


def _compile(template: str) -> re.Pattern[str]:
    """Turn ``/bundle/{domain}/{dataset}`` into an anchored regex.

    Each ``{name}`` matches a single non-``/`` segment (so it never swallows a
    fixed suffix like ``/graph``). Path params are URL-decoded by the caller.
    """
    parts = []
    for seg in template.strip("/").split("/"):
        m = re.fullmatch(r"\{(\w+)\}", seg)
        if m:
            parts.append(rf"(?P<{m.group(1)}>[^/]+)")
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "/?$")


_COMPILED: list[tuple[str, re.Pattern[str], RouteFn]] = [
    (method, _compile(tmpl), fn) for method, tmpl, fn in _ROUTES
]


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def _parse_body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body")
    if raw is None or raw == "":
        return None
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ApiError(400, "request body is not valid JSON") from e
    if not isinstance(parsed, dict):
        raise ApiError(400, "request body must be a JSON object")
    return parsed


def route(event: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Match the request to a route and dispatch. Returns a full HTTP response.

    Reads the method + path from the API GW v2 (payload 2.0) event, unquotes
    path params, and calls the matched handler adapter. ``ApiError`` becomes its
    carried status; anything unexpected becomes a 500 (logged by the handler
    wrapper). Preflight ``OPTIONS`` short-circuits to 204.
    """
    from urllib.parse import unquote

    http = event.get("requestContext", {}).get("http", {})
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or http.get("path") or event.get("path") or "/"

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": dict(CORS_HEADERS), "body": ""}

    query = event.get("queryStringParameters") or {}
    caller = _caller_identity(event)

    matched_path = False
    for m_method, pattern, fn in _COMPILED:
        match = pattern.match(raw_path)
        if not match:
            continue
        matched_path = True
        if m_method != method:
            continue
        params = {k: unquote(v) for k, v in match.groupdict().items()}
        try:
            body = _parse_body(event)
            status, payload = fn(cfg, params, body, query, caller)
            return _response(status, payload)
        except ApiError as e:
            return _response(e.status, {"error": e.message})

    if matched_path:
        return _response(405, {"error": f"method not allowed: {method} {raw_path}"})
    return _response(404, {"error": f"not found: {method} {raw_path}"})


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point: build clients from env, then route.

    Kept thin so all logic lives in :func:`route` + ``handlers`` and is testable
    without AWS. A failure building config (e.g. a missing required env var) or
    an unexpected handler exception is turned into a 500 rather than crashing the
    invocation.
    """
    try:
        cfg = Config.from_env()
    except KeyError as e:
        return _response(500, {"error": f"missing configuration: {e}"})
    try:
        return route(event, cfg)
    except ApiError as e:  # defensive: adapters normally catch these
        return _response(e.status, {"error": e.message})
    except Exception as e:  # noqa: BLE001 - never leak a raw stack to the client
        import logging

        logging.getLogger("control_api").exception("unhandled error")
        return _response(500, {"error": f"internal error: {type(e).__name__}"})

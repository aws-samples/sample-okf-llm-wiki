"""boto3 client + source construction from the runtime environment.

The AgentCore execution role supplies bootstrap credentials; region and Athena
output/workgroup come from environment variables set on the runtime. Kept in one
place so the entrypoint stays thin and tests can build a source with fakes.

Per-invocation down-scoping (threats #9/#60): AgentCore has NO native way to
scope the execution role per invoke — the service assumes ONE static role and
the MicroVM Metadata Service (MMDS) hands those creds to any code in the VM, so
a tool-layer SQL allow-list is not a credential boundary. Instead, at the start
of every harvest we assume a dedicated DATA role (``OKF_HARVEST_DATA_ROLE_ARN``)
with an inline STS **session policy** pinned to the one Glue database + Athena
workgroup being harvested, and build the Glue/Athena clients from THOSE creds.
The session policy can only intersect the data role's ceiling, so the effective
per-run permission is the single dataset — enforced by IAM, outside this
(prompt-injectable) process. See ``build_scoped_session`` / ``_session_policy``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from harvest.glue_source import GlueAthenaSource
from harvest.redshift_source import RedshiftSource
from harvest.source_base import Source

log = logging.getLogger("harvest.clients")

# STS AssumeRole via the runtime's own (already-assumed) execution role is role
# CHAINING, hard-capped at 1 hour regardless of the target role's MaxSessionDuration.
# Harvests run up to ~8h, so scoped creds are wrapped in RefreshableCredentials
# that re-assume before expiry (see build_scoped_session). Request the full hour.
_ASSUME_DURATION_SECONDS = 3600


def _session_policy(
    region: str,
    account_id: str,
    database: str,
    workgroup: str,
    results_bucket_arn: str | None,
    enable_lakeformation: bool = False,
) -> dict:
    """Inline STS session policy pinning access to ONE dataset's resources.

    Intersected with the data role's (broad) identity policy at assume time, so
    the resulting credential can touch only:

    * Glue metadata for ``database`` and its tables (``glue:GetDatabase/GetTable
      /GetTables/GetPartitions/GetTableVersions`` on ``database/<db>`` +
      ``table/<db>/*``). ``glue:GetDatabases`` (the plural LIST call) is
      catalog-level only — it cannot be pinned to one db — so it targets
      ``catalog`` and remains a metadata-listing capability, not a data read.
    * Athena on ``workgroup/<wg>`` only. The four Athena actions are ARN-scopable
      ONLY to a workgroup (never to a db/table), so cross-database containment is
      carried entirely by the pinned Glue ``GetTable`` ARNs above — Athena
      authorizes table access against Glue per table.
    * S3 write to the Athena results bucket (disposable query output).
    * S3 read on ``*`` for TABLE DATA: a Glue table's storage location can be in
      ANY bucket (e.g. the BIRD data bucket), so this cannot be dataset-scoped
      without enumerating every table's location up front. This is the known
      residual (threat #9/#60 note): metadata + query surface is pinned; the
      underlying object read stays broad.

    Stays well under the 2,048-char inline session-policy limit.
    """
    glue_arn = f"arn:aws:glue:{region}:{account_id}"
    statements: list[dict] = [
        {
            "Sid": "GlueThisDb",
            "Effect": "Allow",
            "Action": [
                "glue:GetDatabase",
                "glue:GetTable",
                "glue:GetTables",
                "glue:GetPartitions",
                "glue:GetTableVersions",
            ],
            "Resource": [
                f"{glue_arn}:catalog",
                f"{glue_arn}:database/{database}",
                f"{glue_arn}:table/{database}/*",
            ],
        },
        {
            # List is catalog-level only (no per-db ARN); metadata listing only.
            "Sid": "GlueListDbs",
            "Effect": "Allow",
            "Action": ["glue:GetDatabases"],
            "Resource": [f"{glue_arn}:catalog"],
        },
        {
            "Sid": "AthenaThisWorkgroup",
            "Effect": "Allow",
            "Action": [
                "athena:StartQueryExecution",
                "athena:GetQueryExecution",
                "athena:GetQueryResults",
                "athena:StopQueryExecution",
            ],
            "Resource": [f"arn:aws:athena:{region}:{account_id}:workgroup/{workgroup}"],
        },
        {
            # Glue tables' data can live in ANY bucket -> cannot be dataset-scoped.
            "Sid": "TableDataRead",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": ["*"],
        },
    ]
    if results_bucket_arn:
        statements.append(
            {
                "Sid": "AthenaResultsWrite",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                "Resource": [results_bucket_arn, f"{results_bucket_arn}/*"],
            }
        )
    if enable_lakeformation:
        # LF-governed catalog: the query engine calls lakeformation:GetDataAccess
        # to vend short-lived S3 creds for governed table data. Must be in BOTH the
        # data role's identity policy AND here — the session policy is intersected
        # with the role, so omitting it would strip the permission. GetDataAccess
        # has no resource-level scoping. (Enabled via OKF_ENABLE_LAKEFORMATION.)
        statements.append(
            {
                "Sid": "LakeFormationDataAccess",
                "Effect": "Allow",
                "Action": ["lakeformation:GetDataAccess"],
                "Resource": ["*"],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _redshift_session_policy(
    region: str,
    account_id: str,
    *,
    secret_arn: str | None,
) -> dict:
    """Inline STS session policy pinning access to ONE Redshift connection secret.

    Intersected with the data role's identity policy at assume time. Grants:

    * ``redshift-data`` API calls (async execute / describe / results / cancel).
      These actions are NOT resource-scopable to a cluster/workgroup, so they
      target ``*``.
    * ``secretsmanager:GetSecretValue`` on the ONE mapping secret. This is the
      cross-target containment: the harvest authenticates exclusively via the
      secret (the only auth mode the descriptor carries), so with a single pinned
      secret the session can only reach targets that secret's DB credentials can
      log in to. (Temp-credential auth — ``redshift:GetClusterCredentials`` /
      ``redshift-serverless:GetCredentials`` with a ``DbUser`` — is deliberately
      NOT granted: nothing wires a db_user from the descriptor, so granting it
      would be dead, un-exercised privilege.)

    Stays well under the 2,048-char inline session-policy limit.
    """
    statements: list[dict] = [
        {
            "Sid": "RedshiftDataApi",
            "Effect": "Allow",
            "Action": [
                "redshift-data:ExecuteStatement",
                "redshift-data:DescribeStatement",
                "redshift-data:GetStatementResult",
                "redshift-data:CancelStatement",
            ],
            "Resource": ["*"],
        }
    ]
    if secret_arn:
        statements.append(
            {
                # Secret-based auth: read only the one connection secret.
                "Sid": "RedshiftSecretRead",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [secret_arn],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _results_bucket_arn(output_location: str | None) -> str | None:
    """Best-effort: derive the results bucket ARN from ``s3://bucket/prefix``.

    Used to include an Athena-results write grant in the session policy. Returns
    None if the location is unset/unparseable (the grant is then omitted and
    Athena result writes would be denied by the scoped creds — which is why the
    runtime always sets OKF_ATHENA_OUTPUT).
    """
    if not output_location or not output_location.startswith("s3://"):
        return None
    bucket = output_location[len("s3://") :].split("/", 1)[0]
    return f"arn:aws:s3:::{bucket}" if bucket else None


def build_scoped_session(
    *,
    role_arn: str,
    session_policy: dict,
    region: str,
    session_name: str = "okf-harvest",
):
    """A botocore Session whose creds are the data role assumed with a session policy.

    Wraps the assume in ``RefreshableCredentials`` so the 1-hour role-chaining cap
    doesn't expire a multi-hour harvest: the refresh callable re-assumes the same
    role with the same inline policy whenever the creds near expiry, transparently
    to every client built from the session.
    """
    import boto3
    from botocore.credentials import RefreshableCredentials
    from botocore.session import get_session

    sts = boto3.client("sts", region_name=region)
    policy_json = json.dumps(session_policy)

    def _refresh() -> dict:
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            Policy=policy_json,
            DurationSeconds=_ASSUME_DURATION_SECONDS,
        )
        c = resp["Credentials"]
        return {
            "access_key": c["AccessKeyId"],
            "secret_key": c["SecretAccessKey"],
            "token": c["SessionToken"],
            "expiry_time": c["Expiration"].isoformat(),
        }

    creds = RefreshableCredentials.create_from_metadata(
        metadata=_refresh(),
        refresh_using=_refresh,
        method="sts-assume-role",
    )
    botocore_session = get_session()
    botocore_session._credentials = creds
    botocore_session.set_config_variable("region", region)
    return boto3.Session(botocore_session=botocore_session)


def build_source(
    dataset: str,
    *,
    source: dict[str, Any] | None = None,
    region: str | None = None,
    account_id: str | None = None,
) -> Source:
    """Build the :class:`~harvest.source_base.Source` for a dataset, dispatching on
    the source descriptor's ``type``.

    ``source`` is the ``okf_core.sources`` descriptor (``{type, ...config}``) the
    Control API resolves from the registry and threads through the invocation
    payload. When absent (a legacy caller / an older payload), we default to a Glue
    source whose database IS the ``dataset`` name — the historical convention — so
    nothing that predates the descriptor breaks.

    Per-invocation credential down-scoping (see ``_glue_session_policy`` /
    ``_redshift_session_policy``): when ``OKF_HARVEST_DATA_ROLE_ARN`` is set we assume
    the data role with an inline policy pinned to THIS dataset's resources and build
    the source's clients from those scoped creds; unset (local dev / tests) falls
    back to ambient creds. Scoping is a production hardening, not a correctness dep.
    """
    from okf_core.sources import normalize_source

    region = region or os.environ.get("AWS_REGION", "us-east-1")
    account_id = account_id or os.environ.get("OKF_ACCOUNT_ID", "")

    # Resolve the descriptor. Absent -> the historical glue-by-dataset-name default.
    resolved = normalize_source(source, glue_database=None if source else dataset)
    source_type = resolved.get("type")

    if source_type == GlueAthenaSource.name:
        return _build_glue_source(resolved, region=region, account_id=account_id)
    if source_type == RedshiftSource.name:
        return _build_redshift_source(resolved, region=region, account_id=account_id)
    # normalize_source already rejects unsupported types, so this is defensive.
    raise ValueError(f"no harvest source implementation for type {source_type!r}")


def _build_glue_source(
    source: dict[str, Any], *, region: str, account_id: str
) -> GlueAthenaSource:
    """A GlueAthenaSource with per-invocation scoped Glue/Athena clients."""
    import boto3

    database = source["glue_database"]
    workgroup = os.environ.get("OKF_ATHENA_WORKGROUP") or "primary"
    output_location = os.environ.get("OKF_ATHENA_OUTPUT")
    data_role_arn = os.environ.get("OKF_HARVEST_DATA_ROLE_ARN")
    enable_lf = bool(os.environ.get("OKF_ENABLE_LAKEFORMATION"))

    glue = athena = None
    if data_role_arn:
        policy = _session_policy(
            region=region,
            account_id=account_id,
            database=database,
            workgroup=workgroup,
            results_bucket_arn=_results_bucket_arn(output_location),
            enable_lakeformation=enable_lf,
        )
        try:
            session = build_scoped_session(
                role_arn=data_role_arn,
                session_policy=policy,
                region=region,
                session_name=f"okf-harvest-{database}"[:64],
            )
            glue = session.client("glue", region_name=region)
            athena = session.client("athena", region_name=region)
            log.info(
                "Harvest using per-invocation scoped creds (data role, db=%s, wg=%s)",
                database,
                workgroup,
            )
        except Exception:  # noqa: BLE001 - never let scoping wiring wedge a harvest
            # A misconfigured trust/role would otherwise brick every harvest. Fail
            # OPEN to ambient creds but log LOUDLY so the regression is visible —
            # correctness is preserved; the scoping (defense-in-depth) is reported
            # as not applied. (The static execution role is itself least-privilege
            # for everything except the moved Glue/Athena, so this is bounded.)
            log.exception(
                "Scoped-session assume FAILED for db=%s; falling back to ambient "
                "execution-role creds (per-invocation scoping NOT applied)",
                database,
            )
            glue = athena = None
    if glue is None:
        glue = boto3.client("glue", region_name=region)
        athena = boto3.client("athena", region_name=region)

    return GlueAthenaSource(
        database=database,
        glue=glue,
        athena=athena,
        region=region,
        account_id=account_id,
        athena_output_location=output_location,
        athena_workgroup=os.environ.get("OKF_ATHENA_WORKGROUP"),
        catalog_id=os.environ.get("OKF_GLUE_CATALOG_ID") or None,
    )


def _build_redshift_source(
    source: dict[str, Any], *, region: str, account_id: str
) -> RedshiftSource:
    """A RedshiftSource with a per-invocation scoped ``redshift-data`` client.

    Connection routing comes ENTIRELY from the mapping's self-describing source
    descriptor (``cluster_identifier``/``workgroup_name`` + ``secret_arn``), which
    the operator sets in the UI — so any cluster/workgroup in the account is
    harvestable with no deploy-time connection config. Mirrors the Glue path's
    fail-open behavior (a scoped-assume failure falls back to ambient creds).
    """
    import boto3

    database = source["redshift_database"]
    # The connection comes ENTIRELY from the mapping's self-describing descriptor —
    # the operator picks the cluster/workgroup + secret in the UI. There is no
    # deploy-time connection env (a db-only descriptor with no target simply has no
    # cluster/workgroup, and RedshiftSource raises if neither is set).
    cluster = source.get("cluster_identifier") or None
    workgroup = source.get("workgroup_name") or None
    secret_arn = source.get("secret_arn") or None
    data_role_arn = os.environ.get("OKF_HARVEST_DATA_ROLE_ARN")

    data = None
    if data_role_arn:
        policy = _redshift_session_policy(
            region=region,
            account_id=account_id,
            secret_arn=secret_arn,
        )
        try:
            session = build_scoped_session(
                role_arn=data_role_arn,
                session_policy=policy,
                region=region,
                session_name=f"okf-harvest-{database}"[:64],
            )
            data = session.client("redshift-data", region_name=region)
            log.info(
                "Harvest using per-invocation scoped creds (data role, redshift db=%s)",
                database,
            )
        except Exception:  # noqa: BLE001 - never let scoping wiring wedge a harvest
            log.exception(
                "Scoped-session assume FAILED for redshift db=%s; falling back to "
                "ambient execution-role creds (per-invocation scoping NOT applied)",
                database,
            )
            data = None
    if data is None:
        data = boto3.client("redshift-data", region_name=region)

    return RedshiftSource(
        database=database,
        data=data,
        cluster_identifier=cluster,
        workgroup_name=workgroup,
        secret_arn=secret_arn,
        region=region,
        account_id=account_id,
    )


def dataset_root(mount_path: str, data_domain: str, dataset: str) -> str:
    """The per-dataset FS root under the shared okf/ mount.

    The S3 Files access point is mounted at ``mount_path`` (e.g. /mnt/data)
    rooted at ``okf/``; each session confines itself to
    ``<mount>/<domain>/<dataset>``.
    """
    return os.path.join(mount_path, data_domain, dataset)

"""Policy-check plumbing for a dataset's wiki — the AWS-facing half.

A dataset's **policy document** (``policies.yaml`` — see
``okf_core.policy_doc`` for the pure format) is a **derived artifact of the
bundle**, exactly like the vector index: authored from the wiki's reference
docs by the harvest runtime's policy-author agent, fingerprinted against the
sources it was authored from, and consumed by the chat policy check's
LLM-judge fleet. ``okf_core.ar_sources`` owns the two pure invariants (which
files are policy material, and the fingerprint a document is stamped with);
this module owns everything S3/DynamoDB-shaped around them, because three
services touch the same shapes and must never disagree:

* **harvest** (post-complete follow-on + ``mode="ar_rules"``) — gather sources, run the
  author, persist the document + author state, stamp the row.
* **incremental** (the rebuild authority) — fingerprint-compare and dispatch
  authoring runs; reap abandoned ``building`` rows.
* **chat** (``policy_check``) — recompute the fingerprint for the staleness
  gate, read the policy document for the judges.

Every client is injected (``s3``, low-level ``dynamodb``) — this package never
constructs one, so the offline tests can fake freely.

History note: v1 of this feature compiled the document into a Bedrock
Automated Reasoning (SMT) policy — hence the legacy ``ar_`` prefixes on the
module name, the registry attributes, and the ``mode="ar_rules"`` dispatch,
all kept to avoid a data migration. The AR engine was removed in favor of the
judge fleet (2026-08-02, see the design doc's v2 pivot), and the per-dataset
``ar_enrolled`` opt-in was retired with it (2026-08-03): policy documents are
an always-on derived artifact now, and the only feature switch left is the
deploy-wide ``OKF_POLICY_BUILD_ENABLED`` flag. Retired attributes
(``ar_enrolled``, the v1 Bedrock set) may linger on rows written by older
deploys; nothing reads them, and deleting the dataset deletes the row.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from okf_aws.s3_bundle import bundle_prefix
from okf_core.ar_sources import compute_source_hash, is_ar_source

log = logging.getLogger(__name__)

#: Cap on the authored document. Generous (a lean document is the prompt's
#: job; this only stops runaway generation) but real: every judge shard's
#: prompt carries a slice of it.
MAX_DOC_CHARS = 50_000


# -- registry row vocabulary -----------------------------------------------------

#: ``ar_build_status`` values. ``building`` doubles as the per-dataset
#: AUTHORING lease (the conditional flip is the ONLY serialization point, so N
#: triggers collapse to one authoring run); ``stale`` means the wiki moved
#: under an authored document.
BUILD_BUILDING = "building"
BUILD_READY = "ready"
BUILD_FAILED = "failed"
BUILD_STALE = "stale"

#: A policy document may be judged against only from these statuses — AND only
#: when its stored fingerprint still matches the live wiki (the second half of
#: the gate is the caller's, since only it knows the freshly computed hash).
USABLE_BUILD_STATUSES: frozenset[str] = frozenset({BUILD_READY})

#: ``flag_stale`` may only move a row out of these. A ``building`` row must not
#: be clobbered (its authoring stamp decides), and a ``failed`` row has nothing
#: to invalidate.
STALE_FLAGGABLE_STATUSES: frozenset[str] = frozenset({BUILD_READY, BUILD_STALE})

ATTR_BUILD_STATUS = "ar_build_status"
ATTR_SOURCE_HASH = "ar_source_hash"
#: The fingerprint of what the author actually SAW, captured at gather time
#: and stamped verbatim onto :data:`ATTR_SOURCE_HASH` when authoring finishes.
#: Never recomputed at stamp time: a wiki that moves mid-authoring must yield
#: a document that is stale on arrival, not one that claims to describe the
#: new state.
ATTR_PENDING_SOURCE_HASH = "ar_pending_source_hash"

#: The live attribute set: {ATTR_BUILD_STATUS, ATTR_SOURCE_HASH,
#: ATTR_PENDING_SOURCE_HASH, ar_build_started_at, ar_built_at,
#: ar_build_detail}. A dataset with none of these has never begun the policy
#: lifecycle — the nightly reconcile deliberately skips such rows (no silent
#: fleet-wide backfill); the first document comes from a manual Sync, the next
#: harvest/increment, or a repromote. Retired attributes (``ar_enrolled``,
#: the v1 Bedrock AR set) may linger on legacy rows; nothing reads them.

_DETAIL_MAX = 512


# -- naming ----------------------------------------------------------------------


def dataset_label(data_domain: str, dataset: str) -> str:
    """``"<domain>/<dataset>"`` — the human label used in prompts and logs."""
    return f"{data_domain}/{dataset}"


# -- off-mount S3 artifacts ------------------------------------------------------

#: Derived policy artifacts live under a top-level prefix SIBLING to ``okf/``
#: (the ``benchmark/`` precedent) in the same bundle bucket: nothing an LLM
#: role's file tools can reach, and outside the reindex rule's ``okf/*.md``
#: filter, so nothing written here ever enqueues a vector update.
_POLICY_PREFIX = "policy/"

#: The policy document's file name. YAML (one trackable entry per policy —
#: see ``okf_core.policy_doc``) rather than the v1 numbered-prose ``ar_rules.md``.
POLICY_DOC_NAME = "policies.yaml"


def policy_prefix(data_domain: str, dataset: str) -> str:
    return f"{_POLICY_PREFIX}{data_domain}/{dataset}/"


def policy_doc_key(data_domain: str, dataset: str) -> str:
    """The authored policy document the judges (and the UI) read."""
    return f"{policy_prefix(data_domain, dataset)}{POLICY_DOC_NAME}"


def put_policy_doc(
    s3, *, bucket: str, data_domain: str, dataset: str, doc_text: str
) -> str:
    """Persist ``policies.yaml`` and return its key. Written at authoring time."""
    key = policy_doc_key(data_domain, dataset)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=doc_text.encode("utf-8"),
        ContentType="application/yaml",
    )
    return key


def read_policy_doc(s3, *, bucket: str, data_domain: str, dataset: str) -> str | None:
    """The stored policy document, or None when no authoring has run."""
    try:
        raw = s3.get_object(Bucket=bucket, Key=policy_doc_key(data_domain, dataset))[
            "Body"
        ].read()
    except Exception:  # noqa: BLE001 - absent artifact is a normal state
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


# -- deterministic rules: schema sidecar ---------------------------------------------
#
# `rules_schema.json` is the QUALIFICATION schema for the document's `rules:`
# blocks — column MEMBERSHIP only ({"databases": {db: {table: [columns]}}}),
# snapshotted at authoring time from the harvest's own `.metadata/columns.tsv`
# (the schema the wiki was authored against — deliberately NOT live Glue,
# which may have drifted ahead of the docs the rules trace to). The chat
# check loads it next to policies.yaml; drift self-heals through the same
# staleness gate (a schema change re-authors the wiki -> sources move ->
# rebuild re-snapshots the sidecar).


def rules_schema_key(data_domain: str, dataset: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}rules_schema.json"


def put_rules_schema(
    s3, *, bucket: str, data_domain: str, dataset: str,
    databases: dict[str, dict[str, list[str]]],
) -> str:
    """Persist the qualification-schema sidecar next to policies.yaml."""
    key = rules_schema_key(data_domain, dataset)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({"version": 1, "databases": databases}).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def read_rules_schema(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> dict[str, dict[str, list[str]]] | None:
    """The sidecar's ``databases`` mapping, or None when it does not exist.

    None (not ``{}``) so callers can tell "no sidecar was ever authored"
    apart from an empty schema — the author gate refuses `rules:` in the
    former case. Only a genuinely MISSING object reads None; a transient S3
    failure (throttle, 5xx) RAISES so the caller can log it as what it is —
    folding it into None made the chat trace misdiagnose an outage as
    "never authored, re-author to fix", which no re-authoring can fix.
    """
    try:
        obj = s3.get_object(
            Bucket=bucket, Key=rules_schema_key(data_domain, dataset)
        )
        raw = json.loads(obj["Body"].read())
    except Exception as e:  # noqa: BLE001 - triaged below
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        raise
    databases = raw.get("databases") if isinstance(raw, dict) else None
    if not isinstance(databases, dict):
        return None
    out: dict[str, dict[str, list[str]]] = {}
    for db, tables in databases.items():
        if not isinstance(tables, dict):
            return None
        out[str(db).lower()] = {
            str(t).lower(): [str(c).lower() for c in (cols or [])]
            for t, cols in tables.items()
            if isinstance(cols, list)
        }
    return out


# -- author state ------------------------------------------------------------------
#
# `sources_manifest.json` + `sources/<rel>` copies record what the policy
# author last SAW. The next authoring run diffs the live wiki against these to
# hand the agent per-file unified diffs — a surgical edit of policies.yaml
# (stable ids!) instead of a from-scratch rewrite.


def sources_manifest_key(data_domain: str, dataset: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}sources_manifest.json"


def source_copy_key(data_domain: str, dataset: str, rel_path: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}sources/{rel_path}"


def build_sources_manifest(sources: list[tuple[str, bytes]]) -> dict[str, Any]:
    """``{fingerprint, files: {rel: sha256hex}}`` for a gathered source set."""
    return {
        "fingerprint": hash_sources(sources) or "",
        "files": {
            rel: hashlib.sha256(content).hexdigest() for rel, content in sources
        },
    }


def persist_author_state(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    sources: list[tuple[str, bytes]],
    doc_text: str,
) -> None:
    """Persist the policy document + the exact sources it was authored from.

    The copies are the DIFF BASE for the next authoring run, so they must
    correspond to whatever ``policies.yaml`` currently says. Copies of
    since-removed files may linger under ``sources/``; the manifest's key set
    is the truth, readers must ignore strays.
    """
    put_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        doc_text=doc_text,
    )
    for rel, content in sources:
        s3.put_object(
            Bucket=bucket,
            Key=source_copy_key(data_domain, dataset, rel),
            Body=content,
            ContentType="text/markdown",
        )
    s3.put_object(
        Bucket=bucket,
        Key=sources_manifest_key(data_domain, dataset),
        Body=json.dumps(build_sources_manifest(sources), sort_keys=True).encode(),
        ContentType="application/json",
    )


def read_sources_manifest(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> dict[str, Any]:
    """The last author state's manifest, or ``{}`` (first authoring run)."""
    try:
        raw = s3.get_object(
            Bucket=bucket, Key=sources_manifest_key(data_domain, dataset)
        )["Body"].read()
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 - absent manifest = author from scratch
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_source_copy(
    s3, *, bucket: str, data_domain: str, dataset: str, rel_path: str
) -> bytes | None:
    """A source file as the author last saw it, or None."""
    try:
        return s3.get_object(
            Bucket=bucket, Key=source_copy_key(data_domain, dataset, rel_path)
        )["Body"].read()
    except Exception:  # noqa: BLE001 - absent copy = treat the file as new
        return None


# -- source gathering ------------------------------------------------------------


def _iter_source_keys(s3, bucket: str, prefix: str):
    """Yield ``(rel_path, full_key)`` for every policy-source object (LIST only)."""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj.get("Key", "")
            rel = key[len(prefix) :]
            # is_ar_source works on DATASET-relative paths, which is also what
            # excludes external/<domain>/<dataset>/references/... for free.
            if not rel or rel.endswith("/") or not is_ar_source(rel):
                continue
            yield rel, key
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")
        if not token:
            return


def list_source_paths(s3, bucket: str, data_domain: str, dataset: str) -> list[str]:
    """The dataset's policy-source rel paths, sorted — LIST only, no body GETs.

    For callers that need the file SET but not a fingerprint (e.g. a status
    poll while a build is running): a fraction of :func:`gather_sources`'s
    cost, which downloads every body.
    """
    prefix = bundle_prefix(data_domain, dataset)
    return sorted(rel for rel, _key in _iter_source_keys(s3, bucket, prefix))


def gather_sources(
    s3, bucket: str, data_domain: str, dataset: str
) -> list[tuple[str, bytes]]:
    """The dataset's policy source files as sorted ``(relative key, bytes)``.

    One paginated ``list_objects_v2`` over the whole dataset prefix (tens of
    objects) plus a GET per selected file — cheaper than six prefix listings.
    Read from S3, not from the harvest mount, so the fingerprint describes what
    an S3-reading rebuilder (incremental) or verifier (chat) will see, and so
    the same function works in a Lambda with no mount.
    """
    prefix = bundle_prefix(data_domain, dataset)
    pairs: list[tuple[str, bytes]] = []
    for rel, key in _iter_source_keys(s3, bucket, prefix):
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        pairs.append((rel, body if isinstance(body, bytes) else str(body).encode()))
    return sorted(pairs, key=lambda pair: pair[0])


def hash_sources(pairs: Iterable[tuple[str, bytes]]) -> str | None:
    """The fingerprint of an already-gathered source set, None when it's empty.

    The None sentinel is this layer's job: ``compute_source_hash`` raises on an
    empty set so "no sources" can never be confused with a hash of nothing.
    """
    try:
        return compute_source_hash(pairs)
    except ValueError:
        return None


def source_hash(s3, bucket: str, data_domain: str, dataset: str) -> str | None:
    """Gather + fingerprint in one call. None means "nothing to author".

    Callers that also need the bytes (the authoring path) should
    :func:`gather_sources` once and :func:`hash_sources` the result instead of
    walking S3 twice.
    """
    return hash_sources(gather_sources(s3, bucket, data_domain, dataset))


# -- the authoring prompt ----------------------------------------------------------

POLICY_AUTHOR_PROMPT = """\
You convert dataset documentation into a YAML policy document for a fleet of
LLM judges. The judges run MID-TURN, in two tracks: computational policies
are judged against each analytical SQL query the AI data analyst runs (as it
runs), behavioural policies against the steps the agent has taken so far
(the queries, the clarifications it asked, the tools it used). Each judge
receives one small shard of one track's policies.

Output format — a YAML mapping with a top-level `policies` list; every entry
has EXACTLY these fields:

policies:
  - id: P001
    type: <computational | behavioural>
    condition: >-
      <WHEN the policy applies — plain language over the turn's conduct>
    action: >-
      <what the agent/answer MUST do when the condition holds>
    source: references/<the wiki page this policy traces to>.md

Field rules:
* `id` — P-prefixed, unique, and STABLE: when updating an existing document,
  a policy whose meaning survives keeps its id (the UI tracks policies over
  time); only a genuinely new policy mints a new id, and a retired id is
  never reused.
* `type` — EXACTLY ONE of `computational` or `behavioural` (never both;
  when a policy has a computational core plus a disclosure duty, tag it
  computational — the query judge handles the attached obligation):
  - `computational`: the violation is visible in a SQL query itself —
    summing a non-additive/snapshot measure, an ungoverned collapse or
    DISTINCT, a fan-out join, aggregating documented sentinel/placeholder
    values, skipping a mandatory recipe/transform, treating absence as zero.
  - `behavioural`: a process rule about the agent's conduct — ask before
    committing to a reading of an ambiguous term, refuse out-of-domain
    requests, require an explicit scope, honor the documented data horizon
    when framing what can be answered.
* `condition` — one plain-language sentence a judge can test against a turn:
  what the user asked, what queries did, what the answer states. Never
  reference documentation pages in the condition; the policy must stand alone.
* `action` — the checkable obligation (ask for clarification, refuse, include
  a specific caveat, apply a documented transform, never combine X with Y,
  never state figures when …). Write it so a judge can decide violated /
  not violated from the turn alone.
* `source` — the wiki page the policy traces to.
* `rules` — OPTIONAL, computational policies only: when (and ONLY when) the
  policy's anti-pattern is mechanically visible in a query's structure, bind
  it as one or more deterministic rules. Rules are evaluated on the SQL's
  syntax tree BEFORE the judges — a violation they prove flags the query to
  the agent as proof, so bind only what the source document asserts. Each rule
  picks ONE dimension from this CLOSED catalog (an unknown dimension is
  rejected; a policy fitting none stays prose-only, which is fine):
  - `forbidden_aggregation` — an aggregate corrupts the column (cumulative
    snapshots, running totals). `targets: [table.column, …]`,
    `aggs: [sum|avg|max|min|count]` (default `[sum, avg]`).
  - `forbidden_usage` — the column's stored form makes a context wrong (text
    times ordered/compared/aggregated as durations). `targets`, `contexts`
    subset of `[order, aggregate, compare, arithmetic]` (default all).
  - `forbidden_function` — a function must never touch the column (ROUND on
    precise points, CAST-to-int). Function names use Trino spellings
    (`date_diff`, `regexp_extract` — matched underscore-insensitively);
    cast types accept int/integer, float/real, decimal/numeric.
    `targets`, `functions: [name, …]` and/or
    `cast_types: [int|bigint|…]`.
  - `forbidden_grouping` — a column that must never be a GROUP BY key
    (brand-scoped spellings, generation-scoped codes, consolidation
    roles: "never group on X, resolve through Y instead"). `targets:
    [table.column, …]`. Grouping only — reading/filtering the column
    stays legal, so bind it even when the column is fine to filter on.
  - `required_predicate` — using (or aggregating) a table requires a WHERE
    conjunct (soft-deletes, status filters, sentinel exclusion, explicit
    period bounds). `table`,
    `when: aggregate|use` (default aggregate), `when_columns: [col, …]`
    (default any), `require: {column, op: is_null|is_not_null|eq|neq|bounded,
    value?}` (value for eq/neq only; `bounded` = any positive equality/
    range/IN conjunct on the column, whatever the value — use it for
    "must pin an explicit window/date bound" policies rather than
    approximating with is_not_null), `or_group_by: col` (grouping as an
    accepted alternative).
  - `required_guard` — a numeric CAST of mixed-content text needs a
    regexp_like guard or TRY_CAST. `targets`, optional `cast_types`,
    `guard_functions`.
  - `forbidden_sequencing_key` — the column must never define order: opaque
    ids mistaken for chronology. Fires on a window ORDER BY and on
    `ORDER BY … LIMIT 1` ("the latest row"); a plain ordered listing is
    presentational and never violates. `targets`.
  - `required_distinct` — a COUNT that fans out at the table's grain.
    `table`, `group_by: col`, `count_distinct: col`, and normally
    `when_filtered: {column, op, value?}` — the predicate that makes this
    reading wrong (e.g. the winner predicate). WITHOUT the trigger the rule
    fires on every plain count at that grouping, so omit it only when any
    such count is genuinely wrong. Any `COUNT(DISTINCT …)` is fan-out-safe
    and never violates.
  Every rule MUST carry `examples` with one `violation` and one `pass` SQL
  statement — its self-test. The gate executes both against the dataset's
  schema: the violation example must trip the rule and the pass example must
  not (an `unknown` fails too). Table/column names must exist in the schema;
  write them lowercase and unqualified (`table.column`). If no rules schema
  is available for the dataset, write NO rules blocks at all.

BE SELECTIVE — a policy earns its place only if violating it would make a
real answer WRONG (not merely under-documented). Hunt exactly these classes:
mandatory recipes/transforms the docs declare; deduplication the docs require
because duplicates genuinely occur; documented sentinel/placeholder values
that corrupt aggregates; measures the docs declare disjoint/never-combinable;
the documented data horizon; deprecated/superseded objects; snapshot measures
that must not be summed over time; genuinely ambiguous business terms (one
ASK policy per AMBIGUOUS TERM, not per possible reading).
Do NOT write policies that: restate schema facts with no obligation on the
answer; enumerate each value of an enum (one policy about the enum's pitfall
suffices); encode conditions no turn could ever evidence; or split one
failure mode across several near-identical entries — merge them.

Size the document to the dataset's GENUINE semantic richness — a thin schema
warrants a dozen policies; only a truly rich policy surface justifies several
dozen. Every policy must trace to a statement in the source documents; do not
invent policy that is not in the sources.

Output ONLY the YAML document, no preamble, no code fences."""


# -- registry stamps (the DATASET# mapping row) ------------------------------------


def policy_usable(
    *, build_status: str, stored_hash: str | None, live_hash: str | None
) -> bool:
    """THE usability gate: may this document be judged against?

    Both halves are required — a ``ready`` status AND a fingerprint that still
    matches the live wiki. Kept here, on scalars only (no row flavor), so the
    check-time gate and the rebuild authority can never disagree about it. A
    document authored from anything but the current wiki state is unusable by
    definition: only the latest wiki state is truth.
    """
    if build_status not in USABLE_BUILD_STATUSES:
        return False
    if not stored_hash or not live_hash:
        return False
    return stored_hash == live_hash


def registry_key(data_domain: str, dataset: str) -> dict[str, Any]:
    """The mapping row's key — policy attrs live on ``DOMAIN#/DATASET#``.

    NOT the ``HARVEST#…/STATUS`` row: a policy document belongs to the
    dataset, not to one harvest run. Low-level (``{"S": …}``) values because
    the registry table is driven by ``boto3.client("dynamodb")`` everywhere it
    is written.
    """
    return {"pk": {"S": f"DOMAIN#{data_domain}"}, "sk": {"S": f"DATASET#{dataset}"}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _marshal(value: Any) -> dict[str, Any]:
    """bool→BOOL, int/float→N, everything else→S (bool BEFORE int — subclass)."""
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


def _error_code(exc: Exception) -> str:
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", "")


def _is_condition_failure(exc: Exception) -> bool:
    return _error_code(exc) == "ConditionalCheckFailedException"


def _set_attrs(
    ddb,
    table: str,
    *,
    data_domain: str,
    dataset: str,
    attrs: dict[str, Any],
    condition: str = "attribute_exists(pk)",
    extra_names: dict[str, str] | None = None,
    extra_values: dict[str, Any] | None = None,
) -> None:
    """UpdateItem a flat-scalar attr set onto the mapping row.

    Every attribute name is aliased through ``ExpressionAttributeNames`` — cheap
    insurance against a reserved-word collision — and every write is conditional
    on the row still existing: UpdateItem otherwise upserts, and an authoring
    run finishing after the dataset was deleted would resurrect a phantom row.
    """
    names: dict[str, str] = dict(extra_names or {})
    values: dict[str, Any] = dict(extra_values or {})
    sets: list[str] = []
    for i, (attr, value) in enumerate(attrs.items()):
        alias, placeholder = f"#p{i}", f":p{i}"
        names[alias] = attr
        values[placeholder] = _marshal(value)
        sets.append(f"{alias} = {placeholder}")
    ddb.update_item(
        TableName=table,
        Key=registry_key(data_domain, dataset),
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression=condition,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def try_flip_building(
    ddb, table: str, *, data_domain: str, dataset: str, pending_hash: str
) -> bool:
    """Claim the dataset's authoring slot. True when this caller owns the run.

    THE serialization point for the whole feature: the flip to ``building`` is a
    conditional UpdateItem, so the post-harvest build, the nightly reconcile and
    N duplicate ``policy_rebuild`` events collapse to exactly one authoring run.
    Returns False (never raises) when another run already holds the row, and
    False when the mapping row is gone.

    STALENESS TAKEOVER, symmetric with :func:`build_lock_active`: a ``building``
    flip older than :data:`BUILD_LOCK_STALE_SECONDS` is a dead author — past
    that window the READERS already treat the lock as free (harvests proceed),
    so the flip must too, or every completed harvest's follow-on build would
    silently lose to the corpse until the reconcile reaps it.

    ``pending_hash`` is the fingerprint of the sources this run is about to
    author from, parked on :data:`ATTR_PENDING_SOURCE_HASH` for the ready
    stamp to carry over verbatim.
    """
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=BUILD_LOCK_STALE_SECONDS)
    ).isoformat(timespec="seconds")
    try:
        _set_attrs(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            attrs={
                ATTR_BUILD_STATUS: BUILD_BUILDING,
                ATTR_PENDING_SOURCE_HASH: pending_hash,
                "ar_build_started_at": _now(),
            },
            condition=(
                "attribute_exists(pk) AND "
                "(attribute_not_exists(#bs) OR #bs <> :building "
                "OR ar_build_started_at < :stale)"
            ),
            extra_names={"#bs": ATTR_BUILD_STATUS},
            extra_values={
                ":building": {"S": BUILD_BUILDING},
                ":stale": {"S": stale_cutoff},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition means "already building"
        if _is_condition_failure(e):
            return False
        raise


def stamp_ready(
    ddb, table: str, *, data_domain: str, dataset: str, fingerprint: str
) -> str:
    """Publish a finished authoring run onto the row. Returns the status.

    ``fingerprint`` is carried from gather time and stored VERBATIM as
    ``ar_source_hash``: it describes what was authored from, not what the wiki
    says now, so a mutation mid-authoring makes the document stale-on-arrival
    instead of silently mislabelling it as current.
    """
    _set_attrs(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        attrs={
            ATTR_SOURCE_HASH: fingerprint,
            "ar_built_at": _now(),
            ATTR_BUILD_STATUS: BUILD_READY,
        },
    )
    return BUILD_READY


def stamp_build_failed(
    ddb, table: str, *, data_domain: str, dataset: str, reason: str
) -> str:
    """Record an authoring run that produced no usable document."""
    _set_attrs(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        attrs={
            ATTR_BUILD_STATUS: BUILD_FAILED,
            "ar_build_detail": str(reason)[:_DETAIL_MAX],
        },
    )
    return BUILD_FAILED


def flag_stale(ddb, table: str, *, data_domain: str, dataset: str) -> bool:
    """Mark an authored document as superseded by a wiki change. True when applied.

    Conditional on the row being in :data:`STALE_FLAGGABLE_STATUSES`, so a
    lazily discovered staleness (a repromote, a hash mismatch at check time)
    can never clobber an in-flight ``building`` row or resurrect a ``failed``
    one. Re-flagging an already-``stale`` row is a deliberate no-op write,
    which keeps the callers idempotent.
    """
    try:
        _set_attrs(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            attrs={ATTR_BUILD_STATUS: BUILD_STALE},
            condition=(
                "attribute_exists(pk) AND (#bs = :ready OR #bs = :stale)"
            ),
            extra_names={"#bs": ATTR_BUILD_STATUS},
            extra_values={
                ":ready": {"S": BUILD_READY},
                ":stale": {"S": BUILD_STALE},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition means "nothing to stale"
        if _is_condition_failure(e):
            return False
        raise


def lifecycle_begun(item: dict[str, Any] | None) -> bool:
    """True when a raw (typed-attribute) mapping row has policy build state.

    The single reader every "has this dataset started the policy lifecycle?"
    gate uses (the chat check's dataset discovery, the nightly reconcile's
    no-backfill skip), so it can never mean different things on different
    paths. An absent row or absent ``ar_build_status`` has NOT begun.
    """
    return bool(((item or {}).get(ATTR_BUILD_STATUS) or {}).get("S"))


#: How long a ``building`` flip may block bundle-WRITING work (harvest starts,
#: repromotes) before it reads as abandoned. Authoring is minutes-scale; a row
#: stuck at ``building`` past this is a dead run the reconcile will reap, and
#: it must not wedge harvests until then.
BUILD_LOCK_STALE_SECONDS = 3600


def build_lock_active(
    ddb, registry_table: str, *, data_domain: str, dataset: str
) -> bool:
    """Whether a FRESH policy-authoring run holds this dataset's build lock.

    True only when the mapping row says ``building`` AND the flip
    (``ar_build_started_at``) is younger than :data:`BUILD_LOCK_STALE_SECONDS`.
    Bundle-writing work (harvest/annotation/cross starts, repromotes, the
    incremental orchestrator) checks this BEFORE taking the harvest lease:
    the harvest status row goes terminal at the bundle commit, so the policy
    author — still reading the just-committed wiki — is the only writer left
    to serialize against. Fail-open on read errors: the gate is a courtesy,
    not a correctness requirement (a wiki that moves mid-authoring yields a
    stale-on-arrival document that self-heals via the rebuild path).
    """
    try:
        item = (
            ddb.get_item(
                TableName=registry_table, Key=registry_key(data_domain, dataset)
            ).get("Item")
            or {}
        )
        if str((item.get(ATTR_BUILD_STATUS) or {}).get("S") or "") != BUILD_BUILDING:
            return False
        started = str((item.get("ar_build_started_at") or {}).get("S") or "")
        if not started:
            # Building with no start stamp (shouldn't happen) — treat as held;
            # the reconcile reaps abandoned rows either way.
            return True
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=BUILD_LOCK_STALE_SECONDS)
        ).isoformat(timespec="seconds")
        return started >= cutoff
    except Exception:  # noqa: BLE001 - fail-open (see docstring)
        log.warning("build-lock read failed (treating as free)", exc_info=True)
        return False

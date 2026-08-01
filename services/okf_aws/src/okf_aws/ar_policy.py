"""Bedrock Automated Reasoning policies for a dataset's wiki — the boto3 half.

An AR policy is a **derived artifact of the bundle**, exactly like the vector
index. ``okf_core.ar_sources`` owns the two pure invariants (which files are
policy material, and the fingerprint a built policy is stamped with); this
module owns everything boto3-shaped around them, because three services touch
the same wire shapes and must never disagree:

* **harvest** (finalize hook) — gather sources, preprocess, create the policy,
  start the ingest build, flip the registry row to ``building``.
* **incremental** (the single rebuild authority) — poll the workflow, complete
  the build (version + guardrail + fidelity + grounding), stamp the row.
* **chat** (``policy_check``) — recompute the fingerprint for the staleness
  gate, read the grounding map, parse ``apply_guardrail`` findings.

Every client is injected (``bedrock`` control plane, ``bedrock-runtime``, ``s3``,
low-level ``dynamodb``) — this package never constructs one, so the offline
tests can hand-fake the services moto does not implement.

Wire shapes verified against botocore 1.43.47; the traps worth knowing before
editing anything here:

* ``documentContentType`` is ``pdf`` | ``txt`` ONLY — markdown ships as ``txt``.
* ``create_automated_reasoning_policy_version`` REQUIRES the current
  ``definitionHash`` (optimistic concurrency), so a version always follows a
  ``get_automated_reasoning_policy``.
* a finding is a TAGGED UNION whose ``tooComplex``/``noTranslations`` members
  are EMPTY dicts — classify with ``in``, never with truthiness.
* an AR-carrying guardrail without ``crossRegionConfig`` is a
  ``ValidationException``; the profile is mandatory, not optional.
* rule TEXT lives in the exported policy version and the fidelity report, never
  on a finding (which carries only a rule identifier) — hence the build-time
  grounding map.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from okf_aws.s3_bundle import bundle_prefix
from okf_core.ar_sources import compute_source_hash, is_ar_source

log = logging.getLogger(__name__)

# -- service facts ---------------------------------------------------------------

#: Regions offering Bedrock Automated Reasoning checks (verified 2026-08-01).
#: A client-side allow-list, not a try/except on ValidationException: the
#: feature must degrade to *absent* (one stamped row attr, zero API calls),
#: never to an error, and region availability is a service fact rather than a
#: per-model one.
AR_SUPPORTED_REGIONS: frozenset[str] = frozenset(
    {"us-east-1", "us-east-2", "us-west-2", "eu-central-1", "eu-west-1", "eu-west-3"}
)

#: Cross-region guardrail profile stamped on every policy-carrying guardrail.
DEFAULT_GUARDRAIL_PROFILE = "us.guardrail.v1:0"


def env_guardrail_profile() -> str:
    """``OKF_POLICY_GUARDRAIL_PROFILE``, else the region-correct family.

    The profile FAMILY must match the deployment region — LIVE-VERIFIED
    (eu-west-1, 2026-08-01): CreateGuardrail with ``us.guardrail.v1:0`` in an
    EU region is a ValidationException. Terraform normally sets the env
    (region-derived); this fallback keeps an env-less context from silently
    picking the US family in an EU region.
    """
    import os

    configured = os.environ.get("OKF_POLICY_GUARDRAIL_PROFILE", "")
    if configured:
        return configured
    region = os.environ.get("AWS_REGION", "")
    return "eu.guardrail.v1:0" if region.startswith("eu-") else DEFAULT_GUARDRAIL_PROFILE

#: AR primitive type literals as the API spells them in ``policyDefinition``.
#: LIVE-VERIFIED against CreateAutomatedReasoningPolicy in eu-west-1
#: (2026-08-01): the API-reference prose claims ``bool``/``int``/``real``, but
#: the validator rejects those ("uses a type that is not defined") and accepts
#: exactly ``BOOL``/``INT``/``NUMBER`` — the same spellings exported
#: definitions carry. Never inline a type literal below.
AR_TYPE_BOOL = "BOOL"
AR_TYPE_INT = "INT"
AR_TYPE_REAL = "NUMBER"

#: The draft policy's version label. ``ListAutomatedReasoningPolicies`` returns
#: one summary per version, so resolving a policy BY NAME must pick the draft:
#: it is the only arn a build workflow may be started against.
POLICY_VERSION_DRAFT = "DRAFT"

#: The one build workflow type we use: ingest ``ar_rules.md`` into an existing
#: pre-seeded policy. (``ITERATIVELY_REFINE_POLICY`` is the future upgrade.)
BUILD_WORKFLOW_INGEST = "INGEST_CONTENT"

#: Build-workflow result asset types (LIVE-VERIFIED enum, eu-west-1
#: 2026-08-01: POLICY_DEFINITION, GENERATED_TEST_CASES, FIDELITY_REPORT,
#: BUILD_LOG, QUALITY_REPORT, POLICY_SCENARIOS, ASSET_MANIFEST,
#: SOURCE_DOCUMENT). POLICY_DEFINITION is the load-bearing one: an
#: INGEST_CONTENT workflow does NOT mutate the DRAFT — its result is staged
#: here and must be applied via UpdateAutomatedReasoningPolicy. The fidelity
#: report is OPTIONAL (not generated for ingest builds — the asset call
#: raises ResourceNotFoundException); BUILD_LOG is the grounding fallback.
ASSET_POLICY_DEFINITION = "POLICY_DEFINITION"
ASSET_FIDELITY_REPORT = "FIDELITY_REPORT"
ASSET_BUILD_LOG = "BUILD_LOG"

#: Terminal build-workflow statuses (the poller stops on these).
BUILD_WORKFLOW_COMPLETED = "COMPLETED"
BUILD_WORKFLOW_FAILED = "FAILED"
BUILD_WORKFLOW_CANCELLED = "CANCELLED"
TERMINAL_WORKFLOW_STATUSES: frozenset[str] = frozenset(
    {BUILD_WORKFLOW_COMPLETED, BUILD_WORKFLOW_FAILED, BUILD_WORKFLOW_CANCELLED}
)

#: Ingest cap. The blob limit is 5 MB, but a document beyond this is a sign the
#: preprocessing pass ran away, and AR's own guidance keeps ingests small.
MAX_INGEST_CHARS = 50_000

#: The single ingested document's name. Its numbered rules carry the wiki source
#: path in parentheses, which is what makes :func:`parse_rule_source_page` work.
RULES_DOCUMENT_NAME = "ar_rules.md"

#: ``apply_guardrail`` content qualifiers. The premise/claim split is by
#: SPEAKER: the user's utterances are premises (given), everything agent-side is
#: a claim (checked). See the design's §6.
QUALIFIER_PREMISE = "query"
QUALIFIER_CLAIM = "guard_content"

#: Required on every guardrail even though ours never blocks anything: the check
#: is advisory, its ``outputs`` are discarded, and only the AR assessment is
#: read. Never surfaced to a user.
BLOCKED_MESSAGING = "Policy check only; this guardrail does not block content."

#: ``automatedReasoningPolicyConfig.confidenceThreshold`` (0..1). The service
#: drops findings whose translation confidence falls below it, so this trades
#: recall for precision — verify the setting against real findings at the spike
#: before treating it as tuned.
AR_CONFIDENCE_THRESHOLD = 1.0

# -- registry row vocabulary -----------------------------------------------------

#: ``ar_build_status`` values. ``building`` doubles as the per-dataset build
#: lease (the conditional flip is the ONLY serialization point, so N triggers
#: collapse to one build); ``stale`` means the wiki moved under a built policy.
BUILD_BUILDING = "building"
BUILD_READY = "ready"
BUILD_DEGRADED = "degraded"
BUILD_FAILED = "failed"
BUILD_STALE = "stale"
BUILD_UNSUPPORTED_REGION = "unsupported_region"

#: A policy may render a verdict only from these statuses — AND only when its
#: stored fingerprint still matches the live wiki (the second half of the gate
#: is the caller's, since only it knows the freshly computed hash).
USABLE_BUILD_STATUSES: frozenset[str] = frozenset({BUILD_READY, BUILD_DEGRADED})

#: ``flag_stale`` may only move a row out of these. A ``building`` row must not
#: be clobbered (its completion stamp decides), and a ``failed`` row has nothing
#: to invalidate.
STALE_FLAGGABLE_STATUSES: frozenset[str] = frozenset(
    {BUILD_READY, BUILD_DEGRADED, BUILD_STALE}
)

#: Fidelity gate: below either threshold the policy is ``degraded`` — the check
#: still runs, the sidebar notes reduced coverage. v1 constants; the spike
#: calibrates them.
FIDELITY_MIN_COVERAGE = 0.6
FIDELITY_MIN_ACCURACY = 0.8

#: ``stamp_build_failed`` reason that maps to the ``unsupported_region`` status
#: rather than ``failed`` — a deployment fact, not a build error.
REASON_UNSUPPORTED_REGION = BUILD_UNSUPPORTED_REGION

ATTR_BUILD_STATUS = "ar_build_status"
ATTR_BUILD_WORKFLOW_ID = "ar_build_workflow_id"
ATTR_SOURCE_HASH = "ar_source_hash"
#: The fingerprint of what a build actually INGESTED, captured at gather time
#: and stamped verbatim onto :data:`ATTR_SOURCE_HASH` at completion. Never
#: recomputed at stamp time: a wiki that moves mid-build must yield a policy
#: that is stale on arrival, not one that claims to describe the new state.
ATTR_PENDING_SOURCE_HASH = "ar_pending_source_hash"
#: Per-dataset OPT-IN (BOOL). Enrollment is the user's switch under the
#: deploy-wide flags: nothing builds, rebuilds, or renders verdicts for a
#: dataset that is not enrolled — the 100-policy account cap is a budget the
#: operator spends deliberately, never a race between datasets. Set/cleared by
#: the Control API's Reasoning endpoints; read by every build path and the chat
#: check.
ATTR_ENROLLED = "ar_enrolled"

#: Every attribute the AR feature stamps onto the mapping row. Unenrollment
#: (delete semantics) REMOVEs exactly this set, so "not enrolled" and "never
#: enrolled" are indistinguishable on the row — one state, no zombies.
AR_ROW_ATTRS: tuple[str, ...] = (
    ATTR_ENROLLED,
    ATTR_BUILD_STATUS,
    ATTR_BUILD_WORKFLOW_ID,
    ATTR_SOURCE_HASH,
    ATTR_PENDING_SOURCE_HASH,
    "ar_build_started_at",
    "ar_policy_arn",
    "ar_policy_version",
    "ar_guardrail_id",
    "ar_guardrail_version",
    "ar_built_at",
    "ar_bundle_version",
    "ar_fidelity_coverage",
    "ar_fidelity_accuracy",
    "ar_build_detail",
)

_DETAIL_MAX = 512


class PolicyCapError(RuntimeError):
    """The account's 100-policy AR limit is reached — no policy was created.

    Typed so the caller can stamp ``failed`` with a "policy_cap" reason and log
    loudly instead of treating a quota wall as a transient failure.
    """


# -- the pre-seeded variable schema (design §7.3) ---------------------------------

#: The custom enum type behind ``disposition``. AR has no string type: a value
#: set is a named type in ``policyDefinition.types``, and a variable references
#: it by name. Type names, type-value names and variable names share ONE
#: namespace (pattern ``[A-Za-z][A-Za-z0-9_]*``, <= 64 chars).
OKF_DISPOSITION_TYPE: dict[str, Any] = {
    "name": "OKFDisposition",
    "description": (
        "What an AI data analyst's answer ultimately did with the user's "
        "request: committed to an outcome, asked for clarification, blocked on "
        "an unmet documented precondition, or refused the request outright."
    ),
    "values": [
        {
            "value": "COMMIT",
            "description": (
                "The answer states figures, results, conclusions, or delivers a "
                "recommended query — it commits to an outcome. Synonyms: "
                "answered, committed, delivered."
            ),
        },
        {
            "value": "ASK",
            "description": (
                "The answer's primary move is asking the user a clarifying "
                "question (or an ask_human form was the final action) instead "
                "of answering. Synonyms: asked, clarified, requested more "
                "information."
            ),
        },
        {
            "value": "BLOCK",
            "description": (
                "The answer explicitly declines to compute or state the "
                "requested result because a documented precondition is unmet "
                "(e.g. missing as-of handling, unresolvable grain), while "
                "explaining why. Synonyms: declined with conditions, withheld "
                "pending a fix."
            ),
        },
        {
            "value": "REFUSE",
            "description": (
                "The answer declines the request entirely as out of scope or "
                "disallowed. Synonyms: refused, rejected."
            ),
        },
    ],
}

#: The OKF-wide core vocabulary, identical across datasets. Descriptions are
#: deliberately long and synonym-rich: AR's LLM translator binds free text to
#: variables THROUGH these descriptions, which makes description quality the
#: single biggest translation-quality lever (AWS guidance + design §7.2).
#: Pre-seeding also stops each build from inventing overlapping near-synonym
#: variables per dataset. ``type`` here is the NORMATIVE type (design §7.3);
#: :func:`policy_definition` translates it to the wire literal.
CORE_VARIABLES: list[dict[str, str]] = [
    {
        "name": "disposition",
        "type": OKF_DISPOSITION_TYPE["name"],
        "description": (
            "What the agent's answer ultimately did with the user's request. "
            "COMMIT: the answer states figures, results, conclusions, or delivers a "
            "recommended query — it commits to an outcome. ASK: the answer's primary "
            "move is asking the user a clarifying question (or an ask_human form was "
            "the final action) instead of answering. BLOCK: the answer explicitly "
            "declines to compute or state the requested result because a documented "
            "precondition is unmet (e.g. missing as-of handling, unresolvable grain), "
            "while explaining why. REFUSE: the answer declines the request entirely "
            "as out of scope or disallowed. Synonyms: answered/committed/delivered -> "
            "COMMIT; asked/clarified/requested more information -> ASK; declined with "
            "conditions/withheld pending a fix -> BLOCK; refused/rejected -> REFUSE."
        ),
    },
    {
        "name": "clarificationObtained",
        "type": "BOOL",
        "description": (
            "True when the user answered a clarifying question during this turn — an "
            "ask_human form was submitted and answered, or the user's message itself "
            "resolves a previously asked question. This is user-supplied ground truth. "
            "Synonyms: the user confirmed, the user chose, the user specified, a "
            "clarification form was answered, the human replied to the agent's question."
        ),
    },
    {
        "name": "termDisambiguated",
        "type": "BOOL",
        "description": (
            "True when an ambiguous business term in the question (a term the dataset "
            "documentation lists as having multiple readings, e.g. 'revenue', 'active "
            "user', 'points') was pinned to one explicit reading — either by the user's "
            "answer or by the agent explicitly declaring which reading it used. "
            "Synonyms: interpreted X as Y, took X to mean Y, using the Y definition of X, "
            "the user selected reading Y."
        ),
    },
    {
        "name": "periodSpecified",
        "type": "BOOL",
        "description": (
            "True when the effective question pins a concrete time period or date range "
            "(a year, a season, a quarter, explicit start/end dates, 'last month'). "
            "False when the question names no period at all. Synonyms: for 2019, during "
            "the 2020 season, between March and May, in Q3, year-to-date."
        ),
    },
    {
        "name": "periodWithinHorizon",
        "type": "BOOL",
        "description": (
            "True when the requested time period lies entirely inside the dataset's "
            "documented data horizon (the coverage window its documentation declares "
            "reliable). False when any part of the requested period falls before the "
            "documented start or after the documented end of coverage. Synonyms: within "
            "coverage, inside the documented range, data exists for the period; "
            "antonyms: out of range, before data begins, beyond the horizon."
        ),
    },
    {
        "name": "queryExecuted",
        "type": "BOOL",
        "description": (
            "True when at least one SQL query was actually executed against the data "
            "during this turn and returned a result the answer rests on. False when no "
            "query ran this turn — including when the answer only recommends a query "
            "to run, or reports numbers obtained in an earlier turn. Synonyms: ran a "
            "query, executed SQL, queried the table, fetched rows."
        ),
    },
    {
        "name": "dedupApplied",
        "type": "BOOL",
        "description": (
            "True when the executed query's mechanics include an explicit deduplication "
            "step — DISTINCT, GROUP BY collapsing duplicate rows, a ROW_NUMBER/QUALIFY "
            "filter, or an equivalent — in a context where the documentation says rows "
            "may be duplicated. Synonyms: deduplicated, removed duplicates, selected "
            "distinct rows, picked one row per key, collapsed duplicate records."
        ),
    },
    {
        "name": "sentinelExcluded",
        "type": "BOOL",
        "description": (
            "True when the executed query filters out the dataset's documented sentinel "
            "or placeholder values (e.g. -1 meaning unknown, 999... meaning missing, a "
            "documented dummy code) before aggregating. False when a documented sentinel "
            "could flow into the aggregate unfiltered. Synonyms: excluded placeholder "
            "values, filtered out the unknown code, removed sentinel rows, WHERE x <> -1."
        ),
    },
    {
        "name": "snapshotSummedOverTime",
        "type": "BOOL",
        "description": (
            "True when a stock/snapshot/point-in-time measure (a balance, headcount, "
            "inventory level — a value that describes a moment, not a flow) was summed "
            "or otherwise aggregated ACROSS multiple time periods, which double-counts. "
            "Synonyms: added balances across months, summed a snapshot over time, "
            "aggregated a point-in-time metric across periods."
        ),
    },
    {
        "name": "disjointMeasuresCombined",
        "type": "BOOL",
        "description": (
            "True when two measures the documentation declares disjoint (never to be "
            "summed or compared as like-for-like, e.g. booked vs billed, gross vs net) "
            "were added together, differenced, or otherwise combined into one figure. "
            "Synonyms: summed incompatible measures, mixed the two metrics, combined "
            "quantities the docs say never to combine."
        ),
    },
    {
        "name": "deprecatedObjectUsed",
        "type": "BOOL",
        "description": (
            "True when the executed or recommended query reads a table or column the "
            "documentation marks deprecated, superseded, or scheduled for removal. "
            "Synonyms: used the legacy table, queried the old column, read a deprecated "
            "object, used the superseded view."
        ),
    },
    {
        "name": "recipeApplied",
        "type": "BOOL",
        "description": (
            "True when the documented mandatory transform for this kind of question (a "
            "canonical recipe: an as-of filter, a required join path, a mandatory "
            "unit conversion) is present in the executed or recommended query. "
            "Synonyms: followed the documented recipe, applied the required as-of "
            "handling, used the canonical join, applied the mandatory transform."
        ),
    },
    {
        "name": "zeroRowsReturned",
        "type": "BOOL",
        "description": (
            "True when the answer-bearing executed query returned zero rows — an empty "
            "result set. This is a deterministic fact taken from the query result "
            "metadata, not an interpretation. Synonyms: empty result, no rows came back, "
            "the query returned nothing, 0 records."
        ),
    },
    {
        "name": "resultTruncated",
        "type": "BOOL",
        "description": (
            "True when the executed query's result hit the row cap and was truncated, "
            "so the returned rows are a prefix of the full result. Deterministic fact "
            "from result metadata. Synonyms: capped, cut off at the limit, partial "
            "result, more rows exist than were returned."
        ),
    },
    {
        "name": "rowCount",
        "type": "INT",
        "description": (
            "The number of rows the answer-bearing executed query returned, taken "
            "verbatim from result metadata. 0 means an empty result. Synonyms: rows "
            "returned, record count, result size, N rows."
        ),
    },
    {
        "name": "assumptionStated",
        "type": "BOOL",
        "description": (
            "True when the final answer (or its declared assumptions) explicitly states "
            "the interpretive choices it rests on — which reading of an ambiguous term "
            "was used, which period was assumed, which entity scope applied. Synonyms: "
            "the answer declares its assumption, notes the interpretation, says 'assuming "
            "X', states which definition was used."
        ),
    },
    {
        "name": "caveatIncluded",
        "type": "BOOL",
        "description": (
            "True when the final answer carries the caveat, warning, or disclaimer the "
            "documentation requires for this situation (e.g. 'duplicates possible in "
            "2019', 'excludes late-arriving records', 'figures truncated'). Synonyms: "
            "the answer warns about, notes the known issue, includes the required "
            "disclaimer, flags the limitation."
        ),
    },
]

#: Normative type name -> wire literal. Anything else is a custom type name and
#: passes through verbatim (AR resolves it against ``policyDefinition.types``).
_WIRE_TYPES = {"BOOL": AR_TYPE_BOOL, "INT": AR_TYPE_INT, "NUMBER": AR_TYPE_REAL}


def policy_definition() -> dict[str, Any]:
    """The pre-seeded ``policyDefinition``: our vocabulary, zero rules.

    Rules arrive later, from the ``INGEST_CONTENT`` build over ``ar_rules.md``.
    Creating the policy schema-first is what keeps the vocabulary identical
    across datasets — see :data:`CORE_VARIABLES`.
    """
    return {
        "types": [OKF_DISPOSITION_TYPE],
        "rules": [],
        "variables": [
            {
                "name": var["name"],
                "type": _WIRE_TYPES.get(var["type"], var["type"]),
                # REQUIRED by the API, and the translation-quality lever.
                "description": var["description"],
            }
            for var in CORE_VARIABLES
        ],
    }


# -- naming ----------------------------------------------------------------------


def dataset_label(data_domain: str, dataset: str) -> str:
    """``"<domain>/<dataset>"`` — the human label used in AR descriptions."""
    return f"{data_domain}/{dataset}"


def _sanitize(label: str) -> str:
    """``[0-9a-zA-Z_-]`` only: the intersection of the policy and guardrail name
    patterns (neither admits ``/``, so a dataset label cannot be used raw)."""
    return re.sub(r"[^0-9a-zA-Z_-]+", "-", label).strip("-")


def policy_name(label: str) -> str:
    """AR policy name for a dataset label (pattern ``[0-9a-zA-Z-_ ]+``, <=256)."""
    return f"okf-{_sanitize(label)}"[:256]


def guardrail_name(label: str) -> str:
    """Guardrail name for a dataset label (pattern ``[0-9a-zA-Z-_]+``, <=50).

    Truncation can in principle collide for very long labels; the registry row
    carries ``ar_guardrail_id``, so identity never depends on this name after
    the first build.
    """
    return f"okf-ar-{_sanitize(label)}"[:50]


# -- off-mount S3 artifacts ------------------------------------------------------

#: Derived AR artifacts live under a top-level prefix SIBLING to ``okf/`` (the
#: ``benchmark/`` precedent) in the same bundle bucket: nothing an LLM role's
#: file tools can reach, and outside the reindex rule's ``okf/*.md`` filter, so
#: writing a ``.md`` here never enqueues a vector update.
_POLICY_PREFIX = "policy/"


def policy_prefix(data_domain: str, dataset: str) -> str:
    return f"{_POLICY_PREFIX}{data_domain}/{dataset}/"


def ar_rules_key(data_domain: str, dataset: str) -> str:
    """The numbered if-then rules document ingested by the build."""
    return f"{policy_prefix(data_domain, dataset)}{RULES_DOCUMENT_NAME}"


def grounding_key(data_domain: str, dataset: str) -> str:
    """The rule id -> (text, source page) map the sidebar quotes from."""
    return f"{policy_prefix(data_domain, dataset)}grounding.json"


def put_ar_rules(
    s3, *, bucket: str, data_domain: str, dataset: str, rules_text: str
) -> str:
    """Persist ``ar_rules.md`` and return its key. Written at preprocess time."""
    key = ar_rules_key(data_domain, dataset)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=rules_text.encode("utf-8"),
        ContentType="text/markdown",
    )
    return key


def read_ar_rules(s3, *, bucket: str, data_domain: str, dataset: str) -> str | None:
    """The stored rules document, or None when no build has run."""
    try:
        raw = s3.get_object(Bucket=bucket, Key=ar_rules_key(data_domain, dataset))[
            "Body"
        ].read()
    except Exception:  # noqa: BLE001 - absent artifact is a normal state
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def put_grounding(
    s3, *, bucket: str, data_domain: str, dataset: str, grounding: dict[str, Any]
) -> str:
    """Persist the grounding map and return its key.

    Off the registry row on purpose: a few KB of nested structure has no place
    on a flat-scalar item, and the check reads exactly one small object.
    """
    key = grounding_key(data_domain, dataset)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(grounding, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def read_grounding(s3, *, bucket: str, data_domain: str, dataset: str) -> dict[str, Any]:
    """The grounding map, or ``{}`` when absent/unreadable.

    Degrading to empty is deliberate: a finding without resolvable rule text is
    still worth showing, so a missing artifact must not fail a check.
    """
    try:
        raw = s3.get_object(Bucket=bucket, Key=grounding_key(data_domain, dataset))[
            "Body"
        ].read()
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 - the sidebar renders fine without rule text
        return {}
    return parsed if isinstance(parsed, dict) else {}


def vocabulary_key(data_domain: str, dataset: str) -> str:
    """The policy's variable vocabulary: ``policy/<d>/<ds>/vocabulary.json``."""
    return f"{policy_prefix(data_domain, dataset)}vocabulary.json"


def definition_vocabulary(definition: dict[str, Any]) -> list[dict[str, str]]:
    """``[{name, type, description}]`` from a policy definition's variables.

    The chat pre-pass feeds these to the transcript writer so it phrases facts
    in the POLICY'S OWN vocabulary — including the dataset-specific variables
    the build derived, which the core contract can't know. Live-observed: a
    transcript that merely paraphrases a dataset variable ("selected the last
    standings checkpoint…") yields TRANSLATION_AMBIGUOUS — two candidate
    readings differing on whether the variable binds; naming it collapses the
    ambiguity.
    """
    out: list[dict[str, str]] = []
    for var in (definition or {}).get("variables") or []:
        name = str(var.get("name") or "")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "type": str(var.get("type") or ""),
                "description": str(var.get("description") or ""),
            }
        )
    return out


def put_vocabulary(
    s3, *, bucket: str, data_domain: str, dataset: str,
    vocabulary: list[dict[str, str]],
) -> str:
    """Persist the vocabulary artifact and return its key."""
    key = vocabulary_key(data_domain, dataset)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(vocabulary, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def read_vocabulary(
    s3, *, bucket: str, data_domain: str, dataset: str
) -> list[dict[str, str]]:
    """The vocabulary artifact, or ``[]`` when absent/unreadable (degrade)."""
    try:
        raw = s3.get_object(Bucket=bucket, Key=vocabulary_key(data_domain, dataset))[
            "Body"
        ].read()
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 - the pre-pass runs fine without it
        return []
    return parsed if isinstance(parsed, list) else []


# -- author state + content-addressed snapshots ------------------------------------
#
# Two stores under policy/<d>/<ds>/ make the lifecycle deterministic:
#
# * **Author state** (`sources_manifest.json` + `sources/<rel>` copies): what the
#   rules author last SAW. The next authoring run diffs the live wiki against
#   these to hand the agent per-file unified diffs — a surgical edit of
#   ar_rules.md instead of a from-scratch rewrite.
# * **Snapshots** (`snapshots/<fingerprint>.json`): the BUILT policy, verbatim —
#   the exported solver definition, grounding, rules text, and fidelity — keyed
#   by the source fingerprint it was built from. Content-addressed on purpose:
#   a repromote (or any A→B→A edit cycle) that lands the wiki on a previously
#   built state restores its exact policy in seconds with ZERO model calls,
#   instead of re-rolling two LLM translations. Fidelity is NOT re-measured on
#   restore (operator decision): same inputs, same policy — re-measuring is noise.


def sources_manifest_key(data_domain: str, dataset: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}sources_manifest.json"


def source_copy_key(data_domain: str, dataset: str, rel_path: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}sources/{rel_path}"


def snapshot_key(data_domain: str, dataset: str, fingerprint: str) -> str:
    return f"{policy_prefix(data_domain, dataset)}snapshots/{fingerprint}.json"


def build_sources_manifest(sources: list[tuple[str, bytes]]) -> dict[str, Any]:
    """``{fingerprint, files: {rel: sha256hex}}`` for a gathered source set."""
    import hashlib

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
    rules_text: str,
) -> None:
    """Persist the rules doc + the exact sources it was authored from.

    Written at authoring time (not completion): the copies are the DIFF BASE
    for the next authoring run, so they must correspond to whatever
    ``ar_rules.md`` currently says — even when the Bedrock build that follows
    fails. Copies of since-removed files may linger under ``sources/``; the
    manifest's key set is the truth, readers must ignore strays.
    """
    put_ar_rules(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        rules_text=rules_text,
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


def make_snapshot(
    stamp: dict[str, Any], *, fingerprint: str, rules_text: str
) -> dict[str, Any]:
    """Assemble the snapshot payload from :func:`complete_build`'s return.

    ``policy_version_arn`` records the immutable AR policy VERSION frozen at
    build time — the version-first restore's target (one guardrail repoint, no
    draft mutation). ``policy_definition`` stays as the fallback for when that
    version no longer exists (unenroll deleted the policy and its versions).
    """
    return {
        "fingerprint": fingerprint,
        "created_at": _now(),
        "policy_arn": str(stamp.get("policy_arn") or ""),
        "policy_version": str(stamp.get("policy_version") or ""),
        "policy_version_arn": str(stamp.get("policy_version_arn") or ""),
        "policy_definition": stamp.get("policy_definition") or {},
        "grounding": stamp.get("grounding") or {},
        "rules_text": rules_text,
        "fidelity_coverage": float(stamp.get("fidelity_coverage") or 0.0),
        "fidelity_accuracy": float(stamp.get("fidelity_accuracy") or 0.0),
        "build_status": str(stamp.get("build_status") or BUILD_READY),
    }


def write_snapshot(
    s3, *, bucket: str, data_domain: str, dataset: str, snapshot: dict[str, Any]
) -> str:
    """Persist one content-addressed snapshot; returns its key."""
    fingerprint = str(snapshot.get("fingerprint") or "")
    if not fingerprint:
        raise ValueError("a snapshot must carry the fingerprint it was built from")
    key = snapshot_key(data_domain, dataset, fingerprint)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(snapshot, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def read_snapshot(
    s3, *, bucket: str, data_domain: str, dataset: str, fingerprint: str
) -> dict[str, Any] | None:
    """The snapshot built from exactly this source fingerprint, or None.

    None is the caller's cue to run the full authoring + ingest pipeline; a
    hit means a deterministic restore (:func:`restore_snapshot`) suffices. A
    snapshot without a usable ``policy_definition`` counts as a miss — restoring
    an empty definition would blank the live policy.
    """
    if not fingerprint:
        return None
    try:
        raw = s3.get_object(
            Bucket=bucket, Key=snapshot_key(data_domain, dataset, fingerprint)
        )["Body"].read()
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 - a miss is the normal never-built state
        return None
    if not isinstance(parsed, dict) or not parsed.get("policy_definition"):
        return None
    return parsed


def restore_snapshot(
    bedrock,
    ddb,
    s3,
    *,
    table: str,
    bucket: str,
    data_domain: str,
    dataset: str,
    snapshot: dict[str, Any],
    guardrail_profile: str = DEFAULT_GUARDRAIL_PROFILE,
    guardrail_id: str | None = None,
) -> str:
    """Point the live check back at a snapshot's EXACT solver rules. Returns the
    status stamped.

    Deterministic and model-free, VERSION-FIRST: the snapshot records the
    immutable AR policy version frozen when this content state was built, so
    when that version still exists the whole restore is one guardrail repoint
    — no draft mutation (nothing to race an in-flight build's apply step) and
    no new version burned from the 1,000-per-policy quota. The definition-push
    fallback covers a dead version (unenroll deleted the policy and all its
    versions; re-enroll created a fresh policy): push the snapshot's full
    definition into the draft and freeze a NEW version. Either way the row is
    stamped with the snapshot's ERA fingerprint — which matches the
    just-restored wiki by construction, so the usability gate opens immediately
    (no dark window). Fidelity restores verbatim; it is never re-measured for
    content that was already built. The caller owns the ``building`` flip.
    """
    label = dataset_label(data_domain, dataset)
    policy_arn, _hash = ensure_policy(
        bedrock,
        name=policy_name(label),
        description=(
            "Automated Reasoning policy derived from the OKF Data Wiki "
            f"reference docs for {label}."
        ),
    )
    version_arn = ""
    policy_version = ""
    recorded_arn = str(snapshot.get("policy_version_arn") or "")
    # Same policy generation only: a version ARN from a pre-unenroll policy id
    # can never be repointed to (the prefix check is what detects that).
    if recorded_arn.startswith(f"{policy_arn}:"):
        try:
            bedrock.export_automated_reasoning_policy_version(policyArn=recorded_arn)
            version_arn = recorded_arn
            policy_version = str(snapshot.get("policy_version") or "")
        except Exception:  # noqa: BLE001 - a dead version just means fallback
            log.info(
                "snapshot version %s is gone for %s; falling back to a "
                "definition push",
                recorded_arn,
                label,
            )
    if not version_arn:
        bedrock.update_automated_reasoning_policy(
            policyArn=policy_arn,
            policyDefinition=snapshot["policy_definition"],
        )
        definition_hash = str(
            bedrock.get_automated_reasoning_policy(policyArn=policy_arn).get(
                "definitionHash"
            )
            or ""
        )
        version = bedrock.create_automated_reasoning_policy_version(
            policyArn=policy_arn, lastUpdatedDefinitionHash=definition_hash
        )
        version_arn = str(version.get("policyArn") or policy_arn)
        policy_version = str(version.get("version") or "")
    guardrail = ensure_guardrail(
        bedrock,
        name=guardrail_name(label),
        policy_version_arn=version_arn,
        guardrail_profile=guardrail_profile,
        description=f"OKF Data Wiki policy check for {label}",
        guardrail_id=guardrail_id,
    )
    put_grounding(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        grounding=snapshot.get("grounding") or {},
    )
    put_ar_rules(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        rules_text=str(snapshot.get("rules_text") or ""),
    )
    put_vocabulary(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        vocabulary=definition_vocabulary(snapshot.get("policy_definition") or {}),
    )
    return stamp_build_complete(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        stamp={
            "policy_arn": policy_arn,
            "policy_version": policy_version,
            "guardrail_id": guardrail["guardrail_id"],
            "guardrail_version": guardrail["guardrail_version"],
            "fidelity_coverage": snapshot.get("fidelity_coverage") or 0.0,
            "fidelity_accuracy": snapshot.get("fidelity_accuracy") or 0.0,
            "build_status": str(snapshot.get("build_status") or BUILD_READY),
        },
        pending_hash=str(snapshot.get("fingerprint") or ""),
    )


# -- source gathering ------------------------------------------------------------


def gather_sources(
    s3, bucket: str, data_domain: str, dataset: str
) -> list[tuple[str, bytes]]:
    """The dataset's AR source files as sorted ``(dataset-relative key, bytes)``.

    One paginated ``list_objects_v2`` over the whole dataset prefix (tens of
    objects) plus a GET per selected file — cheaper than six prefix listings.
    Read from S3, not from the harvest mount, so the fingerprint describes what
    an S3-reading rebuilder (incremental) or verifier (chat) will see, and so
    the same function works in a Lambda with no mount.
    """
    prefix = bundle_prefix(data_domain, dataset)
    pairs: list[tuple[str, bytes]] = []
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
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            pairs.append((rel, body if isinstance(body, bytes) else str(body).encode()))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
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
    """Gather + fingerprint in one call. None means "nothing to build".

    Callers that also need the bytes (the preprocess pass) should
    :func:`gather_sources` once and :func:`hash_sources` the result instead of
    walking S3 twice.
    """
    return hash_sources(gather_sources(s3, bucket, data_domain, dataset))


# -- the preprocessing prompt (design §7.2) --------------------------------------

#: The instruction half of the rules-extraction pass, verbatim. Two notes are
#: load-bearing and must survive any edit: result-dependent rules have to be
#: conditioned on ``queryExecuted`` (a turn reporting an earlier turn's numbers
#: executes no query and is not a violation), and the zero-row rule is the
#: strongest pure-fabrication catch this feature has.
AR_RULES_PROMPT = """\
You convert dataset documentation into a numbered list of formal, checkable
rules for an automated reasoner. The reasoner validates an AI data analyst's
process and answer against dataset policy.

Write rules ONLY in conditional process-state -> answer-obligation form:
  "IF <process/question state> THEN <obligation on the answer or disposition>."
Never flat prohibitions ("never do X") — always the conditional consequence
("IF X was done THEN the answer must … / THEN disposition must be BLOCK").

Phrase every condition and obligation using EXACTLY this vocabulary where it
applies (these are the reasoner's variables):
  disposition (COMMIT/ASK/BLOCK/REFUSE), clarificationObtained,
  termDisambiguated, periodSpecified, periodWithinHorizon, queryExecuted,
  dedupApplied, sentinelExcluded, snapshotSummedOverTime,
  disjointMeasuresCombined, deprecatedObjectUsed, recipeApplied,
  zeroRowsReturned, resultTruncated, rowCount, assumptionStated,
  caveatIncluded.
Dataset-specific values (measure names, sentinel values, enum codes) may be
introduced as new enum-style terms; define each once, in one sentence, before
first use — and ONLY when no core term above expresses the condition. Every
new term becomes a solver variable the downstream transcript writer must
learn, and variable count directly degrades translation quality and check
latency: the leanest vocabulary that captures the policy wins.

BE SELECTIVE — a rule earns its place only if violating it would make a real
answer WRONG (not merely under-documented). Hunt exactly these classes:
  * mandatory recipes/transforms the docs declare (as-of handling, required
    join paths, unit conversions) — recipeApplied;
  * deduplication the docs require because duplicates genuinely occur —
    dedupApplied;
  * documented sentinel/placeholder values that corrupt aggregates —
    sentinelExcluded;
  * measures the docs declare disjoint / never-combinable —
    disjointMeasuresCombined;
  * the documented data horizon — periodWithinHorizon;
  * deprecated/superseded objects — deprecatedObjectUsed;
  * snapshot measures that must not be summed over time —
    snapshotSummedOverTime;
  * genuinely ambiguous business terms (the docs list multiple readings) —
    one ASK rule per AMBIGUOUS TERM, not per possible reading.
Do NOT write rules that: restate schema facts with no answer obligation;
enumerate each value of an enum (one rule about the enum's pitfall suffices);
encode conditions no process transcript could ever evidence; or split one
failure mode across several near-identical rules — merge them.

Hard constraints:
1. Any rule whose condition depends on a query result (zeroRowsReturned,
   resultTruncated, rowCount) MUST also be conditioned on queryExecuted being
   true. A turn that reports figures from an earlier turn's query executes no
   query — that alone is never a violation.
2. Always include this rule when the source mentions any figures/aggregates:
   "IF queryExecuted is true AND zeroRowsReturned is true THEN the answer must
   not state figures, totals, or values derived from this query."
3. One rule per line, numbered. Each rule must trace to a statement in the
   source documents; append the source path in parentheses. Do not invent
   policy that is not in the sources. Skip narrative/anecdotal content that
   cannot be expressed as a condition over the vocabulary.
4. Size the document to the dataset's GENUINE semantic richness — a thin
   schema warrants a dozen rules; only a truly rich policy surface justifies
   several dozen. Never pad toward coverage: if the sources yield more
   candidates than distinct failure modes, keep the ones whose violation most
   damages an answer and fold the rest into their nearest kin.

Output ONLY the numbered rules document, no preamble."""


def build_rules_prompt(sources: Iterable[tuple[str, bytes | str]]) -> str:
    """The full preprocessing prompt: the instructions plus the fenced sources.

    Each source is fenced under its DATASET-RELATIVE path because constraint 3
    asks the model to append that path to every rule — it is the only thing
    that later lets a finding point back at a wiki page
    (:func:`parse_rule_source_page`).
    """
    blocks: list[str] = []
    for rel_path, content in sources:
        text = (
            content.decode("utf-8", errors="replace")
            if isinstance(content, bytes)
            else str(content)
        )
        blocks.append(f"### Source: {rel_path}\n```markdown\n{text.rstrip()}\n```")
    joined = "\n\n".join(blocks)
    return f"{AR_RULES_PROMPT}\n\nSource documents:\n\n{joined}\n"


def parse_rules_response(text: str) -> str:
    """The ``ar_rules.md`` body out of a model response.

    Tolerant of the two things a small model does anyway despite "output ONLY
    the numbered rules": wrapping the document in a code fence, and prefixing a
    sentence of preamble. Everything from the first numbered line onward is
    kept verbatim — rules may wrap across lines.
    """
    body = (text or "").strip()
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*\d+[.)]\s", line):
            return "\n".join(lines[i:]).strip()
    return body


# -- policy + build lifecycle ----------------------------------------------------


def _error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def _find_policy_by_name(bedrock, name: str) -> str | None:
    """The DRAFT policy arn for ``name``, or None. Paginates ListPolicies.

    ``ListAutomatedReasoningPolicies`` returns one summary per VERSION, and only
    the draft arn accepts a build workflow — so a name match is not enough.
    """
    token: str | None = None
    fallback: str | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["nextToken"] = token
        resp = bedrock.list_automated_reasoning_policies(**kwargs)
        for summary in resp.get("automatedReasoningPolicySummaries", []):
            if summary.get("name") != name:
                continue
            if summary.get("version") == POLICY_VERSION_DRAFT:
                return summary.get("policyArn")
            fallback = fallback or summary.get("policyArn")
        token = resp.get("nextToken")
        if not token:
            return fallback


def ensure_policy(bedrock, *, name: str, description: str) -> tuple[str, str]:
    """Resolve or create the dataset's policy. Returns ``(arn, definitionHash)``.

    Created schema-first with :func:`policy_definition` and zero rules, so every
    dataset's policy speaks the same vocabulary. Idempotent: an existing policy
    of this name is resolved instead (a name lookup, then a Get for the hash —
    summaries carry no ``definitionHash``).

    Raises :class:`PolicyCapError` when the account's 100-policy limit is hit;
    the caller stamps ``failed`` and logs loudly rather than crashing a harvest.
    """
    existing = _find_policy_by_name(bedrock, name)
    if existing:
        got = bedrock.get_automated_reasoning_policy(policyArn=existing)
        return existing, str(got.get("definitionHash") or "")
    try:
        created = bedrock.create_automated_reasoning_policy(
            name=name,
            description=description,
            policyDefinition=policy_definition(),
        )
    except Exception as e:  # noqa: BLE001 - quota and races are expected outcomes
        code = _error_code(e)
        if code == "ServiceQuotaExceededException":
            raise PolicyCapError(
                f"AR policy quota reached; cannot create policy {name!r}"
            ) from e
        if code in ("ConflictException", "ResourceInUseException"):
            # Lost a create race — the winner's policy is the one to use.
            raced = _find_policy_by_name(bedrock, name)
            if raced:
                got = bedrock.get_automated_reasoning_policy(policyArn=raced)
                return raced, str(got.get("definitionHash") or "")
        raise
    return str(created["policyArn"]), str(created.get("definitionHash") or "")


def truncate_rules(rules_text: str, *, max_chars: int = MAX_INGEST_CHARS) -> str:
    """Bound the ingest document, cutting at a rule (line) boundary.

    A mid-rule cut would ingest a half-sentence as policy, so the last complete
    line wins. Loud on truncation: it means the preprocessing pass ignored the
    120-rule constraint and the policy is now missing documented rules.
    """
    if len(rules_text) <= max_chars:
        return rules_text
    cut = rules_text.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    log.warning(
        "ar_rules.md is %d chars (> %d) — truncating to %d at a rule boundary; "
        "the ingested policy will be missing rules",
        len(rules_text),
        max_chars,
        cut,
    )
    return rules_text[:cut] + "\n"


def start_build(bedrock, *, policy_arn: str, rules_text: str) -> str:
    """Start the ``INGEST_CONTENT`` build. Returns the build workflow id.

    Exactly ONE document (``ar_rules.md``), sent as ``txt`` — the content-type
    enum is ``pdf``/``txt`` only, there is no markdown member. Fire-and-forget:
    builds are minutes-scale, so nothing waits here.
    """
    resp = bedrock.start_automated_reasoning_policy_build_workflow(
        policyArn=policy_arn,
        buildWorkflowType=BUILD_WORKFLOW_INGEST,
        sourceContent={
            "workflowContent": {
                "documents": [
                    {
                        "document": truncate_rules(rules_text).encode("utf-8"),
                        "documentContentType": "txt",
                        "documentName": RULES_DOCUMENT_NAME,
                        "documentDescription": (
                            "Numbered if-then policy rules derived from the "
                            "dataset's wiki reference docs."
                        ),
                    }
                ]
            }
        },
    )
    return str(resp["buildWorkflowId"])


def get_build_status(bedrock, *, policy_arn: str, workflow_id: str) -> str:
    """One poll of the build workflow. ``""`` when the status is missing.

    Terminal statuses are :data:`TERMINAL_WORKFLOW_STATUSES`; everything else
    means "still running", and the caller re-polls on its own schedule (the
    nightly reconcile) rather than blocking.
    """
    resp = bedrock.get_automated_reasoning_policy_build_workflow(
        policyArn=policy_arn, buildWorkflowId=workflow_id
    )
    return str(resp.get("status") or "")


def parse_rule_source_page(rule_text: str) -> str | None:
    """The ``references/….md`` wiki page a rule traces to, or None.

    Preprocessing constraint 3 appends the source path in parentheses to every
    rule; the LAST such reference wins (a merged rule cites several, and the
    trailing one is the one the numbering convention appends).
    """
    matches = re.findall(r"\((references/[^()\s]+\.md)\)", rule_text or "")
    return matches[-1] if matches else None


def build_log_pages(build_log: dict[str, Any]) -> dict[str, str]:
    """``{rule_id: wiki page}`` recovered from the BUILD_LOG asset.

    The built rules carry no source references (``alternateExpression`` is a
    plain restatement), so per-rule grounding is recovered from the log: each
    entry pairs an annotation — for ingested content, a chunk of
    ``ar_rules.md`` whose numbered lines cite their wiki page in parentheses —
    with the mutations it produced. A rule is attributed to its chunk's page
    only when the chunk cites exactly ONE page: a multi-page chunk would be a
    guess, and a wrong attribution is worse than none. Later refinement
    entries (the workflow's own add/delete/update-rule annotations) carry no
    page and contribute nothing.
    """
    out: dict[str, str] = {}
    for entry in (build_log or {}).get("entries") or []:
        annotation = entry.get("annotation") or {}
        content = str((annotation.get("ingestContent") or {}).get("content") or "")
        pages = sorted(set(re.findall(r"\((references/[^()\s]+\.md)\)", content)))
        if len(pages) != 1:
            continue
        for step in entry.get("buildSteps") or []:
            mutation = (step.get("context") or {}).get("mutation") or {}
            rule_id = str(((mutation.get("addRule") or {}).get("rule") or {}).get("id") or "")
            if rule_id:
                out[rule_id] = pages[0]
    return out


def build_grounding(
    rules: Iterable[dict[str, Any]],
    rule_reports: dict[str, Any],
    log_pages: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """``{rule_id: {rule_text, rule_source_page}}`` for the sidebar.

    A finding carries only a rule IDENTIFIER, so rule text has to be captured
    at build time. Text prefers the exported rule's ``alternateExpression``
    (the natural-language restatement — this string faces humans in the
    sidebar and the finding renderer) over the SMT ``expression``. The source
    page comes from the first of: the rule text itself (authored rules embed
    ``(references/…)``), the fidelity report's ``ruleReports`` source
    sentence, or the BUILD_LOG chunk attribution (``log_pages``).
    """
    out: dict[str, dict[str, Any]] = {}
    pages = log_pages or {}
    for rule in rules or []:
        rule_id = str(rule.get("id") or "")
        if not rule_id:
            continue
        text = str(rule.get("alternateExpression") or rule.get("expression") or "")
        report_text = str(((rule_reports or {}).get(rule_id) or {}).get("rule") or "")
        out[rule_id] = {
            "rule_text": text,
            "rule_source_page": (
                parse_rule_source_page(text)
                or parse_rule_source_page(report_text)
                or pages.get(rule_id)
            ),
        }
    for rule_id, report in (rule_reports or {}).items():
        if out.get(rule_id, {}).get("rule_text"):
            continue
        text = str((report or {}).get("rule") or "")
        out[str(rule_id)] = {
            "rule_text": text,
            "rule_source_page": parse_rule_source_page(text) or pages.get(str(rule_id)),
        }
    return out


def build_status_for_fidelity(coverage: float, accuracy: float) -> str:
    """``ready`` above both fidelity thresholds, else ``degraded``.

    Never ``failed``: a low-fidelity policy still finds real violations, so the
    check runs and the sidebar notes reduced coverage instead.
    """
    if coverage < FIDELITY_MIN_COVERAGE or accuracy < FIDELITY_MIN_ACCURACY:
        return BUILD_DEGRADED
    return BUILD_READY


def ensure_guardrail(
    bedrock,
    *,
    name: str,
    policy_version_arn: str,
    guardrail_profile: str = DEFAULT_GUARDRAIL_PROFILE,
    description: str = "",
    guardrail_id: str | None = None,
) -> dict[str, str]:
    """Create or update the dataset's guardrail, then cut a version.

    One guardrail per dataset (a guardrail holds at most 2 AR policies).
    ``crossRegionConfig`` is mandatory for an AR-carrying guardrail — omitting
    it is a ``ValidationException`` — and Update re-requires ``name`` plus both
    blocked-messaging fields, so the whole config is rebuilt on every call.
    The DRAFT version is never used at runtime; the numbered version is.
    """
    config: dict[str, Any] = {
        "name": name,
        "blockedInputMessaging": BLOCKED_MESSAGING,
        "blockedOutputsMessaging": BLOCKED_MESSAGING,
        "automatedReasoningPolicyConfig": {
            "policies": [policy_version_arn],
            "confidenceThreshold": AR_CONFIDENCE_THRESHOLD,
        },
        "crossRegionConfig": {"guardrailProfileIdentifier": guardrail_profile},
    }
    if description:
        config["description"] = description
    resp: dict[str, Any] | None = None
    if guardrail_id:
        try:
            resp = bedrock.update_guardrail(guardrailIdentifier=guardrail_id, **config)
        except Exception as e:  # noqa: BLE001 - a deleted guardrail is recoverable
            if _error_code(e) != "ResourceNotFoundException":
                raise
            # The row's guardrail was deleted out of band; recreating it is
            # cheaper than wedging the dataset at failed until someone notices.
            log.warning(
                "Guardrail %s is gone — recreating it for %s", guardrail_id, name
            )
            guardrail_id = None
    if resp is None:
        resp = bedrock.create_guardrail(**config)
        guardrail_id = str(resp["guardrailId"])
    versioned = bedrock.create_guardrail_version(guardrailIdentifier=guardrail_id)
    return {
        "guardrail_id": str(guardrail_id),
        "guardrail_arn": str(resp.get("guardrailArn") or ""),
        "guardrail_version": str(versioned.get("version") or ""),
    }


def complete_build(
    bedrock,
    *,
    policy_arn: str,
    workflow_id: str,
    guardrail_profile: str = DEFAULT_GUARDRAIL_PROFILE,
    dataset_label: str,
    guardrail_id: str | None = None,
) -> dict[str, Any]:
    """Turn a COMPLETED build into a usable policy. Returns the row stamp.

    LIVE-VERIFIED (eu-west-1, 2026-08-01): an ``INGEST_CONTENT`` workflow does
    NOT mutate the DRAFT — its result is staged as the ``POLICY_DEFINITION``
    build asset. Versioning the untouched draft would freeze an EMPTY policy,
    so the ordered steps are: read the built definition from the asset, APPLY
    it to the draft (``UpdateAutomatedReasoningPolicy`` takes a full
    definition), read the fresh ``definitionHash`` (the API's optimistic
    concurrency token), freeze an immutable policy VERSION, export that
    version for the grounding map, and point the dataset's guardrail at it.
    The FIDELITY_REPORT asset is OPTIONAL — ingest builds don't generate one
    (``ResourceNotFoundException``); unmeasured fidelity stays ``ready`` with
    0.0 scores rather than degrading. Grounding pages fall back to the
    BUILD_LOG chunk attribution (:func:`build_log_pages`).

    ``-> {policy_arn, policy_version, policy_version_arn, guardrail_id,
    guardrail_arn, guardrail_version, fidelity_coverage, fidelity_accuracy,
    build_status, grounding, policy_definition}``. The caller persists
    ``grounding`` to S3 and the scalars to the registry row
    (:func:`stamp_build_complete`).
    """
    assets = bedrock.get_automated_reasoning_policy_build_workflow_result_assets(
        policyArn=policy_arn,
        buildWorkflowId=workflow_id,
        assetType=ASSET_POLICY_DEFINITION,
    )
    built = (assets.get("buildWorkflowAssets") or {}).get("policyDefinition") or {}
    if not built.get("rules"):
        # A rule-free policy renders no verdicts; versioning it would swap a
        # working policy for a useless one. Callers stamp `failed` with this.
        raise ValueError(
            f"build workflow {workflow_id} produced no rules "
            "(POLICY_DEFINITION asset is empty)"
        )
    bedrock.update_automated_reasoning_policy(
        policyArn=policy_arn, policyDefinition=built
    )

    report: dict[str, Any] = {}
    try:
        fidelity = bedrock.get_automated_reasoning_policy_build_workflow_result_assets(
            policyArn=policy_arn,
            buildWorkflowId=workflow_id,
            assetType=ASSET_FIDELITY_REPORT,
        )
        report = (fidelity.get("buildWorkflowAssets") or {}).get("fidelityReport") or {}
    except Exception:  # noqa: BLE001 - the asset legitimately may not exist
        log.info("no fidelity report for build workflow %s", workflow_id)
    coverage = float(report.get("coverageScore") or 0.0)
    accuracy = float(report.get("accuracyScore") or 0.0)

    log_pages: dict[str, str] = {}
    try:
        log_assets = bedrock.get_automated_reasoning_policy_build_workflow_result_assets(
            policyArn=policy_arn,
            buildWorkflowId=workflow_id,
            assetType=ASSET_BUILD_LOG,
        )
        log_pages = build_log_pages(
            (log_assets.get("buildWorkflowAssets") or {}).get("buildLog") or {}
        )
    except Exception:  # noqa: BLE001 - grounding pages are best-effort
        log.info("no build log for build workflow %s", workflow_id)

    definition_hash = str(
        bedrock.get_automated_reasoning_policy(policyArn=policy_arn).get(
            "definitionHash"
        )
        or ""
    )
    version = bedrock.create_automated_reasoning_policy_version(
        policyArn=policy_arn, lastUpdatedDefinitionHash=definition_hash
    )
    policy_version = str(version.get("version") or "")
    version_arn = str(version.get("policyArn") or policy_arn)

    exported = bedrock.export_automated_reasoning_policy_version(policyArn=version_arn)
    rules = ((exported.get("policyDefinition") or {}).get("rules")) or []
    grounding = build_grounding(
        rules, report.get("ruleReports") or {}, log_pages=log_pages
    )

    guardrail = ensure_guardrail(
        bedrock,
        name=guardrail_name(dataset_label),
        policy_version_arn=version_arn,
        guardrail_profile=guardrail_profile,
        description=f"OKF Data Wiki policy check for {dataset_label}",
        guardrail_id=guardrail_id,
    )
    return {
        "policy_arn": policy_arn,
        "policy_version": policy_version,
        "policy_version_arn": version_arn,
        "guardrail_id": guardrail["guardrail_id"],
        "guardrail_arn": guardrail["guardrail_arn"],
        "guardrail_version": guardrail["guardrail_version"],
        "fidelity_coverage": coverage,
        "fidelity_accuracy": accuracy,
        # UNMEASURED fidelity (no report generated) is not LOW fidelity: only
        # a real report may degrade the status.
        "build_status": (
            build_status_for_fidelity(coverage, accuracy) if report else BUILD_READY
        ),
        "grounding": grounding,
        # The exported definition verbatim — the snapshot's definition-push
        # FALLBACK for restores whose recorded policy version no longer exists
        # (the version-first path repoints the guardrail instead).
        "policy_definition": (exported.get("policyDefinition") or {}),
    }


def finish_completed_build(
    bedrock,
    ddb,
    s3,
    *,
    table: str,
    bucket: str,
    data_domain: str,
    dataset: str,
    policy_arn: str,
    workflow_id: str,
    pending_hash: str,
    guardrail_id: str | None = None,
    guardrail_profile: str = DEFAULT_GUARDRAIL_PROFILE,
) -> str:
    """The ONE completion path: :func:`complete_build` + live artifacts +
    snapshot + row stamp. Returns the status written.

    Shared by every completion authority — the harvest runtime's post-start
    poll, the rebuild authority's sync/event path, and the nightly reconcile —
    so they cannot drift. ``pending_hash`` is the fingerprint carried from
    gather time, stamped VERBATIM (a wiki that moved mid-build must yield a
    policy that is stale on arrival, never one mislabelled as current). The
    snapshot write is best-effort: losing it costs one future re-author,
    never truth.
    """
    label = dataset_label(data_domain, dataset)
    stamp = complete_build(
        bedrock,
        policy_arn=policy_arn,
        workflow_id=workflow_id,
        guardrail_profile=guardrail_profile,
        dataset_label=label,
        guardrail_id=guardrail_id,
    )
    put_grounding(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        grounding=stamp["grounding"],
    )
    try:
        # The policy's own variable names, for the chat pre-pass's transcript
        # vocabulary. Advisory: without it the pre-pass still runs on the core
        # terms alone (more TRANSLATION_AMBIGUOUS, never a wrong verdict).
        put_vocabulary(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
            vocabulary=definition_vocabulary(stamp.get("policy_definition") or {}),
        )
    except Exception:  # noqa: BLE001 - translation-quality accelerator only
        log.warning(
            "AR vocabulary write failed for %s/%s", data_domain, dataset,
            exc_info=True,
        )
    try:
        write_snapshot(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
            snapshot=make_snapshot(
                stamp,
                fingerprint=pending_hash,
                rules_text=read_ar_rules(
                    s3, bucket=bucket, data_domain=data_domain, dataset=dataset
                )
                or "",
            ),
        )
    except Exception:  # noqa: BLE001 - the snapshot is an accelerator
        log.warning(
            "AR snapshot write failed for %s/%s", data_domain, dataset,
            exc_info=True,
        )
    return stamp_build_complete(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        stamp=stamp,
        pending_hash=pending_hash,
    )


# -- runtime response parsing ----------------------------------------------------

#: Union member -> the finding type the report/UI speaks (UPPER_SNAKE).
_FINDING_TYPES: tuple[tuple[str, str], ...] = (
    ("valid", "VALID"),
    ("invalid", "INVALID"),
    ("satisfiable", "SATISFIABLE"),
    ("impossible", "IMPOSSIBLE"),
    ("translationAmbiguous", "TRANSLATION_AMBIGUOUS"),
    ("tooComplex", "TOO_COMPLEX"),
    ("noTranslations", "NO_TRANSLATIONS"),
)


def _statements(scenario: Any) -> list[str]:
    return [
        str(st.get("naturalLanguage") or "")
        for st in ((scenario or {}).get("statements") or [])
        if st.get("naturalLanguage")
    ]


def _logic_variable(stmt: dict[str, Any]) -> str:
    """The variable a translated statement binds, or "" for non-variable logic.

    Statement ``logic`` is SMT-ish: a bare name, ``(not name)``, or compound
    expressions (and the literal ``true``/``false``, which appear as trivial
    claims and are pure noise for a human summary).
    """
    logic = str((stmt or {}).get("logic") or "").strip()
    m = re.fullmatch(r"\(?\s*(?:not\s+)?([A-Za-z][A-Za-z0-9_]*)\s*\)?", logic)
    name = m.group(1) if m else ""
    return "" if name in ("true", "false") else name


def _ambiguity_claim(body: dict[str, Any]) -> str:
    """A human-readable claim for a ``translationAmbiguous`` finding.

    The finding's top-level translation is EMPTY — the substance is in
    ``options``: N candidate readings. Two live-observed ambiguity classes:
    readings that bind DIFFERENT variable sets (a paraphrased term the prose
    should have named), and readings that bind the SAME variables but split
    them differently across premises (given) and claims (asserted). Naming
    which turns a dead-end "couldn't be expressed" into actionable feedback.
    """
    per_option: list[set[str]] = []
    per_option_claims: list[set[str]] = []
    for option in body.get("options") or []:
        names: set[str] = set()
        claim_names: set[str] = set()
        for translation in option.get("translations") or []:
            for stmt in translation.get("premises") or []:
                name = _logic_variable(stmt)
                if name:
                    names.add(name)
            for stmt in translation.get("claims") or []:
                name = _logic_variable(stmt)
                if name:
                    names.add(name)
                    claim_names.add(name)
        per_option.append(names)
        per_option_claims.append(claim_names)
    if len(per_option) < 2:
        return ""
    union = set().union(*per_option)
    common = set.intersection(*per_option)
    differing = sorted(union - common)
    if differing:
        return "the readings differ on whether these bind: " + ", ".join(differing)
    if len({frozenset(c) for c in per_option_claims}) > 1:
        return (
            "the readings agree on the facts but differ on which are "
            "given (premises) versus asserted (claims)"
        )
    return ""


def _translation(body: dict[str, Any]) -> dict[str, Any]:
    """The finding's translation. ``translationAmbiguous`` has none of its own —
    it carries competing ``options[].translations[]``; the first is the one whose
    claims are worth showing next to the ambiguity."""
    direct = body.get("translation")
    if isinstance(direct, dict):
        return direct
    for option in body.get("options") or []:
        for candidate in (option or {}).get("translations") or []:
            if isinstance(candidate, dict):
                return candidate
    return {}


def ar_ran(response: dict[str, Any]) -> bool:
    """True when the guardrail actually evaluated an AR policy.

    A guardrail with no reachable policy returns a perfectly shaped response
    with zero findings, which is indistinguishable from "checked and consistent"
    unless the billing counter is consulted.
    """
    usage = (response or {}).get("usage") or {}
    try:
        return int(usage.get("automatedReasoningPolicyUnits") or 0) > 0
    except (TypeError, ValueError):
        return False


def parse_ar_findings(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``apply_guardrail``'s AR assessments into report findings.

    ``-> [{type, claim, rule_ids, scenario, confidence}]``, one entry per
    finding across every assessment. ``claim`` is the translated claim text,
    ``scenario`` the counter-example statements (false scenario preferred — it is
    the one that explains a violation), ``rule_ids`` the contradicting or
    supporting rule identifiers (resolved to text via the grounding map, which
    the runtime response does not carry).

    Membership is tested with ``in``: ``tooComplex`` and ``noTranslations`` are
    EMPTY structs, so any truthiness test silently drops them — and those are
    exactly the "not checkable" outcomes the sidebar must report.
    """
    out: list[dict[str, Any]] = []
    for assessment in (response or {}).get("assessments") or []:
        policy = (assessment or {}).get("automatedReasoningPolicy") or {}
        for finding in policy.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            for member, type_name in _FINDING_TYPES:
                if member not in finding:
                    continue
                body = finding.get(member) or {}
                translation = _translation(body)
                claims = [
                    str(c.get("naturalLanguage") or "")
                    for c in translation.get("claims") or []
                    if c.get("naturalLanguage")
                ]
                if member == "translationAmbiguous":
                    # The first option's claims (the `_translation` fallback)
                    # are one arbitrary reading; the DIFFERENCE between the
                    # readings is the actionable part — show that instead.
                    ambiguity = _ambiguity_claim(body)
                    if ambiguity:
                        claims = [ambiguity]
                rules = (
                    body.get("contradictingRules") or body.get("supportingRules") or []
                )
                scenario = _statements(
                    body.get("claimsFalseScenario") or body.get("claimsTrueScenario")
                )
                confidence = translation.get("confidence")
                out.append(
                    {
                        "type": type_name,
                        "claim": " ".join(claims),
                        "rule_ids": [
                            str(r.get("identifier") or "")
                            for r in rules
                            if r.get("identifier")
                        ],
                        "scenario": scenario,
                        "confidence": (
                            float(confidence) if confidence is not None else None
                        ),
                    }
                )
                break
    return out


# -- registry stamps (the DATASET# mapping row) ----------------------------------
#
# Every stamp below speaks the LOW-LEVEL ``boto3.client("dynamodb")`` wire format
# (``{"S": …}``), matching how the registry table is written everywhere else in
# harvest. A service holding only a resource must construct a SEPARATE
# ``boto3.client("dynamodb")`` — a resource's ``.meta.client`` carries the
# resource's document transformations and silently mismatches typed expressions.


def region_supported(region: str) -> bool:
    """True when AR checks exist in ``region``. See :data:`AR_SUPPORTED_REGIONS`."""
    return region in AR_SUPPORTED_REGIONS


def policy_usable(
    *, build_status: str, stored_hash: str | None, live_hash: str | None
) -> bool:
    """THE usability gate: may this policy render a verdict?

    Both halves are required — a built status AND a fingerprint that still
    matches the live wiki. Kept here, on scalars only (no row flavor), so the
    check-time gate and the rebuild authority can never disagree about it. A
    policy built from anything but the current wiki state is unusable by
    definition: running it behind a "possibly stale" banner was considered and
    rejected, because only the latest wiki state is truth.
    """
    if build_status not in USABLE_BUILD_STATUSES:
        return False
    if not stored_hash or not live_hash:
        return False
    return stored_hash == live_hash


def registry_key(data_domain: str, dataset: str) -> dict[str, Any]:
    """The mapping row's key — AR attrs live on ``DOMAIN#/DATASET#``.

    NOT the ``HARVEST#…/STATUS`` row: a policy belongs to the dataset, not to
    one harvest run. Low-level (``{"S": …}``) values because the registry table
    is driven by ``boto3.client("dynamodb")`` everywhere it is written.
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
    on the row still existing: UpdateItem otherwise upserts, and a build
    finishing after the dataset was deleted would resurrect a phantom row.
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
    """Claim the dataset's build slot. True when this caller owns the build.

    THE serialization point for the whole feature: the flip to ``building`` is a
    conditional UpdateItem, so the finalize hook, the nightly reconcile and N
    duplicate ``policy_rebuild`` events collapse to exactly one build. Returns
    False (never raises) when another build already holds the row, and False
    when the mapping row is gone.

    ``pending_hash`` is the fingerprint of the sources this build is about to
    ingest, parked on :data:`ATTR_PENDING_SOURCE_HASH` for the completion stamp
    to carry over verbatim.
    """
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
                "(attribute_not_exists(#bs) OR #bs <> :building)"
            ),
            extra_names={"#bs": ATTR_BUILD_STATUS},
            extra_values={":building": {"S": BUILD_BUILDING}},
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition means "already building"
        if _is_condition_failure(e):
            return False
        raise


def stamp_build_started(
    ddb, table: str, *, data_domain: str, dataset: str, workflow_id: str
) -> None:
    """Attach the build workflow id, written AFTER the flip and the Start call.

    Separate from the flip on purpose: the flip must be the thing that
    serializes builds, so it has to happen before any workflow exists.
    """
    _set_attrs(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        attrs={ATTR_BUILD_WORKFLOW_ID: workflow_id},
    )


def stamp_build_complete(
    ddb,
    table: str,
    *,
    data_domain: str,
    dataset: str,
    stamp: dict[str, Any],
    pending_hash: str,
    bundle_version: str = "",
) -> str:
    """Publish a finished build onto the row. Returns the status written.

    ``stamp`` is :func:`complete_build`'s return. ``pending_hash`` is carried
    from gather time and stored VERBATIM as ``ar_source_hash``: it describes
    what was ingested, not what the wiki says now, so a mutation mid-build makes
    the policy stale-on-arrival instead of silently mislabelling it as current.
    """
    status = str(stamp.get("build_status") or BUILD_READY)
    attrs: dict[str, Any] = {
        "ar_policy_arn": stamp.get("policy_arn", ""),
        "ar_policy_version": stamp.get("policy_version", ""),
        "ar_guardrail_id": stamp.get("guardrail_id", ""),
        "ar_guardrail_version": stamp.get("guardrail_version", ""),
        "ar_built_at": _now(),
        ATTR_SOURCE_HASH: pending_hash,
        "ar_fidelity_coverage": float(stamp.get("fidelity_coverage") or 0.0),
        "ar_fidelity_accuracy": float(stamp.get("fidelity_accuracy") or 0.0),
        ATTR_BUILD_STATUS: status,
    }
    if bundle_version:
        attrs["ar_bundle_version"] = bundle_version
    _set_attrs(ddb, table, data_domain=data_domain, dataset=dataset, attrs=attrs)
    return status


def stamp_build_failed(
    ddb, table: str, *, data_domain: str, dataset: str, reason: str
) -> str:
    """Record a build that cannot produce a usable policy. Returns the status.

    :data:`REASON_UNSUPPORTED_REGION` becomes its own status — the feature
    degrades to *absent* in a region without AR, which is a deployment fact and
    not something a nightly retry should keep chasing.
    """
    status = (
        BUILD_UNSUPPORTED_REGION
        if reason == REASON_UNSUPPORTED_REGION
        else BUILD_FAILED
    )
    _set_attrs(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        attrs={
            ATTR_BUILD_STATUS: status,
            "ar_build_detail": str(reason)[:_DETAIL_MAX],
        },
    )
    return status


def flag_stale(ddb, table: str, *, data_domain: str, dataset: str) -> bool:
    """Mark a built policy as superseded by a wiki change. True when it applied.

    Conditional on the row being in :data:`STALE_FLAGGABLE_STATUSES`, so a
    lazily discovered staleness (a repromote, a hash mismatch at check time)
    can never clobber an in-flight ``building`` row or resurrect a ``failed``
    one. Re-flagging an already-``stale`` row is a deliberate no-op write, which
    keeps the callers idempotent.
    """
    try:
        _set_attrs(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            attrs={ATTR_BUILD_STATUS: BUILD_STALE},
            condition=(
                "attribute_exists(pk) AND "
                "(#bs = :ready OR #bs = :degraded OR #bs = :stale)"
            ),
            extra_names={"#bs": ATTR_BUILD_STATUS},
            extra_values={
                ":ready": {"S": BUILD_READY},
                ":degraded": {"S": BUILD_DEGRADED},
                ":stale": {"S": BUILD_STALE},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition means "nothing to stale"
        if _is_condition_failure(e):
            return False
        raise


def is_enrolled(item: dict[str, Any] | None) -> bool:
    """True when a raw (typed-attribute) mapping row is enrolled in reasoning.

    The single reader every gate uses — build trigger, rebuild authority, and
    the chat check — so "enrolled" can never mean different things on different
    paths. An absent row, absent attr, or non-BOOL value is NOT enrolled.
    """
    return bool(((item or {}).get(ATTR_ENROLLED) or {}).get("BOOL"))


def set_enrolled(ddb, table: str, *, data_domain: str, dataset: str) -> None:
    """Opt the dataset into reasoning. Conditional on the mapping row existing.

    Only the flag: the first build is triggered separately (a ``policy_rebuild``
    event to the rebuild authority), so enrollment itself is instant and the
    caller never blocks on Bedrock.
    """
    _set_attrs(
        ddb,
        table,
        data_domain=data_domain,
        dataset=dataset,
        attrs={ATTR_ENROLLED: True},
    )


def clear_ar_attrs(ddb, table: str, *, data_domain: str, dataset: str) -> None:
    """Unenrollment's row half: REMOVE every AR attribute (delete semantics).

    Leaves the row exactly as if the dataset had never been enrolled — no
    paused/zombie third state to reason about. The caller owns the other
    halves (Bedrock policy + guardrail deletion, the ``policy/`` prefix purge)
    and their ordering. No condition beyond row existence: clearing attrs off
    a row that lacks them is an idempotent no-op.
    """
    names = {f"#r{i}": attr for i, attr in enumerate(AR_ROW_ATTRS)}
    ddb.update_item(
        TableName=table,
        Key=registry_key(data_domain, dataset),
        UpdateExpression="REMOVE " + ", ".join(names),
        ExpressionAttributeNames=names,
        ConditionExpression="attribute_exists(pk)",
    )

"""Attested Computations over the published S3 bundle: list / load / run.

The pure contract (doc shape, content hash, parameter validation, literal
rendering, substitution) lives in :mod:`okf_core.computations`; this module
adds the S3 and engine plumbing and is shared by every execution surface —
the consumption MCP, the chat agent's tools, and the Control API — so all
three run the exact same attestation path (docs/ATTESTED_COMPUTATIONS.md §6).

Verification overlay: the human Verify click lands OFF-MOUNT at
``verification/<domain>/<dataset>.json`` (one entry per computation:
``{slug, sha256, verified, verified_by}``) — never on the doc itself, because
a raw Lambda ``put_object`` into the mounted bundle tree materializes a
root-owned path the harvest runtime's uid-1000 mount identity can then not
write (the ``pending.json`` EACCES incident; "the mount is the sole writer of
the bundle tree"). The runtime folds matching entries into doc frontmatter at
its next run start; until then serving merges doc + overlay, overlay wins.
"""

from __future__ import annotations

import json
from typing import Any

from okf_core.computations import (
    COMPUTATIONS_PREFIX,
    Computation,
    ComputationError,
    DOMAINS_REL,
    domain_warnings,
    flatten_domains,
    parse_computation_text,
    render_literal,
    resolve_values,
    substitute,
    verification_state,
)
from okf_core.paths import parse_concept_id
from okf_aws.s3_bundle import bundle_prefix

# Reply-size bound for a list call — a bundle authoring hundreds of
# computations is a modeling smell we surface via `truncated`, not a crash.
_LIST_MAX = 200


def computations_key_prefix(data_domain: str, dataset: str) -> str:
    return f"{bundle_prefix(data_domain, dataset)}{COMPUTATIONS_PREFIX}"


def computation_doc_key(data_domain: str, dataset: str, slug: str) -> str:
    """S3 key of one computation doc. The slug is validated as a single
    concept-id segment so a client-supplied name can never traverse."""
    parts = parse_concept_id(slug)
    if len(parts) != 1:
        raise ComputationError(f"invalid computation name {slug!r}")
    return f"{computations_key_prefix(data_domain, dataset)}{parts[0]}.md"


def verification_overlay_key(data_domain: str, dataset: str) -> str:
    """OFF-MOUNT (outside ``okf/``) — see the module docstring."""
    return f"verification/{data_domain}/{dataset}.json"


def load_overlay(
    s3, bucket: str, data_domain: str, dataset: str, *, strict: bool = False
) -> dict[str, Any]:
    """``{slug: entry}`` from the verification overlay.

    Two postures, because the callers differ in what a bad read costs:

    * READ paths (``strict=False``, default — list/describe/run/freeze
      resolution): any failure degrades to ``{}`` — verification reads as
      ``unverified``, never a crash.
    * WRITE paths (``strict=True`` — verify/unverify handlers, the fold-in):
      these do load → mutate → whole-object save, so a transient S3 error
      (throttle, 5xx, KMS/AccessDenied blip) misread as "empty overlay" would
      make the save SILENTLY DELETE every other computation's stamp and — far
      worse — every ``revoked`` tombstone, resurrecting revoked verifications
      from the docs' folded frontmatter. Only the true missing-object case
      (NoSuchKey/404) may read as empty; everything else raises so the write
      aborts loudly.
    """
    try:
        obj = s3.get_object(
            Bucket=bucket, Key=verification_overlay_key(data_domain, dataset)
        )
        raw = json.loads(obj["Body"].read())
    except Exception as e:  # noqa: BLE001 - triaged below
        if strict and not _is_missing_object(e):
            raise
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        if strict:
            raise RuntimeError(
                "verification overlay exists but is malformed — refusing a "
                "read-modify-write that would replace it"
            )
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _is_missing_object(e: Exception) -> bool:
    """True iff ``e`` is S3's missing-object error (the one legitimate
    empty-overlay case for a strict read). NoSuchBucket deliberately does NOT
    qualify — a misconfigured bucket on a write path must abort, not wipe."""
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "404")


def save_overlay(
    s3, bucket: str, data_domain: str, dataset: str, entries: dict[str, Any]
) -> None:
    """Persist the overlay (the whole small map — one object per dataset; the
    versioned bucket makes every flip an auditable object version). No
    conditional PUT: if the doc changed between the Verify screen loading and
    the click, the signed hash simply no longer matches and the entry reads as
    stale — the binding self-corrects."""
    s3.put_object(
        Bucket=bucket,
        Key=verification_overlay_key(data_domain, dataset),
        Body=json.dumps({"version": 1, "entries": entries}, indent=1).encode(
            "utf-8"
        ),
        ContentType="application/json",
    )


def load_domains(s3, bucket: str, data_domain: str, dataset: str) -> dict[str, Any]:
    """The profile pass's enum domains, flattened for the advisory layer
    (``{}`` when the bundle has none)."""
    key = f"{bundle_prefix(data_domain, dataset)}{DOMAINS_REL}"
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return flatten_domains(json.loads(obj["Body"].read()))
    except Exception:  # noqa: BLE001 - advisory layer degrades to silence
        return {}


def load_computation(
    s3, bucket: str, data_domain: str, dataset: str, slug: str
) -> tuple[Computation | None, list[str]]:
    """Fetch + parse one computation doc: ``(comp, errors)``."""
    try:
        key = computation_doc_key(data_domain, dataset, slug)
    except ValueError:  # covers ComputationError — a bad name is a caller error
        return None, [f"invalid computation name {slug!r}"]
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        text = obj["Body"].read().decode("utf-8")
    except Exception:  # noqa: BLE001 - absent doc = a caller-facing error
        return None, [
            f"no computation named {slug!r} — list_computations shows what "
            f"this dataset defines"
        ]
    rel = COMPUTATIONS_PREFIX + f"{slug}.md"
    return parse_computation_text(rel, text)


def summarize(comp: Computation, overlay_entry: dict[str, Any] | None) -> dict[str, Any]:
    """One list/describe entry: the contract a consumer needs to call it."""
    fm = {
        "verified": comp.verified,
        "verified_by": comp.verified_by,
        "verified_sha256": comp.verified_sha256,
    }
    return {
        "computation": comp.slug,
        "title": comp.title,
        "description": comp.description,
        "runtime": comp.runtime,
        "parameters": comp.parameters,
        "sha256": comp.sha256,
        **verification_state(comp.sha256, frontmatter=fm, overlay_entry=overlay_entry),
    }


def list_computations(s3, bucket: str, data_domain: str, dataset: str) -> dict[str, Any]:
    """Every computation the dataset publishes, with merged verification.

    Lists the ``references/computations/`` prefix and parses frontmatter live
    (bundles are modest; a catalog precompute is deliberately deferred until
    listing cost ever shows up). Invalid docs are surfaced by name + first
    error, never silently dropped — per the spec's "surface, not silently
    drop, a failing attestation".
    """
    prefix = computations_key_prefix(data_domain, dataset)
    overlay = load_overlay(s3, bucket, data_domain, dataset)
    computations: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    truncated = False
    params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_objects_v2(**params)
        for item in resp.get("Contents", []) or []:
            key = item.get("Key", "")
            rel = key[len(prefix) :]
            if not key.endswith(".md") or "/" in rel:
                continue
            if len(computations) + len(invalid) >= _LIST_MAX:
                truncated = True
                break
            try:
                text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode(
                    "utf-8"
                )
            except Exception:  # noqa: BLE001 - a torn read is an invalid entry
                invalid.append({"computation": rel[: -3], "error": "unreadable"})
                continue
            comp, errors = parse_computation_text(COMPUTATIONS_PREFIX + rel, text)
            if comp is None:
                invalid.append({"computation": rel[: -3], "error": errors[0]})
                continue
            computations.append(summarize(comp, overlay.get(comp.slug)))
        if truncated or not resp.get("IsTruncated"):
            break
        params["ContinuationToken"] = resp.get("NextContinuationToken")
    out: dict[str, Any] = {
        "data_domain": data_domain,
        "dataset": dataset,
        "computations": computations,
    }
    if invalid:
        out["invalid"] = invalid
    if truncated:
        out["truncated"] = True
    return out


def run_computation(
    comp: Computation,
    values: dict[str, Any] | None,
    *,
    athena=None,
    redshift_data=None,
    source: dict[str, Any] | None = None,
    athena_workgroup: str = "",
    athena_output: str = "",
    athena_catalog: str = "AwsDataCatalog",
    max_rows: int = 200,
    timeout_s: float = 120.0,
    domains: dict[str, Any] | None = None,
    overlay_entry: dict[str, Any] | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """Validate values, substitute, execute, and assemble the receipt.

    The executor — never the model — does parameter validation, dialect
    literal rendering, substitution at the ``@name`` sites, read-only
    execution under the caps, and receipt assembly; that is what makes the
    attestation meaningful. Raises :class:`ComputationError` only for invalid
    parameter values (a corrective tool error); engine failures return the
    receipt un-executed with a ``note`` — the rendered SQL is always included
    so the caller can fall back to its own access path.
    """
    resolved = resolve_values(comp.parameters, values)  # raises ComputationError
    literals = {
        p["name"]: render_literal(p["type"], resolved[p["name"]], comp.runtime)
        for p in comp.parameters
    }
    executed_sql = substitute(comp.statement, literals)
    warnings = domain_warnings(comp.parameters, resolved, domains or {})
    fm = {
        "verified": comp.verified,
        "verified_by": comp.verified_by,
        "verified_sha256": comp.verified_sha256,
    }
    receipt: dict[str, Any] = {
        "computation": comp.slug,
        "runtime": comp.runtime,
        "executed_sql": executed_sql,
        "computation_sha256": comp.sha256,
        **verification_state(comp.sha256, frontmatter=fm, overlay_entry=overlay_entry),
        "warnings": warnings,
        "executed": False,
    }
    if not execute:
        receipt["note"] = (
            "execution is not enabled on this deployment — the rendered SQL "
            "is included: run it through your own access path"
        )
        return receipt

    if comp.runtime == "athena":
        if athena is None:
            receipt["note"] = (
                "no Athena execution path here — the rendered SQL is included"
            )
            return receipt
        database = (source or {}).get("glue_database")
        if not database:
            receipt["note"] = (
                "this dataset's mapping carries no Glue database — the "
                "rendered SQL is included"
            )
            return receipt
        from okf_aws.athena_query import run_select

        try:
            run = run_select(
                athena,
                sql=executed_sql,
                database=str(database),
                workgroup=athena_workgroup or None,
                output_location=athena_output or None,
                catalog=athena_catalog,
                max_rows=max_rows,
                timeout_s=timeout_s,
            )
        except Exception as e:  # noqa: BLE001 - receipt survives engine failure
            return _execution_failed(receipt, e)
    elif comp.runtime == "redshift":
        src = source or {}
        if redshift_data is None:
            receipt["note"] = (
                "no Redshift execution path here — the rendered SQL is included"
            )
            return receipt
        if src.get("type") != "redshift" or not src.get("redshift_database"):
            receipt["note"] = (
                "this dataset's mapping carries no Redshift connection "
                "descriptor — the rendered SQL is included"
            )
            return receipt
        from okf_aws.redshift_query import run_select as rs_run_select

        try:
            run = rs_run_select(
                redshift_data,
                sql=executed_sql,
                database=str(src.get("redshift_database")),
                cluster_identifier=src.get("cluster_identifier") or None,
                workgroup_name=src.get("workgroup_name") or None,
                secret_arn=src.get("secret_arn") or None,
                max_rows=max_rows,
                timeout_s=timeout_s,
            )
        except Exception as e:  # noqa: BLE001 - receipt survives engine failure
            return _execution_failed(receipt, e)
    else:  # unreachable for parsed docs (runtime is validated), kept honest
        receipt["note"] = f"runtime {comp.runtime!r} has no execution path here"
        return receipt

    receipt.update(run)
    receipt["engine_query_id"] = run.get("query_execution_id")
    receipt["executed"] = True
    # An empty result plus an unverified parameter value is the classic
    # silent-typo shape — say so (one-round self-correction).
    if receipt.get("row_count") == 0 and warnings:
        receipt["note"] = (
            "0 rows — a parameter value could not be verified against "
            "profiled data: check the warnings for a likely typo"
        )
    return receipt


def _execution_failed(receipt: dict[str, Any], error: Exception) -> dict[str, Any]:
    """Engine failure -> the un-executed receipt plus a note. Never re-raise:
    the receipt contract promises the rendered ``executed_sql`` on every
    reply (an AccessDenied while IAM propagates is exactly when the caller's
    fallback path matters)."""
    receipt["note"] = (
        f"the query did not execute ({type(error).__name__}: {error}) — the "
        "rendered `executed_sql` is included: run it through your own access "
        "path, or retry"
    )
    return receipt

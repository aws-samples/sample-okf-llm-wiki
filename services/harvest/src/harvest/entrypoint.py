"""AgentCore Runtime entrypoint for the harvest agent (HTTP protocol, port 8080).

A harvest is long (minutes to hours), so the entrypoint MUST NOT block: it
validates the payload, starts the crawl on a daemon thread, and returns an
immediate ack. While any job runs, ``/ping`` reports ``HealthyBusy`` so
AgentCore keeps the runtime session alive (up to 8 h) instead of idling it out
at ~15 min.

Payload (from the Control API's InvokeAgentRuntime call):
  {
    "data_domain": "sales",
    "dataset": "orders",              # the Glue database name
    "mode": "full" | "incremental" | "annotated",
    "changed_table": "customers",     # incremental only
    "diff": {...}                      # incremental only, optional
    "user_sub": "<cognito sub>",      # annotated only (whose annotations)
    "annotations": [{...}],            # annotated only (the live feedback)
    "model": "openai.gpt-5.6-sol",    # optional per-harvest override; falls
    "effort": "xhigh"                 # back to OKF_HARVEST_* env when omitted
  }

``runtimeSessionId`` is set to the dataset id by the caller, giving one session
per dataset as designed.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading

from harvest.clients import build_source, dataset_root
from harvest.runner import (
    run_annotation_harvest,
    run_full_harvest,
    run_incremental_harvest,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("harvest.entrypoint")

MOUNT_PATH = os.environ.get("OKF_MOUNT_PATH", "/mnt/data")

_busy_lock = threading.Lock()
_active_jobs = 0


def _run_job(payload: dict, session_id: str | None = None) -> None:
    global _active_jobs
    try:
        _dispatch(payload, session_id=session_id)
    except Exception:  # noqa: BLE001 - log and keep the runtime healthy
        log.exception("Harvest job failed for payload=%s", _safe(payload))
    finally:
        with _busy_lock:
            _active_jobs -= 1


def _dispatch(payload: dict, session_id: str | None = None) -> None:
    data_domain = payload["data_domain"]
    dataset = payload.get("dataset", "")
    mode = payload.get("mode", "full")

    if mode == "write_domain_doc":
        run_write_domain_doc(payload)
        return

    root = dataset_root(MOUNT_PATH, data_domain, dataset)
    source = build_source(dataset)

    # Domain description/context (enriched by control_api from DOMAIN#/META).
    domain_description = payload.get("domain_description")
    domain_context = payload.get("domain_context")

    # Dataset-level guidance (shared authoring instructions) + the version being
    # applied. Steers the prompt on every mode; on a successful full/incremental/
    # annotation run the runner stamps guidance_applied_version = this version so
    # the guidance clears its DIRTY state.
    dataset_guidance = payload.get("dataset_guidance")
    dataset_guidance_version = payload.get("dataset_guidance_version")

    # Recursive-improvement config (optional). Presence of the validated block is
    # the enable signal; the Control API already validated/clamped it. Absent ⇒ a
    # normal harvest. Threaded into full/incremental/annotated identically.
    recursive_improvement = payload.get("recursive_improvement")

    # Per-harvest model/effort override (chosen in the UI, validated by the
    # Control API against the TF catalog). When absent, resolve_model_config
    # falls back to the deploy-time OKF_HARVEST_* env vars — so this is fully
    # backward compatible. We build the config here (not lower down) so both run
    # paths share it and max_tokens keys off the resolved model.
    model_config = _model_config_from_payload(payload)

    # session_id is the run's runtime_session_id — the SAME id on the DynamoDB
    # STATUS row and the OTEL baggage — so the live step feed's log lines can be
    # correlated back to this run by the Control API.
    if mode == "incremental":
        run_incremental_harvest(
            source=source,
            dataset_root=root,
            data_domain=data_domain,
            dataset=dataset,
            changed_table=payload["changed_table"],
            diff=payload.get("diff"),
            model_config=model_config,
            domain_description=domain_description,
            domain_context=domain_context,
            dataset_guidance=dataset_guidance,
            dataset_guidance_version=dataset_guidance_version,
            recursive_improvement=recursive_improvement,
            session_id=session_id,
        )
    elif mode == "annotated":
        run_annotation_harvest(
            source=source,
            dataset_root=root,
            data_domain=data_domain,
            dataset=dataset,
            user_sub=payload["user_sub"],
            annotations=payload.get("annotations") or [],
            model_config=model_config,
            domain_description=domain_description,
            domain_context=domain_context,
            dataset_guidance=dataset_guidance,
            dataset_guidance_version=dataset_guidance_version,
            recursive_improvement=recursive_improvement,
            session_id=session_id,
        )
    else:
        run_full_harvest(
            source=source,
            dataset_root=root,
            data_domain=data_domain,
            dataset=dataset,
            model_config=model_config,
            domain_description=domain_description,
            domain_context=domain_context,
            dataset_guidance=dataset_guidance,
            dataset_guidance_version=dataset_guidance_version,
            recursive_improvement=recursive_improvement,
            session_id=session_id,
        )


def _model_config_from_payload(payload: dict) -> dict | None:
    """Build a model_config override from payload ``model``/``effort``, or None.

    Returns None when neither key is present so the runner falls back to the
    env-var defaults (``resolve_model_config()``). When either is present we call
    ``resolve_model_config`` with the overrides so the SAME provider-aware
    max_tokens / concurrency defaulting still applies. The Control API already
    validated the pair against the catalog; the runtime trusts it (consistent
    with the runtime not allow-listing effort itself).
    """
    model = payload.get("model")
    effort = payload.get("effort")
    if not model and not effort:
        return None
    from harvest.agent import resolve_model_config

    return resolve_model_config(model_override=model, effort_override=effort)


def _safe(payload: dict) -> dict:
    safe = {
        k: payload.get(k)
        for k in (
            "data_domain",
            "dataset",
            "mode",
            "changed_table",
            "model",
            "effort",
            "domain_description",
            "domain_context",
            "user_sub",
        )
    }
    # Never log annotation bodies (reader feedback text); just how many there were.
    if payload.get("annotations") is not None:
        safe["annotations"] = f"<{len(payload.get('annotations') or [])} items>"
    return safe


def _validate(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return "payload must be a JSON object"
    mode = payload.get("mode", "full")
    # write_domain_doc only needs data_domain.
    if mode == "write_domain_doc":
        if not payload.get("data_domain"):
            return "missing required field: data_domain"
        return None
    for key in ("data_domain", "dataset"):
        if not payload.get(key):
            return f"missing required field: {key}"
    if mode == "incremental" and not payload.get("changed_table"):
        return "incremental mode requires 'changed_table'"
    if mode == "annotated":
        if not payload.get("user_sub"):
            return "annotated mode requires 'user_sub'"
        # A run must carry SOMETHING to do: open annotations to apply, OR pending
        # dataset guidance to apply (a guidance-only re-harvest). The Control API
        # only invokes this mode when at least one holds, so an empty payload for
        # both is a programming error, not a valid run.
        if not payload.get("annotations") and not payload.get("dataset_guidance"):
            return (
                "annotated mode requires open annotations or dataset guidance to apply"
            )
    return None


def run_write_domain_doc(payload: dict) -> dict:
    """Write (or overwrite) the domain's concept doc through the mount.

    Called by the Control API via ``mode=write_domain_doc``. The doc lives at
    ``<mount>/<domain>/_domain/overview.md`` — we write it THROUGH the mount so
    the ``<domain>/`` directory is created with the mount's uid-1000 identity
    (preventing the root-owned-dir EACCES that would wedge any dataset harvest
    under this domain). This is synchronous and fast (one write).
    """
    from datetime import datetime, timezone
    from pathlib import Path

    from harvest.fsutil import write_text
    from okf_core.domain import DOMAIN_CONCEPT_ID, DOMAIN_DATASET, build_domain_document

    data_domain = payload["data_domain"]
    description = payload.get("description", "")
    context = payload.get("context", "")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    doc_text = build_domain_document(
        data_domain=data_domain,
        description=description,
        context=context,
        timestamp=timestamp,
    )

    # Write through the mount: <mount>/<domain>/_domain/overview.md
    mount_root = Path(MOUNT_PATH).resolve()
    target = mount_root / data_domain / DOMAIN_DATASET / f"{DOMAIN_CONCEPT_ID}.md"
    # Safety: must stay under the mount root.
    resolved = target.resolve()
    if mount_root not in resolved.parents and resolved != mount_root:
        log.error("domain doc target %s escapes mount root; aborting", resolved)
        return {"status": "rejected", "error": "path escape"}

    write_text(target, doc_text)
    log.info("Wrote domain doc for '%s' at %s", data_domain, target)
    return {
        "status": "written",
        "data_domain": data_domain,
        "path": str(target),
    }


def run_cleanup(payload: dict) -> dict:
    """Maintenance op: remove a dataset (or whole data_domain) subtree from the mount.

    ``{"mode":"cleanup","data_domain":"health_care"}`` removes
    ``$OKF_MOUNT_PATH/health_care`` (and everything under it); adding
    ``"dataset":"toxicology"`` scopes it to ``.../health_care/toxicology``.

    WHY this exists: the S3-Files mount can end up with a directory the runtime's
    uid can't write into (e.g. a prefix materialized by a non-mount S3 put_object
    with no POSIX metadata). Deleting the S3 object doesn't repair the NFS-layer
    directory — only a delete THROUGH the mount does, and this runtime is the thing
    that holds the mount. Runs SYNCHRONOUSLY (fast) and returns the result.

    Safety: the target is built from ``data_domain``/``dataset`` and resolved; it
    MUST stay strictly inside the mount root (no ``..``/absolute escapes) or the op
    is rejected. It only ever removes within the OKF tree.
    """
    import os
    from pathlib import Path

    domain = payload.get("data_domain")
    dataset = payload.get("dataset")
    if not domain:
        return {"status": "rejected", "error": "cleanup requires data_domain"}

    mount_root = Path(MOUNT_PATH).resolve()
    # Build the target from sanitized components (reject separators / traversal).
    for comp in (domain, dataset):
        if comp and ("/" in comp or "\\" in comp or comp in ("..", ".")):
            return {"status": "rejected", "error": f"unsafe path component: {comp!r}"}
    target = (
        (mount_root / domain / dataset).resolve()
        if dataset
        else (mount_root / domain).resolve()
    )
    # Containment check: target must be under the mount root and not the root itself.
    if target == mount_root or mount_root not in target.parents:
        return {
            "status": "rejected",
            "error": f"refusing to remove {target} (outside mount root)",
        }

    if not target.exists():
        return {
            "status": "cleaned",
            "target": str(target),
            "removed": False,
            "data_domain": domain,
            "dataset": dataset,
        }

    # Report ownership/mode of the whole subtree — diagnostic for the case where a
    # non-mount writer (a raw S3 put_object) created inodes the mount uid can't
    # remove. euid/egid is what the mount presents.
    def _stat(p):
        try:
            st = os.stat(p, follow_symlinks=False)
            return {
                "path": str(p),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "mode": oct(st.st_mode),
            }
        except OSError as e:
            return {"path": str(p), "error": f"{type(e).__name__}: {e}"}

    inventory = [_stat(target)]
    for dirpath, dirnames, filenames in os.walk(target):
        for n in sorted(dirnames) + sorted(filenames):
            inventory.append(_stat(os.path.join(dirpath, n)))

    # Attempt removal bottom-up, capturing per-path failures instead of aborting.
    # Files first (unlink), then dirs deepest-first (rmdir) — so we remove whatever
    # this uid CAN, and report exactly which inodes it can't (the poisoned ones).
    removed_paths, failed = [], []
    for dirpath, dirnames, filenames in os.walk(target, topdown=False):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                os.unlink(fp)
                removed_paths.append(fp)
            except OSError as e:
                failed.append({"path": fp, "error": f"{type(e).__name__}: {e}"})
        try:
            os.rmdir(dirpath)
            removed_paths.append(dirpath)
        except OSError as e:
            failed.append({"path": dirpath, "error": f"{type(e).__name__}: {e}"})

    fully_removed = not target.exists()
    log.info(
        "Cleanup target=%s fully_removed=%s failed=%d",
        target,
        fully_removed,
        len(failed),
    )
    return {
        "status": "cleaned" if fully_removed else "partial",
        "target": str(target),
        "removed": fully_removed,
        "euid": os.geteuid(),
        "egid": os.getegid(),
        "removed_count": len(removed_paths),
        "failed": failed,
        "inventory": inventory,
        "data_domain": domain,
        "dataset": dataset,
    }


def run_provision(payload: dict) -> dict:
    """Maintenance op: create a dataset's bundle dirs THROUGH the mount.

    ``{"mode":"provision","data_domain":"sport","dataset":"spider2_ipl"}`` makes
    ``$OKF_MOUNT_PATH/<domain>/<dataset>/`` and its ``.context/`` subdir exist,
    created BY the mount so they carry the access point's POSIX identity (uid
    1000) and are writable by every later harvest.

    WHY this exists: a presigned ``.context/`` upload PUTs straight to S3,
    bypassing the mount. S3 Files then auto-materializes the missing parent dirs
    (``<domain>/<dataset>/`` and ``.context/``) owned by root — an identity the
    access point (which forces all ops to uid 1000) cannot then write into. The
    next full harvest's ``mark_in_progress`` tries to ``mkdir .harvest/`` inside
    that root-owned dataset dir and gets EACCES, wedging the dataset. Creating the
    dirs through the mount FIRST (at dataset-registration time) means the out-of-
    band upload lands inside an already-uid-1000 tree, so the harvest can write.

    Runs SYNCHRONOUSLY (a couple of mkdirs) and returns the result. Idempotent:
    ``mkdirs`` is exist_ok. Safety mirrors :func:`run_cleanup` — the target is
    built from sanitized components and MUST stay strictly inside the mount root.
    """
    from pathlib import Path

    from harvest.fsutil import mkdirs

    domain = payload.get("data_domain")
    dataset = payload.get("dataset")
    if not domain or not dataset:
        return {
            "status": "rejected",
            "error": "provision requires data_domain and dataset",
        }

    mount_root = Path(MOUNT_PATH).resolve()
    for comp in (domain, dataset):
        if "/" in comp or "\\" in comp or comp in ("..", "."):
            return {"status": "rejected", "error": f"unsafe path component: {comp!r}"}
    target = (mount_root / domain / dataset).resolve()
    if target == mount_root or mount_root not in target.parents:
        return {
            "status": "rejected",
            "error": f"refusing to provision {target} (outside mount root)",
        }

    # Create the dataset root and .context/ THROUGH the mount (uid 1000). The
    # dataset root is what a later harvest's mark_in_progress must mkdir into;
    # .context/ is where uploads land, so make it now too (harvest only reads it).
    context_dir = target / ".context"
    mkdirs(context_dir)  # mkdirs creates all ancestors, incl. the dataset root
    log.info("Provisioned bundle dirs (uid=%s): %s", os.geteuid(), context_dir)
    return {
        "status": "provisioned",
        "target": str(target),
        "context_dir": str(context_dir),
        "euid": os.geteuid(),
        "egid": os.getegid(),
        "data_domain": domain,
        "dataset": dataset,
    }


def start_harvest(payload: dict, session_id: str | None = None) -> dict:
    """Validate + launch a harvest on a background thread; return an ack.

    Split out from the decorated entrypoint so it's unit-testable without the
    bedrock_agentcore package. ``session_id`` (the run's ``runtime_session_id``)
    is threaded into the crawl so the live step feed's log lines correlate to it.
    """
    if payload.get("mode") == "cleanup":
        return run_cleanup(payload)
    if payload.get("mode") == "provision":
        return run_provision(payload)
    if payload.get("mode") == "write_domain_doc":
        return run_write_domain_doc(payload)

    err = _validate(payload)
    if err:
        return {"status": "rejected", "error": err}

    global _active_jobs
    with _busy_lock:
        _active_jobs += 1
    # Run the crawl inside a COPY of the current context so the active
    # OpenTelemetry span + session baggage propagate into the worker thread.
    # OTEL context lives in contextvars, which a bare threading.Thread does NOT
    # inherit — without this, every LLM/tool/sub-agent span the crawl emits would
    # detach from the invoke span into orphan traces (breaking the trajectory
    # + session grouping in CloudWatch GenAI Observability).
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run, args=(_run_job, payload, session_id), daemon=True
    ).start()
    log.info("Accepted harvest: %s", _safe(payload))
    return {
        "status": "accepted",
        "data_domain": payload["data_domain"],
        "dataset": payload["dataset"],
        "mode": payload.get("mode", "full"),
    }


def is_busy() -> bool:
    with _busy_lock:
        return _active_jobs > 0


def session_id_for(payload: dict, context_arg) -> str | None:
    """The observability session id used to GROUP a harvest's spans.

    Prefer the AgentCore-assigned ``runtimeSessionId`` (the Control API sets it to
    the per-dataset id, so the console session matches our logical id). Fall back
    to deriving it from the payload if the runtime didn't supply a context.
    """
    sid = getattr(context_arg, "session_id", None)
    if sid:
        return str(sid)
    domain, dataset = payload.get("data_domain"), payload.get("dataset")
    return f"{domain}-{dataset}" if domain and dataset else None


def attach_session_baggage(session_id: str | None) -> None:
    """Best-effort: put ``session.id`` on OTEL baggage for the CURRENT context.

    Must be called SYNCHRONOUSLY in ``invoke`` (before ``start_harvest`` runs its
    ``contextvars.copy_context()``), so the copied context the daemon crawl thread
    runs under carries the baggage. The ADOT distro auto-registers a
    ``BaggageSpanProcessor`` with ``session.id`` in its allowed keys under
    ``AGENT_OBSERVABILITY_ENABLED``, so it copies this onto EVERY span (root +
    LangChain/tool/sub-agent children) → Sessions grouping in GenAI Observability.
    No-op if OpenTelemetry isn't importable (observability is best-effort).
    """
    if not session_id:
        return
    try:
        from opentelemetry import baggage
        from opentelemetry import context as otel_context

        otel_context.attach(baggage.set_baggage("session.id", session_id))
    except Exception:  # noqa: BLE001 - observability must never break a harvest
        log.debug("Could not attach session.id baggage", exc_info=True)


# -- AgentCore wiring (only when the SDK is present) -------------------------

try:  # pragma: no cover - exercised only in the runtime image
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload, context=None):
        payload = payload or {}
        # Stamp session.id BEFORE start_harvest copies the context into the crawl
        # thread, so every span in the run inherits it (see attach_session_baggage).
        # The SAME id also correlates the live step feed's log lines to this run.
        sid = session_id_for(payload, context)
        attach_session_baggage(sid)
        return start_harvest(payload, session_id=sid)

    @app.ping
    def ping():
        return "HealthyBusy" if is_busy() else "Healthy"

    if __name__ == "__main__":
        app.run()
except Exception:  # pragma: no cover
    app = None

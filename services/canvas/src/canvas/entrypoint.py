"""AgentCore Runtime entrypoint for the canvas CI (HTTP protocol, port 8080).

A job runs minutes, so the entrypoint MUST NOT block: validate, launch on a
daemon thread, ack immediately. ``/ping`` reports HealthyBusy while any job
runs so the session survives past the idle reaper (the harvest entrypoint's
arrangement, minus every mount/lease concern — this runtime never touches the
bundle tree).

Payload (from the chat runtime's or Control API's InvokeAgentRuntime call):
  {
    "mode": "canvas",                # the only job mode
    "job_id": "<hex>",               # caller-generated; runtimeSessionId is
                                     #   derived from it (control affinity)
    "user_sub": "<cognito sub>",     # whose dashboard (structural isolation)
    "data_domain": "sales",
    "dataset": "orders",
    "request": "revenue overview…",  # new dashboards: free-form context
    "spec": {...},                   # new dashboards: the tile plan agreed
                                     #   in chat (okf_core normalize_skeleton
                                     #   shape) — request and/or spec
    "canvas_id": "<hex>",            # revisions: the stored canvas to evolve
    "feedback": "make the trend…",   # revisions: the user's feedback
                                     #   (+ optional `spec` = updated plan)
  }
"""

from __future__ import annotations

import contextvars
import logging
import threading

from canvas.runner import run_canvas_job

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("canvas.entrypoint")

_busy_lock = threading.Lock()
_active_jobs = 0


def validate(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return "payload must be a JSON object"
    for key in ("job_id", "user_sub", "data_domain", "dataset"):
        if not payload.get(key):
            return f"missing required field: {key}"
    if (
        not payload.get("request")
        and not payload.get("spec")
        and not (payload.get("canvas_id") and payload.get("feedback"))
    ):
        return (
            "a job needs `request` and/or `spec` (new) or "
            "`canvas_id` + `feedback` (revision)"
        )
    return None


def _run(payload: dict, session_id: str | None) -> None:
    global _active_jobs
    try:
        run_canvas_job(payload, session_id=session_id)
    except Exception as e:  # noqa: BLE001 — keep the runtime healthy
        # The runner rows failures that happen inside its own try — but a
        # crash BEFORE that (client build, its initial put_job) would leave
        # the chat-written row stranded "queued" with the UI polling forever.
        # Best-effort: write the failure onto the row from here.
        log.exception("canvas job crashed: %s", payload.get("job_id"))
        try:
            import os

            from canvas import jobs
            from canvas.runner import build_clients

            jobs.set_job_status(
                build_clients()["ddb"],
                table=os.environ.get("OKF_CANVAS_TABLE", "okf-canvas"),
                user_sub=str(payload.get("user_sub") or ""),
                job_id=str(payload.get("job_id") or ""),
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )
        except Exception:  # noqa: BLE001 — nothing left to try
            log.exception("could not record the failure on the job row")
    finally:
        with _busy_lock:
            _active_jobs -= 1


def start_job(payload: dict, session_id: str | None = None) -> dict:
    """Validate + launch on a daemon thread; return an ack. Split from the
    decorated entrypoint so it's unit-testable without bedrock_agentcore."""
    err = validate(payload)
    if err:
        return {"status": "rejected", "error": err}
    global _active_jobs
    with _busy_lock:
        _active_jobs += 1
    # Copy the context so OTEL span/baggage propagate into the worker thread
    # (same reasoning as the harvest entrypoint).
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(_run, payload, session_id), daemon=True).start()
    log.info(
        "Accepted canvas job %s for %s/%s (user=%s)",
        payload["job_id"], payload["data_domain"], payload["dataset"],
        payload["user_sub"],
    )
    return {"status": "accepted", "job_id": payload["job_id"]}


def is_busy() -> bool:
    with _busy_lock:
        return _active_jobs > 0


# -- AgentCore wiring (only when the SDK is present) -------------------------

try:  # pragma: no cover - exercised only in the runtime image
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload, context=None):
        payload = payload or {}
        sid = getattr(context, "session_id", None) or payload.get("job_id")
        return start_job(payload, session_id=str(sid) if sid else None)

    @app.ping
    def ping():
        return "HealthyBusy" if is_busy() else "Healthy"

    if __name__ == "__main__":
        app.run()
except Exception:  # pragma: no cover
    app = None

"""Canvas job + canvas rows in DynamoDB (okf_core.canvas item conventions).

The job row is the LIFECYCLE record (queued/running/submitted/failed) plus a
bounded, emit-time-filtered step feed (tool labels + final text only — the
model runs with ``surface_reasoning=False``, so no reasoning exists to leak,
and the feed never carries raw tool payloads either). The durable, complete
record of the run is the LangGraph checkpoint thread
(``<sub>:canvasjob-<job_id>`` in the chat checkpoint table); the feed is the
cheap poll surface the UI's jobs panel renders.

The canvas row is the dashboard itself: the CI runtime is its only writer
(save_draft / submit_dashboard), the control API reads/verifies/deletes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from okf_core import canvas as cv

MAX_STEPS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_key(user_sub: str, job_id: str) -> dict[str, Any]:
    return {"pk": {"S": cv.canvas_job_pk(user_sub)}, "sk": {"S": job_id}}


def put_job(
    ddb,
    *,
    table: str,
    user_sub: str,
    job_id: str,
    request: str,
    data_domain: str,
    dataset: str,
    canvas_id: str = "",
    thread_id: str = "",
    title: str = "",
    ttl_seconds: int = cv.JOB_TTL_SECONDS,
) -> None:
    now = _now()
    # The CHAT tool wrote the queued row when it dispatched the job — its
    # created_at is when the user asked, and queue latency (now - created_at)
    # only reads truthfully if this overwrite preserves it.
    prior = ddb.get_item(TableName=table, Key=job_key(user_sub, job_id)).get("Item")
    created = (prior or {}).get("created_at", {}).get("S", "") or now
    item = {
        **job_key(user_sub, job_id),
        "job_id": {"S": job_id},
        "status": {"S": cv.JOB_RUNNING},
        # `title` is what the UI shows for the job (the caller agent names the
        # canvas); `request` is kept for the agents' status tool + debugging.
        "title": {"S": title[:200]},
        "request": {"S": request[:2000]},
        "data_domain": {"S": data_domain},
        "dataset": {"S": dataset},
        "steps": {"S": "[]"},
        "created_at": {"S": created},
        "updated_at": {"S": now},
        # DynamoDB TTL (epoch seconds): job rows are drawer history only.
        "expires_at": {
            "N": str(int(datetime.now(timezone.utc).timestamp()) + int(ttl_seconds))
        },
    }
    if canvas_id:
        item["canvas_id"] = {"S": canvas_id}
    # The delegating CHAT conversation (client thread id): the UI shows a
    # conversation only its own canvas jobs.
    if thread_id:
        item["thread_id"] = {"S": thread_id}
    ddb.put_item(TableName=table, Item=item)


def set_job_status(
    ddb,
    *,
    table: str,
    user_sub: str,
    job_id: str,
    status: str,
    canvas_id: str = "",
    error: str = "",
) -> None:
    update = "SET #s = :s, updated_at = :now"
    names = {"#s": "status"}
    values: dict[str, Any] = {":s": {"S": status}, ":now": {"S": _now()}}
    if canvas_id:
        update += ", canvas_id = :c"
        values[":c"] = {"S": canvas_id}
    if error:
        update += ", #e = :e"
        names["#e"] = "error"
        values[":e"] = {"S": error[:2000]}
    ddb.update_item(
        TableName=table,
        Key=job_key(user_sub, job_id),
        UpdateExpression=update,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def append_step(
    ddb,
    *,
    table: str,
    user_sub: str,
    job_id: str,
    kind: str,
    label: str,
    tool_input: dict[str, Any] | None = None,
    cache: list[dict[str, Any]] | None = None,
) -> None:
    """Append one feed event onto the JSON blob.

    One writer per job (the runner thread), so the writer can OWN the list:
    pass ``cache`` (the writer's in-memory steps, mutated here) and the
    per-event GetItem disappears — without it every event paid a read plus a
    rewrite of the ever-growing blob. The blob stays a single attribute so a
    poll is one GetItem. Beyond MAX_STEPS the feed truncates with a final
    marker — the checkpoint thread remains the complete record.
    """
    if cache is not None:
        steps = cache
    else:
        resp = ddb.get_item(TableName=table, Key=job_key(user_sub, job_id))
        raw = resp.get("Item", {}).get("steps", {}).get("S", "[]")
        try:
            steps = json.loads(raw)
        except ValueError:
            steps = []
    if len(steps) >= MAX_STEPS:
        if not steps or steps[-1].get("type") != "truncated":
            steps.append({"t": _now(), "type": "truncated", "label": ""})
        else:
            return
    else:
        # Text steps get more room than tool labels — they're the agent's
        # narration between calls, cut mid-thought at 300 they read broken.
        cap = 1000 if kind == "text" else 300
        clipped = label[:cap] + ("…" if len(label) > cap else "")
        step: dict[str, Any] = {"t": _now(), "type": kind, "label": clipped}
        # Sanitized tool args (small, truncated values) — the UI's curated
        # labels ("Read tables/races", "Computed points_by_season") key off
        # (tool name, args), exactly like the chat timeline.
        if tool_input:
            step["input"] = tool_input
        steps.append(step)
    ddb.update_item(
        TableName=table,
        Key=job_key(user_sub, job_id),
        UpdateExpression="SET steps = :s, updated_at = :now",
        ExpressionAttributeValues={
            ":s": {"S": json.dumps(steps)},
            ":now": {"S": _now()},
        },
    )


# ---------------------------------------------------------------------------
# Canvas rows
# ---------------------------------------------------------------------------


def canvas_key(user_sub: str, canvas_id: str) -> dict[str, Any]:
    return {"pk": {"S": cv.canvas_pk(user_sub)}, "sk": {"S": canvas_id}}


def get_canvas_row(
    ddb, *, table: str, user_sub: str, canvas_id: str
) -> dict[str, Any] | None:
    resp = ddb.get_item(TableName=table, Key=canvas_key(user_sub, canvas_id))
    return resp.get("Item")


class CanvasRowConflict(Exception):
    """The canvas row changed (or vanished) since this job last saw it."""


def write_canvas_row(
    ddb,
    *,
    table: str,
    user_sub: str,
    canvas_id: str,
    title: str,
    data_domain: str,
    dataset: str,
    status: str,
    config: dict[str, Any],
    expected_updated_at: str | None = None,
    require_existing: bool = False,
) -> str:
    """Upsert the canvas row, preserving created_at across revisions. A
    revision over a VERIFIED canvas lands as ``status`` (draft/submitted) —
    re-verification is deliberately a fresh human decision.

    Optimistic concurrency against the HUMAN's direct writes (the Control
    API's layout/title patches, a delete): ``expected_updated_at`` is the
    row's ``updated_at`` as this job last saw or wrote it — the put lands
    only if it still matches, so a drag/rename made while the job ran is
    never silently reverted to the job's start-of-run config.
    ``require_existing`` alone (a row seen without a stamp) still refuses to
    resurrect a deleted canvas. ``None`` + not required = a brand-new id.
    Raises :class:`CanvasRowConflict`; returns the written ``updated_at``
    (microsecond precision — it IS the version token)."""
    existing = get_canvas_row(ddb, table=table, user_sub=user_sub, canvas_id=canvas_id)
    created = (existing or {}).get("created_at", {}).get("S", "") or _now()
    kwargs: dict[str, Any] = {}
    if expected_updated_at:
        kwargs["ConditionExpression"] = "updated_at = :prev"
        kwargs["ExpressionAttributeValues"] = {":prev": {"S": expected_updated_at}}
    elif require_existing:
        kwargs["ConditionExpression"] = "attribute_exists(pk)"
    else:
        kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        ddb.put_item(
            TableName=table,
            Item={
                **canvas_key(user_sub, canvas_id),
                "canvas_id": {"S": canvas_id},
                "title": {"S": title[:200]},
                "data_domain": {"S": data_domain},
                "dataset": {"S": dataset},
                "status": {"S": status},
                "config": {"S": json.dumps(config)},
                "created_at": {"S": created},
                "updated_at": {"S": stamp},
            },
            **kwargs,
        )
    except ddb.exceptions.ConditionalCheckFailedException as e:
        raise CanvasRowConflict(canvas_id) from e
    return stamp

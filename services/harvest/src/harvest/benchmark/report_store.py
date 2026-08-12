"""Persist a benchmark report: the S3 artifacts + the REPORT# index row.

Two artifacts per report, both off-mount (they carry gold — no LLM role may
read them; served only via the Cognito-authed Control API):

* ``benchmark/<d>/<ds>/reports/<report_id>/report.json`` — config recap,
  scores, stability, per-question detail, judge output, telemetry.
* ``.../traces.json`` — EVERY attempt's solver trace, passing and failing
  (large; the UI fetches it lazily). Written best-effort AFTER the report:
  losing step detail must never lose the report.

The **index row** (``pk=HARVEST#<d>#<ds>``, ``sk=REPORT#<report_id>``) is what
the Benchmark list polls: status, config summary, live progress, and headline
KPIs — all FLAT scalars (``status._marshal`` handles only bool/int/float/str;
structure lives in the S3 JSON). The Control API writes the row as ``queued``
when it invokes the runtime; everything after that is written HERE, from the
runtime, via UpdateItem (never PutItem — the queued row carries ``created_at``,
``config`` summary and ``requested_by`` that must not be clobbered).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from okf_core import benchmark_report as br

log = logging.getLogger("harvest.benchmark.report_store")

# Progress stamps ride the row (the list poller's cadence is seconds); cap how
# often we write so a 100-question round doesn't hammer DynamoDB.
_PROGRESS_MIN_INTERVAL_S = 2.0

# Terminal-status writes get a bounded retry: every non-terminal write has a
# later write to correct it, but a transient DDB fault on the LAST write would
# leave an eternal `running` row in front of a durable S3 report.
_TERMINAL_WRITE_ATTEMPTS = 3
_TERMINAL_WRITE_BACKOFF_S = 0.5


def persist_report_artifacts(
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    report_id: str,
    report_doc: dict[str, Any],
    traces_doc: dict[str, Any] | None,
    put_object: Any = None,
    registry: tuple[Any, str] | None = None,
) -> None:
    """PUT report.json (must succeed — raises) then traces.json (best-effort).

    When ``registry`` is provided, the REPORT# row is checked first: a report
    the human deleted mid-run must not have its artifacts re-materialize as
    orphans (the row write is already conditional; this closes the S3 side —
    a small race between the check and the PUTs remains and is acceptable).
    The check itself is best-effort: a registry read error never blocks the
    durable report.
    """
    if registry is not None and not _report_row_exists(
        registry, data_domain=data_domain, dataset=dataset, report_id=report_id
    ):
        log.info(
            "Report row %s for %s/%s is gone (deleted); skipping artifact PUTs.",
            report_id, data_domain, dataset,
        )
        return
    put = put_object or _default_put_object
    put(
        bucket,
        br.report_key(data_domain, dataset, report_id),
        json.dumps(report_doc).encode("utf-8"),
    )
    if not traces_doc or not traces_doc.get("traces"):
        return
    try:
        put(
            bucket,
            br.traces_key(data_domain, dataset, report_id),
            json.dumps(traces_doc).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 - step detail is supporting, the report is durable
        log.warning(
            "Failed to persist solver traces for report %s.", report_id, exc_info=True
        )


def _default_put_object(bucket: str, key: str, body: bytes) -> None:
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def _report_row_exists(
    registry: tuple[Any, str], *, data_domain: str, dataset: str, report_id: str
) -> bool:
    """Whether the REPORT# row still exists. Fail-open on read errors."""
    client, table = registry
    try:
        resp = client.get_item(
            TableName=table,
            Key={
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": br.report_sk(report_id)},
            },
        )
        return bool(resp.get("Item"))
    except Exception:  # noqa: BLE001 - the check is a courtesy, never a gate
        return True


def headline_kpis(report_doc: dict[str, Any]) -> dict[str, Any]:
    """Flatten the report's per-check scores into the row's scalar KPI attrs.

    ``<check>_raw`` / ``<check>_adjusted`` (the means) + ``<check>_graded``,
    plus the run's token total — the fields the Benchmark list renders without
    fetching the S3 JSON.
    """
    out: dict[str, Any] = {}
    for check, block in (report_doc.get("scores") or {}).items():
        out[f"{check}_raw"] = float(block.get("raw", {}).get("mean") or 0.0)
        # A judge-graded check (Behavior) has adjusted=None — omit the KPI
        # entirely so the UI's "is a number" gate hides the line instead of
        # rendering a misleading 0%.
        adjusted = block.get("adjusted")
        if isinstance(adjusted, dict):
            out[f"{check}_adjusted"] = float(adjusted.get("mean") or 0.0)
        out[f"{check}_graded"] = int(block.get("graded") or 0)
    telemetry = report_doc.get("telemetry") or {}
    tokens = 0
    for role in ("solver", "judge"):
        tokens += int(
            ((telemetry.get(role) or {}).get("tokens") or {}).get("total_tokens") or 0
        )
    out["total_tokens"] = tokens
    candidates = (report_doc.get("annotations") or {}).get("candidates") or []
    out["annotation_candidates"] = len(candidates)
    return out


def update_report_row(
    registry: tuple[Any, str] | None,
    *,
    data_domain: str,
    dataset: str,
    report_id: str,
    attrs: dict[str, Any],
    sk: str | None = None,
    unless_status: str | None = None,
) -> None:
    """Best-effort UpdateItem of flat scalars onto the REPORT# row.

    ``sk`` overrides the sort key for row kinds that share this exact
    lifecycle (the QBANK# rows of ``mode=generate_questions``); default is the
    REPORT# row of ``report_id``. ``unless_status`` additionally guards the
    write on the row's ``status`` NOT being that value — how a runtime's
    terminal ``complete`` is prevented from resurrecting a row the operator
    just CANCELLED (the same shape as the deleted-row condition: the blocked
    write is dropped, never retried — that drop is the point).

    ``attrs`` values must be bool/int/float/str (the ``_marshal`` contract).
    Never raises — a row write must not fail a run whose S3 report is already
    durable; the reader treats the row as an INDEX, the JSON as truth.
    Conditional on the row still EXISTING: UpdateItem otherwise upserts, and a
    runtime finishing after the human deleted the report would resurrect a
    phantom row (partial attrs, no ``status``) that the UI renders as a stuck
    active run. A deleted report stays deleted; the late write is dropped.
    TERMINAL statuses (run or agg) additionally retry a transient fault — the
    terminal write has no later write to correct it (see the module constants);
    a failed condition (deleted row) is never retried, that drop is the point.
    """
    if registry is None or not attrs:
        return
    import time
    from datetime import datetime, timezone

    client, table = registry
    terminal = attrs.get("status") in (br.STATUS_COMPLETE, br.STATUS_FAILED) or attrs.get(
        "agg_status"
    ) in (br.AGG_COMPLETE, br.AGG_FAILED)
    attempts = _TERMINAL_WRITE_ATTEMPTS if terminal else 1
    for attempt in range(1, attempts + 1):
        try:
            names: dict[str, str] = {}
            values: dict[str, Any] = {}
            sets: list[str] = []
            for i, (key, value) in enumerate(attrs.items()):
                alias, placeholder = f"#a{i}", f":v{i}"
                names[alias] = key
                values[placeholder] = _marshal(value)
                sets.append(f"{alias} = {placeholder}")
            names["#u"] = "updated_at"
            values[":u"] = {
                "S": datetime.now(timezone.utc).isoformat(timespec="seconds")
            }
            sets.append("#u = :u")
            condition = "attribute_exists(pk)"
            if unless_status:
                names["#st"] = "status"
                values[":blocked"] = {"S": unless_status}
                condition += " AND #st <> :blocked"
            client.update_item(
                TableName=table,
                Key={
                    "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                    "sk": {"S": sk or br.report_sk(report_id)},
                },
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return
        except Exception as e:  # noqa: BLE001 - the S3 report is the durable truth
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # The row was deleted (or holds the blocked status) — dropping
                # the late write is the contract.
                log.info(
                    "Report row %s for %s/%s is gone or %s; write dropped.",
                    report_id, data_domain, dataset, unless_status or "deleted",
                )
                return
            if attempt < attempts:
                time.sleep(_TERMINAL_WRITE_BACKOFF_S * attempt)
                continue
            log.warning(
                "Failed to update report row %s for %s/%s (continuing)",
                report_id,
                data_domain,
                dataset,
                exc_info=True,
            )


def _marshal(value: Any) -> dict[str, Any]:
    """bool→BOOL, int/float→N, everything else→S (bool BEFORE int — subclass)."""
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


class RowProgress:
    """Throttled progress stamper: folds engine ticks onto the REPORT# row.

    The Benchmark list polls rows, so live progress rides the row itself —
    ``phase`` / ``check`` / ``run`` / ``current`` / ``total`` — written at most
    every ``_PROGRESS_MIN_INTERVAL_S`` (the engine already throttles ticks to
    ~10 per phase; this caps bursts when rounds are tiny). A tick that CHANGES
    the (phase, check, run) identity always writes, throttle or not: phase
    transitions are what the human is watching for, and dropping one used to
    leave the row lying about which check/step the run was on until the new
    phase's next tick (minutes, for judge phases). A cross-run phase (behavior
    grading, the judge reviews) passes ``run_index=-1`` → ``progress_run=0``,
    which the UI reads as "no run part" — those phases span ALL runs, and
    stamping the last run's number on them mislabeled the line. The terminal
    write comes from the mode runner, not from here.
    """

    def __init__(
        self,
        registry: tuple[Any, str] | None,
        *,
        data_domain: str,
        dataset: str,
        report_id: str,
        total_runs: int,
        now: Any = None,
    ):
        import time

        self._registry = registry
        self._domain = data_domain
        self._dataset = dataset
        self._report_id = report_id
        self._total_runs = total_runs
        self._now = now or time.monotonic
        # None (not 0.0) so the FIRST tick always writes — monotonic clocks can
        # start near zero, where `now - 0.0` would spuriously throttle it.
        self._last_write: float | None = None
        self._last_key: tuple[str, str, int] | None = None

    def __call__(
        self, phase: str, check: str, run_index: int, current: int, total: int
    ) -> None:
        key = (phase, check, run_index)
        is_final_tick = current >= total
        now = self._now()
        if (
            key == self._last_key  # only SAME-phase repeats are throttleable
            and not is_final_tick
            and self._last_write is not None
            and now - self._last_write < _PROGRESS_MIN_INTERVAL_S
        ):
            return
        self._last_write = now
        self._last_key = key
        update_report_row(
            self._registry,
            data_domain=self._domain,
            dataset=self._dataset,
            report_id=self._report_id,
            attrs={
                "phase": phase,
                "progress_check": check,
                "progress_run": int(run_index) + 1,
                "total_runs": self._total_runs,
                "progress_current": int(current),
                "progress_total": int(total),
            },
        )

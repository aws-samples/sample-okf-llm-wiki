"""Runner-side benchmark setup: fetch the off-mount CSV, assemble RI kwargs.

Called by the harvest runner when a run carries an enabled ``recursive_improvement``
config. It fetches the ``question,gold_sql`` CSV from its **off-mount** S3 key
(``benchmark/<domain>/<dataset>/…`` — never under the ``okf/`` mount prefix, so no
LLM role can read the gold) into memory, parses + caps it, and returns everything
``build_harvest_agent`` needs to wire the ``run_benchmark`` tool.

Best-effort with a hard failure mode: if the CSV can't be fetched or has no valid
questions, recursive improvement is DISABLED for the run (returns ``None``) and the
harvest proceeds as a normal harvest — a benchmark misconfiguration must not wedge
authoring. The caller logs the reason.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from harvest.benchmark.questions import BenchmarkQuestion, load_questions
from harvest.status import write_benchmark_kpi
from okf_core import recursive_improvement as ri

log = logging.getLogger("harvest.benchmark.setup")


class BenchmarkSetup:
    """The RI kwargs for ``build_harvest_agent`` plus the run identifiers."""

    def __init__(
        self,
        *,
        ri_config: dict[str, Any],
        questions: list[BenchmarkQuestion],
        run: dict[str, Any],
        persist_kpi: Any,
        total_in_csv: int,
        dropped: int,
    ):
        self.ri_config = ri_config
        self.questions = questions
        self.run = run
        self.persist_kpi = persist_kpi
        self.total_in_csv = total_in_csv
        self.dropped = dropped


def _fetch_csv(bucket: str, key: str) -> str:
    """GET the CSV text from S3 (off-mount). Raises on any S3 error."""
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def prepare(
    *,
    ri_config: dict[str, Any] | None,
    data_domain: str,
    dataset: str,
    runtime_session_id: str | None,
    registry: Any,
    fetch_csv: Any = None,
) -> BenchmarkSetup | None:
    """Fetch + parse the question set and assemble RI wiring, or None if unusable.

    ``ri_config`` is the VALIDATED block from the payload (or None). ``registry`` is
    the (client, table) tuple for KPI writes. ``fetch_csv(bucket, key) -> str`` is
    injectable for tests; defaults to the boto3 S3 GET. Returns None (RI off for
    this run) when the config is disabled, the bucket env is missing, the fetch
    fails, or the CSV yields zero valid questions — always logging why.
    """
    if not ri.is_enabled(ri_config):
        return None
    assert ri_config is not None

    key = ri_config.get(ri.FIELD_QUESTIONS_KEY)
    bucket = os.environ.get("OKF_BUNDLE_BUCKET")
    if not bucket:
        log.warning("RI enabled but OKF_BUNDLE_BUCKET unset; disabling benchmark.")
        return None
    if not key:
        log.warning("RI enabled but questions_key missing; disabling benchmark.")
        return None

    fetch = fetch_csv or _fetch_csv
    try:
        csv_text = fetch(bucket, key)
    except Exception:  # noqa: BLE001 - a fetch failure disables RI, never wedges harvest
        log.warning(
            "Could not fetch benchmark CSV s3://%s/%s; disabling benchmark.",
            bucket,
            key,
            exc_info=True,
        )
        return None

    try:
        loaded = load_questions(csv_text)
    except Exception:  # noqa: BLE001 - a malformed CSV disables RI
        log.warning("Benchmark CSV parse failed; disabling benchmark.", exc_info=True)
        return None

    if not loaded.questions:
        log.warning("Benchmark CSV has no valid questions; disabling benchmark.")
        return None
    if loaded.dropped:
        log.info(
            "Benchmark question set capped: %d of %d rows used (dropped %d over cap %d).",
            len(loaded.questions),
            loaded.total_in_csv,
            loaded.dropped,
            ri.MAX_QUESTIONS,
        )

    session_id = runtime_session_id or ""

    def persist_kpi(iteration: Any, attrs: dict) -> None:
        write_benchmark_kpi(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            runtime_session_id=session_id,
            iteration=iteration,
            attrs=attrs,
        )

    return BenchmarkSetup(
        ri_config=ri_config,
        questions=loaded.questions,
        run={
            "data_domain": data_domain,
            "dataset": dataset,
            "runtime_session_id": session_id,
        },
        persist_kpi=persist_kpi,
        total_in_csv=loaded.total_in_csv,
        dropped=loaded.dropped,
    )

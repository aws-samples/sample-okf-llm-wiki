"""Studio plumbing: report store, S3 snapshots, mode validation + row lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from okf_core import benchmark_report as br

from harvest.benchmark.report_store import (
    RowProgress,
    headline_kpis,
    persist_report_artifacts,
    update_report_row,
)
from harvest.benchmark.s3_snapshot import SnapshotError, materialize_snapshots
from harvest.benchmark.studio import (
    validate_aggregate_payload,
    validate_benchmark_payload,
)
from harvest.entrypoint import _validate


# -- report store ---------------------------------------------------------------


def test_persist_report_writes_both_artifacts():
    puts = []
    persist_report_artifacts(
        bucket="b", data_domain="sales", dataset="orders", report_id="r1-aaaa1111",
        report_doc={"report_id": "r1-aaaa1111"},
        traces_doc={"report_id": "r1-aaaa1111", "traces": [{"q_id": 0}]},
        put_object=lambda b, k, body: puts.append((k, json.loads(body))),
    )
    assert [k for k, _ in puts] == [
        "benchmark/sales/orders/reports/r1-aaaa1111/report.json",
        "benchmark/sales/orders/reports/r1-aaaa1111/traces.json",
    ]


def test_persist_report_skips_empty_traces_and_survives_trace_put_failure():
    puts = []
    persist_report_artifacts(
        bucket="b", data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        report_doc={}, traces_doc={"traces": []},
        put_object=lambda b, k, body: puts.append(k),
    )
    assert puts == ["benchmark/d/ds/reports/r1-aaaa1111/report.json"]

    def flaky(bucket, key, body):
        puts.append(key)
        if key.endswith("traces.json"):
            raise RuntimeError("S3 down")

    # The report already landed; a traces failure must not raise.
    persist_report_artifacts(
        bucket="b", data_domain="d", dataset="ds", report_id="r2-aaaa1111",
        report_doc={}, traces_doc={"traces": [{"q_id": 0}]}, put_object=flaky,
    )


def test_headline_kpis_flatten_scores_and_tokens():
    report = {
        "scores": {
            "sql": {"raw": {"mean": 0.75}, "adjusted": {"mean": 0.9}, "graded": 40},
            # Judge-graded check: adjusted is None → the KPI must be OMITTED
            # (a 0.0 would render as a misleading "Judge adjudication 0%").
            "behavior": {"raw": {"mean": 1.0}, "adjusted": None, "graded": 10},
        },
        "telemetry": {
            "solver": {"tokens": {"total_tokens": 1000}},
            "judge": {"tokens": {"total_tokens": 200}},
        },
        "annotations": {"candidates": [{"q_id": 1}]},
    }
    kpis = headline_kpis(report)
    assert kpis["sql_raw"] == 0.75 and kpis["sql_adjusted"] == 0.9
    assert kpis["behavior_raw"] == 1.0 and kpis["behavior_graded"] == 10
    assert "behavior_adjusted" not in kpis
    assert kpis["total_tokens"] == 1200
    assert kpis["annotation_candidates"] == 1
    # Everything is a flat scalar — the _marshal contract.
    assert all(isinstance(v, (bool, int, float, str)) for v in kpis.values())


@mock_aws
def test_update_report_row_updates_only_named_attrs():
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="reg",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # The Control API wrote the queued row with created_at.
    ddb.put_item(TableName="reg", Item={
        "pk": {"S": "HARVEST#d#ds"}, "sk": {"S": "REPORT#r1-aaaa1111"},
        "status": {"S": "queued"}, "created_at": {"S": "2026-07-29T00:00:00"},
    })
    update_report_row(
        (ddb, "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"status": br.STATUS_RUNNING, "sql_raw": 0.5, "done": False},
    )
    item = ddb.get_item(TableName="reg", Key={
        "pk": {"S": "HARVEST#d#ds"}, "sk": {"S": "REPORT#r1-aaaa1111"},
    })["Item"]
    assert item["status"]["S"] == "running"
    assert item["created_at"]["S"] == "2026-07-29T00:00:00"  # not clobbered
    assert item["sql_raw"]["N"] == "0.5"
    assert item["done"]["BOOL"] is False
    assert "updated_at" in item
    # 'status' is a DynamoDB reserved word — the aliased write must handle it
    # (this test failing with ValidationException is the regression signal).


@mock_aws
def test_update_report_row_never_resurrects_a_deleted_row():
    # UpdateItem upserts by default: a runtime (or aggregator) finishing AFTER
    # the human deleted the report used to recreate a phantom row with partial
    # attrs and no status. The write is conditional on the row existing.
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="reg",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    update_report_row(
        (ddb, "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"agg_status": "complete"},
    )
    resp = ddb.get_item(TableName="reg", Key={
        "pk": {"S": "HARVEST#d#ds"}, "sk": {"S": "REPORT#r1-aaaa1111"},
    })
    assert "Item" not in resp  # the late write was dropped, not upserted


def test_update_report_row_never_raises():
    class Boom:
        def update_item(self, **_kw):
            raise RuntimeError("ddb down")

    update_report_row(
        (Boom(), "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"status": "running"},
    )
    update_report_row(None, data_domain="d", dataset="ds",
                      report_id="r1-aaaa1111", attrs={"status": "running"})


def test_update_report_row_retries_terminal_writes_only(monkeypatch):
    # A transient DDB fault on the TERMINAL write would leave an eternal
    # `running` row in front of a durable S3 report — terminal statuses get a
    # bounded retry; every non-terminal write has a later write to correct it.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    class FlakyOnce:
        def __init__(self):
            self.calls = 0

        def update_item(self, **_kw):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("ddb down")

    flaky = FlakyOnce()
    update_report_row(
        (flaky, "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"status": br.STATUS_COMPLETE},
    )
    assert flaky.calls == 2  # retried once, then succeeded

    flaky = FlakyOnce()
    update_report_row(
        (flaky, "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"phase": "solving"},
    )
    assert flaky.calls == 1  # non-terminal → no retry

    class CondFail:
        def __init__(self):
            self.calls = 0

        def update_item(self, **_kw):
            self.calls += 1
            e = RuntimeError("gone")
            e.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise e

    cond = CondFail()
    update_report_row(
        (cond, "reg"), data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        attrs={"status": br.STATUS_FAILED},
    )
    assert cond.calls == 1  # a deleted row is a deliberate drop, never retried


@mock_aws
def test_persist_report_artifacts_skips_when_row_deleted():
    # A run finishing AFTER the human deleted its report must not orphan
    # report.json/traces.json behind no row.
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="reg",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    puts = []
    persist_report_artifacts(
        bucket="b", data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        report_doc={}, traces_doc=None,
        put_object=lambda b, k, body: puts.append(k),
        registry=(ddb, "reg"),
    )
    assert puts == []  # no row → the PUTs are skipped

    ddb.put_item(TableName="reg", Item={
        "pk": {"S": "HARVEST#d#ds"}, "sk": {"S": "REPORT#r1-aaaa1111"},
        "status": {"S": "running"},
    })
    persist_report_artifacts(
        bucket="b", data_domain="d", dataset="ds", report_id="r1-aaaa1111",
        report_doc={}, traces_doc=None,
        put_object=lambda b, k, body: puts.append(k),
        registry=(ddb, "reg"),
    )
    assert puts == ["benchmark/d/ds/reports/r1-aaaa1111/report.json"]


def test_row_progress_throttles_but_always_writes_final_tick():
    writes = []
    clock = {"t": 0.0}

    class Fake:
        def update_item(self, **kw):
            writes.append(kw)

    progress = RowProgress(
        (Fake(), "reg"), data_domain="d", dataset="ds",
        report_id="r1-aaaa1111", total_runs=3, now=lambda: clock["t"],
    )
    progress("solving", "sql", 0, 10, 100)   # first tick writes
    progress("solving", "sql", 0, 20, 100)   # same instant → throttled
    clock["t"] = 5.0
    progress("solving", "sql", 0, 30, 100)   # past the interval → writes
    progress("solving", "sql", 0, 100, 100)  # FINAL tick always writes
    assert len(writes) == 3
    # A tick that CHANGES the phase identity is never throttled — the live
    # line must flip the instant a new phase/check/run begins.
    progress("grading", "sql", 0, 0, 100)    # same instant, new phase → writes
    progress("grading", "sql", 0, 10, 100)   # same phase, same instant → throttled
    progress("grading", "behavior", -1, 0, 40)  # new identity → writes
    assert len(writes) == 5
    # The cross-run sentinel (-1) lands as progress_run=0 — the UI's "no run
    # part" signal for phases that span all runs.
    last = writes[-1]["ExpressionAttributeValues"]
    assert {"N": "0"} in last.values()


# -- S3 snapshot materialization --------------------------------------------------


@mock_aws
def _bucket_with_bundle():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    return s3


@mock_aws
def test_materialize_live_snapshot_splits_solver_and_judge_trees(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    s3.put_bucket_versioning(
        Bucket="bundles", VersioningConfiguration={"Status": "Enabled"}
    )
    prefix = "okf/sales/orders/"
    s3.put_object(Bucket="bundles", Key=prefix + "index.md", Body=b"# index")
    s3.put_object(Bucket="bundles", Key=prefix + "tables/races.md", Body=b"# races")
    s3.put_object(
        Bucket="bundles", Key=prefix + ".metadata/columns.tsv", Body=b"races\tyear"
    )
    s3.put_object(
        Bucket="bundles", Key=prefix + ".context/spec.md", Body=b"status codes"
    )
    s3.put_object(
        Bucket="bundles", Key=prefix + ".harvest/state.json",
        Body=json.dumps({"status": "complete", "completed_at": "t"}).encode(),
    )

    solver_dir, judge_dir = tmp_path / "solver", tmp_path / "judge"
    n = materialize_snapshots(
        s3, bucket="bundles", data_domain="sales", dataset="orders",
        version_id="", solver_dir=str(solver_dir), judge_dir=str(judge_dir),
    )
    assert n == 2
    # Solver: docs only — gold-blindness stays physical.
    assert (solver_dir / "tables/races.md").read_text() == "# races"
    assert not (solver_dir / ".metadata").exists()
    assert not (solver_dir / ".context").exists()
    # Judge: docs + the dot-dir inputs.
    assert (judge_dir / "tables/races.md").exists()
    assert (judge_dir / ".metadata/columns.tsv").read_text() == "races\tyear"
    assert (judge_dir / ".context/spec.md").read_text() == "status codes"


@mock_aws
def test_materialize_without_judge_dir_skips_judge_tree(tmp_path):
    # The annotation aggregator only needs the doc tree: judge_dir=None must
    # skip the judge copy AND the .metadata/.context sweep (a throwaway judge
    # tree used to leak a bundle-sized temp dir per aggregation).
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    s3.put_bucket_versioning(
        Bucket="bundles", VersioningConfiguration={"Status": "Enabled"}
    )
    prefix = "okf/sales/orders/"
    s3.put_object(Bucket="bundles", Key=prefix + "index.md", Body=b"# index")
    s3.put_object(
        Bucket="bundles", Key=prefix + ".metadata/columns.tsv", Body=b"races\tyear"
    )

    solver_dir = tmp_path / "solver"
    n = materialize_snapshots(
        s3, bucket="bundles", data_domain="sales", dataset="orders",
        version_id="", solver_dir=str(solver_dir), judge_dir=None,
    )
    assert n == 1
    assert (solver_dir / "index.md").read_text() == "# index"
    assert not (solver_dir / ".metadata").exists()


@mock_aws
def test_materialize_pinned_version_fetches_at_version(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    s3.put_bucket_versioning(
        Bucket="bundles", VersioningConfiguration={"Status": "Enabled"}
    )
    prefix = "okf/sales/orders/"
    marker_key = prefix + ".harvest/state.json"
    # Harvest 1: doc v1 + complete marker.
    s3.put_object(Bucket="bundles", Key=prefix + "index.md", Body=b"v1")
    m1 = s3.put_object(
        Bucket="bundles", Key=marker_key,
        Body=json.dumps({"status": "complete", "completed_at": "t1"}).encode(),
    )["VersionId"]
    import time as _time

    _time.sleep(1.1)  # S3 timestamps are second-granular; the cut point needs distance
    # Harvest 2: doc v2 + a new complete marker.
    s3.put_object(Bucket="bundles", Key=prefix + "index.md", Body=b"v2")
    s3.put_object(
        Bucket="bundles", Key=marker_key,
        Body=json.dumps({"status": "complete", "completed_at": "t2"}).encode(),
    )

    solver_dir, judge_dir = tmp_path / "solver", tmp_path / "judge"
    materialize_snapshots(
        s3, bucket="bundles", data_domain="sales", dataset="orders",
        version_id=m1, solver_dir=str(solver_dir), judge_dir=str(judge_dir),
    )
    # Pinned to version 1 → the OLD doc content, though v2 is live.
    assert (solver_dir / "index.md").read_text() == "v1"


@mock_aws
def test_materialize_unknown_version_and_empty_bundle_fail_loudly(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    with pytest.raises(SnapshotError):
        materialize_snapshots(
            s3, bucket="bundles", data_domain="sales", dataset="orders",
            version_id="", solver_dir=str(tmp_path / "s"), judge_dir=str(tmp_path / "j"),
        )
    with pytest.raises(SnapshotError):
        materialize_snapshots(
            s3, bucket="bundles", data_domain="sales", dataset="orders",
            version_id="nope", solver_dir=str(tmp_path / "s"),
            judge_dir=str(tmp_path / "j"),
        )


# -- mode payload validation -------------------------------------------------------


def _benchmark_payload(**over):
    payload = {
        "mode": "benchmark", "data_domain": "sales", "dataset": "orders",
        "report_id": "r20260729t1-abcd1234", "checks": ["sql"],
        "questions_key": "benchmark/sales/orders/questions.csv",
    }
    payload.update(over)
    return payload


def test_validate_benchmark_payload():
    assert validate_benchmark_payload(_benchmark_payload()) is None
    assert "report_id" in validate_benchmark_payload(_benchmark_payload(report_id=""))
    assert "invalid report_id" in validate_benchmark_payload(
        _benchmark_payload(report_id="NOT VALID")
    )
    assert "check" in validate_benchmark_payload(_benchmark_payload(checks=[]))
    assert "questions_key" in validate_benchmark_payload(
        _benchmark_payload(questions_key="")
    )


def test_validate_aggregate_payload():
    good = {"mode": "aggregate_annotations", "data_domain": "d", "dataset": "ds",
            "report_id": "r20260729t1-abcd1234"}
    assert validate_aggregate_payload(good) is None
    assert "report_id" in validate_aggregate_payload({**good, "report_id": ""})


def test_entrypoint_validates_studio_modes():
    # The entrypoint's _validate dispatches to the studio validators, so a bad
    # benchmark payload is REJECTED at the ack (never accepted then failed).
    assert _validate(_benchmark_payload()) is None
    assert _validate(_benchmark_payload(checks=["vibes"]))
    assert _validate({"mode": "aggregate_annotations", "data_domain": "d",
                      "dataset": "ds", "report_id": "r20260729t1-abcd1234"}) is None


def test_run_benchmark_report_marks_row_failed_on_error(monkeypatch):
    # No OKF_BUNDLE_BUCKET → the run must fail LOUDLY: row → failed + detail,
    # and the exception propagates (the entrypoint's job logger records it).
    from harvest.benchmark import studio

    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    rows = []
    monkeypatch.setattr(studio, "build_registry_client", lambda: ("client", "t"))
    monkeypatch.setattr(
        studio,
        "update_report_row",
        lambda reg, **kw: rows.append(kw["attrs"]),
    )
    with pytest.raises(Exception):
        studio.run_benchmark_report(_benchmark_payload(), session_id="sess-1")
    assert rows[0]["status"] == br.STATUS_RUNNING
    assert rows[0]["runtime_session_id"] == "sess-1"
    assert rows[-1]["status"] == br.STATUS_FAILED
    assert "OKF_BUNDLE_BUCKET" in rows[-1]["detail"]


def test_write_judge_traces_lays_files_into_the_judge_tree(tmp_path):
    from okf_core.benchmark_questions import BenchmarkQuestion

    from harvest.benchmark.grader import Outcome
    from harvest.benchmark.report_run import Attempt
    from harvest.benchmark.studio import _write_judge_traces
    from harvest.benchmark.trace import SolverTrace

    questions = {0: BenchmarkQuestion(q_id=0, question="How many wins?", gold_sql="G")}
    attempts = [
        Attempt(q_id=0, check="sql", run_index=0, prediction="P",
                outcome=Outcome.PASS, reason="result sets match",
                trace=SolverTrace(turns=3, tool_calls=1, files_read=["tables/t.md"])),
        Attempt(q_id=0, check="sql", run_index=1, prediction="P2",
                outcome=Outcome.FAIL, reason="row counts differ",
                trace=SolverTrace(turns=4, tool_calls=2)),
        Attempt(q_id=0, check="sql", run_index=2, trace=None),  # no trace → no file
    ]
    _write_judge_traces(str(tmp_path), questions, attempts)

    root = tmp_path / ".traces" / "sql"
    names = sorted(p.name for p in root.iterdir())
    assert names == ["q000-run1.md", "q000-run2.md"]
    body = (root / "q000-run1.md").read_text()
    assert "How many wins?" in body
    assert "Outcome: PASS — result sets match" in body
    assert "tables/t.md" in body


def test_write_judge_traces_is_best_effort(tmp_path):
    # A bad judge_dir (a FILE, so mkdir fails) must not raise — the report and
    # the inline judge renderings still stand.
    from harvest.benchmark.report_run import Attempt
    from harvest.benchmark.studio import _write_judge_traces
    from harvest.benchmark.trace import SolverTrace

    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    attempts = [Attempt(q_id=0, check="sql", run_index=0, trace=SolverTrace())]
    _write_judge_traces(str(blocker), {}, attempts)  # must not raise


def test_run_aggregate_annotations_failure_writes_agg_detail(monkeypatch):
    # The agg failure lands on its OWN attr (agg_detail) — writing the shared
    # `detail` clobbered the RUN's failure reason.
    from harvest.benchmark import studio

    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    rows = []
    monkeypatch.setattr(studio, "build_registry_client", lambda: ("client", "t"))
    monkeypatch.setattr(
        studio, "update_report_row", lambda reg, **kw: rows.append(kw["attrs"])
    )
    with pytest.raises(Exception):
        studio.run_aggregate_annotations(
            {"data_domain": "d", "dataset": "ds",
             "report_id": "r20260729t1-abcd1234"}
        )
    # Start clears any previous failure's agg_detail.
    assert rows[0] == {"agg_status": br.AGG_RUNNING, "agg_detail": ""}
    assert rows[-1]["agg_status"] == br.AGG_FAILED
    assert "OKF_BUNDLE_BUCKET" in rows[-1]["agg_detail"]
    assert "detail" not in rows[-1]  # the run's failure reason stays intact


def test_grading_execute_threads_env_knobs_and_positional(monkeypatch):
    from harvest.benchmark.studio import _grading_execute

    monkeypatch.setenv("OKF_BENCHMARK_GRADER_TIMEOUT_S", "5")
    monkeypatch.setenv("OKF_BENCHMARK_GRADER_MAX_ROWS", "7")
    seen = {}

    class Src:
        def run_query(self, sql, *, timeout_s, max_rows, positional):
            seen.update(
                sql=sql, timeout_s=timeout_s, max_rows=max_rows,
                positional=positional,
            )
            return ["h"], [["v", None]]

    execute = _grading_execute(Src())
    assert execute("SELECT 1") == [["v", None]]  # rows only; None preserved
    assert seen == {
        "sql": "SELECT 1", "timeout_s": 5.0, "max_rows": 7, "positional": True,
    }


def test_grading_execute_defaults(monkeypatch):
    from harvest.benchmark.studio import _grader_max_rows, _grader_timeout_s

    monkeypatch.delenv("OKF_BENCHMARK_GRADER_TIMEOUT_S", raising=False)
    monkeypatch.delenv("OKF_BENCHMARK_GRADER_MAX_ROWS", raising=False)
    assert _grader_timeout_s() == 60.0
    assert _grader_max_rows() == 50000


def test_get_text_pins_the_questions_version():
    import io

    from harvest.benchmark.studio import _get_text

    calls = []

    class S3:
        def get_object(self, **kw):
            calls.append(kw)
            return {"Body": io.BytesIO(b"question,gold_sql\nQ,S\n")}

    _get_text(S3(), "b", "k", version_id="v123")
    assert calls[-1] == {"Bucket": "b", "Key": "k", "VersionId": "v123"}
    # Back-compat: no version (older payloads) → the latest object.
    _get_text(S3(), "b", "k")
    assert calls[-1] == {"Bucket": "b", "Key": "k"}


@mock_aws
def test_materialize_skips_path_escaping_judge_extras(tmp_path):
    # S3 keys are raw strings — a `..` segment under .metadata/ must not lay a
    # file outside the judge's temp tree.
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bundles")
    prefix = "okf/sales/orders/"
    s3.put_object(Bucket="bundles", Key=prefix + "index.md", Body=b"# index")
    s3.put_object(
        Bucket="bundles", Key=prefix + ".metadata/columns.tsv", Body=b"ok"
    )
    s3.put_object(
        Bucket="bundles", Key=prefix + ".metadata/../../../evil.txt", Body=b"evil"
    )

    solver_dir, judge_dir = tmp_path / "solver", tmp_path / "judge"
    materialize_snapshots(
        s3, bucket="bundles", data_domain="sales", dataset="orders",
        version_id="", solver_dir=str(solver_dir), judge_dir=str(judge_dir),
    )
    assert (judge_dir / ".metadata/columns.tsv").exists()
    escaped = [p for p in tmp_path.rglob("evil.txt")]
    assert escaped == []  # nothing written outside the trees
    assert not (tmp_path.parent / "evil.txt").exists()


def test_judge_toolset_includes_run_code_only_with_a_sandbox(tmp_path):
    pytest.importorskip("deepagents")
    from harvest.benchmark.studio import _judge_toolset

    class _Sandbox:
        def run_code(self, code):
            return {"stdout": "", "stderr": "", "is_error": False}

    with_sandbox = {
        t.name for t in _judge_toolset(str(tmp_path), object(), _Sandbox())
    }
    # The diagnostician set: judge-tree files + live data + the binary-context
    # extractor (only when the sandbox actually came up).
    assert {"read_file", "grep", "run_sql", "sample_rows", "run_code"} <= with_sandbox

    without = {t.name for t in _judge_toolset(str(tmp_path), object(), None)}
    assert "run_code" not in without
    assert {"read_file", "run_sql"} <= without

"""Runner-side benchmark setup: off-mount CSV fetch, cap, disable-on-failure."""

from __future__ import annotations

import harvest.benchmark.setup as setup
from okf_core import recursive_improvement as ri


def _cfg(**over):
    base = {"enabled": True, "questions_key": "benchmark/d/ds/q.csv"}
    base.update(over)
    return ri.validate(base)


def _csv(n):
    rows = "\n".join(f"Q{i},SELECT {i}" for i in range(n))
    return "question,gold_sql\n" + rows + "\n"


def test_prepare_disabled_returns_none():
    assert setup.prepare(
        ri_config=None, data_domain="d", dataset="ds",
        runtime_session_id="s", registry=None,
    ) is None


def test_prepare_none_when_bucket_unset(monkeypatch):
    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    out = setup.prepare(
        ri_config=_cfg(), data_domain="d", dataset="ds",
        runtime_session_id="s", registry=None,
        fetch_csv=lambda b, k: _csv(3),
    )
    assert out is None


def test_prepare_loads_and_wires(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "bundles")
    seen = {}

    def fake_fetch(bucket, key):
        seen["bucket"] = bucket
        seen["key"] = key
        return _csv(3)

    out = setup.prepare(
        ri_config=_cfg(), data_domain="sport", dataset="f1",
        runtime_session_id="sess-1", registry=None, fetch_csv=fake_fetch,
    )
    assert out is not None
    assert seen["bucket"] == "bundles"
    assert seen["key"] == "benchmark/d/ds/q.csv"  # the off-mount key from config
    assert len(out.questions) == 3
    assert out.run == {"data_domain": "sport", "dataset": "f1", "runtime_session_id": "sess-1"}
    assert out.ri_config["enabled"] is True


def test_prepare_caps_to_100(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")
    out = setup.prepare(
        ri_config=_cfg(), data_domain="d", dataset="ds",
        runtime_session_id="s", registry=None,
        fetch_csv=lambda b, k: _csv(130),
    )
    assert len(out.questions) == 100
    assert out.total_in_csv == 130
    assert out.dropped == 30


def test_prepare_none_on_fetch_error(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")

    def boom(bucket, key):
        raise RuntimeError("NoSuchKey")

    out = setup.prepare(
        ri_config=_cfg(), data_domain="d", dataset="ds",
        runtime_session_id="s", registry=None, fetch_csv=boom,
    )
    assert out is None  # disabled, not raised


def test_prepare_none_on_empty_question_set(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")
    out = setup.prepare(
        ri_config=_cfg(), data_domain="d", dataset="ds",
        runtime_session_id="s", registry=None,
        fetch_csv=lambda b, k: "question,gold_sql\n",  # header only
    )
    assert out is None


def test_persist_kpi_callback_writes_to_registry(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "b")

    class _PutDDB:
        def __init__(self):
            self.puts = []
        def put_item(self, **kw):
            self.puts.append(kw)

    ddb = _PutDDB()
    out = setup.prepare(
        ri_config=_cfg(), data_domain="sport", dataset="f1",
        runtime_session_id="sess-1", registry=(ddb, "okf-registry"),
        fetch_csv=lambda b, k: _csv(2),
    )
    out.persist_kpi(0, {"ex_score": 0.5, "iteration": 0})
    assert len(ddb.puts) == 1
    item = ddb.puts[0]["Item"]
    assert item["pk"] == {"S": "HARVEST#sport#f1"}
    assert item["sk"] == {"S": "BENCH#sess-1#0"}

import contextvars
import json
import time
from pathlib import Path

import harvest.entrypoint as ep
from harvest.clients import dataset_root
from harvest.finalize import finalize_bundle, mark_in_progress


def test_validate_rejects_missing_fields():
    assert ep.start_harvest({})["status"] == "rejected"
    assert ep.start_harvest({"data_domain": "d"})["status"] == "rejected"
    r = ep.start_harvest({"data_domain": "d", "dataset": "x", "mode": "incremental"})
    assert r["status"] == "rejected"
    assert "changed_table" in r["error"]


def test_model_config_from_payload_absent_is_none():
    # No model/effort in the payload -> None so the runner uses env defaults.
    assert ep._model_config_from_payload({"data_domain": "d", "dataset": "x"}) is None


def test_model_config_from_payload_builds_override(monkeypatch):
    monkeypatch.delenv("OKF_HARVEST_MAX_TOKENS", raising=False)
    cfg = ep._model_config_from_payload({"model": "openai.gpt-5.6-sol", "effort": "high"})
    assert cfg["model"] == "openai.gpt-5.6-sol"
    assert cfg["effort"] == "high"


def test_cleanup_removes_domain_subtree(monkeypatch, tmp_path):
    # Seed a mount with two domains; cleanup must remove only the target one.
    mount = tmp_path / "mnt"
    (mount / "health_care" / "toxicology" / ".harvest").mkdir(parents=True)
    (mount / "health_care" / "toxicology" / ".harvest" / "pending.json").write_text("x")
    (mount / "education" / "california_schools").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest({"mode": "cleanup", "data_domain": "health_care"})
    assert r["status"] == "cleaned"
    assert r["removed"] is True
    assert not (mount / "health_care").exists()
    assert (mount / "education").exists()  # other domain untouched


def test_cleanup_scoped_to_dataset(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "health_care" / "toxicology").mkdir(parents=True)
    (mount / "health_care" / "thrombosis_prediction").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest(
        {"mode": "cleanup", "data_domain": "health_care", "dataset": "toxicology"}
    )
    assert r["status"] == "cleaned" and r["removed"] is True
    assert not (mount / "health_care" / "toxicology").exists()
    assert (mount / "health_care" / "thrombosis_prediction").exists()  # sibling kept


def test_cleanup_absent_target_is_noop(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    r = ep.start_harvest({"mode": "cleanup", "data_domain": "ghost"})
    assert r["status"] == "cleaned" and r["removed"] is False


def test_cleanup_rejects_traversal_and_missing_domain(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "sport").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    # path traversal / separators in components are rejected before any removal
    assert (
        ep.start_harvest({"mode": "cleanup", "data_domain": ".."})["status"]
        == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "cleanup", "data_domain": "a/b"})["status"]
        == "rejected"
    )
    assert ep.start_harvest({"mode": "cleanup"})["status"] == "rejected"
    assert (mount / "sport").exists()  # nothing removed


def test_provision_creates_dataset_and_context_dirs(monkeypatch, tmp_path):
    # provision must create the dataset root AND .context/ through the mount so a
    # later out-of-band .context/ upload lands inside an already-writable tree.
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))

    r = ep.start_harvest(
        {"mode": "provision", "data_domain": "sport", "dataset": "spider2_ipl"}
    )
    assert r["status"] == "provisioned"
    assert (mount / "sport" / "spider2_ipl").is_dir()
    assert (mount / "sport" / "spider2_ipl" / ".context").is_dir()


def test_provision_is_idempotent(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "sport" / "spider2_ipl" / ".context").mkdir(parents=True)
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    # Re-provisioning an existing tree must succeed (exist_ok), not error.
    r = ep.start_harvest(
        {"mode": "provision", "data_domain": "sport", "dataset": "spider2_ipl"}
    )
    assert r["status"] == "provisioned"


def test_provision_rejects_traversal_and_missing_fields(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(ep, "MOUNT_PATH", str(mount))
    assert (
        ep.start_harvest({"mode": "provision", "data_domain": "sport"})["status"]
        == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "provision", "dataset": "x"})["status"] == "rejected"
    )
    assert (
        ep.start_harvest({"mode": "provision", "data_domain": "..", "dataset": "x"})[
            "status"
        ]
        == "rejected"
    )
    assert (
        ep.start_harvest(
            {"mode": "provision", "data_domain": "sport", "dataset": "a/b"}
        )["status"]
        == "rejected"
    )


def test_start_harvest_accepts_and_runs_background(monkeypatch):
    called = {}

    def fake_dispatch(payload, session_id=None):
        called["payload"] = payload
        called["session_id"] = session_id

    monkeypatch.setattr(ep, "_dispatch", fake_dispatch)
    r = ep.start_harvest(
        {"data_domain": "sales", "dataset": "orders"}, session_id="sid-1"
    )
    assert r["status"] == "accepted"
    assert r["dataset"] == "orders"
    # let the daemon thread run
    for _ in range(50):
        if "payload" in called:
            break
        time.sleep(0.01)
    assert called["payload"]["dataset"] == "orders"
    # The run's session id is threaded into the crawl (used to correlate the feed).
    assert called["session_id"] == "sid-1"
    # busy flag clears after the job finishes
    for _ in range(50):
        if not ep.is_busy():
            break
        time.sleep(0.01)
    assert ep.is_busy() is False


def test_crawl_thread_inherits_context(monkeypatch):
    # The crawl runs on a background thread; OTEL context lives in contextvars,
    # which a bare threading.Thread does NOT inherit. start_harvest must copy the
    # current context into the worker (contextvars.copy_context + ctx.run) so the
    # crawl's spans stay parented under the invoke span. Stand-in for the OTEL
    # span/baggage: a ContextVar set before the call must be visible in _dispatch.
    marker = contextvars.ContextVar("okf_trace_marker", default=None)
    seen = {}

    def fake_dispatch(payload, session_id=None):
        seen["marker"] = marker.get()

    monkeypatch.setattr(ep, "_dispatch", fake_dispatch)
    marker.set("trace-abc")
    ep.start_harvest({"data_domain": "sales", "dataset": "orders"})
    for _ in range(50):
        if "marker" in seen:
            break
        time.sleep(0.01)
    assert seen["marker"] == "trace-abc", "crawl thread lost the OTEL context"


def test_dataset_root_layout():
    assert dataset_root("/mnt/data", "sales", "orders") == "/mnt/data/sales/orders"


def test_finalize_writes_commit_marker_last(tmp_path):
    # a minimal bundle
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "races.md").write_text(
        "---\ntype: Glue Table\ntitle: Races\ndescription: d\ntimestamp: t\n---\n\nbody\n"
    )
    state = finalize_bundle(
        tmp_path,
        data_domain="sales",
        dataset="orders",
        tables=["races"],
        timestamp="2026-07-01T00:00:00Z",
        table_versions={"races": "1"},
    )
    marker = tmp_path / ".harvest" / "state.json"
    assert marker.is_file()
    doc = json.loads(marker.read_text())
    assert doc["status"] == "complete"
    assert doc["tables"] == ["races"]
    assert doc["table_versions"] == {"races": "1"}
    # index.md regenerated
    assert (tmp_path / "index.md").is_file()
    assert (tmp_path / "tables" / "index.md").is_file()


def test_mark_in_progress_then_complete(tmp_path):
    mark_in_progress(tmp_path, data_domain="s", dataset="o", timestamp="t0")
    marker = tmp_path / ".harvest" / "state.json"
    assert json.loads(marker.read_text())["status"] == "in_progress"
    finalize_bundle(tmp_path, data_domain="s", dataset="o", tables=[], timestamp="t1")
    assert json.loads(marker.read_text())["status"] == "complete"

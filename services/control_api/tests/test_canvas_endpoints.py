"""The canvas (dashboard) serving surface: per-user list/get/delete, the
human verify/unverify flips (conditional, 409 on wrong state), and tile
execution through the computation Run path with dial overrides.

Rows are seeded directly (the canvas CI runtime owns the writes in
production — this API never accepts a config body).
"""

from __future__ import annotations

import json

import pytest

from control_api import handlers
from control_api.handlers import ApiError
from okf_core import canvas as cv

from tests.conftest import BUCKET, CANVAS, REGISTRY
from tests.test_computation_endpoints import _COMP_DOC, FakeAthena

DOMAIN, DATASET = "sales", "orders"
SUB, OTHER_SUB = "user-1", "user-2"

CONFIG = {
    "canvas_version": 1,
    "title": "Revenue",
    "data_domain": DOMAIN,
    "dataset": DATASET,
    "dials": [{"name": "region", "label": "Region", "default": "EMEA"}],
    "tiles": [
        {
            "id": "rev",
            "title": "Revenue",
            "widget": "kpi",
            "data": {
                "computation": "revenue_by_region",
                "bind": {"region": {"dial": "region"}},
            },
            "encoding": {"value": {"field": "revenue", "format": "number"}},
            "layout": {"x": 0, "y": 0, "w": 4, "h": 3},
        }
    ],
}


def _seed_canvas(
    ddb,
    *,
    canvas_id: str = "c1",
    sub: str = SUB,
    status: str = cv.STATUS_CREATED,
    config: dict | None = None,
    updated_at: str = "2026-08-14T10:00:00+00:00",
) -> None:
    ddb.put_item(
        TableName=CANVAS,
        Item={
            "pk": {"S": cv.canvas_pk(sub)},
            "sk": {"S": canvas_id},
            "canvas_id": {"S": canvas_id},
            "title": {"S": "Revenue"},
            "data_domain": {"S": DOMAIN},
            "dataset": {"S": DATASET},
            "status": {"S": status},
            "config": {"S": json.dumps(config or CONFIG)},
            "created_at": {"S": "2026-08-14T09:00:00+00:00"},
            "updated_at": {"S": updated_at},
        },
    )


def _seed_bundle(cfg) -> None:
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/computations/revenue_by_region.md",
        Body=_COMP_DOC.encode(),
    )
    cfg.ddb.put_item(
        TableName=REGISTRY,
        Item={
            "pk": {"S": f"DOMAIN#{DOMAIN}"},
            "sk": {"S": f"DATASET#{DATASET}"},
            "glue_database": {"S": "shop"},
        },
    )


def test_list_is_caller_scoped_and_newest_first(cfg):
    _seed_canvas(cfg.ddb, canvas_id="old", updated_at="2026-08-13T10:00:00+00:00")
    _seed_canvas(cfg.ddb, canvas_id="new", updated_at="2026-08-14T10:00:00+00:00")
    _seed_canvas(cfg.ddb, canvas_id="theirs", sub=OTHER_SUB)
    out = handlers.list_canvases(cfg.ddb, canvas_table=CANVAS, user_sub=SUB)
    assert [c["canvas_id"] for c in out] == ["new", "old"]
    # Meta only — the config never rides the list.
    assert "config" not in out[0]


def test_get_returns_config_and_isolates(cfg):
    _seed_canvas(cfg.ddb)
    out = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1"
    )
    assert out["status"] == cv.STATUS_CREATED
    assert out["config"]["tiles"][0]["id"] == "rev"
    # Another caller's key can't see it — structural isolation via the pk.
    with pytest.raises(ApiError) as ei:
        handlers.get_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=OTHER_SUB, canvas_id="c1"
        )
    assert ei.value.status == 404


def test_delete(cfg):
    _seed_canvas(cfg.ddb)
    handlers.delete_canvas(cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1")
    with pytest.raises(ApiError):
        handlers.get_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1"
        )


def test_verify_unverify_flips_and_races(cfg):
    _seed_canvas(cfg.ddb)  # created
    out = handlers.verify_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        verified_by="analyst@example.com",
    )
    assert out["status"] == cv.STATUS_VERIFIED
    assert out["verified_by"] == "analyst@example.com"
    # Verifying an already-verified canvas is a 409 naming the actual state.
    with pytest.raises(ApiError) as ei:
        handlers.verify_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
            verified_by="analyst@example.com",
        )
    assert ei.value.status == 409 and "verified" in str(ei.value)
    out = handlers.unverify_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1"
    )
    assert out["status"] == cv.STATUS_CREATED
    assert out["verified_by"] is None


def test_verify_requires_identity_and_existing_row(cfg):
    _seed_canvas(cfg.ddb)
    with pytest.raises(ApiError) as ei:
        handlers.verify_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
            verified_by="",
        )
    assert ei.value.status == 401
    with pytest.raises(ApiError) as ei:
        handlers.verify_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="ghost",
            verified_by="analyst@example.com",
        )
    assert ei.value.status == 404


def test_run_tile_executes_with_dial_override(cfg):
    _seed_canvas(cfg.ddb)
    _seed_bundle(cfg)
    athena = FakeAthena()
    # Default dial state (EMEA).
    out = handlers.run_canvas_tile(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        canvas_table=CANVAS, user_sub=SUB, canvas_id="c1", tile_id="rev",
        dials=None, athena=athena, enabled=True,
    )
    assert out["executed"] is True and out["rows"] == [["42.5"]]
    assert "region = 'EMEA'" in out["executed_sql"]
    # A dial override rides into the bound parameter.
    out = handlers.run_canvas_tile(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        canvas_table=CANVAS, user_sub=SUB, canvas_id="c1", tile_id="rev",
        dials={"region": "NA"}, athena=athena, enabled=True,
    )
    assert "region = 'NA'" in out["executed_sql"]


def test_run_tile_refusals(cfg):
    _seed_canvas(cfg.ddb)
    _seed_bundle(cfg)
    athena = FakeAthena()
    common = dict(
        bucket=BUCKET, registry_table=REGISTRY, canvas_table=CANVAS,
        user_sub=SUB, athena=athena, enabled=True,
    )
    with pytest.raises(ApiError) as ei:
        handlers.run_canvas_tile(
            cfg.s3, cfg.ddb, canvas_id="c1", tile_id="ghost", dials=None, **common
        )
    assert ei.value.status == 404
    with pytest.raises(ApiError) as ei:
        handlers.run_canvas_tile(
            cfg.s3, cfg.ddb, canvas_id="c1", tile_id="rev",
            dials={"ghost": 1}, **common,
        )
    assert ei.value.status == 400 and "unknown dial" in str(ei.value)
    # A dial value outside the computation's declared enum is CONTRACT — the
    # Run path 400s and the query never starts.
    with pytest.raises(ApiError) as ei:
        handlers.run_canvas_tile(
            cfg.s3, cfg.ddb, canvas_id="c1", tile_id="rev",
            dials={"region": "MOON"}, **common,
        )
    assert ei.value.status == 400
    assert athena.started == []


def test_run_tile_disabled_returns_rendered_receipt(cfg):
    _seed_canvas(cfg.ddb)
    _seed_bundle(cfg)
    out = handlers.run_canvas_tile(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        canvas_table=CANVAS, user_sub=SUB, canvas_id="c1", tile_id="rev",
        dials=None, enabled=False,
    )
    assert out["executed"] is False
    assert "region = 'EMEA'" in out["executed_sql"]


def test_list_canvas_jobs_scoped_and_filtered(cfg):
    import okf_core.canvas as cvv

    def _job(job_id, status, sub=SUB, created="2026-08-14T10:00:00+00:00", thread=""):
        item = {
            "pk": {"S": cvv.canvas_job_pk(sub)},
            "sk": {"S": job_id},
            "job_id": {"S": job_id},
            "status": {"S": status},
            "request": {"S": "r"},
            "data_domain": {"S": DOMAIN},
            "dataset": {"S": DATASET},
            "steps": {"S": '[{"t":"t","type":"tool","label":"lint_canvas"}]'},
            "created_at": {"S": created},
            "updated_at": {"S": created},
        }
        if thread:
            item["thread_id"] = {"S": thread}
        cfg.ddb.put_item(TableName=CANVAS, Item=item)

    _job("j1", "running", created="2026-08-14T11:00:00+00:00", thread="t1")
    _job("j2", "submitted", thread="t2")
    _job("j3", "running", sub=OTHER_SUB, thread="t1")
    out = handlers.list_canvas_jobs(cfg.ddb, canvas_table=CANVAS, user_sub=SUB)
    assert [j["job_id"] for j in out] == ["j1", "j2"]  # newest first, own only
    assert out[0]["steps"][0]["label"] == "lint_canvas"
    active = handlers.list_canvas_jobs(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, active_only=True
    )
    assert [j["job_id"] for j in active] == ["j1"]
    # The chat-scoped view: only the jobs THAT conversation delegated.
    by_thread = handlers.list_canvas_jobs(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, thread_id="t2"
    )
    assert [j["job_id"] for j in by_thread] == ["j2"]
    assert by_thread[0]["thread_id"] == "t2"
    both = handlers.list_canvas_jobs(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, active_only=True, thread_id="t2"
    )
    assert both == []


def test_list_canvas_jobs_returns_only_the_newest_page(cfg, monkeypatch):
    import okf_core.canvas as cvv

    monkeypatch.setattr(handlers, "_CANVAS_JOBS_LIMIT", 2)
    for i in range(4):
        ts = f"2026-08-1{i}T10:00:00+00:00"
        cfg.ddb.put_item(TableName=CANVAS, Item={
            "pk": {"S": cvv.canvas_job_pk(SUB)}, "sk": {"S": f"j{i}"},
            "job_id": {"S": f"j{i}"}, "status": {"S": "completed"},
            "created_at": {"S": ts}, "updated_at": {"S": ts},
        })
    out = handlers.list_canvas_jobs(cfg.ddb, canvas_table=CANVAS, user_sub=SUB)
    assert [j["job_id"] for j in out] == ["j3", "j2"]


def test_run_tile_adhoc_sql(cfg):
    config = json.loads(json.dumps(CONFIG))
    config["tiles"][0]["data"] = {
        "sql": "SELECT SUM(amount) AS revenue FROM orders WHERE region = @region",
        "parameters": [
            {"name": "region", "type": "string", "required": True,
             "example": "EMEA", "enum": ["EMEA", "NA"]},
        ],
        "bind": {"region": {"dial": "region"}},
    }
    _seed_canvas(cfg.ddb, canvas_id="adhoc1", config=config)
    _seed_bundle(cfg)
    athena = FakeAthena()
    out = handlers.run_canvas_tile(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        canvas_table=CANVAS, user_sub=SUB, canvas_id="adhoc1", tile_id="rev",
        dials={"region": "NA"}, athena=athena, enabled=True,
    )
    # The synthesized inline computation executes with the SAME typed
    # substitution as a bundle one — and the same contract refusals.
    assert out["executed"] is True
    assert "region = 'NA'" in out["executed_sql"]
    with pytest.raises(ApiError) as ei:
        handlers.run_canvas_tile(
            cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
            canvas_table=CANVAS, user_sub=SUB, canvas_id="adhoc1", tile_id="rev",
            dials={"region": "MOON"}, athena=athena, enabled=True,
        )
    assert ei.value.status == 400


def test_run_tile_refuses_invalid_stored_config(cfg):
    _seed_canvas(cfg.ddb, canvas_id="bad", config={"canvas_version": 1})
    with pytest.raises(ApiError) as ei:
        handlers.run_canvas_tile(
            cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
            canvas_table=CANVAS, user_sub=SUB, canvas_id="bad", tile_id="rev",
            dials=None, enabled=False,
        )
    assert ei.value.status == 500 and "fails validation" in str(ei.value)


def test_layout_patch_moves_geometry_and_keeps_status(cfg):
    _seed_canvas(cfg.ddb, status=cv.STATUS_VERIFIED)
    out = handlers.update_canvas_layout(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        layouts={"rev": {"x": 8, "y": 2, "w": 4, "h": 3}},
    )
    # Geometry moved; the human's attestation survives (layout can't change
    # what the numbers mean); updated_at bumped.
    assert out["config"]["tiles"][0]["layout"] == {"x": 8, "y": 2, "w": 4, "h": 3}
    assert out["status"] == cv.STATUS_VERIFIED
    assert out["updated_at"] > "2026-08-14T10:00:00+00:00"
    stored = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1"
    )
    assert stored["config"]["tiles"][0]["layout"]["x"] == 8
    assert stored["status"] == cv.STATUS_VERIFIED
    # Data stayed structurally untouched.
    assert stored["config"]["tiles"][0]["data"]["computation"] == "revenue_by_region"


def test_layout_patch_refusals(cfg):
    _seed_canvas(cfg.ddb)
    # Unknown tile and off-grid geometry are 400s; a foreign canvas is a 404.
    with pytest.raises(ApiError) as e:
        handlers.update_canvas_layout(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
            layouts={"ghost": {"x": 0, "y": 0, "w": 4, "h": 3}},
        )
    assert e.value.status == 400 and "unknown tile" in e.value.message
    with pytest.raises(ApiError) as e:
        handlers.update_canvas_layout(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
            layouts={"rev": {"x": 10, "y": 0, "w": 6, "h": 3}},
        )
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        handlers.update_canvas_layout(
            cfg.ddb, canvas_table=CANVAS, user_sub=OTHER_SUB, canvas_id="c1",
            layouts={"rev": {"x": 0, "y": 0, "w": 4, "h": 3}},
        )
    assert e.value.status == 404


def test_title_patch_renames_row_and_config_and_keeps_status(cfg):
    _seed_canvas(cfg.ddb, status=cv.STATUS_VERIFIED)
    out = handlers.update_canvas_title(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        title="  Season revenue overview  ",
    )
    # Trimmed; row attr and config.title move TOGETHER (list/describe read
    # the row, the page reads the config — they must not drift); the human's
    # attestation survives (a display name can't change what numbers mean).
    assert out["title"] == "Season revenue overview"
    assert out["config"]["title"] == "Season revenue overview"
    assert out["status"] == cv.STATUS_VERIFIED
    assert out["updated_at"] > "2026-08-14T10:00:00+00:00"
    stored = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1"
    )
    assert stored["title"] == "Season revenue overview"
    assert stored["config"]["title"] == "Season revenue overview"
    assert stored["status"] == cv.STATUS_VERIFIED
    # Everything but the title stayed structurally untouched.
    assert stored["config"]["tiles"][0]["data"]["computation"] == "revenue_by_region"


def test_title_patch_refusals(cfg):
    _seed_canvas(cfg.ddb)
    # Empty / non-string / over-cap titles are 400s; a foreign canvas is a 404.
    for bad in ["", "   ", None, 42, "x" * (cv.MAX_TITLE + 1)]:
        with pytest.raises(ApiError) as e:
            handlers.update_canvas_title(
                cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
                title=bad,
            )
        assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        handlers.update_canvas_title(
            cfg.ddb, canvas_table=CANVAS, user_sub=OTHER_SUB, canvas_id="c1",
            title="Mine now",
        )
    assert e.value.status == 404


def test_get_canvas_enriches_adhoc_params_with_observed_domains(cfg):
    # An ad-hoc tile whose param declares a `column` binding gets the profile
    # evidence (domains.json) attached as advisory `observed` values — the
    # dial bar's type-ahead. Params without a column stay bare.
    config = dict(CONFIG)
    config["tiles"] = [
        {
            "id": "wins",
            "title": "Wins by driver",
            "widget": "bar",
            "data": {
                "sql": "SELECT driver, wins FROM results WHERE driver = @driver",
                "parameters": [
                    {"name": "driver", "type": "string", "column": "results.driver",
                     "example": "Michael Schumacher"},
                    {"name": "season", "type": "integer", "example": 2004},
                ],
                "bind": {"driver": {"dial": "driver"}, "season": {"value": 2004}},
            },
            "encoding": {"x": {"field": "driver"}, "y": {"field": "wins"}},
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
        }
    ]
    _seed_canvas(cfg.ddb, canvas_id="c7", config=config)
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/.metadata/profile/domains.json",
        Body=json.dumps(
            {
                "tables": {
                    "results": {
                        "columns": {
                            "driver": {
                                "values": ["Michael Schumacher", "Ayrton Senna"],
                                "distinct": 2,
                                "exhaustive": True,
                            }
                        }
                    }
                }
            }
        ).encode(),
    )
    out = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c7",
        s3=cfg.s3, bundle_bucket=BUCKET,
    )
    assert out["observed"]["wins"]["driver"] == {
        "values": ["Michael Schumacher", "Ayrton Senna"],
        "exhaustive": True,
    }
    assert "season" not in out["observed"]["wins"]  # no column binding
    # Without the bundle clients (older callers), the payload is unchanged.
    bare = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c7"
    )
    assert "observed" not in bare


class _DomainAthena(FakeAthena):
    """A DISTINCT result set with oversized text values (one column)."""

    def __init__(self, values):
        super().__init__()
        self.values = values

    def get_query_results(self, **kwargs):
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Label": "name"}]},
                "Rows": [{"Data": [{"VarCharValue": "name"}]}]
                + [{"Data": [{"VarCharValue": v}]} for v in self.values],
            }
        }


def test_column_domain_caches_and_byte_budgets(cfg, monkeypatch):
    _seed_bundle(cfg)
    athena = _DomainAthena(["alpha", "beta", "gamma"])
    kwargs = dict(
        registry_table=REGISTRY, canvas_table=CANVAS,
        data_domain=DOMAIN, dataset=DATASET, column="drivers.name",
        athena=athena, athena_output="s3://results/x/",
    )
    out = handlers.get_column_domain(cfg.ddb, now=1000.0, **kwargs)
    assert out["values"] == ["alpha", "beta", "gamma"]
    assert out["exhaustive"] is True and out["cached"] is False
    # Second call inside the TTL rides the cache — no new engine query.
    ran = len(athena.started)
    out = handlers.get_column_domain(cfg.ddb, now=2000.0, **kwargs)
    assert out["cached"] is True and len(athena.started) == ran
    # A DDB item caps at 400KB: oversized value lists are TRIMMED to the byte
    # budget (advisory type-ahead — fewer suggestions beat a ValidationException)
    # and marked non-exhaustive.
    monkeypatch.setattr(handlers, "_DOMAIN_CACHE_MAX_BYTES", 15)
    out = handlers.get_column_domain(
        cfg.ddb, now=1000.0, **{**kwargs, "column": "drivers.bio"}
    )
    assert out["values"] == ["alpha"]
    assert out["exhaustive"] is False


def test_column_domain_survives_a_failed_cache_write(cfg):
    _seed_bundle(cfg)

    class _NoPutDdb:
        """Delegates to the real client but refuses the cache put — the
        caller must still get the values (cache is an optimization)."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def put_item(self, **kwargs):
            raise RuntimeError("simulated DDB failure")

    out = handlers.get_column_domain(
        _NoPutDdb(cfg.ddb),
        registry_table=REGISTRY, canvas_table=CANVAS,
        data_domain=DOMAIN, dataset=DATASET, column="drivers.name",
        athena=_DomainAthena(["alpha"]), athena_output="s3://results/x/",
        now=1000.0,
    )
    assert out["values"] == ["alpha"] and out["cached"] is False


# --- verification pins the computations' content hashes ----------------------


def test_verify_pins_hashes_and_a_changed_statement_voids_it(cfg):
    _seed_bundle(cfg)
    _seed_canvas(cfg.ddb)
    handlers.verify_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        verified_by="analyst@example.com", s3=cfg.s3, bundle_bucket=BUCKET,
    )
    still = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        s3=cfg.s3, bundle_bucket=BUCKET,
    )
    assert still["status"] == cv.STATUS_VERIFIED  # unchanged SQL keeps it

    # A harvest re-authors the statement behind the tile.
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/computations/revenue_by_region.md",
        Body=_COMP_DOC.replace("SUM(", "AVG(").encode(),
    )
    out = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        s3=cfg.s3, bundle_bucket=BUCKET,
    )
    assert out["status"] == cv.STATUS_CREATED
    assert out["verification_reset"]["changed_computations"] == ["revenue_by_region"]
    row = cfg.ddb.get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": "c1"}},
    )["Item"]
    assert row["status"]["S"] == cv.STATUS_CREATED
    assert "verified_computations" not in row and "verified_by" not in row


def test_verify_refuses_when_a_bound_computation_is_gone(cfg):
    _seed_canvas(cfg.ddb)  # no bundle doc seeded
    with pytest.raises(ApiError) as e:
        handlers.verify_canvas(
            cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
            verified_by="analyst@example.com", s3=cfg.s3, bundle_bucket=BUCKET,
        )
    assert e.value.status == 409 and "revenue_by_region" in str(e.value)


def test_unpinned_legacy_verification_is_voided_on_read(cfg):
    _seed_bundle(cfg)
    _seed_canvas(cfg.ddb, status=cv.STATUS_VERIFIED)  # no verified_computations
    out = handlers.get_canvas(
        cfg.ddb, canvas_table=CANVAS, user_sub=SUB, canvas_id="c1",
        s3=cfg.s3, bundle_bucket=BUCKET,
    )
    assert out["status"] == cv.STATUS_CREATED


def test_column_domain_never_queries_with_execution_off(cfg):
    _seed_bundle(cfg)
    athena = _DomainAthena(["alpha"])
    out = handlers.get_column_domain(
        cfg.ddb, registry_table=REGISTRY, canvas_table=CANVAS,
        data_domain=DOMAIN, dataset=DATASET, column="drivers.name",
        athena=athena, athena_output="s3://results/x/", enabled=False,
    )
    assert out["values"] == [] and out["executed"] is False
    assert athena.started == []


def test_column_domain_route_forwards_the_lambda_safe_timeout_and_gate(cfg, monkeypatch):
    from control_api import app

    seen = {}
    monkeypatch.setattr(
        handlers, "get_column_domain", lambda ddb, **kw: seen.update(kw) or {}
    )
    cfg.computation_timeout_s = 25.0
    cfg.computations_enabled = False

    class Caller:
        sub = SUB
        ident = "analyst@example.com"

    app._r_column_domain(
        cfg, {"domain": DOMAIN, "dataset": DATASET}, None,
        {"column": "drivers.name"}, Caller(),
    )
    assert seen["timeout_s"] == 25.0 and seen["enabled"] is False

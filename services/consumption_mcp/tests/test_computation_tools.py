"""Attested Computation tools (list / describe / run): parsing over S3,
verification overlay merging, execution gating, and the receipt contract."""

from __future__ import annotations

import json

import pytest

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

from .conftest import BUNDLE_BUCKET, DATASET, DOMAIN, REGISTRY_TABLE
from .fakes import FakeBedrock, FakeS3Vectors

_PREFIX = f"okf/{DOMAIN}/{DATASET}/"

_COMP_DOC = (
    "---\n"
    "type: Attested Computation\n"
    "title: Season points\n"
    "description: Total points for one season.\n"
    "runtime: athena\n"
    "parameters:\n"
    "  - {name: season, type: integer, required: true, example: 2024,\n"
    "     column: races.season}\n"
    "verified: null\n"
    "verified_by: null\n"
    "timestamp: t\n"
    "---\n\n"
    "Reads [results](../../tables/results.md).\n\n"
    "# Computation\n\n"
    "```sql\n"
    "SELECT SUM(r.points) AS total\n"
    "FROM results r JOIN races ra ON r.raceid = ra.raceid\n"
    "WHERE ra.season = @season\n"
    "```\n"
)

_BROKEN_DOC = (
    "---\ntype: Attested Computation\ntitle: B\ndescription: d\n"
    "runtime: athena\nparameters: []\ntimestamp: t\n---\n\n"
    "# Computation\n\n```sql\nDELETE FROM races\n```\n"
)

_DOMAINS = {
    "version": 1,
    "tables": {
        "races": {
            "profiled_at": "t",
            "columns": {
                "season": {"values": ["2023", "2024"], "distinct": 2, "exhaustive": True}
            },
        }
    },
}


def _seed(s3, *, broken=False, overlay=None):
    s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"{_PREFIX}references/computations/season_points.md",
        Body=_COMP_DOC.encode(),
    )
    s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"{_PREFIX}.metadata/profile/domains.json",
        Body=json.dumps(_DOMAINS).encode(),
    )
    if broken:
        s3.put_object(
            Bucket=BUNDLE_BUCKET,
            Key=f"{_PREFIX}references/computations/broken.md",
            Body=_BROKEN_DOC.encode(),
        )
    if overlay is not None:
        s3.put_object(
            Bucket=BUNDLE_BUCKET,
            Key=f"verification/{DOMAIN}/{DATASET}.json",
            Body=json.dumps({"version": 1, "entries": overlay}).encode(),
        )


class FakeAthena:
    """Instant-SUCCEEDED Athena stub returning one page of rows."""

    def __init__(self, rows=(("1025.0",),)):
        self.started: list[dict] = []
        self._rows = rows

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": 12, "EngineExecutionTimeInMillis": 5},
            }
        }

    def get_query_results(self, **kwargs):
        rows = [{"Data": [{"VarCharValue": "total"}]}]
        rows += [{"Data": [{"VarCharValue": v} for v in r]} for r in self._rows]
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Label": "total"}]},
                "Rows": rows,
            }
        }


def _exec_tools(aws, athena=None, enabled=True):
    config = ConsumptionConfig(
        bundle_bucket=BUNDLE_BUCKET,
        vector_bucket="okf-vectors",
        vector_index="okf-index",
        registry_table=REGISTRY_TABLE,
        computations_enabled=enabled,
    )
    return ConsumptionTools(
        s3=aws["s3"],
        s3vectors=FakeS3Vectors(),
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
        athena=athena,
    )


def test_list_computations_with_invalid_surfaced(tools, aws):
    _seed(aws["s3"], broken=True)
    out = tools.list_computations(DOMAIN, DATASET)
    assert [c["computation"] for c in out["computations"]] == ["season_points"]
    entry = out["computations"][0]
    assert entry["verification"] == "unverified"
    assert entry["parameters"][0]["name"] == "season"
    # Broken docs are surfaced by name + first error, never silently dropped.
    assert out["invalid"][0]["computation"] == "broken"


def test_list_merges_overlay_verification(tools, aws):
    from okf_core.computations import parse_computation_text

    comp, _ = parse_computation_text(
        "references/computations/season_points.md", _COMP_DOC
    )
    _seed(
        aws["s3"],
        overlay={
            "season_points": {
                "slug": "season_points",
                "sha256": comp.sha256,
                "verified": "2026-08-14T09:30:00Z",
                "verified_by": "analyst@example.com",
            }
        },
    )
    out = tools.list_computations(DOMAIN, DATASET)
    entry = out["computations"][0]
    assert entry["verification"] == "verified"
    assert entry["verified_by"] == "analyst@example.com"
    # A later doc edit (hash mismatch) surfaces as stale, never hidden.
    _seed(
        aws["s3"],
        overlay={
            "season_points": {
                "slug": "season_points",
                "sha256": "b" * 64,
                "verified": "2026-08-14T09:30:00Z",
                "verified_by": "analyst@example.com",
            }
        },
    )
    out = tools.list_computations(DOMAIN, DATASET)
    assert out["computations"][0]["verification"] == "stale"


def test_describe_computation(tools, aws):
    _seed(aws["s3"])
    d = tools.describe_computation("season_points", DOMAIN, DATASET)
    assert d["runtime"] == "athena"
    assert "@season" in d["sql"]
    assert d["parameters"][0]["example"] == 2024
    missing = tools.describe_computation("nope", DOMAIN, DATASET)
    assert "no computation named" in missing["error"]


def test_run_disabled_returns_rendered_sql(tools, aws):
    # The default conftest `tools` has computations_enabled=False.
    _seed(aws["s3"])
    out = tools.run_computation("season_points", DOMAIN, DATASET, {"season": 2024})
    assert out["executed"] is False
    assert "not enabled" in out["note"]
    assert "ra.season = 2024" in out["executed_sql"]
    assert out["verification"] == "unverified"


def test_run_executes_on_athena_with_mapping_database(aws):
    _seed(aws["s3"])
    athena = FakeAthena()
    tools = _exec_tools(aws, athena=athena)
    out = tools.run_computation("season_points", DOMAIN, DATASET, {"season": 2024})
    assert out["executed"] is True
    assert out["rows"] == [["1025.0"]]
    assert out["engine_query_id"] == "q-1"
    assert out["computation_sha256"]
    # The engine ran the substituted statement against the MAPPING's database.
    started = athena.started[0]
    assert "ra.season = 2024" in started["QueryString"]
    assert (
        started["QueryExecutionContext"]["Database"] == "na_mi_formula_1_curated"
    )
    assert out["warnings"] == []  # 2024 is an observed value


def test_run_refuses_bad_values_with_corrective_error(aws):
    _seed(aws["s3"])
    tools = _exec_tools(aws, athena=FakeAthena())
    out = tools.run_computation("season_points", DOMAIN, DATASET, {"season": "abc"})
    assert "not an integer" in out["error"]
    assert out["parameters"][0]["name"] == "season"
    out = tools.run_computation("season_points", DOMAIN, DATASET, {})
    assert "missing required parameter" in out["error"]


def test_run_warns_on_unobserved_value_and_zero_rows_hint(aws):
    _seed(aws["s3"])
    athena = FakeAthena(rows=())
    tools = _exec_tools(aws, athena=athena)
    out = tools.run_computation("season_points", DOMAIN, DATASET, {"season": 1999})
    assert out["executed"] is True
    assert out["row_count"] == 0
    assert any("1999" in w for w in out["warnings"])
    assert "typo" in out["note"]


def test_run_survives_engine_failure_with_receipt(aws):
    _seed(aws["s3"])

    class BoomAthena:
        def start_query_execution(self, **kwargs):
            raise RuntimeError("AccessDenied while IAM propagates")

    tools = _exec_tools(aws, athena=BoomAthena())
    out = tools.run_computation("season_points", DOMAIN, DATASET, {"season": 2024})
    assert out["executed"] is False
    assert "did not execute" in out["note"]
    assert "ra.season = 2024" in out["executed_sql"]


def test_slug_traversal_is_refused(tools, aws):
    _seed(aws["s3"])
    out = tools.run_computation("../.harvest/state", DOMAIN, DATASET, {})
    assert "error" in out

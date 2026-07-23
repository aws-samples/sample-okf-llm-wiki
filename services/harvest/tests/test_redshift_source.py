"""RedshiftSource: concept enumeration, metadata shape, sampling, and the
Source-protocol contract — driven by an in-memory Redshift Data API fake."""

import pytest

from harvest.metadata_export import METADATA_DIR, export_metadata
from harvest.redshift_source import RedshiftSource
from harvest.source_base import Source
from tests.fakes import FakeRedshiftData, f1_like_redshift


def _source(data=None, **kw):
    return RedshiftSource(
        database="dev",
        data=data if data is not None else f1_like_redshift(),
        cluster_identifier="f1-cluster",
        db_user="admin",
        region="us-east-1",
        account_id="123456789012",
        **kw,
    )


def test_requires_cluster_or_workgroup():
    with pytest.raises(ValueError):
        RedshiftSource(database="dev", data=f1_like_redshift())


def test_is_a_source():
    # Structural (runtime_checkable) conformance to the harvest Source protocol.
    assert isinstance(_source(), Source)
    assert _source().name == "redshift"


def test_list_concepts_yields_db_native_and_external():
    src = _source()
    by_id = {c.id_str: c for c in src.list_concepts()}
    assert by_id["datasets/dev"].type == "Redshift Database"
    assert by_id["tables/public.races"].type == "Redshift Table"
    assert by_id["tables/spectrum.results_ext"].type == "Redshift External Table"


def test_table_names_are_schema_qualified():
    assert set(_source().table_names()) == {
        "public.races",
        "spectrum.results_ext",
    }


def test_resource_uris_connection_form():
    src = _source()
    db = src.find(("datasets", "dev"))
    assert db.resource == "redshift://f1-cluster:5439/dev"
    races = src.find(("tables", "public.races"))
    assert races.resource == "redshift://f1-cluster:5439/dev#public.races"


def test_serverless_resource_uri():
    src = RedshiftSource(
        database="dev",
        data=f1_like_redshift(),
        workgroup_name="wg1",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
        region="us-east-1",
        account_id="123456789012",
    )
    db = src.find(("datasets", "dev"))
    assert db.resource == (
        "redshift://wg1.123456789012.us-east-1.redshift-serverless.amazonaws.com"
        ":5439/dev"
    )


def test_read_concept_native_table_schema_and_design():
    src = _source()
    meta = src.read_concept(src.find(("tables", "public.races")))
    assert meta["table"] == "public.races"
    assert meta["table_type"] == "TABLE"
    names = [f["name"] for f in meta["flat_schema"]]
    assert names == ["raceid", "name"]
    # varchar length is rendered as a type suffix.
    types = {f["name"]: f["type"] for f in meta["flat_schema"]}
    assert types["name"] == "character varying(255)"
    # SVV_TABLE_INFO design + scan-free row count land in parameters. Cells come
    # back as strings (the run_query contract stringifies every value).
    assert meta["parameters"]["diststyle"] == "KEY(raceid)"
    assert meta["parameters"]["tbl_rows"] == "976"


def test_read_concept_external_table_location_and_partitions():
    src = _source()
    meta = src.read_concept(src.find(("tables", "spectrum.results_ext")))
    assert meta["table_type"] == "EXTERNAL TABLE"
    assert meta["location"] == "s3://fake-f1/results/"
    # Partition keys come from SVV_EXTERNAL_COLUMNS (part_key > 0).
    part_names = [f["name"] for f in meta["flat_partition_schema"]]
    assert part_names == ["season"]
    # Externals have no SVV_TABLE_INFO row -> no scan-free params.
    assert meta["parameters"] == {}


def test_database_concept_metadata():
    meta = _source().read_concept(_source().find(("datasets", "dev")))
    assert meta["database"] == "dev"
    assert meta["table_count"] == 2
    assert meta["resource"] == "redshift://f1-cluster:5439/dev"


def test_run_query_null_vs_empty_string():
    # A SQL NULL comes back as None; an empty string stays "" (the contract shared
    # with GlueAthenaSource).
    data = FakeRedshiftData(
        [(lambda sql: "SELECT" in sql, [{"a": None, "b": "", "c": "x"}])]
    )
    rows = _source(data=data).run_query("SELECT a, b, c FROM t")
    assert rows == [{"a": None, "b": "", "c": "x"}]


def test_run_query_coerces_scalar_types():
    data = FakeRedshiftData(
        [(lambda sql: "SELECT" in sql, [{"n": 976, "f": 1.5, "flag": True}])]
    )
    rows = _source(data=data).run_query("SELECT n, f, flag FROM t")
    assert rows == [{"n": "976", "f": "1.5", "flag": "true"}]


def test_sample_rows_native_table_issues_limit_query():
    data = f1_like_redshift()
    # Add a handler for the sample SELECT so it returns a row.
    data._handlers.append(
        (lambda sql: sql.startswith("SELECT *"), [{"raceid": "1", "name": "A"}])
    )
    src = _source(data=data)
    rows = src.sample_rows(src.find(("tables", "public.races")), n=5)
    assert rows == [{"raceid": "1", "name": "A"}]
    assert any(
        s.startswith('SELECT * FROM "public"."races" LIMIT 5') for s in data.executed
    )


def test_sample_rows_none_for_database_concept():
    src = _source()
    assert src.sample_rows(src.find(("datasets", "dev"))) is None


def test_run_query_raises_on_failed_statement():
    data = FakeRedshiftData(
        [(lambda sql: True, [{"x": "1"}])], status="FAILED"
    )
    with pytest.raises(RuntimeError):
        _source(data=data).run_query("SELECT 1")


def test_metadata_export_renders_redshift_snapshot(tmp_path):
    # The shared .metadata/ machinery drives off the source's SourceMetadataProfile,
    # so a Redshift source produces a correctly-labeled snapshot with no code fork.
    export_metadata(_source(), tmp_path)
    meta = tmp_path / METADATA_DIR

    index = (meta / "index.md").read_text()
    assert "# Redshift metadata snapshot: `dev`" in index
    assert "Redshift system catalog (SVV_* views)" in index
    assert "`public.races`" in index and "`spectrum.results_ext`" in index

    # Schema-qualified table sheets exist and use URI (not ARN) labels + row-count.
    native = (meta / "tables" / "public.races.md").read_text()
    assert "**Resource (URI)**: `redshift://f1-cluster:5439/dev#public.races`" in native
    assert "Row-count hint (from Redshift Parameters, unverified)**: 976" in native
    assert "character varying(255)" in native

    external = (meta / "tables" / "spectrum.results_ext.md").read_text()
    assert "**S3 location**: `s3://fake-f1/results/`" in external
    assert "## Partition keys" in external

    # columns.tsv is the same cross-table grep target, keyed by schema.table.
    tsv = (meta / "columns.tsv").read_text()
    assert tsv.splitlines()[0] == "table\tcolumn\ttype\tcomment"
    assert "public.races\traceid" in tsv

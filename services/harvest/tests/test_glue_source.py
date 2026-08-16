import pytest

from harvest.glue_source import GlueAthenaSource, ResultCapExceeded
from tests.fakes import FakeAthena, QueryKeyedAthena, f1_like_glue


def _source(athena=None):
    return GlueAthenaSource(
        database="na_mi_formula_1_curated",
        glue=f1_like_glue(),
        athena=athena,
        region="us-east-1",
        account_id="123456789012",
    )


def test_list_concepts_yields_db_and_tables():
    src = _source()
    concepts = src.list_concepts()
    ids = {c.id_str: c for c in concepts}
    assert "datasets/na_mi_formula_1_curated" in ids
    assert ids["datasets/na_mi_formula_1_curated"].type == "Glue Database"
    assert "tables/races" in ids and "tables/results" in ids
    assert ids["tables/races"].type == "Glue Table"


def test_resource_arns_match_golden_shape():
    src = _source()
    db = src.find(("datasets", "na_mi_formula_1_curated"))
    assert db.resource == (
        "arn:aws:glue:us-east-1:123456789012:database/na_mi_formula_1_curated"
    )
    races = src.find(("tables", "races"))
    assert races.resource == (
        "arn:aws:glue:us-east-1:123456789012:table/na_mi_formula_1_curated/races"
    )


def test_read_concept_table_flat_schema():
    src = _source()
    meta = src.read_concept(src.find(("tables", "races")))
    assert meta["table"] == "races"
    assert meta["version_id"] == "1"
    names = {f["name"] for f in meta["flat_schema"]}
    assert {"raceid", "year", "circuitid", "name"} <= names
    # top-level column carries its comment
    raceid = next(f for f in meta["flat_schema"] if f["name"] == "raceid")
    assert raceid["comment"] == "Unique id (PK)"


def test_read_concept_database():
    src = _source()
    meta = src.read_concept(src.find(("datasets", "na_mi_formula_1_curated")))
    assert meta["database"] == "na_mi_formula_1_curated"
    assert meta["table_count"] == 2


def test_sample_rows_via_athena():
    athena = FakeAthena(
        rows=[{"raceid": "1", "year": "2009"}, {"raceid": "2", "year": "2009"}]
    )
    src = _source(athena=athena)
    rows = src.sample_rows(src.find(("tables", "races")), n=2)
    assert rows == [{"raceid": "1", "year": "2009"}, {"raceid": "2", "year": "2009"}]


def test_sample_rows_preserves_null_vs_empty_string():
    # Athena returns a SQL NULL as an empty Datum (no VarCharValue) and an empty
    # string as VarCharValue="". The tool must keep them distinct: None vs "".
    athena = FakeAthena(rows=[{"raceid": "1", "name": None, "note": ""}])
    src = _source(athena=athena)
    rows = src.sample_rows(src.find(("tables", "races")), n=1)
    assert rows == [{"raceid": "1", "name": None, "note": ""}]
    assert rows[0]["name"] is None
    assert rows[0]["note"] == ""


def test_run_query_raises_on_failed_state():
    src = _source(athena=FakeAthena(state="FAILED"))
    with pytest.raises(RuntimeError) as e:
        src.run_query("SELECT 1")
    assert "FAILED" in str(e.value)


def test_run_query_timeout_cancels_query():
    # A timed-out query must be best-effort cancelled — an orphaned execution
    # keeps holding an Athena workgroup slot after we stop waiting for it.
    athena = FakeAthena(state="RUNNING")  # never reaches a terminal state
    src = _source(athena=athena)
    with pytest.raises(TimeoutError):
        src.run_query("SELECT 1", timeout_s=-1.0)
    assert athena.stop_calls == [{"QueryExecutionId": "qid-123"}]


def test_run_query_positional_preserves_duplicate_headers_and_nulls():
    # `SELECT r.name, c.name` yields a duplicate header; dict rows collapse it
    # (last value wins) — the positional shape must keep every cell, and the
    # None (SQL NULL) vs "" distinction with it.
    athena = QueryKeyedAthena({"Q": (["name", "name"], [["a", None], ["", "b"]])})
    src = _source(athena=athena)
    header, rows = src.run_query("Q", positional=True)
    assert header == ["name", "name"]
    assert rows == [["a", None], ["", "b"]]
    # The default dict shape is unchanged (existing harvest callers).
    assert src.run_query("Q") == [{"name": None}, {"name": "b"}]


def test_run_query_max_rows_cap_raises_classified_error():
    athena = QueryKeyedAthena({"Q": (["c"], [["1"], ["2"], ["3"]])})
    src = _source(athena=athena)
    with pytest.raises(ResultCapExceeded) as e:
        src.run_query("Q", max_rows=2)
    assert "exceeds 2 rows" in str(e.value)
    # At or under the cap collects normally.
    assert src.run_query("Q", max_rows=3) == [{"c": "1"}, {"c": "2"}, {"c": "3"}]


def test_sample_rows_none_without_athena():
    src = _source(athena=None)
    assert src.sample_rows(src.find(("tables", "races"))) is None


def test_table_names():
    assert set(_source().table_names()) == {"races", "results"}


# ---------------------------------------------------------------------------
# estimate_table_bytes — the size-gate fallback for hint-less tables
# ---------------------------------------------------------------------------


class _FakeS3Pages:
    """list_objects_v2 fake: yields scripted pages of object sizes."""

    def __init__(self, pages, truncate_forever=False):
        self.pages = pages
        self.truncate_forever = truncate_forever
        self.calls = 0

    def list_objects_v2(self, **kwargs):
        i = min(self.calls, len(self.pages) - 1)
        page = self.pages[i]
        self.calls += 1
        truncated = self.truncate_forever or self.calls < len(self.pages)
        return {
            "Contents": [{"Size": s} for s in page],
            "IsTruncated": truncated,
            "NextContinuationToken": f"t{self.calls}",
        }


def _sized_source(location, s3):
    class _Glue:
        def get_table(self, **kwargs):
            return {
                "Table": {
                    "Name": kwargs["Name"],
                    "StorageDescriptor": {"Location": location},
                }
            }

    from harvest.glue_source import GlueAthenaSource

    return GlueAthenaSource("db", glue=_Glue(), s3=s3)


def test_estimate_table_bytes_sums_the_location(tmp_path):
    s3 = _FakeS3Pages([[100, 200], [300]])
    src = _sized_source("s3://bucket/data/orders/", s3)
    assert src.estimate_table_bytes("orders", stop_at=10_000) == 600
    assert s3.calls == 2


def test_estimate_table_bytes_early_exits_over_the_gate():
    s3 = _FakeS3Pages([[5_000], [5_000], [5_000]], truncate_forever=True)
    src = _sized_source("s3://bucket/data/big/", s3)
    # Exceeds stop_at on page 2 — no need to keep listing.
    assert src.estimate_table_bytes("big", stop_at=8_000) == 10_000
    assert s3.calls == 2


def test_estimate_table_bytes_cannot_tell_returns_none():
    from harvest.glue_source import GlueAthenaSource

    # A VIEW (no s3:// location) has nothing listable.
    src = _sized_source("", _FakeS3Pages([[1]]))
    assert src.estimate_table_bytes("v", stop_at=100) is None
    # No s3 client injected (tests/fakes): can't tell.
    class _Glue:
        def get_table(self, **kwargs):
            return {"Table": {"Name": "t"}}

    assert (
        GlueAthenaSource("db", glue=_Glue()).estimate_table_bytes("t", stop_at=1)
        is None
    )
    # Page cap hit while still under the gate: cannot be sure -> None.
    s3 = _FakeS3Pages([[1]], truncate_forever=True)
    src = _sized_source("s3://bucket/tiny/", s3)
    assert src.estimate_table_bytes("t", stop_at=10_000) is None
    assert s3.calls == 50


def test_bottomk_sketch_hashes_distinct_values_not_rows():
    # Two load-bearing tokens, both invisible to fakes and both fatal live:
    # DISTINCT — Trino's min(x, n) is an order statistic over input ROWS, so
    # without it a fact-side FK with fanout f collapses to ~k/f usable
    # hashes; from_big_endian_64 — Trino's xxhash64 returns VARBINARY, so
    # without the decode every sketch cell is unparseable and the whole
    # nominator silently disables itself.
    expr = _source().sql_bottomk_sketch('"driverid"', 256)
    assert expr == (
        "min(DISTINCT from_big_endian_64(xxhash64(to_utf8("
        'cast("driverid" as varchar)))), 256)'
    )


def test_sampled_ref_is_an_aliasable_subquery():
    # Trino nests the alias INSIDE a sampled relation ('orders o TABLESAMPLE
    # SYSTEM (10)'), so a bare 'ref TABLESAMPLE ...' suffix can never be
    # composed as 'FROM {ref} t' — which every probe does. The subquery form
    # aliases like any derived table.
    ref = _source().sql_sampled_ref('"db"."big"', 2.5)
    assert ref == '(SELECT * FROM "db"."big" TABLESAMPLE SYSTEM (2.5))'


def test_estimate_table_bytes_prefix_is_slash_terminated():
    # 'data/orders' must not also sum a sibling 'data/orders_v2'.
    class _CapturingS3(_FakeS3Pages):
        def __init__(self, pages):
            super().__init__(pages)
            self.prefixes = []

        def list_objects_v2(self, **kwargs):
            self.prefixes.append(kwargs.get("Prefix"))
            return super().list_objects_v2(**kwargs)

    s3 = _CapturingS3([[100]])
    src = _sized_source("s3://bucket/data/orders", s3)
    assert src.estimate_table_bytes("orders", stop_at=10_000) == 100
    assert s3.prefixes == ["data/orders/"]


def test_estimate_table_bytes_full_listing_with_no_stop_at():
    # stop_at=None is the SAMPLE-PERCENT contract: no early exit, the figure
    # is exact. (The gate contract early-exits and returns a lower bound.)
    s3 = _FakeS3Pages([[6000], [4000], [2000]])
    src = _sized_source("s3://bucket/t/", s3)
    assert src.estimate_table_bytes("t", stop_at=None) == 12000
    assert s3.calls == 3


def test_estimate_table_bytes_partitioned_empty_root_is_unmeasurable():
    # ADD PARTITION can point partitions OUTSIDE the table's root location:
    # an empty root is 'cannot tell', not 'zero bytes' (zero would earn a
    # huge table an unbudgeted FULL probe).
    class _PartitionedGlue:
        def get_table(self, **kwargs):
            return {
                "Table": {
                    "Name": kwargs["Name"],
                    "StorageDescriptor": {"Location": "s3://bucket/t/"},
                    "PartitionKeys": [{"Name": "dt"}],
                }
            }

    src = GlueAthenaSource("db", glue=_PartitionedGlue(), s3=_FakeS3Pages([[]]))
    assert src.estimate_table_bytes("t", stop_at=None) is None

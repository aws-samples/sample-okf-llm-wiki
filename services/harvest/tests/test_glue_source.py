import pytest

from harvest.glue_source import GlueAthenaSource
from tests.fakes import FakeAthena, f1_like_glue


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


def test_sample_rows_none_without_athena():
    src = _source(athena=None)
    assert src.sample_rows(src.find(("tables", "races"))) is None


def test_table_names():
    assert set(_source().table_names()) == {"races", "results"}

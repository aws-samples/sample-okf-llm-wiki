"""Source-type vocabulary + the new/legacy mapping-shape adapter."""

from __future__ import annotations

import pytest

from okf_core.sources import (
    DEFAULT_SOURCE_TYPE,
    SOURCE_TYPE_GLUE,
    SOURCE_TYPE_REDSHIFT,
    SUPPORTED_SOURCE_TYPES,
    SourceError,
    build_glue_source,
    build_redshift_source,
    is_supported_source_type,
    normalize_source,
    source_glue_database,
    validate_source,
)


def test_glue_is_the_default_and_supported():
    assert DEFAULT_SOURCE_TYPE == SOURCE_TYPE_GLUE == "glue"
    # Glue and Redshift are the supported source types today.
    assert SUPPORTED_SOURCE_TYPES == ("glue", "redshift")
    assert is_supported_source_type("glue")
    assert is_supported_source_type("redshift")
    assert not is_supported_source_type("bigquery")
    assert not is_supported_source_type(None)


def test_build_glue_source_shape():
    assert build_glue_source("sales_db") == {
        "type": "glue",
        "glue_database": "sales_db",
    }


def test_build_glue_source_requires_database():
    with pytest.raises(SourceError):
        build_glue_source("")


def test_normalize_from_new_source_object():
    src = normalize_source({"type": "glue", "glue_database": "db"})
    assert src == {"type": "glue", "glue_database": "db"}


def test_normalize_from_legacy_flat_glue_database():
    # Pre-`source` rows carried only the flat attribute; it lifts into a source.
    src = normalize_source(None, glue_database="legacy_db")
    assert src == {"type": "glue", "glue_database": "legacy_db"}


def test_normalize_prefers_source_object_over_flat():
    src = normalize_source(
        {"type": "glue", "glue_database": "new"}, glue_database="old"
    )
    assert src["glue_database"] == "new"


def test_normalize_defaults_missing_type_to_glue():
    src = normalize_source({"glue_database": "db"})
    assert src == {"type": "glue", "glue_database": "db"}


def test_normalize_rejects_unsupported_type():
    with pytest.raises(SourceError, match="unsupported source type"):
        normalize_source({"type": "bigquery", "project": "p"})


def test_build_redshift_source_shape():
    assert build_redshift_source("dev") == {
        "type": SOURCE_TYPE_REDSHIFT,
        "redshift_database": "dev",
    }


def test_build_redshift_source_requires_database():
    with pytest.raises(SourceError):
        build_redshift_source("")


def test_normalize_from_redshift_source_object():
    src = normalize_source({"type": "redshift", "redshift_database": "dev"})
    assert src == {"type": "redshift", "redshift_database": "dev"}


def test_normalize_rejects_redshift_without_database():
    with pytest.raises(SourceError, match="redshift source requires"):
        normalize_source({"type": "redshift"})


def test_build_redshift_source_with_serverless_connection():
    src = build_redshift_source(
        "dev",
        workgroup_name="wg1",
        secret_arn="arn:aws:secretsmanager:eu-west-1:1:secret:x",
    )
    assert src == {
        "type": "redshift",
        "redshift_database": "dev",
        "workgroup_name": "wg1",
        "secret_arn": "arn:aws:secretsmanager:eu-west-1:1:secret:x",
    }


def test_build_redshift_source_with_provisioned_connection():
    src = build_redshift_source(
        "dev",
        cluster_identifier="c1",
        secret_arn="arn:aws:secretsmanager:eu-west-1:1:secret:x",
    )
    assert src["cluster_identifier"] == "c1"
    assert "workgroup_name" not in src


def test_build_redshift_source_rejects_both_targets():
    with pytest.raises(SourceError, match="only ONE"):
        build_redshift_source(
            "dev", cluster_identifier="c1", workgroup_name="wg1", secret_arn="s"
        )


def test_build_redshift_source_target_requires_secret():
    with pytest.raises(SourceError, match="secret_arn is required"):
        build_redshift_source("dev", workgroup_name="wg1")


def test_build_redshift_source_secret_requires_target():
    with pytest.raises(SourceError, match="cluster_identifier or workgroup_name"):
        build_redshift_source("dev", secret_arn="s")


def test_normalize_redshift_roundtrips_connection_fields():
    obj = {
        "type": "redshift",
        "redshift_database": "dev",
        "cluster_identifier": "c1",
        "secret_arn": "arn:aws:secretsmanager:eu-west-1:1:secret:x",
    }
    assert normalize_source(obj) == obj


def test_normalize_rejects_glue_without_database():
    with pytest.raises(SourceError):
        normalize_source({"type": "glue"})


def test_normalize_rejects_empty():
    with pytest.raises(SourceError):
        normalize_source(None, glue_database=None)


def test_validate_source_rejects_non_dict():
    with pytest.raises(SourceError):
        validate_source("glue")  # type: ignore[arg-type]


def test_source_glue_database_accessor():
    assert source_glue_database({"type": "glue", "glue_database": "db"}) == "db"
    assert source_glue_database({"type": "redshift"}) is None
    assert source_glue_database(None) is None

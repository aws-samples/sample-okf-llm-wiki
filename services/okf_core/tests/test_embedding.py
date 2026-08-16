import json

from okf_core.embedding import (
    ConceptCoordinates,
    FILTERABLE_METADATA_BUDGET_BYTES,
    NON_FILTERABLE_METADATA_KEYS,
    build_embed_text,
    build_filterable_metadata,
    build_non_filterable_metadata,
    vector_key,
)


def _coords():
    return ConceptCoordinates(
        data_domain="sales",
        dataset="orders",
        concept_path="sales/orders/tables/customers",
        s3_key="okf/sales/orders/tables/customers.md",
        table="customers",
    )


def test_vector_key_is_concept_path():
    assert vector_key(_coords()) == "sales/orders/tables/customers"


def test_build_embed_text_includes_frontmatter_and_overview():
    fm = {
        "type": "Glue Table",
        "title": "Customers",
        "description": "One row per customer.",
        "tags": ["sales", "dimension"],
    }
    body = "# Overview\nThe customers table.\n\n# Schema\n`id`\n"
    text = build_embed_text(fm, body)
    assert "Customers" in text
    assert "One row per customer." in text
    assert "sales, dimension" in text
    assert "The customers table." in text
    # schema section is NOT embedded
    assert "`id`" not in text


def test_filterable_metadata_shape():
    fm = {"type": "Glue Table", "tags": ["a", "b"]}
    md = build_filterable_metadata(_coords(), fm)
    assert md["data_domain"] == "sales"
    assert md["dataset"] == "orders"
    assert md["table"] == "customers"
    assert md["type"] == "Glue Table"
    assert md["tags"] == ["a", "b"]


def test_filterable_metadata_trims_tags_to_budget():
    # Many long tags must be trimmed to stay under the 2 KB budget.
    fm = {"type": "Glue Table", "tags": [f"tag-{'x' * 50}-{i}" for i in range(200)]}
    md = build_filterable_metadata(_coords(), fm)
    size = len(json.dumps(md).encode("utf-8"))
    assert size <= FILTERABLE_METADATA_BUDGET_BYTES
    assert len(md.get("tags", [])) < 200  # some were dropped


def test_non_filterable_metadata_only_declared_keys():
    fm = {
        "title": "Customers",
        "description": "One row per customer.",
        "type": "Glue Table",
    }
    md = build_non_filterable_metadata(_coords(), fm)
    assert set(md.keys()) == set(NON_FILTERABLE_METADATA_KEYS)
    assert md["title"] == "Customers"
    assert md["s3_key"] == "okf/sales/orders/tables/customers.md"


def test_tags_accept_comma_string():
    fm = {"type": "Glue Table", "tags": "a, b, c"}
    md = build_filterable_metadata(_coords(), fm)
    assert md["tags"] == ["a", "b", "c"]

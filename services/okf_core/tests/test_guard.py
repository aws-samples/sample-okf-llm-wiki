import pytest

from okf_core.guard import (
    check_augmentation,
    check_frontmatter,
    citation_entry_count,
    ensure_timestamp,
    reorder_frontmatter,
    schema_field_names,
)


def test_check_frontmatter_ok():
    r = check_frontmatter(
        {"type": "Glue Table", "title": "T", "description": "d", "timestamp": "t"}
    )
    assert r.ok
    assert r.error is None


def test_check_frontmatter_missing():
    r = check_frontmatter({"type": "Glue Table"})
    assert not r.ok
    assert "title" in r.error


def test_schema_field_names_extracts_backticked():
    body = "# Schema\n| `raceid` | bigint | id |\n| `year` | bigint | y |\n"
    assert schema_field_names(body) == {"raceid", "year"}


def test_schema_field_names_ignores_type_and_example_tokens():
    # Regression: backticked type names and example values in the Type /
    # Description columns must NOT be counted as columns (only the first cell).
    body = (
        "# Schema\n"
        "| Column | Type | Description |\n"
        "|---|---|---|\n"
        "| `positiontext` | string | `R` retired, `D` disqualified |\n"
        "| `time` | string | e.g. `M:SS.mmm` |\n"
    )
    assert schema_field_names(body) == {"positiontext", "time"}


def test_schema_field_names_fence_aware():
    # A '#'-prefixed comment inside a code fence must not end the # Schema section.
    body = "# Schema\n```\n# a fenced comment\n```\n| `a` | int |\n| `b` | int |\n"
    assert schema_field_names(body) == {"a", "b"}


def test_augmentation_blocks_schema_shrink():
    old = "# Schema\n| `a` | int |\n| `b` | int |\n| `c` | int |\n"
    new = "# Schema\n| `a` | int |\n"
    r = check_augmentation(old, new, existing_type="Glue Table")
    assert not r.ok
    assert "`b`" in r.error and "`c`" in r.error


def test_augmentation_allows_reworded_description():
    # Rewording a Description (with backticked example values) while keeping all
    # columns must be allowed — this is the real bug the guard used to block.
    old = "# Schema\n| `positiontext` | string | `R`, `D` |\n| `pos` | int | order |\n"
    new = "# Schema\n| `positiontext` | string | Retired or Disqualified |\n| `pos` | int | finishing order |\n"
    r = check_augmentation(old, new, existing_type="Glue Table")
    assert r.ok


def test_augmentation_allows_schema_growth():
    old = "# Schema\n| `a` | int |\n| `b` | int |\n"
    new = "# Schema\n| `a` | int |\n| `b` | int |\n| `c` | int |\n"
    r = check_augmentation(old, new, existing_type="Glue Table")
    assert r.ok


def test_augmentation_blocks_citation_shrink():
    old = "# Schema\n`a`\n# Citations\n- one\n- two\n"
    new = "# Schema\n`a`\n# Citations\n- one\n"
    r = check_augmentation(old, new, existing_type="Glue Table")
    assert not r.ok
    assert "Citations" in r.error


def test_augmentation_ignores_non_glue_types():
    old = "# Schema\n`a` `b`\n"
    new = "# nothing\n"
    r = check_augmentation(old, new, existing_type="Reference")
    assert r.ok


@pytest.mark.parametrize(
    "concept_type",
    ["Redshift Database", "Redshift Table", "Redshift External Table"],
)
def test_augmentation_protects_redshift_types(concept_type):
    # The guard routes on the concept-type registry (is_schema_bearing_type), so
    # the Redshift types get the same shrink protection as the Glue ones.
    old = "# Schema\n| `a` | int |\n| `b` | int |\n"
    new = "# Schema\n| `a` | int |\n"
    r = check_augmentation(old, new, existing_type=concept_type)
    assert not r.ok
    assert "`b`" in r.error


def test_citation_entry_count():
    body = "# Citations\n- [1] a\n- [2] b\n\n"
    assert citation_entry_count(body) == 2


def test_reorder_frontmatter():
    fm = {"tags": ["x"], "title": "T", "type": "Glue Table", "extra": 1}
    out = list(reorder_frontmatter(fm))
    assert out[0] == "type"
    assert out.index("title") < out.index("tags")
    assert "extra" in out  # unknown keys retained at the end


def test_ensure_timestamp_fills_when_missing():
    fm = ensure_timestamp({"type": "T"})
    assert fm["timestamp"]
    fm2 = ensure_timestamp({"type": "T", "timestamp": "keep"})
    assert fm2["timestamp"] == "keep"

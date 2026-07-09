from okf_core.document import OKFDocument, OKFDocumentError

import pytest


def test_parse_roundtrip():
    text = (
        "---\n"
        "type: Glue Table\n"
        "title: Races\n"
        "description: One row per race.\n"
        "timestamp: '2026-06-30T00:00:00Z'\n"
        "---\n"
        "\n"
        "# Overview\n"
        "\nBody text.\n"
    )
    doc = OKFDocument.parse(text)
    assert doc.frontmatter["type"] == "Glue Table"
    assert doc.frontmatter["title"] == "Races"
    assert doc.body.startswith("# Overview")
    # Re-parse of the serialized form yields the same frontmatter + body.
    doc2 = OKFDocument.parse(doc.serialize())
    assert doc2.frontmatter == doc.frontmatter
    assert doc2.body.rstrip("\n") == doc.body.rstrip("\n")


def test_no_frontmatter_is_all_body():
    doc = OKFDocument.parse("just a plain body\nno frontmatter")
    assert doc.frontmatter == {}
    assert "plain body" in doc.body


def test_unterminated_frontmatter_raises():
    with pytest.raises(OKFDocumentError):
        OKFDocument.parse("---\ntype: X\nno closing delimiter\n")


def test_validate_missing_keys():
    doc = OKFDocument(frontmatter={"type": "Glue Table"}, body="")
    with pytest.raises(OKFDocumentError) as e:
        doc.validate()
    assert "title" in str(e.value)


def test_serialize_preserves_key_order():
    doc = OKFDocument(
        frontmatter={"type": "T", "title": "A", "description": "d", "timestamp": "t"},
        body="hello",
    )
    out = doc.serialize()
    assert out.index("type:") < out.index("title:") < out.index("description:")
    assert out.endswith("\n")

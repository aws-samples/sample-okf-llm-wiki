"""Plain-text SQL/JSON extraction (replaces structured output under adaptive thinking)."""

from __future__ import annotations

import types

from harvest.benchmark.extract import extract_json, extract_sql, message_text


# -- message_text: strip thinking blocks, handle str / list / message ---------


def test_message_text_plain_string():
    assert message_text("SELECT 1") == "SELECT 1"


def test_message_text_skips_reasoning_blocks():
    msg = types.SimpleNamespace(
        content=[
            {"type": "reasoning_content", "reasoning_content": {"text": "hmm"}},
            {"type": "text", "text": "the answer"},
        ]
    )
    assert message_text(msg) == "the answer"


def test_message_text_list_of_text_blocks():
    assert message_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


# -- extract_sql --------------------------------------------------------------


def test_extract_sql_from_fence():
    text = "Here is the query:\n```sql\nSELECT count(*) FROM races\n```\n"
    assert extract_sql(text) == "SELECT count(*) FROM races"


def test_extract_sql_prefers_last_fence():
    text = "```sql\nSELECT 1\n```\nOn reflection:\n```sql\nSELECT 2\n```"
    assert extract_sql(text) == "SELECT 2"


def test_extract_sql_bare_fence():
    assert extract_sql("```\nSELECT 3\n```") == "SELECT 3"


def test_extract_sql_no_fence_falls_back_to_text():
    assert extract_sql("SELECT 4") == "SELECT 4"


def test_extract_sql_empty():
    assert extract_sql("") == ""
    assert extract_sql("   ") == ""


def test_extract_sql_from_message_with_thinking():
    msg = types.SimpleNamespace(
        content=[
            {"type": "reasoning_content", "reasoning_content": {"text": "think"}},
            {"type": "text", "text": "```sql\nSELECT 5\n```"},
        ]
    )
    assert extract_sql(msg) == "SELECT 5"


# -- extract_json -------------------------------------------------------------


def test_extract_json_from_fence():
    text = '```json\n{"category": "GENUINE_ERROR", "gap": "docs miss X"}\n```'
    out = extract_json(text)
    assert out == {"category": "GENUINE_ERROR", "gap": "docs miss X"}


def test_extract_json_bare_object():
    assert extract_json('{"improvements": ["a", "b"]}') == {"improvements": ["a", "b"]}


def test_extract_json_embedded_in_prose():
    text = 'My verdict is: {"category": "NOISY_GOLD", "gap": ""} — done.'
    assert extract_json(text)["category"] == "NOISY_GOLD"


def test_extract_json_prefers_last_fence():
    text = '```json\n{"category":"AMBIGUOUS"}\n```\nactually:\n```json\n{"category":"GENUINE_ERROR"}\n```'
    assert extract_json(text)["category"] == "GENUINE_ERROR"


def test_extract_json_returns_default_on_garbage():
    assert extract_json("no json here", default={}) == {}
    assert extract_json("", default={}) == {}


def test_extract_json_array():
    assert extract_json("```json\n[1, 2, 3]\n```") == [1, 2, 3]

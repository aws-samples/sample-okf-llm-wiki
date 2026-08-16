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


def test_extract_sql_sql_fence_beats_trailing_other_tagged_fence():
    # A trailing non-SQL fence (a model summarizing its answer in ```text) must
    # not beat an earlier ```sql fence.
    text = "```sql\nSELECT 1\n```\nIn summary:\n```text\nthe count is 1\n```"
    assert extract_sql(text) == "SELECT 1"


def test_extract_sql_last_sql_tagged_fence_wins_over_untagged():
    text = "```sql\nSELECT 1\n```\nscratch:\n```\nSELECT 2\n```"
    assert extract_sql(text) == "SELECT 1"


def test_extract_sql_tag_word_never_bleeds_into_payload():
    # The old `(?:sql)?` regex captured a foreign tag word into the SQL body
    # ("text\nSELECT ..."). Tags are now captured separately.
    text = "```text\nnot sql\n```\n```sql\nSELECT 3\n```"
    assert extract_sql(text) == "SELECT 3"


def test_extract_sql_other_tagged_fence_never_treated_as_sql():
    # No sql/untagged fence at all → the whole-text fallback, never the
    # other-tagged fence body.
    text = 'prose\n```json\n{"a": 1}\n```\nmore prose'
    assert extract_sql(text) == text.strip()


def test_extract_sql_case_insensitive_tag():
    assert extract_sql("```SQL\nSELECT 4\n```") == "SELECT 4"


def test_extract_sql_single_line_fence_is_content_not_a_tag():
    # A single-line fence has no tag line: the tag-capturing regex used to eat
    # "SELECT" as a language tag, classify the fence as foreign, and fall back
    # to the whole prose reply (backticks included).
    assert (
        extract_sql("Here is the query: ```SELECT * FROM t```")
        == "SELECT * FROM t"
    )
    assert extract_sql("``` SELECT 2 ```") == "SELECT 2"


def test_extract_sql_standard_tagged_block_still_extracts():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_extract_sql_sql_fence_beats_python_fence():
    text = "```python\nprint(1)\n```\n```sql\nSELECT 9\n```"
    assert extract_sql(text) == "SELECT 9"


def test_extract_sql_single_line_sql_tagged_fence():
    # The one tagging idiom on a single-line fence: a leading `sql` token
    # followed by whitespace tags the remainder.
    assert extract_sql("```sql SELECT 3```") == "SELECT 3"


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

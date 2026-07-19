"""ask_human: question/answer validators, the tool, and the interrupt middleware."""

from __future__ import annotations

import json

import pytest

from chat.ask_human import (
    AskHumanError,
    make_ask_human_tool,
    normalize_answers,
    normalize_questions,
)


# --- normalize_questions -----------------------------------------------------


def test_single_choice_gets_options_and_allow_other():
    out = normalize_questions(
        [{"id": "grain", "prompt": "Which grain?", "kind": "single",
          "options": ["Daily", "Weekly"]}]
    )
    assert out == [
        {
            "id": "grain",
            "prompt": "Which grain?",
            "kind": "single",
            "options": ["Daily", "Weekly"],
            "allow_other": True,  # the free-text 5th option
        }
    ]


def test_multi_choice_also_allows_other():
    out = normalize_questions(
        [{"prompt": "Include which?", "kind": "multi", "options": ["A", "B", "C"]}]
    )
    assert out[0]["kind"] == "multi"
    assert out[0]["allow_other"] is True
    assert out[0]["id"] == "q1"  # derived when missing


def test_text_kind_has_no_options_and_no_other():
    out = normalize_questions([{"prompt": "Describe the goal", "kind": "text"}])
    assert out[0]["kind"] == "text"
    assert out[0]["options"] == []
    assert out[0]["allow_other"] is False


def test_kind_defaults_to_single():
    out = normalize_questions([{"prompt": "Pick", "options": ["x", "y"]}])
    assert out[0]["kind"] == "single"


def test_duplicate_ids_are_disambiguated():
    out = normalize_questions(
        [
            {"id": "q", "prompt": "A", "kind": "text"},
            {"id": "q", "prompt": "B", "kind": "text"},
        ]
    )
    assert out[0]["id"] != out[1]["id"]


def test_json_string_questions_are_tolerated():
    out = normalize_questions(json.dumps([{"prompt": "P", "kind": "text"}]))
    assert out[0]["prompt"] == "P"


@pytest.mark.parametrize(
    "bad",
    [
        [],  # empty
        "not json",  # unparseable string
        [{"kind": "text"}],  # missing prompt
        [{"prompt": "P", "kind": "bogus"}],  # invalid kind
        [{"prompt": "P", "kind": "single"}],  # single without options
        [{"prompt": "P", "kind": "multi", "options": []}],  # empty options
    ],
)
def test_bad_questions_raise(bad):
    with pytest.raises(AskHumanError):
        normalize_questions(bad)


# --- normalize_answers -------------------------------------------------------


def _qs():
    return normalize_questions(
        [
            {"id": "grain", "prompt": "Which grain?", "kind": "single", "options": ["Daily"]},
            {"id": "tables", "prompt": "Which tables?", "kind": "multi", "options": ["a", "b"]},
            {"id": "goal", "prompt": "Goal?", "kind": "text"},
        ]
    )


def test_answers_from_list_shape():
    ans = normalize_answers(
        [
            {"id": "grain", "answer": "Weekly"},
            {"id": "tables", "answer": ["a", "b"]},
            {"id": "goal", "answer": "trend over time"},
        ],
        _qs(),
    )
    assert ans == [
        {"id": "grain", "prompt": "Which grain?", "answer": "Weekly"},
        {"id": "tables", "prompt": "Which tables?", "answer": "a, b"},
        {"id": "goal", "prompt": "Goal?", "answer": "trend over time"},
    ]


def test_answers_from_mapping_shape():
    ans = normalize_answers({"grain": "Daily", "goal": "x"}, _qs())
    by_id = {a["id"]: a["answer"] for a in ans}
    assert by_id["grain"] == "Daily"
    # A question with no submitted answer is kept, marked (no answer).
    assert by_id["tables"] == "(no answer)"


def test_answers_always_one_entry_per_question():
    ans = normalize_answers([], _qs())
    assert [a["id"] for a in ans] == ["grain", "tables", "goal"]
    assert all(a["answer"] == "(no answer)" for a in ans)


# --- the tool ----------------------------------------------------------------


def test_tool_shape():
    tool = make_ask_human_tool()
    assert tool.name == "ask_human"
    assert set(tool.args) == {"questions"}
    assert "clarifying questions" in tool.description.lower()

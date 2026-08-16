"""policies.yaml format invariants: parse strictness, the author gate, sharding."""

from __future__ import annotations

import pytest

from okf_core import policy_doc as pd

GOOD = """\
policies:
  - id: P001
    type: behavioural
    condition: a points request could mean two readings and was not pinned
    action: ask for clarification instead of committing to a figure
    source: references/usage_guardrails.md
  - id: P002
    type: computational
    condition: the answer states figures from a query that returned zero rows
    action: never state figures derived from that query
    source: references/known_issues/empty_results.md
"""


def test_parse_good_document():
    entries = pd.parse_policies(GOOD)
    assert [e["id"] for e in entries] == ["P001", "P002"]
    assert [e["type"] for e in entries] == ["behavioural", "computational"]
    assert entries[0]["action"].startswith("ask for clarification")
    assert entries[1]["source"] == "references/known_issues/empty_results.md"


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("not yaml: [unclosed", "not valid YAML"),
        ("policies: {}", "top-level `policies` list"),
        ("- id: P1", "top-level `policies` list"),
        ("policies:\n  - type: computational\n    condition: c\n    action: a\n    source: references/x.md",
         "`id`"),
        ("policies:\n  - id: X001\n    type: computational\n    condition: c\n    action: a\n    source: references/x.md",
         "must match P<number>"),
        (GOOD.replace("P002", "P001"), "duplicate policy id"),
        ("policies:\n  - id: P001\n    type: computational\n    condition: c\n    action: a\n    source: tables/races.md",
         "references/"),
        ("policies:\n  - id: P001\n    type: computational\n    condition: ''\n    action: a\n    source: references/x.md",
         "`condition`"),
        # The v3 migration path: a pre-split document (no type) is REJECTED, which
        # is what flips the row stale and triggers the re-authoring rebuild.
        ("policies:\n  - id: P001\n    condition: c\n    action: a\n    source: references/x.md",
         "`type`"),
        ("policies:\n  - id: P001\n    type: both\n    condition: c\n    action: a\n    source: references/x.md",
         "computational, behavioural"),
    ],
)
def test_parse_rejects_and_names_the_problem(text, fragment):
    with pytest.raises(pd.PolicyDocError) as e:
        pd.parse_policies(text)
    assert fragment in str(e.value)


def test_policies_of_type_filters_one_track_and_rejects_typos():
    entries = pd.parse_policies(GOOD)
    assert [p["id"] for p in pd.policies_of_type(entries, "behavioural")] == ["P001"]
    assert [p["id"] for p in pd.policies_of_type(entries, "computational")] == ["P002"]
    with pytest.raises(ValueError):
        pd.policies_of_type(entries, "behavioral")  # US spelling ≠ the format's


def test_validate_gate_checks_live_sources_and_backstop():
    assert pd.validate_policy_doc(GOOD) is None
    assert pd.validate_policy_doc("policies: []") == "the document contains no policies"
    # Dead source: the author must delete or fix the entry, never dangle it.
    message = pd.validate_policy_doc(
        GOOD, known_sources={"references/usage_guardrails.md"}
    )
    assert "references/known_issues/empty_results.md" in message
    # Count backstop, tunable by the caller.
    assert "too many" in pd.validate_policy_doc(GOOD, max_policies=1)


def test_shard_policies_preserves_order_and_covers_everything():
    entries = pd.parse_policies(GOOD)
    shards = pd.shard_policies(entries, size=1)
    assert [s[0]["id"] for s in shards] == ["P001", "P002"]
    assert pd.shard_policies(entries, size=10) == [entries]
    with pytest.raises(ValueError):
        pd.shard_policies(entries, size=0)


def test_shard_policies_groups_by_source_and_never_straddles():
    # Policies from ONE wiki page must land in ONE judge's shard (shared
    # vocabulary/context), whatever their document order; a group moves to a
    # fresh shard rather than straddling a boundary, and only a group larger
    # than the cap itself splits.
    def _p(i, source):
        return {"id": f"P{i:03}", "type": "computational",
                "condition": "c", "action": "a", "source": source}

    entries = [_p(1, "references/a.md"), _p(2, "references/b.md"),
               _p(3, "references/a.md"), _p(4, "references/b.md"),
               _p(5, "references/a.md"), _p(6, "references/c.md")]
    shards = pd.shard_policies(entries, size=3)
    assert [[p["id"] for p in s] for s in shards] == [
        ["P001", "P003", "P005"],  # a.md's three, together
        ["P002", "P004", "P006"],  # b.md whole + c.md riding along
    ]
    assert all(len(s) <= 3 for s in shards)
    # An oversized single-source group still splits at the cap.
    big = [_p(i, "references/a.md") for i in range(1, 6)]
    assert [len(s) for s in pd.shard_policies(big, size=2)] == [2, 2, 1]


def test_render_policies_for_judge_is_verbatim():
    entries = pd.parse_policies(GOOD)
    text = pd.render_policies_for_judge(entries[:1])
    assert "id: P001" in text
    assert "action: ask for clarification" in text
    assert "source: references/usage_guardrails.md" in text


def test_parse_collapses_internal_whitespace_to_one_line():
    # YAML block scalars legally carry embedded newlines; every consumer (the
    # judge shard rendering, the reminder lines, the UI's line-oriented
    # display slice) treats a field as ONE line — so single-line is enforced
    # at parse time, the format's source of truth.
    doc = (
        "policies:\n"
        "  - id: P001\n"
        "    type: behavioural\n"
        "    condition: |\n"
        "      a points request could mean\n"
        "      two readings\n"
        "    action: >-\n"
        "      ask   for\tclarification\n"
        "      before answering\n"
        "    source: references/x.md\n"
    )
    entry = pd.parse_policies(doc)[0]
    assert entry["condition"] == "a points request could mean two readings"
    assert entry["action"] == "ask for clarification before answering"
    # One rendered line per field — a multi-line field would corrupt the
    # judge's shard text and truncate the UI's display slice.
    assert len(pd.render_policies_for_judge([entry]).splitlines()) == 4

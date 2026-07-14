"""Annotation primitives: key shape, quote re-anchoring, orphan detection."""

from __future__ import annotations

import pytest

from okf_core import annotations as anno


# --- key construction ------------------------------------------------------


def test_pk_embeds_user_for_structural_isolation():
    # Two users, same dataset -> different partitions. Isolation is in the key,
    # so a Query can never cross users.
    a = anno.annotation_pk("sales", "orders", "sub-alice")
    b = anno.annotation_pk("sales", "orders", "sub-bob")
    assert a != b
    assert a == "ANNO#sales#orders#sub-alice"


def test_pk_requires_user_sub():
    # A missing subject would collapse everyone into one shared partition.
    with pytest.raises(ValueError):
        anno.annotation_pk("sales", "orders", "")


def test_sk_and_prefix_select_one_concept():
    sk = anno.annotation_sk("tables/races", "abc123")
    assert sk == "tables/races#abc123"
    assert sk.startswith(anno.concept_sk_prefix("tables/races"))
    # A different concept's prefix does NOT match.
    assert not sk.startswith(anno.concept_sk_prefix("tables/results"))


# --- normalize_text: source markdown vs rendered selection -----------------


def test_normalize_folds_markdown_and_whitespace():
    # A quote captured from the DOM ("status 9 means refunds") matches source
    # that wrote it with emphasis/code and reflowed whitespace.
    dom = "status 9 means refunds"
    src = "**status** `9`  means\n  refunds"
    assert anno.normalize_text(dom) == anno.normalize_text(src)


def test_normalize_unwraps_links_to_visible_text():
    assert anno.normalize_text("see [the races table](tables/races.md)") == (
        "see the races table"
    )


def test_normalize_strips_list_and_heading_markers():
    assert anno.normalize_text("- one row per race") == anno.normalize_text(
        "### one row per race"
    )


# A `# Schema` table is the most likely annotation target, so its shapes matter.
_SCHEMA_DOC = """# Schema

| Column | Type | Description |
|---|---|---|
| `raceId` | int | one row per race, the primary key |
| `year` | int | the season year |

# Gotchas
Status **9** means refunds, not chargebacks.
"""


@pytest.mark.parametrize(
    "quote",
    [
        "Status 9 means refunds",  # prose, bold stripped by the DOM
        "one row per race, the primary key",  # inside one table cell
        "raceId int one row per race",  # spans cells: DOM joins with spaces
        "raceId",  # a bare column name
    ],
)
def test_find_quote_matches_real_selection_shapes(quote):
    # Every shape a browser selection can produce over a schema table must
    # re-anchor against the raw markdown source (pipes/backticks and all).
    assert anno.find_quote(_SCHEMA_DOC, quote)


def test_table_pipes_become_spaces_not_fused():
    # Cell separators must not fuse adjacent cell tokens into one word.
    assert "raceid int" in anno.normalize_text("| `raceId` | int | x |")


def test_inline_markup_fuses_to_match_dom():
    # Mid-word inline markup renders as one word in the DOM, so it must fold to
    # one word here too (markers -> nothing, unlike pipes -> space).
    assert anno.normalize_text("un`bel`ievable") == "unbelievable"


def test_table_delimiter_row_folds_to_empty():
    assert anno.normalize_text("|---|:--:|---|") == ""


# A join/metric doc's most-selected region is prose wrapped around a SQL block.
_JOIN_DOC = """---
type: Reference
title: races join circuits
description: how races link to circuits
---
# Overview

Join race events to their circuit dimension.

```sql
"races"."circuitid" = "circuits"."circuitid"
```

`circuits.circuitid` is unique, so the relationship is many races to one circuit.

# Citations
- arn:aws:glue:eu-west-1:158204760618:table/formula_1/races
"""


def test_selection_spanning_a_sql_code_block_is_not_orphaned():
    # The DOM renders the code WITHOUT the fence's ```sql language tag, so a
    # selection that crosses prose -> code -> prose must still re-anchor. (The
    # tag is in the source but never in the selection — it must not block the match.)
    quote = (
        'Join race events to their circuit dimension. '
        '"races"."circuitid" = "circuits"."circuitid" '
        "circuits.circuitid is unique, so the relationship is many races to one circuit."
    )
    assert not anno.is_orphaned(_JOIN_DOC, quote)


def test_code_fence_info_string_does_not_leak_a_token():
    # ```sql renders to nothing, so the word "sql" must not survive normalization.
    assert "sql" not in anno.normalize_text('```sql\nSELECT 1\n```').split()


@pytest.mark.parametrize(
    "doc",
    ["```\nSELECT 1\n```", "```python\nSELECT 1\n```", "~~~\nSELECT 1\n~~~"],
)
def test_code_inside_any_fence_still_matches(doc):
    # Bare, language-tagged, and tilde fences all keep the code between them.
    assert anno.find_quote(doc, "SELECT 1")


# --- find_quote / is_orphaned ---------------------------------------------


def test_find_quote_present_after_reformatting():
    body = "# Overview\n\nThe table has **one row per race**, keyed by raceId.\n"
    assert anno.find_quote(body, "one row per race")


def test_find_quote_absent_is_orphan():
    body = "# Overview\n\nSomething entirely different now.\n"
    assert not anno.find_quote(body, "one row per race")
    assert anno.is_orphaned(body, "one row per race")


def test_missing_doc_orphans_everything():
    # A dropped concept (body is None) orphans every annotation on it.
    assert anno.is_orphaned(None, "any quote at all")


_FM_DOC = """---
type: Glue Database
title: Formula 1
description: Formula 1 calendars, participants, race outcomes
tags: [motorsport]
---
# Overview
one row per race, the primary key.
"""


def test_orphan_matches_body_only_not_frontmatter():
    # Only the rendered body is annotatable, so re-anchoring runs against the body
    # with YAML frontmatter stripped. A body quote resolves; frontmatter text does
    # NOT (it can't be selected, and its YAML layout must not leak into matching).
    assert not anno.is_orphaned(_FM_DOC, "one row per race")
    assert anno.is_orphaned(_FM_DOC, "Formula 1 calendars, participants")
    assert "title:" not in anno.annotatable_text(_FM_DOC)


def test_annotatable_text_falls_back_on_malformed_doc():
    # No frontmatter block -> the whole text is the body (best-effort).
    assert anno.annotatable_text("just some text") == "just some text"


def test_empty_quote_never_matches():
    assert not anno.find_quote("some body text", "")
    assert anno.is_orphaned("some body text", "")


def test_repeated_quote_is_present_existence_only():
    # find_quote answers existence only (which occurrence is the agent's job at
    # apply time). A quote appearing multiple times is present -> not orphaned.
    body = "status 9 is refunds. later, status 9 is chargebacks."
    assert anno.find_quote(body, "status 9")
    assert not anno.is_orphaned(body, "status 9")


# --- outcome vocabulary ----------------------------------------------------


def test_terminal_outcomes():
    assert anno.is_terminal_outcome(anno.OUTCOME_APPLIED)
    assert anno.is_terminal_outcome(anno.OUTCOME_REJECTED)
    assert anno.is_terminal_outcome(anno.OUTCOME_ORPHANED)
    assert not anno.is_terminal_outcome(None)
    assert not anno.is_terminal_outcome("open")


def test_history_ttl_is_seven_days():
    assert anno.HISTORY_TTL_SECONDS == 7 * 24 * 60 * 60

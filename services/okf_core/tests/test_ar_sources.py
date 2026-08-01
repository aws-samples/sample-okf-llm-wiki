"""AR source-set selection and the source fingerprint.

The fingerprint is the staleness gate for every AR policy verdict — writer
(harvest/incremental build) and readers (chat check) must agree byte-for-byte,
so these tests pin the selector membership and every hash property the gate
relies on: order-independence, and sensitivity to content, keys, additions,
and removals.
"""

from __future__ import annotations

import pytest

from okf_core.ar_sources import (
    compute_source_hash,
    is_ar_source,
    select_ar_sources,
)


# --- selector membership ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "references/usage_guardrails.md",
        "references/enums/status_codes.md",
        "references/named_sets/sentinels.md",
        "references/metrics/points_per_race.md",
        "references/recipes/as_of_snapshot.md",
        "references/known_issues/dup_2019.md",
        "references/enums/nested/deep.md",  # prefix selectors are recursive
    ],
)
def test_source_paths_are_selected(path):
    assert is_ar_source(path)


@pytest.mark.parametrize(
    "path",
    [
        "tables/results.md",  # table docs (incl. gotchas) are excluded
        "references/joins/results_races.md",  # joins excluded on principle
        "references/glossary.md",
        "references/usage_guardrails.md.bak",  # exact-match selector, not prefix
        "external/other/dataset/references/usage_guardrails.md",  # cross-refs
        "index.md",
        "references/enums",  # the bare prefix dir itself is not a file match
    ],
)
def test_non_source_paths_are_excluded(path):
    assert not is_ar_source(path)


def test_select_ar_sources_filters_and_sorts():
    paths = [
        "tables/results.md",
        "references/named_sets/sentinels.md",
        "references/enums/status.md",
        "index.md",
    ]
    assert select_ar_sources(paths) == [
        "references/enums/status.md",
        "references/named_sets/sentinels.md",
    ]


# --- fingerprint properties ----------------------------------------------------


_A = ("references/usage_guardrails.md", b"never sum booked and billed")
_B = ("references/enums/status.md", b"-1 means unknown")


def test_hash_is_order_independent():
    assert compute_source_hash([_A, _B]) == compute_source_hash([_B, _A])


def test_hash_changes_on_content_change():
    changed = (_A[0], _A[1] + b" (updated)")
    assert compute_source_hash([_A, _B]) != compute_source_hash([changed, _B])


def test_hash_changes_on_key_rename():
    renamed = ("references/usage_guardrails2.md", _A[1])
    assert compute_source_hash([_A, _B]) != compute_source_hash([renamed, _B])


def test_hash_changes_on_addition_and_removal():
    extra = ("references/recipes/as_of.md", b"point-in-time requires as-of")
    both = compute_source_hash([_A, _B])
    assert both != compute_source_hash([_A, _B, extra])
    assert both != compute_source_hash([_A])


def test_hash_is_stable_hex():
    # Same inputs, same digest, twice — and a well-formed sha256 hex string.
    first = compute_source_hash([_A, _B])
    assert first == compute_source_hash([_A, _B])
    assert len(first) == 64 and int(first, 16) >= 0


def test_empty_source_set_raises():
    # "No sources" must be an explicit caller state, never a hash of nothing.
    with pytest.raises(ValueError):
        compute_source_hash([])

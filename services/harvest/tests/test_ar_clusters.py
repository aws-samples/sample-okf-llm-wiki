"""Deterministic source clustering (harvest.ar_clusters).

Clustering is code, not a model call, precisely so it CAN be pinned by unit
tests: same sources in, same clusters out, every time.
"""

from __future__ import annotations

from harvest import ar_clusters
from harvest.ar_clusters import (
    CONTRACT_PATH,
    TOPIC_CODES,
    TOPIC_CONTRACT,
    TOPIC_DISCLOSURE,
    TOPIC_MEASURES,
    TOPIC_MISC,
    TOPIC_POPULATION,
    TOPIC_TRAPS,
    cluster_sources,
)


# Default body is fat enough (~5k tokens) that a single-page topic clears the
# thin-topic fold — routing tests must see routing, not packing.
def _page(rel: str, body: str = "x" * 20_000, tags: tuple[str, ...] = ()) -> tuple[str, bytes]:
    front = ""
    if tags:
        front = "---\ntags:\n" + "".join(f"- {t}\n" for t in tags) + "---\n"
    return rel, (front + body).encode()


_CORPUS = [
    _page(CONTRACT_PATH),
    _page("references/enums/status.md"),
    _page("references/enums/bestellart.md"),
    _page("references/metrics/stock_level.md"),
    _page("references/named_sets/stock_kpis.md"),
    _page("references/known_issues/free_text_and_confidentiality.md"),
    _page("references/known_issues/sentinel_values.md"),
    _page("references/known_issues/checkpoints_fact_covers_order_subset.md"),
    _page("references/known_issues/weird_page.md"),  # no route keywords -> misc
]


def test_routing_by_path_and_filename():
    clusters, shared = cluster_sources(_CORPUS)
    topics = {c.topic: [r for r, _ in c.files] for c in clusters}
    assert [r for r, _ in shared] == [CONTRACT_PATH]
    # The contract is ALSO its own isolated cluster (its unique rules must be
    # mined directly, not just leaned on as context).
    assert topics[TOPIC_CONTRACT] == [CONTRACT_PATH]
    assert set(topics[TOPIC_CODES]) == {
        "references/enums/bestellart.md",
        "references/enums/status.md",
    }
    assert set(topics[TOPIC_MEASURES]) == {
        "references/metrics/stock_level.md",
        "references/named_sets/stock_kpis.md",
    }
    assert topics[TOPIC_DISCLOSURE] == [
        "references/known_issues/free_text_and_confidentiality.md"
    ]
    assert topics[TOPIC_TRAPS] == ["references/known_issues/sentinel_values.md"]
    assert topics[TOPIC_POPULATION] == [
        "references/known_issues/checkpoints_fact_covers_order_subset.md"
    ]
    assert topics[TOPIC_MISC] == ["references/known_issues/weird_page.md"]


def test_routing_by_frontmatter_tags_beats_opaque_filename():
    clusters, _ = cluster_sources(
        [
            _page(CONTRACT_PATH),
            _page("references/known_issues/kn_0042.md", tags=("confidentiality",)),
            _page("references/known_issues/kn_0043.md", tags=("sign-convention",)),
        ]
    )
    topics = {c.topic: [r for r, _ in c.files] for c in clusters}
    assert topics[TOPIC_DISCLOSURE] == ["references/known_issues/kn_0042.md"]
    assert topics[TOPIC_TRAPS] == ["references/known_issues/kn_0043.md"]


def test_thin_topics_fold_into_misc_but_isolated_topics_stand():
    # One tiny enums page (below both minimums) folds into misc; the equally
    # tiny disclosure page stands alone — isolation is the remedy for the page
    # single-pass extraction kept dropping.
    clusters, _ = cluster_sources(
        [
            _page(CONTRACT_PATH, body="x" * 100),
            _page("references/enums/tiny.md", body="x" * 100),
            _page("references/known_issues/free_text_and_confidentiality.md", body="x" * 100),
            _page("references/known_issues/other.md", body="x" * 100),
        ]
    )
    topics = {c.topic: [r for r, _ in c.files] for c in clusters}
    assert TOPIC_CODES not in topics
    assert set(topics[TOPIC_MISC]) == {
        "references/enums/tiny.md",
        "references/known_issues/other.md",
    }
    assert topics[TOPIC_DISCLOSURE] == [
        "references/known_issues/free_text_and_confidentiality.md"
    ]
    assert topics[TOPIC_CONTRACT] == [CONTRACT_PATH]


def test_oversized_topic_splits_under_the_caps():
    pages = [_page(f"references/enums/e{i:02d}.md") for i in range(11)]
    clusters, _ = cluster_sources(pages, max_files=4, max_tokens=10**9)
    code_clusters = [c for c in clusters if c.topic == TOPIC_CODES]
    assert [len(c.files) for c in code_clusters] == [4, 4, 3]
    # Stable ordering: sorted paths, chunked in order.
    assert [r for r, _ in code_clusters[0].files] == [
        "references/enums/e00.md",
        "references/enums/e01.md",
        "references/enums/e02.md",
        "references/enums/e03.md",
    ]


def test_token_cap_splits_before_the_file_cap():
    pages = [
        _page(f"references/enums/e{i}.md", body="x" * 40_000) for i in range(3)
    ]  # ~10k tokens each
    clusters, _ = cluster_sources(pages, max_files=8, max_tokens=15_000)
    code_clusters = [c for c in clusters if c.topic == TOPIC_CODES]
    assert [len(c.files) for c in code_clusters] == [1, 1, 1]


def test_deterministic_across_input_order():
    a = cluster_sources(_CORPUS)
    b = cluster_sources(list(reversed(_CORPUS)))
    as_names = [(c.topic, [r for r, _ in c.files]) for c in a[0]]
    bs_names = [(c.topic, [r for r, _ in c.files]) for c in b[0]]
    assert as_names == bs_names


def test_est_tokens_is_the_documented_heuristic():
    assert ar_clusters.est_tokens(b"x" * 400) == 100

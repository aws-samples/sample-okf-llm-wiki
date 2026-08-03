"""Deterministic source clustering for the map-reduce policy author.

Single-pass extraction over a whole source corpus loses recall to attention:
measured live (2026-08-03, the hifa dataset), three from-scratch authoring
runs — one Sonnet, two Opus, same sources — each produced a DIFFERENT
incomplete rule set (33/29/31 rules; whole pages like the confidentiality
handling rules silently skipped by two of the three). The fix is structural,
not model-tier: split the sources into topic clusters, give each cluster a
dedicated extractor, and let a synthesizer merge the union.

This module owns ONLY the clustering, and it is deliberately code, not a
model call: the same wiki state must always produce the same clusters
(reproducible runs, unit-testable routing) — an LLM-decided clustering would
reintroduce run-to-run variance at exactly the layer added to remove it.

Shape: :func:`cluster_sources` splits the gathered source list into
``(clusters, shared)`` where ``shared`` is the operating-contract page(s)
every extractor receives as context (``usage_guardrails.md``), and each
cluster is a topic-labelled file group packed under size caps:

* **Topic assignment** by path + filename + frontmatter tags (the authored
  pages carry tags like ``sign-convention``, ``confidentiality``).
* **Packing**: oversized topics split (the ``shard_policies`` packing loop's
  shape); undersized topics merge into the catch-all — EXCEPT isolated
  topics (disclosure/confidentiality), which stand alone on purpose: the
  page that keeps being dropped is the page that gets its own extractor.
* **Stable ordering** (sorted paths, fixed topic order) throughout.

The contract page is BOTH shared context for every cluster AND its own
isolated cluster — its unique rules (qualification, NULL-checkpoint
semantics, disclosure defaults) must be mined directly, not only leaned on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The operating-contract page: shared context for every extractor and an
#: isolated cluster of its own.
CONTRACT_PATH = "references/usage_guardrails.md"

#: Packing caps. Token counts are the cheap bytes/4 estimate — the caps bound
#: attention dilution, they are not billing math.
MAX_CLUSTER_FILES = 8
MAX_CLUSTER_TOKENS = 25_000
#: A topic below BOTH minimums merges into the catch-all rather than paying a
#: whole extractor for one thin page (isolated topics are exempt).
MIN_CLUSTER_FILES = 2
MIN_CLUSTER_TOKENS = 4_000

#: Fixed topic order — part of the determinism contract.
TOPIC_CONTRACT = "operating-contract"
TOPIC_DISCLOSURE = "confidentiality-and-disclosure"
TOPIC_CODES = "codes-and-enums"
TOPIC_MEASURES = "measures-and-additivity"
TOPIC_TRAPS = "data-traps"
TOPIC_POPULATION = "population-and-joins"
TOPIC_MISC = "misc"
_TOPIC_ORDER = (
    TOPIC_CONTRACT,
    TOPIC_DISCLOSURE,
    TOPIC_CODES,
    TOPIC_MEASURES,
    TOPIC_TRAPS,
    TOPIC_POPULATION,
    TOPIC_MISC,
)

#: Topics that never merge into the catch-all however thin: isolation IS the
#: remedy for the pages single-pass extraction kept dropping.
_ISOLATED_TOPICS = frozenset({TOPIC_CONTRACT, TOPIC_DISCLOSURE})

#: known_issues routing: (topic, keywords matched against filename + tags).
#: First match wins; order encodes priority (disclosure before everything).
_KNOWN_ISSUE_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (TOPIC_DISCLOSURE, ("free_text", "free-text", "confidential", "privacy")),
    (
        TOPIC_TRAPS,
        (
            "sentinel",
            "sign",
            "seconds",
            "unit",
            "string_encoded",
            "string-encoded",
            "boundary",
            "contradiction",
        ),
    ),
    (
        TOPIC_POPULATION,
        (
            "coverage",
            "subset",
            "duplicate",
            "deleted",
            "empty",
            "join",
            "partition",
            "permission",
            "undocumented",
            "undecoded",
        ),
    ),
)


@dataclass
class Cluster:
    """One topic-labelled extractor workload."""

    topic: str
    files: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(est_tokens(content) for _rel, content in self.files)


def est_tokens(content: bytes) -> int:
    """The cheap size estimate the packing caps are stated in."""
    return len(content) // 4


def _frontmatter_tags(content: bytes) -> list[str]:
    """Tag list from a page's YAML frontmatter, [] when absent/unparseable.

    Deliberately a cheap line scan (not a YAML parser): routing only needs the
    ``- tag`` items under ``tags:``, and a malformed page must degrade to
    filename routing rather than fail clustering.
    """
    text = content.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return []
    lines = text.split("\n")
    tags: list[str] = []
    in_tags = False
    for line in lines[1:200]:
        if line.strip() == "---":
            break
        if line.startswith("tags:"):
            in_tags = True
            continue
        if in_tags:
            if line.startswith("- "):
                tags.append(line[2:].strip().lower())
            elif not line.startswith(" "):
                in_tags = False
    return tags


def _topic_of(rel: str, content: bytes) -> str:
    """The routing rule: path family first, keyword routing for known_issues."""
    if rel == CONTRACT_PATH:
        return TOPIC_CONTRACT
    if rel.startswith("references/enums/"):
        return TOPIC_CODES
    if rel.startswith(("references/metrics/", "references/named_sets/", "references/recipes/")):
        return TOPIC_MEASURES
    if rel.startswith("references/known_issues/"):
        haystack = rel.lower() + " " + " ".join(_frontmatter_tags(content))
        for topic, keywords in _KNOWN_ISSUE_ROUTES:
            if any(k in haystack for k in keywords):
                return topic
        return TOPIC_MISC
    return TOPIC_MISC


def cluster_sources(
    sources: list[tuple[str, bytes]],
    *,
    max_files: int = MAX_CLUSTER_FILES,
    max_tokens: int = MAX_CLUSTER_TOKENS,
    min_files: int = MIN_CLUSTER_FILES,
    min_tokens: int = MIN_CLUSTER_TOKENS,
) -> tuple[list[Cluster], list[tuple[str, bytes]]]:
    """``(clusters, shared_context)`` for the extractor fleet.

    ``shared_context`` is the contract page(s) — handed to EVERY extractor in
    addition to its own cluster. The contract also forms its own isolated
    cluster so its unique rules are mined, not just leaned on.
    """
    by_topic: dict[str, list[tuple[str, bytes]]] = {t: [] for t in _TOPIC_ORDER}
    for rel, content in sorted(sources, key=lambda s: s[0]):
        by_topic[_topic_of(rel, content)].append((rel, content))

    shared = list(by_topic[TOPIC_CONTRACT])

    # Thin topics fold into the catch-all (isolated topics stand however thin).
    for topic in _TOPIC_ORDER:
        if topic in _ISOLATED_TOPICS or topic == TOPIC_MISC:
            continue
        files = by_topic[topic]
        total = sum(est_tokens(c) for _r, c in files)
        if files and len(files) < min_files and total < min_tokens:
            by_topic[TOPIC_MISC].extend(files)
            by_topic[topic] = []
    by_topic[TOPIC_MISC].sort(key=lambda s: s[0])

    # Pack each topic under the caps; an oversized topic splits into ordered
    # chunks (same shape as okf_core.policy_doc.shard_policies' group packing).
    clusters: list[Cluster] = []
    for topic in _TOPIC_ORDER:
        current = Cluster(topic=topic)
        for rel, content in by_topic[topic]:
            would_overflow = current.files and (
                len(current.files) >= max_files
                or current.tokens + est_tokens(content) > max_tokens
            )
            if would_overflow:
                clusters.append(current)
                current = Cluster(topic=topic)
            current.files.append((rel, content))
        if current.files:
            clusters.append(current)
    return clusters, shared

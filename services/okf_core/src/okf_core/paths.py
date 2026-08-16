"""Concept-id <-> filesystem path helpers.

A concept id is the slash-joined path of the concept's markdown file within a
bundle, minus the ``.md`` suffix (OKF SPEC §2). ``tables/races.md`` -> concept
id ``tables/races``.
"""

from __future__ import annotations

import re
from pathlib import Path

# A path segment: starts alnum/underscore, then alnum/underscore/dot/dash.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")

# Top-level bundle dir holding CROSS-DATASET reference docs, one subtree per
# counterpart dataset: ``external/<data_domain>/<dataset>/...``. Written ONLY by
# a cross-mode harvest, and ONLY in the initiating bundle (the pair docs have
# one home — the counterpart gets a derived XREF discovery signal, never a
# copy); wiped by a full harvest like any other authored output (it is NOT
# dot-prefixed — the docs are published concepts, indexed and embedded like the
# rest of the bundle).
EXTERNAL_DIR = "external"

# The ONE behavioural-contract doc every consumer reads before querying. Its
# id is a cross-service contract (the harvest authors it, lint requires it,
# the review workflow reserves it for the supervisor, and the chat agent's
# guardrails gate keys its read-tracking on it) — import it, don't retype it.
GUARDRAILS_CONCEPT_ID = "references/usage_guardrails"
GUARDRAILS_DOC_PATH = GUARDRAILS_CONCEPT_ID + ".md"

# Directory names deepagents uses for internal scratch (offloaded large tool
# results / conversation history). Routed to an ephemeral backend at runtime,
# but every bundle surface still refuses to treat a leak as concept docs.
INTERNAL_SCRATCH_DIRS = frozenset({"large_tool_results", "conversation_history"})


def is_reserved_rel_segments(segments: "tuple[str, ...] | list[str]") -> bool:
    """True when any path segment marks a NON-CONCEPT path: dot-prefixed
    (``.metadata``/``.context``/``.harvest`` — run inputs and state) or a
    deepagents internal scratch dir. The one rule shared by index generation,
    lint, and the link graph — a doc one surface hides and another indexes is
    an integration bug."""
    return any(
        seg.startswith(".") or seg in INTERNAL_SCRATCH_DIRS for seg in segments
    )


def external_pair_prefix(data_domain: str, dataset: str) -> str:
    """The concept-id prefix of the cross-dataset subtree for one counterpart.

    ``external_pair_prefix("sales", "orders") == "external/sales/orders/"`` —
    every doc a cross harvest against ``sales/orders`` authors lives under this
    prefix in the initiating bundle. Both segments are VALIDATED (ValueError on
    ``/``, ``..``, ``#``, …), which is why the harvest runtime, the write
    guard, and the reindex XREF keying all build the pair path through this
    helper rather than f-strings.
    """
    for seg in (data_domain, dataset):
        _validate_segment(seg)
    return f"{EXTERNAL_DIR}/{data_domain}/{dataset}/"


def is_external_concept_id(concept_id: str) -> bool:
    """True iff ``concept_id`` names a cross-dataset doc (under ``external/``)."""
    return concept_id.startswith(f"{EXTERNAL_DIR}/")


def _validate_segment(seg: str) -> None:
    if not _SEGMENT_RE.fullmatch(seg):
        raise ValueError(f"Invalid concept id segment: {seg!r}")


def concept_id_to_path(bundle_root: Path, concept_id: tuple[str, ...]) -> Path:
    if not concept_id:
        raise ValueError("concept_id must have at least one segment")
    for seg in concept_id:
        _validate_segment(seg)
    *dirs, name = concept_id
    return bundle_root.joinpath(*dirs, f"{name}.md")


def path_to_concept_id(bundle_root: Path, path: Path) -> tuple[str, ...]:
    rel = path.relative_to(bundle_root).with_suffix("")
    return tuple(rel.parts)


def parse_concept_id(s: str) -> tuple[str, ...]:
    parts = tuple(p for p in s.split("/") if p)
    if not parts:
        raise ValueError(f"Empty concept id: {s!r}")
    for p in parts:
        _validate_segment(p)
    return parts

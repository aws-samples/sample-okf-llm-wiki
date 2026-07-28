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

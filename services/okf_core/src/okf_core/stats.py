"""Deterministic bundle inventory — counts per concept type, no judgment.

One question, answered by a loop instead of a model: what is in the bundle?
The harvest supervisor otherwise answers it by globbing directories and
counting listing lines in its head — hundreds of paths of prompt tokens per
look, and models miscount long lists. This module returns the same answer in
a dozen fields, exactly.

Two deliberate properties:

* **Known concept types always appear, zeros included** — ``named_sets: 0``
  is a fact made visible, not a warning. Whether zero is RIGHT for this
  dataset is the model's judgment; this module never nags. That distinction
  is also why it exists apart from :mod:`okf_core.lint`: lint answers "is
  the bundle valid" (errors, a gate), this answers "what's in it"
  (inventory, no verdict).
* **Unknown reference subdirectories still count**, under their actual
  directory name — unknown is never invisible, so the total always
  reconciles with what a ``glob`` would show (minus the internal dirs,
  reported separately).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from okf_core.lint import _Context
from okf_core.paths import GUARDRAILS_DOC_PATH

#: Reference subtypes the authoring skill teaches (SKILL.md's bundle layout /
#: fact-types.md). Fixed so absence is VISIBLE: each key appears in every
#: report, zero or not. Keep in step with the vendored skill.
KNOWN_REFERENCE_SUBTYPES = (
    "joins",
    "enums",
    "metrics",
    "named_sets",
    "glossary",
    "recipes",
    "known_issues",
)


def bundle_stats(bundle_root: str | Path) -> dict[str, Any]:
    """Count the bundle's published concept docs, by concept type.

    Uses lint's ``_Context`` walk on purpose — the same exclusion rules
    (dot-dirs, generated ``index.md``/``log.md``) and the same snapshot-table
    resolution, so stats and lint can never disagree about what a "doc" is.
    """
    root = Path(bundle_root)
    ctx = _Context(root)
    docs = ctx.doc_paths

    references: dict[str, int] = {"usage_guardrails": 0}
    references.update({k: 0 for k in KNOWN_REFERENCE_SUBTYPES})
    counts = {"datasets": 0, "tables": 0, "external": 0, "other": 0}
    for rel in docs:
        parts = rel.split("/")
        top = parts[0]
        if rel == GUARDRAILS_DOC_PATH:
            references["usage_guardrails"] = 1
        elif top == "references":
            if len(parts) >= 3:
                # references/<subtype>/... — known subtypes pre-seeded at 0;
                # an unknown subdirectory counts under its real name.
                references[parts[1]] = references.get(parts[1], 0) + 1
            else:
                # A loose doc directly under references/ (guardrails aside).
                references["other"] = references.get("other", 0) + 1
        elif top in counts:
            counts[top] += 1
        else:
            counts["other"] += 1

    snapshot = ctx.snapshot_tables
    return {
        "total_docs": len(docs),
        "datasets": counts["datasets"],
        "tables": counts["tables"],
        # Snapshot tables ride along because "tables documented vs tables in
        # the source" is the one comparison a supervisor makes every run.
        # None = no snapshot on disk (nothing to compare against).
        "snapshot_tables": len(snapshot) if snapshot is not None else None,
        "references": references,
        "external": counts["external"],
        "other_docs": counts["other"],
    }

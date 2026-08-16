"""Dataset guidance — persistent, editable authoring instructions for one dataset.

A dataset often has nuances a generic harvest can't infer (a column's real
meaning, a metric convention, "ignore the staging tables"). Guidance is
free-text the operator attaches to a dataset that steers EVERY harvest of it —
full, incremental, and the annotation re-harvest. Unlike an annotation (per-user,
anchored to a quote, one-shot feedback) guidance is:

- **dataset-shared** (not per-user) — it lives on the dataset's registry mapping
  row (``pk="DOMAIN#<domain>"``, ``sk="DATASET#<dataset>"``), so a full harvest
  (which has no user) can pick it up, and every consumer sees one bundle;
- **persistent + editable** — it stays until changed, and editing it re-steers the
  next harvest;
- **change-driven** — a *changed* guidance is reason enough to re-harvest, even
  with zero open annotations (see :func:`is_dirty`).

This module owns only the pure invariants (attribute names, the dirty rule, the
length cap); the DynamoDB reads/writes live in the Control API and the prompt
threading in the harvest runner.

## Versioning + the dirty rule

Two timestamps track whether the LIVE guidance has been harvested yet:

- ``guidance_updated_at`` — bumped every time the text is edited.
- ``guidance_applied_version`` — set by the harvest runner, on a SUCCESSFUL run
  that carried the guidance, to the ``updated_at`` value it applied.

Guidance is **dirty** (needs a re-harvest to take effect) when it is non-empty and
its ``applied_version`` doesn't match its current ``updated_at`` — i.e. it was
edited (or never harvested) since the last successful apply. Stamping the applied
VERSION (not just "now") is what makes this precise: a run that used version X
clears dirtiness only for X, so an edit made mid-run stays dirty. A FAILED run
never stamps, so dirty guidance stays dirty until it actually lands.
"""

from __future__ import annotations

# Registry attribute names (on the DATASET# mapping row). Kept here so the Control
# API writer/reader and any future consumer agree on one spelling.
ATTR_TEXT = "guidance"
ATTR_UPDATED_AT = "guidance_updated_at"
ATTR_APPLIED_VERSION = "guidance_applied_version"

# A generous cap so guidance can't bloat the harvest prompt (or a DynamoDB item)
# unbounded. Long enough for a paragraph or two of real nuance.
MAX_LEN = 4000


def normalize(text: str | None) -> str:
    """Trim + cap raw guidance input. Empty/whitespace collapses to ``""`` (cleared)."""
    if not text:
        return ""
    return text.strip()[:MAX_LEN]


def is_dirty(text: str | None, updated_at: str | None, applied_version: str | None) -> bool:
    """True iff non-empty guidance has NOT been harvested at its current version.

    Dirty ⇔ there is guidance AND the last successfully-applied version differs
    from the current ``updated_at`` (never applied, or edited since). Empty
    guidance is never dirty — clearing it is not itself a reason to re-harvest.
    """
    if not (text or "").strip():
        return False
    return (applied_version or "") != (updated_at or "")

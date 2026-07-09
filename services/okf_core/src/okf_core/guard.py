"""OKF correctness rules as pure, side-effect-free functions.

The harvest agent authors with the *canonical* deepagents filesystem tools
(``write_file`` / ``edit_file``); it has no bespoke ``write_concept_doc``. OKF
correctness therefore rides on top in ``OKFGuardMiddleware`` (see
``harvest/okf_guard.py``), which calls the functions here to decide whether a
write is allowed *before* it touches disk.

Keeping the rules here — pure ``str``/``dict`` in, verdict out — means they are
unit-testable without deepagents, LangChain, or any AWS dependency, and are
shared identically by the middleware and by any offline validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from okf_core.document import (
    REQUIRED_FRONTMATTER_KEYS,
    OKFDocument,
    OKFDocumentError,
)

# Frontmatter key order used by the reference producer, so our output matches
# the golden bundle. Unknown keys keep their original order after these.
PREFERRED_KEY_ORDER = ("type", "resource", "title", "description", "tags", "timestamp")

# Matches a backtick-quoted identifier, e.g. `raceid` or `results.grid`.
_FIELD_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")


def _section_content_lines(body: str, heading: str) -> list[str]:
    """Non-blank lines under a top-level ``# heading`` section.

    Fence-aware: a ``#``-prefixed line *inside* a ```` ``` ```` code fence (e.g.
    a SQL/shell/Python comment) is NOT treated as a section boundary, so a fenced
    comment can't prematurely end the section. Fenced content lines are still
    returned as part of the section.
    """
    in_section = False
    in_fence = False
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if in_section and stripped:
                out.append(line)
            continue
        if not in_fence and stripped.startswith("# "):
            in_section = stripped == heading
            continue
        if in_section and stripped:
            out.append(line)
    return out


def schema_field_names(body: str) -> set[str]:
    """Column names declared under the ``# Schema`` section.

    The OKF ``# Schema`` section is a markdown table whose FIRST cell is the
    (backtick-quoted) column name; the Type/Description cells routinely contain
    other backticked tokens — type names (``bigint``), example values
    (``R``/``D``), formats (``M:SS.mmm``). Counting all of them would make the
    augmentation guard flag reworded prose as dropped columns (verified against
    the real ``results.md``). So we take the identifier from the FIRST table
    cell only; for non-table lines we fall back to the first backticked token.
    """
    names: set[str] = set()
    for line in _section_content_lines(body, "# Schema"):
        stripped = line.strip()
        if stripped.startswith("|"):
            # Markdown table row: the column name lives in the first cell.
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if cells:
                m = _FIELD_NAME_RE.search(cells[0])
                if m:
                    names.add(m.group(1))
        else:
            # Non-table line: first backticked token, if any.
            m = _FIELD_NAME_RE.search(line)
            if m:
                names.add(m.group(1))
    return names


def citation_entry_count(body: str) -> int:
    return len(_section_content_lines(body, "# Citations"))


def reorder_frontmatter(fm: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in PREFERRED_KEY_ORDER:
        if key in fm:
            ordered[key] = fm[key]
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def ensure_timestamp(
    fm: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return a copy of ``fm`` with ``timestamp`` filled in if missing."""
    out = dict(fm)
    if not out.get("timestamp"):
        stamp = now or datetime.now(timezone.utc)
        out["timestamp"] = stamp.isoformat(timespec="seconds")
    return out


@dataclass
class GuardResult:
    """Outcome of a guard check.

    ``ok`` -> the write may proceed. ``error`` -> a human-readable message to
    hand back to the model as a tool result so it self-corrects (no disk write).
    """

    ok: bool
    error: str | None = None


def check_frontmatter(frontmatter: dict[str, Any]) -> GuardResult:
    """Reject a write whose frontmatter is missing required OKF keys."""
    doc = OKFDocument(frontmatter=dict(frontmatter), body="")
    try:
        doc.validate()
    except OKFDocumentError as e:
        return GuardResult(
            ok=False,
            error=(
                f"Refusing to write document with invalid frontmatter: {e}. "
                f"Required keys: {', '.join(REQUIRED_FRONTMATTER_KEYS)}. "
                f"Re-write the file with the complete frontmatter block."
            ),
        )
    return GuardResult(ok=True)


def check_augmentation(
    existing_body: str,
    new_body: str,
    *,
    existing_type: str | None,
) -> GuardResult:
    """Reject a write that *shrinks* a Glue-derived concept's ``# Schema`` field
    set or its ``# Citations`` entry count.

    Real column names and the source ARN are populated from Glue metadata; any
    later pass (a doc-context enrichment, an incremental re-review) must
    *augment*, not silently drop them. This is the augmentation guard the
    reference producer baked into ``write_concept_doc``; here it is enforced by
    the middleware for arbitrary ``write_file``/``edit_file`` calls.
    """
    if existing_type not in ("Glue Table", "Glue Database"):
        return GuardResult(ok=True)

    old_fields = schema_field_names(existing_body)
    new_fields = schema_field_names(new_body)
    missing = sorted(old_fields - new_fields)
    if missing:
        shown = ", ".join(f"`{m}`" for m in missing[:10])
        truncated = " (and more)" if len(missing) > 10 else ""
        return GuardResult(
            ok=False,
            error=(
                f"Refusing to write: the existing # Schema section lists "
                f"{len(old_fields)} field(s) populated from Glue metadata, but "
                f"your new # Schema is missing {len(missing)} of them: {shown}"
                f"{truncated}. Augment the existing schema — read the current "
                f"file, then re-write with every field name preserved."
            ),
        )

    old_cites = citation_entry_count(existing_body)
    new_cites = citation_entry_count(new_body)
    if new_cites < old_cites:
        return GuardResult(
            ok=False,
            error=(
                f"Refusing to write: the existing # Citations section had "
                f"{old_cites} entries (including the Glue resource ARN), but "
                f"your new # Citations has only {new_cites}. Append rather than "
                f"replace — preserve every existing citation plus any new one."
            ),
        )
    return GuardResult(ok=True)

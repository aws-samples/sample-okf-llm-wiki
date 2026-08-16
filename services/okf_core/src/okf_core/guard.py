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

from okf_core.concept_types import is_schema_bearing_type
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
    """Reject a write that *shrinks* a source-derived concept's ``# Schema`` field
    set or its ``# Citations`` entry count.

    Real column names and the source resource (ARN/URI) are populated from the
    source's catalog metadata; any later pass (a doc-context enrichment, an
    incremental re-review) must *augment*, not silently drop them. This is the
    augmentation guard the reference producer baked into ``write_concept_doc``;
    here it is enforced by the middleware for arbitrary ``write_file``/
    ``edit_file`` calls. It applies to every schema-bearing concept type (see
    ``okf_core.concept_types.SCHEMA_BEARING_TYPES``), so a new source's table/
    database concepts are protected by registering their types there — no edit
    to this guard.
    """
    if not is_schema_bearing_type(existing_type):
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
                f"{len(old_fields)} field(s) populated from source metadata, but "
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
                f"{old_cites} entries (including the source resource), but "
                f"your new # Citations has only {new_cites}. Append rather than "
                f"replace — preserve every existing citation plus any new one."
            ),
        )
    return GuardResult(ok=True)


def check_verification_fields(
    new_fm: dict[str, Any], existing_fm: dict[str, Any] | None
) -> GuardResult:
    """Agents can never verify — without this single rule, ``verified`` is
    theater. A write may leave the verification triple null, or PRESERVE the
    existing doc's exact values (so a maintenance pass that keeps a verified
    computation's fence verbatim keeps its stamp — the content hash, not the
    field, is what binds); it may never invent or alter them. Only the human
    Verify path (Control API overlay -> the runtime's fold-in) sets them.
    Enforced on EVERY doc write, not just computations, so the fields can't
    be squatted on elsewhere either."""
    from okf_core.computations import VERIFICATION_FIELDS

    existing = existing_fm or {}
    for key in VERIFICATION_FIELDS:
        new_val = new_fm.get(key)
        if new_val is not None and new_val != existing.get(key):
            return GuardResult(
                ok=False,
                error=(
                    f"Refusing to write: `{key}` is set by a HUMAN through the "
                    f"verification UI, never by an agent. Leave the "
                    f"verification fields (`verified`, `verified_by`, "
                    f"`verified_sha256`) null — or, when re-writing an "
                    f"already-verified doc unchanged, preserve their existing "
                    f"values exactly."
                ),
            )
    return GuardResult(ok=True)


def check_computation_doc(
    rel_path: str, frontmatter: dict[str, Any], body: str
) -> GuardResult:
    """A computation doc must be shape-valid at WRITE time (same rationale as
    ``check_join_doc``: the author self-corrects in one retry instead of the
    supervisor inheriting a finding per doc after the fan-out). Reuses the
    exact parser lint and the executors run, so the guard can never disagree
    with them about what a valid computation is. Also pins the folder<->type
    pairing: an `Attested Computation` doc anywhere else would be invisible
    to `list_computations`, and a differently-typed doc inside the folder
    would be dead weight consumers can't run."""
    from okf_core.computations import (
        COMPUTATION_TYPE,
        COMPUTATIONS_PREFIX,
        is_computation_path,
        parse_computation,
    )
    from okf_core.document import OKFDocument

    is_comp_type = frontmatter.get("type") == COMPUTATION_TYPE
    in_folder = is_computation_path(rel_path)
    if not is_comp_type and not in_folder:
        return GuardResult(ok=True)
    if is_comp_type and not in_folder:
        return GuardResult(
            ok=False,
            error=(
                f"Refusing to write: a `type: {COMPUTATION_TYPE}` doc must "
                f"live directly under {COMPUTATIONS_PREFIX} (one doc per "
                f"computation, slug = filename) — `{rel_path}` is outside it, "
                f"so `list_computations` would never find it."
            ),
        )
    comp, errors = parse_computation(
        rel_path, OKFDocument(frontmatter=dict(frontmatter), body=body)
    )
    if errors:
        shown = "; ".join(errors[:6]) + (" …" if len(errors) > 6 else "")
        return GuardResult(
            ok=False,
            error=(
                f"Refusing to write this computation doc: {shown}. A "
                f"computation carries ONE read-only SELECT/WITH statement in "
                f"a ```sql fence under `# Computation`, with every `@hole` "
                f"declared in `parameters` (each with `type` and `example`) "
                f"and every declared parameter used. Fix and retry."
            ),
        )
    return GuardResult(ok=True)


def check_join_doc(rel_path: str, body: str) -> GuardResult:
    """A join doc must ship its ON clause as a qualified equality inside a
    ```sql fence. Enforced at WRITE time (not just lint time) so the author
    self-corrects in one retry instead of the supervisor inheriting one
    finding per join doc after the fan-out — seen live: 107 warnings on a
    240-table harvest. Inline backticks don't count: only fenced SQL gets
    schema-checked and EXPLAIN-validated downstream. Reuses lint's own
    primitives so guard and lint can never disagree on the rule."""
    from okf_core.lint import _JOIN_EQ_RE, _is_join_doc, _mask_sql, _sql_fences_in

    if not _is_join_doc(rel_path):
        return GuardResult(ok=True)
    for fence in _sql_fences_in(body):
        if _JOIN_EQ_RE.search(_mask_sql(fence, idents=False)):
            return GuardResult(ok=True)
    return GuardResult(
        ok=False,
        error=(
            "Refusing to write this join doc: no `table.column = "
            "table.column` condition inside a ```sql fence. A join doc must "
            "ship its ON clause as FENCED SQL — inline backticks don't "
            "count (only fenced SQL is schema-checked and EXPLAIN-"
            "validated). Add e.g.\n\n```sql\norders.customer_id = "
            "customers.id\n```\n\nwith any required cast/TRIM baked in, "
            "then retry the write."
        ),
    )

"""Framework-agnostic OKF write-guard logic.

``OKFGuardMiddleware`` (deepagents) is a thin adapter over this engine, so all
the OKF-correctness decisions live here and are unit-testable with plain
strings — no deepagents, LangChain, or AWS import required.

The engine decides, for a proposed ``write_file`` / ``edit_file`` on a ``.md``
path inside the dataset root, whether to:

* **allow** the write (optionally with rewritten frontmatter — auto-filled
  ``timestamp`` and canonical key order), or
* **deny** it with a message the model sees and self-corrects from,

and flips the link graph's dirty flag on an allowed write. Containment
(blocking ``../`` etc.) is handled by the deepagents ``FilesystemBackend``'s
``virtual_mode`` — NOT here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from okf_core.document import OKFDocument
from okf_core.guard import (
    check_augmentation,
    check_frontmatter,
    ensure_timestamp,
    reorder_frontmatter,
)
from okf_core.link_graph import LinkGraph


@dataclass
class WriteDecision:
    """Outcome of guarding a write.

    ``allow`` False -> ``message`` is returned to the model as the tool result
    and nothing touches disk. ``allow`` True -> if ``new_content`` is set, the
    write proceeds with that (frontmatter normalized) content instead of the
    original.
    """

    allow: bool
    message: str | None = None
    new_content: str | None = None


class OKFGuardEngine:
    """Holds per-session OKF-guard state (the link graph) and rules."""

    def __init__(self, link_graph: LinkGraph, *, now_fn=None):
        self.link_graph = link_graph
        # Injectable clock for deterministic tests.
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- the two tools we guard -----------------------------------------

    def guard_write_file(
        self, content: str, existing_text: str | None
    ) -> WriteDecision:
        """Guard a full-file ``write_file`` of a ``.md`` concept doc.

        ``existing_text`` is the current on-disk content (or None if new).
        """
        try:
            doc = OKFDocument.parse(content)
        except Exception as e:  # noqa: BLE001 - surface parse errors to the model
            return WriteDecision(
                allow=False,
                message=(
                    f"Refusing to write: could not parse the document "
                    f"({e}). An OKF concept doc must start with a YAML "
                    f"frontmatter block delimited by '---'."
                ),
            )

        # Auto-fill timestamp FIRST (the prompt tells the agent to omit it), so
        # a legitimately-absent timestamp isn't a spurious rejection — then
        # validate the remaining required keys (type/title/description).
        fm = ensure_timestamp(doc.frontmatter, now=self._now())
        fm_check = check_frontmatter(fm)
        if not fm_check.ok:
            return WriteDecision(allow=False, message=fm_check.error)

        if existing_text:
            existing = OKFDocument.parse(existing_text)
            aug = check_augmentation(
                existing.body,
                doc.body,
                existing_type=str(existing.frontmatter.get("type") or ""),
            )
            if not aug.ok:
                return WriteDecision(allow=False, message=aug.error)

        # Canonicalize key order. Rewrite the content so what lands on disk is
        # normalized (timestamp already filled above).
        fm = reorder_frontmatter(fm)
        normalized = OKFDocument(frontmatter=fm, body=doc.body).serialize()

        self.link_graph.mark_dirty()
        return WriteDecision(allow=True, new_content=normalized)

    def guard_edit_file(
        self, old_string: str, new_string: str, existing_text: str | None
    ) -> WriteDecision:
        """Guard an ``edit_file`` (exact string replacement) of a ``.md`` doc.

        We simulate the edit against the current file, then run the same
        frontmatter + augmentation checks on the *result*. If the file can't be
        read or the old_string isn't present, we defer to the handler (which
        will surface its own error) by allowing it through unchanged.
        """
        if existing_text is None:
            # Editing a file that doesn't exist yet — let the FS tool error.
            return WriteDecision(allow=True)
        if old_string not in existing_text:
            # Let the built-in edit tool report the no-match error itself.
            return WriteDecision(allow=True)

        resulting = existing_text.replace(old_string, new_string, 1)
        try:
            result_doc = OKFDocument.parse(resulting)
        except Exception as e:  # noqa: BLE001
            return WriteDecision(
                allow=False,
                message=(
                    f"Refusing this edit: the result would not parse as an OKF "
                    f"document ({e})."
                ),
            )

        # Validate required keys on the edit result, tolerating an absent
        # timestamp (it is auto-managed, not something an edit must preserve).
        fm_check = check_frontmatter(
            ensure_timestamp(result_doc.frontmatter, now=self._now())
        )
        if not fm_check.ok:
            return WriteDecision(
                allow=False,
                message=(
                    "Refusing this edit: the result would break required "
                    f"frontmatter. {fm_check.error}"
                ),
            )

        existing = OKFDocument.parse(existing_text)
        aug = check_augmentation(
            existing.body,
            result_doc.body,
            existing_type=str(existing.frontmatter.get("type") or ""),
        )
        if not aug.ok:
            return WriteDecision(allow=False, message=aug.error)

        # Edits are surgical; we don't rewrite content (that would defeat the
        # exact-string contract). Just mark the graph dirty and allow.
        self.link_graph.mark_dirty()
        return WriteDecision(allow=True)

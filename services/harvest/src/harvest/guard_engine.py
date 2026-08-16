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
    check_computation_doc,
    check_frontmatter,
    check_join_doc,
    check_verification_fields,
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
        self, content: str, existing_text: str | None,
        rel_path: str | None = None,
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

        existing_fm: dict | None = None
        if existing_text:
            # Tolerant: an existing doc that no longer parses (a torn write, a
            # doc bricked by an earlier bug) must be REPAIRABLE — treating it
            # as absent lets a full rewrite replace it, where raising here
            # would refuse every attempt forever.
            try:
                existing = OKFDocument.parse(existing_text)
            except Exception:  # noqa: BLE001 - unparseable existing = new doc
                existing = None
            if existing is not None:
                existing_fm = existing.frontmatter
                aug = check_augmentation(
                    existing.body,
                    doc.body,
                    existing_type=str(existing.frontmatter.get("type") or ""),
                )
                if not aug.ok:
                    return WriteDecision(allow=False, message=aug.error)

        # Verification is a HUMAN act: an agent write may only leave the
        # triple null or preserve the existing doc's exact values.
        ver_check = check_verification_fields(fm, existing_fm)
        if not ver_check.ok:
            return WriteDecision(allow=False, message=ver_check.error)

        join_check = check_join_doc(rel_path or "", doc.body)
        if not join_check.ok:
            return WriteDecision(allow=False, message=join_check.error)

        comp_check = check_computation_doc(rel_path or "", fm, doc.body)
        if not comp_check.ok:
            return WriteDecision(allow=False, message=comp_check.error)

        # Canonicalize key order. Rewrite the content so what lands on disk is
        # normalized (timestamp already filled above).
        fm = reorder_frontmatter(fm)
        normalized = OKFDocument(frontmatter=fm, body=doc.body).serialize()

        # The canonical serialization must ROUND-TRIP: yaml.safe_dump renders
        # a multi-line string containing '---' as an indented line inside the
        # scalar, and OKFDocument.parse's terminator scan (`strip() == '---'`)
        # then truncates the frontmatter mid-string — every later read of the
        # doc fails, verification can never bind, and no agent can repair it.
        # Refuse the write with the fix instead of bricking the doc.
        try:
            OKFDocument.parse(normalized).validate()
        except Exception as e:  # noqa: BLE001 - surface as a corrective refusal
            return WriteDecision(
                allow=False,
                message=(
                    f"Refusing to write: the document does not survive the "
                    f"canonical serialize/parse round-trip ({e}). This usually "
                    f"means a frontmatter string contains a line that is "
                    f"exactly `---` — rephrase that value (e.g. use a shorter "
                    f"dash run or move the text into the body) and retry."
                ),
            )

        self.link_graph.mark_dirty()
        return WriteDecision(allow=True, new_content=normalized)

    def guard_edit_file(
        self, old_string: str, new_string: str, existing_text: str | None,
        rel_path: str | None = None, replace_all: bool = False,
    ) -> WriteDecision:
        """Guard an ``edit_file`` (exact string replacement) of a ``.md`` doc.

        We simulate the edit against the current file, then run the same
        frontmatter + augmentation checks on the *result*. ``replace_all``
        MUST mirror the tool arg deepagents forwards to the backend — the
        guard otherwise validates a single-replacement document while the
        backend writes the replace-everywhere one, and every check here
        (verification fields, augmentation, computation shape) can be
        bypassed through the divergence. If the file can't be read or the
        old_string isn't present, we defer to the handler (which will surface
        its own error) by allowing it through unchanged.
        """
        if existing_text is None:
            # Editing a file that doesn't exist yet — let the FS tool error.
            return WriteDecision(allow=True)
        if old_string not in existing_text:
            # Let the built-in edit tool report the no-match error itself.
            return WriteDecision(allow=True)

        resulting = existing_text.replace(
            old_string, new_string, -1 if replace_all else 1
        )
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

        ver_check = check_verification_fields(
            result_doc.frontmatter, existing.frontmatter
        )
        if not ver_check.ok:
            return WriteDecision(
                allow=False, message=f"Refusing this edit: {ver_check.error}"
            )

        join_check = check_join_doc(rel_path or "", result_doc.body)
        if not join_check.ok:
            return WriteDecision(
                allow=False,
                message=f"Refusing this edit: {join_check.error}",
            )

        comp_check = check_computation_doc(
            rel_path or "", result_doc.frontmatter, result_doc.body
        )
        if not comp_check.ok:
            return WriteDecision(
                allow=False, message=f"Refusing this edit: {comp_check.error}"
            )

        # Edits are surgical; we don't rewrite content (that would defeat the
        # exact-string contract). Just mark the graph dirty and allow.
        self.link_graph.mark_dirty()
        return WriteDecision(allow=True)

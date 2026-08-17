"""``read_skill`` — one tool serving every vendored chat methodology skill.

Skills are how-to documents vendored beside the service under
``services/chat/skills/<name>/SKILL.md`` (the harvest okf-authoring layout)
and served as a TOOL (the read_me pattern): pulled into context on demand,
never riding every turn's system prompt — most turns need no methodology.

One generic tool replaces per-skill tools (``report_skill`` was the first):
adding a skill is dropping a directory. The catalog is scanned once at import,
the tool description enumerates it (name + description — the model's whole
discovery surface), and ``read_skill(name)`` returns the body.

Each SKILL.md opens with YAML frontmatter carrying ``name`` and
``description`` (the agent-skills convention, same as harvest's vendored
skill). A file that is missing, unreadable, or lacks either field is skipped
with a log line — a broken skill must never take the runtime down.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
from typing import Any

from langchain_core.tools import StructuredTool

from okf_core.document import OKFDocument, OKFDocumentError

log = logging.getLogger(__name__)

# The vendored skills root: services/chat/skills/<name>/SKILL.md.
_SKILLS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "skills"


def load_skill_catalog(root: pathlib.Path | None = None) -> list[dict[str, str]]:
    """Scan ``root`` for skills; return ``[{name, description, body}, ...]``.

    Sorted by name so the tool description (and therefore the cached prompt
    prefix) is stable across processes. Every skippable failure is logged,
    never raised.
    """
    root = _SKILLS_ROOT if root is None else root
    catalog: list[dict[str, str]] = []
    if not root.is_dir():
        log.warning("skills root %s missing — read_skill serves nothing", root)
        return catalog
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            doc = OKFDocument.parse(path.read_text(encoding="utf-8"))
        except (OSError, OKFDocumentError) as e:
            log.warning("skipping skill %s: %s", path.parent.name, e)
            continue
        name = str(doc.frontmatter.get("name") or "").strip()
        description = str(doc.frontmatter.get("description") or "").strip()
        if not name or not description:
            log.warning(
                "skipping skill %s: frontmatter must carry name + description",
                path.parent.name,
            )
            continue
        catalog.append({"name": name, "description": description, "body": doc.body})
    return catalog


# Loaded once at import (like the old REPORT_SKILL constant): the files ship
# in the image and never change within a process.
CATALOG: list[dict[str, str]] = load_skill_catalog()


def make_read_skill_tool(catalog: list[dict[str, str]] | None = None) -> Any:
    """Build the ``read_skill`` tool over ``catalog`` (defaults to the vendored set)."""
    skills = {s["name"]: s for s in (CATALOG if catalog is None else catalog)}

    def _read(name: str) -> Any:
        skill = skills.get((name or "").strip())
        if skill is None:
            return {
                "error": f"unknown skill {name!r} — available: "
                + (", ".join(sorted(skills)) or "(none)")
            }
        return skill["body"]

    listing = "\n".join(f"- {s['name']}: {s['description']}" for s in skills.values())
    doc = (
        inspect.cleandoc(
            """Read a methodology skill — the required structure, language, and
            discipline for a task, everything the task's tool schema cannot
            carry. Read the relevant skill BEFORE first doing its task in a
            conversation (once is enough — it stays in context), and never
            preemptively. Available skills:"""
        )
        + "\n"
        + (listing or "(none on this deployment — apply your best judgment)")
    )
    return StructuredTool.from_function(_read, name="read_skill", description=doc)

"""Markdown link extraction.

Ported from the OKF viewer's ``_extract_links`` so the harvest agent's link
graph and the viewer agree exactly on what counts as an intra-bundle link.

A link is a standard markdown link whose target is a ``.md`` file, resolved
relative to the linking document's directory. External links (``http://``) and
absolute paths (``/foo``) are ignored. Targets that resolve outside the bundle
root are dropped (OKF tolerates dangling cross-bundle links).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Capture group 1 = the .md target; an optional #anchor is allowed but dropped.
_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[A-Za-z0-9_\-]*)?\)")

# A markdown ATX heading line, e.g. "# Schema" or "## Joins".
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass
class Link:
    """A resolved intra-bundle link and the heading it sits under."""

    target: str  # concept id (no .md), relative to bundle root
    heading: str  # nearest preceding heading text, or "" at the top of the doc


def extract_links(body: str, doc_dir: Path, bundle_root: Path) -> list[str]:
    """Resolved concept ids that ``body`` links to (deduped, order-preserving)."""
    return [
        link.target for link in extract_links_with_headings(body, doc_dir, bundle_root)
    ]


def extract_links_with_headings(
    body: str, doc_dir: Path, bundle_root: Path
) -> list[Link]:
    """Like :func:`extract_links` but also records the heading each link is under.

    The heading lets the harvest agent's ``get_backlinks`` tell the model *where*
    in a referencing doc to edit, not merely that a reference exists.
    """
    out: list[Link] = []
    seen: set[str] = set()
    bundle_root_resolved = bundle_root.resolve()
    current_heading = ""
    for line in body.splitlines():
        m_head = _HEADING_RE.match(line)
        if m_head:
            current_heading = m_head.group(2).strip()
            continue
        for m in _LINK_RE.finditer(line):
            target = m.group(1)
            if "://" in target or target.startswith("/"):
                continue
            try:
                resolved = (
                    (doc_dir / target).resolve().relative_to(bundle_root_resolved)
                )
            except ValueError:
                continue
            rel = resolved.as_posix()
            if rel.endswith(".md"):
                rel = rel[:-3]
            if rel and rel not in seen:
                seen.add(rel)
                out.append(Link(target=rel, heading=current_heading))
    return out

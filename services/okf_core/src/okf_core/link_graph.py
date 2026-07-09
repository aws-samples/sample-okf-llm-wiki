"""A pure link/backlink graph over a bundle subtree — the harvest agent's
consistency utility for impact analysis.

No embeddings, no vector store: nodes are concept ids, edges are resolved
markdown links. When the agent changes ``tables/races.md`` it asks "what else
links to ``races``?" via :meth:`LinkGraph.get_backlinks`, gets the referencing
pages back (with the heading each link sits under), and updates them so the
bundle stays internally consistent.

Freshness model — **dirty on write, rebuild lazily on read**:

* The ``OKFGuardMiddleware`` flips :attr:`LinkGraph.dirty` on every successful
  ``write_file``/``edit_file`` (a cheap flag; no compute).
* The recompute happens only when a read method is called and the graph is
  dirty, so a run that writes 13 tables and never reads pays zero graph cost,
  and a burst of writes before one read collapses into a single rebuild.

Scope is the **dataset subtree only** (``root_dir``), matching the harvest
session's ``FilesystemBackend`` containment: no backlinks from datasets the
agent can't touch, and no walk of the whole ``okf/`` mount.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import networkx as nx

from okf_core.document import OKFDocument, OKFDocumentError
from okf_core.links import extract_links_with_headings

_INDEX_NAME = "index.md"
_LOG_NAME = "log.md"


class LinkGraph:
    """Lazily-rebuilt directed link graph over a single bundle subtree.

    Instantiate once per harvest session with the dataset root. The middleware
    marks it dirty on writes; the ``get_links`` / ``get_backlinks`` tools call
    :meth:`ensure_fresh` before answering.
    """

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir)
        self.graph: nx.DiGraph = nx.DiGraph()
        self.dirty: bool = True

    # -- lifecycle -------------------------------------------------------

    def mark_dirty(self) -> None:
        self.dirty = True

    def ensure_fresh(self) -> None:
        if self.dirty:
            self.rebuild()

    def rebuild(self) -> None:
        """Walk the subtree and rebuild nodes + edges from scratch.

        At OKF scale a full rebuild is milliseconds; we favour simplicity over
        an incremental re-parse. Malformed docs are skipped, not fatal.
        """
        g: nx.DiGraph = nx.DiGraph()
        if self.root.is_dir():
            for md_path in sorted(self.root.rglob("*.md")):
                if md_path.name in (_INDEX_NAME, _LOG_NAME):
                    continue
                rel = md_path.relative_to(self.root).with_suffix("")
                concept_id = "/".join(rel.parts)
                try:
                    doc = OKFDocument.parse(md_path.read_text(encoding="utf-8"))
                except (OKFDocumentError, OSError):
                    continue
                fm = doc.frontmatter or {}
                g.add_node(
                    concept_id,
                    title=str(fm.get("title") or concept_id),
                    type=str(fm.get("type") or "Unknown"),
                )
                for link in extract_links_with_headings(
                    doc.body or "", md_path.parent, self.root
                ):
                    # Links pointing outside the subtree are simply not resolved
                    # to a node; keep the edge only if the target is in-tree
                    # (added lazily so ordering does not matter).
                    g.add_edge(concept_id, link.target, heading=link.heading)
        # Drop edges whose target never resolved to a real in-tree concept doc.
        real_nodes = {n for n, d in g.nodes(data=True) if d.get("title") is not None}
        stale = [(u, v) for u, v in g.edges() if v not in real_nodes]
        g.remove_edges_from(stale)
        # Remove now-orphaned phantom nodes created purely as edge targets.
        phantom = [n for n in list(g.nodes()) if n not in real_nodes]
        g.remove_nodes_from(phantom)
        self.graph = g
        self.dirty = False

    # -- queries (used by the harvest tools) -----------------------------

    def _node_info(self, concept_id: str, heading: str) -> dict[str, Any]:
        data = self.graph.nodes.get(concept_id, {})
        return {
            "id": concept_id,
            "title": data.get("title", concept_id),
            "heading": heading,
        }

    def get_links(self, concept_id: str) -> list[dict[str, Any]]:
        """Concepts that ``concept_id`` links *to* (id + title + heading)."""
        self.ensure_fresh()
        if concept_id not in self.graph:
            return []
        out: list[dict[str, Any]] = []
        for _, target, edata in self.graph.out_edges(concept_id, data=True):
            out.append(self._node_info(target, edata.get("heading", "")))
        return out

    def get_backlinks(self, concept_id: str) -> list[dict[str, Any]]:
        """Concepts that link *to* ``concept_id`` — the impact-analysis query.

        The heading is the section *in the referencing doc* where the link
        sits, so the agent knows where to edit.
        """
        self.ensure_fresh()
        if concept_id not in self.graph:
            return []
        out: list[dict[str, Any]] = []
        for source, _, edata in self.graph.in_edges(concept_id, data=True):
            out.append(self._node_info(source, edata.get("heading", "")))
        return out

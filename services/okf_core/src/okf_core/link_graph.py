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
from typing import Any, Callable

import networkx as nx

from okf_core.document import OKFDocument, OKFDocumentError
from okf_core.links import extract_links_with_headings
from okf_core.paths import is_reserved_rel_segments

_INDEX_NAME = "index.md"
_LOG_NAME = "log.md"


class LinkGraph:
    """Lazily-rebuilt directed link graph over a single bundle subtree.

    Instantiate once per harvest session with the dataset root. The middleware
    marks it dirty on writes; the ``get_links`` / ``get_backlinks`` /
    ``cluster_concepts`` tools call :meth:`ensure_fresh` before answering.
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
                if is_reserved_rel_segments(rel.parts):
                    # Dot-dirs (.metadata/.context/.harvest) are authoring
                    # inputs and deepagents scratch dirs are runtime leakage —
                    # neither is a bundle concept, so keep them out of the
                    # graph (backlinks and review clusters only name real
                    # docs). Shared rule: okf_core.paths (lint + index_gen
                    # apply the same one).
                    continue
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

    def cluster(
        self,
        max_size: int = 5,
        exclude: Callable[[str], bool] | None = None,
    ) -> list[list[str]]:
        """Partition every concept doc into link-based clusters of ≤ ``max_size``.

        The review fan-out's grouping query: instead of one reviewer per doc,
        the supervisor dispatches one per cluster. Grouping is link-driven so a
        cluster holds docs that reference each other (a table with its
        enums/joins) — letting one reviewer also catch cross-doc contradictions
        — and leftover small groups are packed together so the cluster count
        stays low. Every non-reserved doc lands in exactly one cluster; the
        result is deterministic for a given bundle state.

        ``exclude`` drops docs from the partition BEFORE clusters form (a
        predicate on the concept id) — not just from the output: an excluded
        hub (the dataset overview, ``usage_guardrails``) links to everything,
        so leaving it in would seed a cluster of unrelated spokes and steal
        neighbors from their own tables. Excluded docs still exist in the
        graph (backlinks/links unaffected); they are simply nobody's review
        assignment. The caller owns the policy (see harvest.review).
        """
        max_size = max(1, int(max_size))
        self.ensure_fresh()
        und = self.graph.to_undirected(as_view=False)
        unassigned = set(und.nodes)
        if exclude is not None:
            unassigned = {n for n in unassigned if not exclude(n)}

        def rank(node: str) -> tuple[int, str]:
            # Hubs first (a table before its spoke references); id breaks ties
            # so the output is stable across runs.
            return (-und.degree(node), node)

        groups: list[list[str]] = []
        while unassigned:
            seed = min(unassigned, key=rank)
            unassigned.discard(seed)
            group = [seed]
            # Breadth-first over still-unassigned neighbours, hub-first, until
            # the group is full or its link neighbourhood is exhausted.
            i = 0
            while i < len(group) and len(group) < max_size:
                for nbr in sorted(und.neighbors(group[i]), key=rank):
                    if nbr in unassigned:
                        group.append(nbr)
                        unassigned.discard(nbr)
                        if len(group) >= max_size:
                            break
                i += 1
            groups.append(group)

        # First-fit-decreasing pack: merge small groups (isolated docs, tiny
        # components) so they don't each cost a reviewer. Packing concatenates
        # whole groups — it never splits a link-formed one.
        groups.sort(key=lambda g: (-len(g), g[0]))
        packed: list[list[str]] = []
        for group in groups:
            for bin_ in packed:
                if len(bin_) + len(group) <= max_size:
                    bin_.extend(group)
                    break
            else:
                packed.append(group)
        return packed

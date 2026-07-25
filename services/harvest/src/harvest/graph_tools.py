"""LangChain tools that expose the harvest session's ``LinkGraph`` to the model.

Three ordinary tools close over one ``LinkGraph`` instance (one per session):
``get_backlinks`` (who links *to* me — the impact-analysis query), ``get_links``
(who I link *to*), and ``cluster_concepts`` (partition the bundle into
link-related groups of ≤5 docs — the review fan-out's grouping query, so the
supervisor dispatches one reviewer per cluster instead of one per doc). To the
model they're just tools returning JSON. The graph rebuilds lazily on read if
dirty.

The ``langchain_core.tools.tool`` import is deferred so the factory can be
imported without langchain installed; ``make_graph_tools`` raises a clear error
if called in that state.
"""

from __future__ import annotations

from typing import Any

from okf_core.link_graph import LinkGraph


# Hard ceiling on docs per review cluster: a reviewer given more than this many
# docs skims instead of verifying, which defeats the review pass. The tool
# clamps requests above it rather than erroring.
MAX_CLUSTER_SIZE = 5


def make_graph_tools(link_graph: LinkGraph) -> list[Any]:
    """Return ``[get_backlinks, get_links, cluster_concepts]`` bound to this session's graph."""
    from langchain_core.tools import tool

    @tool
    def get_backlinks(concept_id: str) -> list[dict[str, Any]]:
        """List concepts that link TO this concept (impact analysis).

        Call this after changing a concept doc to find every page that
        references it — join docs, metrics, sibling tables — so you can review
        and update them and keep the bundle internally consistent. Returns a
        list of {id, title, heading}, where `heading` is the section in the
        referencing doc where the link sits (so you know where to edit).
        `concept_id` is the slash-joined path without `.md`, e.g. `tables/races`.
        """
        return link_graph.get_backlinks(concept_id)

    @tool
    def get_links(concept_id: str) -> list[dict[str, Any]]:
        """List concepts that this concept links TO.

        Returns a list of {id, title, heading}. `concept_id` is the slash-joined
        path without `.md`, e.g. `tables/results`.
        """
        return link_graph.get_links(concept_id)

    @tool
    def cluster_concepts(max_size: int = MAX_CLUSTER_SIZE) -> list[list[str]]:
        """Group every authored concept doc into link-based review clusters.

        Returns a list of clusters — each a list of concept ids (e.g.
        `["tables/races", "references/joins/circuits__races", ...]`) — covering
        EVERY non-reserved concept doc in the bundle exactly once, at most
        `max_size` docs per cluster (default and hard cap: 5). Docs that link
        to each other (a table with its enums/joins) are grouped together, so
        one reviewer can verify a related set in a single pass and also catch
        cross-doc contradictions. Use this to build the adversarial-review
        fan-out: dispatch one `reviewer` sub-agent per cluster.
        """
        size = min(max(1, int(max_size)), MAX_CLUSTER_SIZE)
        return link_graph.cluster(max_size=size)

    return [get_backlinks, get_links, cluster_concepts]

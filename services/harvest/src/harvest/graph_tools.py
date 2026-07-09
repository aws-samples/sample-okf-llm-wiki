"""LangChain tools that expose the harvest session's ``LinkGraph`` to the model.

Two ordinary tools close over one ``LinkGraph`` instance (one per session):
``get_backlinks`` (who links *to* me — the impact-analysis query) and
``get_links`` (who I link *to*). To the model they're just tools returning JSON:
``[{id, title, heading}, ...]``. The graph rebuilds lazily on read if dirty.

The ``langchain_core.tools.tool`` import is deferred so the factory can be
imported without langchain installed; ``make_graph_tools`` raises a clear error
if called in that state.
"""

from __future__ import annotations

from typing import Any

from okf_core.link_graph import LinkGraph


def make_graph_tools(link_graph: LinkGraph) -> list[Any]:
    """Return ``[get_backlinks, get_links]`` bound to this session's graph."""
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

    return [get_backlinks, get_links]

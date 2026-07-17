"""In-memory fakes for the chat service tests.

- ``FakeConsumptionTools`` mirrors the ConsumptionTools method signatures the
  agent tools wrap, recording the args each was called with (so scope injection
  is observable) and returning canned values.
- ``ScriptedChatModel`` is a minimal LangChain chat model that returns a fixed
  AIMessage (optionally with tool calls) so the graph runs without Bedrock.
"""

from __future__ import annotations

from typing import Any


class FakeConsumptionTools:
    """Records calls; returns canned values. Signatures match ConsumptionTools."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def list_domains(self) -> list[dict[str, Any]]:
        self._record("list_domains")
        return [{"data_domain": "sales", "dataset": "orders"}]

    def list_declared_domains(self) -> list[dict[str, Any]]:
        self._record("list_declared_domains")
        return []

    def search_domains(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        self._record("search_domains", query=query, top_k=top_k)
        return []

    def list_directory(self, data_domain: str, dataset: str, path: str = "") -> dict:
        self._record("list_directory", data_domain=data_domain, dataset=dataset, path=path)
        return {"path": path, "data_domain": data_domain, "dataset": dataset}

    def read_page(
        self,
        concept_id: str,
        data_domain: str,
        dataset: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        """Return a concept's markdown from S3 (paginate large docs by lines)."""
        self._record(
            "read_page",
            concept_id=concept_id,
            data_domain=data_domain,
            dataset=dataset,
            offset=offset,
            limit=limit,
        )
        return {"concept_id": concept_id, "data_domain": data_domain, "dataset": dataset}

    def get_backlinks(self, concept_id: str, data_domain: str, dataset: str) -> list[dict]:
        self._record("get_backlinks", concept_id=concept_id, data_domain=data_domain, dataset=dataset)
        return []

    def glob(self, pattern: str, data_domain: str, dataset: str) -> list[dict]:
        self._record("glob", pattern=pattern, data_domain=data_domain, dataset=dataset)
        return []

    def grep(
        self,
        pattern: str,
        data_domain: str,
        dataset: str,
        ignore_case: bool = True,
        max_results: int = 100,
    ) -> dict:
        self._record(
            "grep",
            pattern=pattern,
            data_domain=data_domain,
            dataset=dataset,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        return {"matches": []}

    def semantic_search(
        self,
        query: str,
        data_domain: str | None = None,
        dataset: str | None = None,
        table: str | None = None,
        type: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        self._record(
            "semantic_search",
            query=query,
            data_domain=data_domain,
            dataset=dataset,
            table=table,
            type=type,
            tags=tags,
            top_k=top_k,
        )
        return []


CHAT_CATALOG = [
    {
        "model": "us.anthropic.claude-opus-4-8",
        "label": "Claude Opus 4.8",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
    },
    {
        "model": "openai.gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
    },
]

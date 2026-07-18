"""Parse SQL / JSON out of a model's PLAIN-TEXT reply.

The harvest model runs with Claude adaptive thinking always on, and Bedrock
Converse rejects the assistant-message *prefill* that ``with_structured_output`` /
``response_format`` use to force a schema ("conversation must end with a user
message"). So the benchmark's LLM roles emit plain text and we extract the payload
here — the same "prompt for it, parse it ourselves" approach the reviewer subagent
uses (which returns plain markdown, not structured output).

These functions are pure (no LLM/AWS deps) and unit-tested, because robust
extraction is exactly what silently mis-scores a benchmark (a correct answer
wrapped in a ```sql fence must not parse to empty).
"""

from __future__ import annotations

import json
import re
from typing import Any

# A fenced code block, optionally tagged (```sql / ```json / ```). Non-greedy so
# multiple blocks are captured individually; DOTALL so bodies span lines.
_SQL_FENCE = re.compile(r"```(?:sql)?\s*\n?(.*?)```", re.S | re.I)
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.S | re.I)


def message_text(message: Any) -> str:
    """Extract the TEXT content of a model message, skipping thinking blocks.

    With adaptive thinking, ``message.content`` is often a list of blocks like
    ``[{"type":"reasoning_content",...}, {"type":"text","text":"..."}]`` — we want
    only the text. Accepts a LangChain message, a raw string, or a content list.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            # reasoning_content / thinking blocks are deliberately skipped.
        return "".join(parts)
    return str(content or "")


def extract_sql(text: Any) -> str:
    """Pull the SQL query out of a reply — the last fenced block, else the text.

    Prefers the LAST ```sql (or bare ```) fenced block (a model often restates
    the final query in a fence after reasoning in prose). Falls back to the whole
    stripped text when there is no fence. Returns "" for empty input.
    """
    s = message_text(text)
    if not s.strip():
        return ""
    blocks = _SQL_FENCE.findall(s)
    if blocks:
        return blocks[-1].strip()
    return s.strip()


def extract_json(text: Any, default: Any = None) -> Any:
    """Parse a JSON object/array out of a reply; return ``default`` on failure.

    Tries, in order: each fenced ```json block (last first), the whole text, then
    the first balanced ``{...}`` / ``[...]`` span. Tolerant by design — a role that
    can't be parsed degrades to ``default`` (the caller treats that as a benign
    outcome, never a crash).
    """
    s = message_text(text)
    if not s.strip():
        return default

    for candidate in reversed(_JSON_FENCE.findall(s)):
        parsed = _try_json(candidate)
        if parsed is not None:
            return parsed

    parsed = _try_json(s)
    if parsed is not None:
        return parsed

    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = s.find(open_c), s.rfind(close_c)
        if 0 <= i < j:
            parsed = _try_json(s[i : j + 1])
            if parsed is not None:
                return parsed

    return default


def _try_json(candidate: str) -> Any:
    try:
        return json.loads(candidate.strip())
    except (ValueError, TypeError):
        return None

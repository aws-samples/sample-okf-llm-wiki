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

# A fenced code block, inner text captured RAW — the tag/body split happens in
# :func:`_fence_candidates`, not here: a regex-captured tag word read the first
# word of a SINGLE-LINE fence (```SELECT 1```) as a language tag, dropping the
# fence as foreign-tagged. Non-greedy so multiple blocks are captured
# individually; DOTALL so bodies span lines.
_FENCE = re.compile(r"```(.*?)```", re.S)
# A fence opener LINE that is a language tag: one optional [\w+-] token, spaces
# allowed. Anything else on the opener line is content, not an info string.
_FENCE_TAG = re.compile(r"[ \t]*([\w+-]*)[ \t]*")
# Single-line fences have no tag line; a leading `sql` token followed by
# whitespace is the one tagging idiom models still use there (```sql SELECT 1```).
_INLINE_SQL_TAG = re.compile(r"[ \t]*sql[ \t]+", re.I)
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


def reasoning_text(message: Any) -> str:
    """Extract the THINKING text of a model message (the inverse of ``message_text``).

    Adaptive thinking returns reasoning as its own content blocks, which
    ``message_text`` deliberately skips. The solver TRACE wants them (the reasoning
    is the most useful part of "what did this solver actually do"), so this pulls
    them out, tolerating the provider shapes ``chat/server.py`` already handles:
    Converse ``{"type":"reasoning_content","reasoning_content":{"text":…}}`` (or a
    bare string), GPT Responses ``{"type":"reasoning","summary":[{"text":…}]}``, and
    a plain ``{"type":"thinking","thinking":…}``. Returns "" when there is none.
    """
    content = getattr(message, "content", message)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in ("reasoning_content", "reasoning", "thinking"):
            continue
        parts.append(_reasoning_block_text(block))
    return "".join(p for p in parts if p)


def _reasoning_block_text(block: dict) -> str:
    """The text inside one reasoning block, across provider shapes."""
    for key in ("reasoning_content", "thinking", "summary"):
        value = block.get(key)
        if isinstance(value, dict):
            return str(value.get("text") or "")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(item.get("text") or "")
                for item in value
                if isinstance(item, dict)
            )
    return str(block.get("text") or "")


def _fence_candidates(s: str) -> list[tuple[str, str]]:
    """Every fenced block in ``s`` as ``(tag, body)``.

    A token counts as a language tag ONLY on a standard fenced block whose
    opener line ends in a newline (```tag\\n...```). A SINGLE-LINE fence
    (open + close on one line, as in ```SELECT 1```) has no tag line — its
    whole inner text is content — except a leading ``sql`` token followed by
    whitespace (```sql SELECT 1```), which tags the remainder as SQL.
    """
    out: list[tuple[str, str]] = []
    for inner in _FENCE.findall(s):
        if "\n" in inner:
            header, _, body = inner.partition("\n")
            if _FENCE_TAG.fullmatch(header):
                out.append((header.strip(), body))
            else:
                # A multi-word opener line (```SELECT *\nFROM t```) is content,
                # not an info string.
                out.append(("", inner))
            continue
        m = _INLINE_SQL_TAG.match(inner)
        if m:
            out.append(("sql", inner[m.end():]))
        else:
            out.append(("", inner))
    return out


def extract_sql(text: Any) -> str:
    """Pull the SQL query out of a reply — the last SQL fence, else the text.

    Prefers the LAST ```sql-tagged fence (a model often restates the final
    query in a fence after reasoning in prose — and a trailing non-SQL fence
    must not beat it); falls back to the last UNTAGGED fence only when no
    sql-tagged one exists. A fence tagged as anything else (```text, ```json)
    is never read as SQL; a single-line fence's first word is content, never a
    tag (see :func:`_fence_candidates`). Falls back to the whole stripped text
    when there is no usable fence. Returns "" for empty input.
    """
    s = message_text(text)
    if not s.strip():
        return ""
    sql_blocks: list[str] = []
    bare_blocks: list[str] = []
    for tag, body in _fence_candidates(s):
        if tag.lower() == "sql":
            sql_blocks.append(body)
        elif not tag:
            bare_blocks.append(body)
    blocks = sql_blocks or bare_blocks
    if blocks:
        return blocks[-1].strip()
    return s.strip()


def extract_text(text: Any) -> str:
    """The reply's plain text, stripped — for checks whose prediction IS prose.

    The Behavior check's solver answers in free-form text (no fence protocol);
    its whole final message is the prediction the judge grades. Thinking blocks
    are already skipped by ``message_text``.
    """
    return message_text(text).strip()


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

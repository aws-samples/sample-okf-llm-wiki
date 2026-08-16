"""Chat memory record parsing + namespace derivation, shared by services.

AgentCore Memory records for the chat (one CUSTOM strategy, prompts +
``metadata_schema`` in ``infra/durable/agent_memory.tf``) carry their
structured fields (``type``/``dataset``/``expires_at``) as REAL record
metadata. Two weaker sources are parsed as fallbacks for records whose
extraction drifted or that predate the schema: a metadata object the
extractor sometimes embeds inside the content JSON, and the legacy header
line::

    [type:stated|binding|personal] [dataset:<domain/dataset>|-] [expires:YYYY-MM-DD|-]
    <memory text>

Both consumers parse with THIS module: the chat runtime (recall filtering +
lazy TTL deletion — ``chat.memory``) and the Control API (the Memory page's
list/edit routes). The namespace/actor derivation ALSO lives here: CreateEvent
constrains actorId/sessionId to ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` (a raw Cognito
sub is normally a UUID, so sanitizing is a no-op — but write and read must
never diverge, so every service derives through :func:`memory_namespace`).
Keep it dependency-free: okf_core owns cross-service invariants precisely so
the two sides cannot drift.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any

_HEADER_RE = re.compile(
    r"\[type:(?P<type>stated|binding|personal)\]\s*"
    r"\[dataset:(?P<dataset>[^\]]*)\]\s*"
    r"\[expires:(?P<expires>[^\]]*)\]",
)

#: CreateEvent's actorId/sessionId constraint: [a-zA-Z0-9][a-zA-Z0-9-_]*.
_ID_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def safe_actor_id(value: str) -> str:
    """``value`` sanitized to the CreateEvent actorId/sessionId pattern.

    The runtime's internal thread id (``<sub>:<thread>``) carries a COLON,
    which failed every event write live (ValidationException).
    """
    cleaned = _ID_UNSAFE_RE.sub("_", value or "")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"m{cleaned}"
    return cleaned


def memory_namespace(user_sub: str, *, prefix: str = "wiki") -> str:
    """The user's namespace (CONVENTIONS.md: ``wiki/<sanitized sub>``).

    The strategy's namespace template resolves ``{actorId}`` from CreateEvent's
    actorId, so the namespace MUST derive from the SAME sanitized id the events
    are written under — the runtime's recall and the Control API's list/own
    checks both call this.
    """
    return f"{prefix}/{safe_actor_id(user_sub)}"


def format_header(*, type: str, dataset: str = "", expires: str = "") -> str:
    """The canonical header line (used when rewriting an edited record)."""
    return (
        f"[type:{type}] [dataset:{dataset or '-'}] [expires:{expires or '-'}]"
    )


def _metadata_str(value: Any) -> str:
    """One metadata value as a plain string (the API wraps values in a typed
    union — ``{"stringValue": ...}`` — but be tolerant of plain strings)."""
    if isinstance(value, dict):
        for k in ("stringValue", "numberValue"):
            if k in value:
                return str(value[k])
        return ""
    return str(value) if value is not None else ""


#: Values a weaker extractor emits INSTEAD of omitting a field it was told to
#: omit (observed live: dataset "N/A", expires_at "N/A") — all mean "unset".
_PLACEHOLDERS = frozenset({"", "-", "n/a", "na", "none", "null", "unknown"})


def _clean_dataset(value: str) -> str:
    v = (value or "").strip()
    return "" if v.lower() in _PLACEHOLDERS else v


def _clean_expires(value: str) -> str:
    """A usable YYYY-MM-DD window, or '' — placeholders, unparseable text, and
    max-date sentinels (observed live: "9999-12-31" for "never expires") all
    mean the memory simply has no window. Dropping the WINDOW, never the
    memory, is the same stance :func:`is_expired` takes on a mangled date."""
    v = (value or "").strip()
    if v.lower() in _PLACEHOLDERS:
        return ""
    try:
        parsed = date.fromisoformat(v)
    except ValueError:
        return ""
    if parsed.year >= 9999:
        return ""
    return v


def parse_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize one raw memory record to ``{id, type, dataset, expires, text}``.

    Structured fields come from the record's REAL metadata (the awscc-managed
    strategy's ``metadata_schema`` — ``type``/``dataset``/``expires_at``);
    records whose extraction drifted (or predate the schema) fall back to the
    legacy content-header line. A record with neither degrades to a usable
    generic memory, not an error — ``type="stated"``, no dataset, no expiry,
    full content as text.
    """
    content = record.get("content") or {}
    text = content.get("text") if isinstance(content, dict) else str(content)
    text = (text or "").strip()
    # The managed user-preference pipeline emits a FIXED JSON output schema
    # ({"context", "preference", "categories"} — appendToPrompt replaces the
    # instructions but "the output schema remains the same", found live) —
    # unwrap it to the human sentence; the structured fields live in record
    # METADATA, not in this JSON.
    content_meta: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict) and ("preference" in body or "context" in body):
            text = str(body.get("preference") or body.get("context") or "").strip()
            # Observed drift: the extractor sometimes embeds a metadata object
            # INSIDE the content JSON too — keep it as a fallback source
            # (weaker than real record metadata, stronger than the header).
            if isinstance(body.get("metadata"), dict):
                content_meta = body["metadata"]
    out = {
        "id": record.get("memoryRecordId") or "",
        "type": "stated",
        "dataset": "",
        "expires": "",
        "text": text,
        # True only when the record's REAL metadata carried the type — i.e. a
        # server-side ``type`` metadataFilter can actually match this record.
        # Fallback-sourced types (content JSON / header) are display-true but
        # filter-invisible, and recall paths must treat them differently.
        "type_from_metadata": False,
    }
    # The legacy header line is parsed FIRST (and always stripped from the
    # text), then real metadata wins wherever it is present.
    m = _HEADER_RE.search(text)
    if m:
        out["type"] = m.group("type")
        dataset = m.group("dataset").strip()
        expires = m.group("expires").strip()
        out["dataset"] = "" if dataset in ("-", "") else dataset
        out["expires"] = "" if expires in ("-", "") else expires
        out["text"] = (text[: m.start()] + text[m.end() :]).strip()
    real_meta = record.get("metadata") or {}
    for meta in (content_meta, real_meta):
        if not isinstance(meta, dict):
            continue
        mtype = _metadata_str(meta.get("type"))
        if mtype in ("stated", "binding", "personal"):
            out["type"] = mtype
            if meta is real_meta:
                out["type_from_metadata"] = True
        mdataset = _metadata_str(meta.get("dataset"))
        if mdataset:
            out["dataset"] = mdataset
        mexpires = _metadata_str(meta.get("expires_at"))
        if mexpires:
            out["expires"] = mexpires
    # Final scrub over EVERY source (header, content JSON, real metadata):
    # placeholder values and sentinel dates an extractor emitted instead of
    # omitting the field degrade to unset — the record stays, its noise goes.
    out["dataset"] = _clean_dataset(out["dataset"])
    out["expires"] = _clean_expires(out["expires"])
    return out


def is_expired(parsed: dict[str, Any], *, today: date | None = None) -> bool:
    """True when the record's validity window has passed.

    An unparseable date does NOT expire the record (a mangled header must not
    silently destroy a memory — it just loses its window).
    """
    raw = parsed.get("expires") or ""
    if not raw:
        return False
    try:
        expires = date.fromisoformat(raw)
    except ValueError:
        return False
    return expires < (today or datetime.now(timezone.utc).date())

"""The S3 Vectors contract, shared by the reindex worker and the consumption
MCP server so they agree on keys, metadata, and embed text.

Frozen decisions (immutable once the index is created — see OKF_DESIGN §"What we
store"): Titan Text Embeddings V2, **512 dims**, **cosine**, one index. The
non-filterable metadata keys are declared at index creation and CANNOT change:
``title``, ``description``, ``s3_key``. Everything else is filterable, and
filterable metadata must stay under 2 KB/vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- Frozen index parameters -------------------------------------------------

EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 512
DISTANCE_METRIC = "cosine"
DATA_TYPE = "float32"

# Declared at CreateIndex; immutable. Bulk text stays in S3, never here.
NON_FILTERABLE_METADATA_KEYS = ("title", "description", "s3_key")

# The query knobs. domain/dataset/table are separate keys because S3 Vectors
# filters have no prefix/substring operator.
FILTERABLE_METADATA_KEYS = ("data_domain", "dataset", "table", "type", "tags")

# Titan V2 rejects inputs longer than this many characters.
MAX_EMBED_CHARS = 50_000

# S3 Vectors filterable metadata is capped per vector; keep hierarchy + tags
# small. We defensively trim tags to fit.
FILTERABLE_METADATA_BUDGET_BYTES = 2_048


@dataclass
class ConceptCoordinates:
    """Where a concept sits in the domain -> dataset -> table hierarchy.

    ``s3_key`` is the object key in the bundle bucket (the source of truth);
    ``concept_path`` is the vector key (deterministic, so re-embedding a concept
    overwrites rather than duplicates).
    """

    data_domain: str
    dataset: str
    concept_path: str  # e.g. "sales/orders/tables/customers"
    s3_key: str
    table: str | None = None


def vector_key(coords: ConceptCoordinates) -> str:
    """Deterministic vector key = the concept path. Overwrites on re-embed."""
    return coords.concept_path


def build_embed_text(frontmatter: dict[str, Any], body: str = "") -> str:
    """The text handed to Titan.

    Per OKF_DESIGN open-question #2 we embed **frontmatter** (title, description,
    type, tags) plus the concept's opening ``# Overview`` prose if present —
    overview improves recall without pulling the whole doc in. Truncated to
    Titan's char limit.
    """
    parts: list[str] = []
    title = frontmatter.get("title")
    desc = frontmatter.get("description")
    typ = frontmatter.get("type")
    tags = frontmatter.get("tags")
    if title:
        parts.append(f"Title: {title}")
    if typ:
        parts.append(f"Type: {typ}")
    if desc:
        parts.append(f"Description: {desc}")
    if tags:
        tag_str = ", ".join(_as_tag_list(tags))
        if tag_str:
            parts.append(f"Tags: {tag_str}")
    overview = _first_section(body, "# Overview")
    if overview:
        parts.append(overview)
    text = "\n".join(parts).strip()
    return text[:MAX_EMBED_CHARS]


def build_filterable_metadata(
    coords: ConceptCoordinates, frontmatter: dict[str, Any]
) -> dict[str, Any]:
    """Filterable metadata (query knobs), trimmed to the 2 KB budget."""
    md: dict[str, Any] = {
        "data_domain": coords.data_domain,
        "dataset": coords.dataset,
        "type": str(frontmatter.get("type") or "Unknown"),
    }
    if coords.table:
        md["table"] = coords.table
    tags = _as_tag_list(frontmatter.get("tags"))
    md = _fit_tags(md, tags)
    return md


def build_non_filterable_metadata(
    coords: ConceptCoordinates, frontmatter: dict[str, Any]
) -> dict[str, Any]:
    """Non-filterable metadata — only the declared keys, nothing else."""
    return {
        "title": str(frontmatter.get("title") or coords.concept_path),
        "description": str(frontmatter.get("description") or ""),
        "s3_key": coords.s3_key,
    }


def _first_section(body: str, heading: str) -> str:
    """Return the prose under a top-level ``# heading`` up to the next ``# ``.

    Used to pull the concept's ``# Overview`` paragraph into the embed text.
    Nested (``##``) headings inside the section are kept; the section ends at
    the next top-level ``# `` heading. Fence-aware: a ``#``-prefixed line inside
    a ```` ``` ```` code fence (e.g. a SQL/shell comment) does NOT end the
    section, so overview prose after a fenced example isn't silently dropped.
    """
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if in_section:
                out.append(line)
            continue
        if not in_fence and stripped.startswith("# "):
            if in_section:
                break  # next top-level section: stop
            in_section = stripped == heading
            continue
        if in_section:
            out.append(line)
    return "\n".join(out).strip()


def _as_tag_list(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, (list, tuple)):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [str(tags)]


def _fit_tags(md: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    """Add as many tags as fit under the filterable-metadata byte budget."""
    import json

    base_size = len(json.dumps(md).encode("utf-8"))
    kept: list[str] = []
    for tag in tags:
        candidate = kept + [tag]
        size = base_size + len(json.dumps({"tags": candidate}).encode("utf-8"))
        if size > FILTERABLE_METADATA_BUDGET_BYTES:
            break
        kept.append(tag)
    if kept:
        md["tags"] = kept
    return md

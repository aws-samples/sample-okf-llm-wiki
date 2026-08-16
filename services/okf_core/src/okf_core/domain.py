"""Declared-domain concept model.

A *data domain* is a first-class, pre-declared entity: an operator declares it
(name + description + context) BEFORE any Glue database is mapped into it, and a
mapping must select from the declared set. See ``docs/CONVENTIONS.md`` (the
``DOMAIN#/META`` registry item) and ``docs/ARCHITECTURE.md``.

So a domain can be semantically searched alongside dataset/table concepts, its
declared description + context are materialised as an ordinary OKF markdown doc
in the bundle bucket and picked up by the normal reindex → S3 Vectors pipeline
(the "S3 markdown is the source of truth; the index is derived" invariant). The
doc lives at a reserved ``_domain`` pseudo-dataset segment so it parses as a
normal 3-segment concept (``okf/<domain>/_domain/overview.md``) with no change
to ``parse_bundle_key`` — and, being a *sibling* of every real dataset dir, it
never leaks into a dataset's index or listing.

This module is pure (no AWS, no agent deps): it owns the constants and the
deterministic doc builder so the harvest writer, the reindex reader, and tests
all agree on the exact shape. The S3-key helpers live in
``okf_aws.s3_bundle`` (they need the ``okf/`` prefix convention).
"""

from __future__ import annotations

from typing import Any

# The reserved pseudo-dataset segment a domain doc lives under. Chosen so the
# key ``okf/<domain>/_domain/overview.md`` has the 3 segments parse_bundle_key
# requires, yet ``_domain`` can never collide with a real Glue database name
# (the mapping guard rejects it) and is trivially filtered out of dataset lists.
DOMAIN_DATASET = "_domain"

# The single concept id under the pseudo-dataset. One doc per domain.
DOMAIN_CONCEPT_ID = "overview"

# The frontmatter ``type`` for a domain concept. Already a filterable S3 Vectors
# metadata key (okf_core.embedding.FILTERABLE_METADATA_KEYS includes "type"), so
# ``semantic_search(type="Domain")`` works with ZERO change to the immutable
# index schema.
DOMAIN_DOC_TYPE = "Domain"


def build_domain_document(
    *,
    data_domain: str,
    description: str,
    context: str,
    timestamp: str,
) -> str:
    """Build the OKF markdown for a declared domain (deterministic, not authored).

    Unlike dataset/table docs (which an LLM authors from Glue), a domain doc is a
    faithful materialisation of the operator's declaration so it can be embedded.
    Both the short ``description`` (frontmatter) and the richer ``context`` (the
    ``# Overview`` body) feed :func:`okf_core.embedding.build_embed_text`, so a
    ``search_domains`` query matches on either.

    Uses :class:`okf_core.document.OKFDocument` to serialise so the frontmatter
    layout matches every other concept exactly.
    """
    from okf_core.document import OKFDocument

    body_parts = ["# Overview", "", (context or description or "").strip()]
    doc = OKFDocument(
        frontmatter={
            "type": DOMAIN_DOC_TYPE,
            "title": data_domain,
            "description": (description or "").strip(),
            "timestamp": timestamp,
        },
        body="\n".join(body_parts).strip() + "\n",
    )
    return doc.serialize()


def is_domain_dataset(dataset: str | None) -> bool:
    """True iff ``dataset`` is the reserved domain-doc pseudo-dataset.

    Callers that enumerate datasets (the consumption ``list_domains``, the UI
    picker) use this to hide ``_domain`` — it is a materialised domain doc, not a
    real Glue-backed dataset.
    """
    return dataset == DOMAIN_DATASET


def domain_vector_key(data_domain: str) -> str:
    """The S3 Vectors key for a domain's concept: ``<domain>/_domain/overview``.

    Mirrors :meth:`okf_aws.ConceptLocation.vector_key` for the domain doc so the
    reindex worker and any direct reader agree. Not built from AWS state — pure.
    """
    return f"{data_domain}/{DOMAIN_DATASET}/{DOMAIN_CONCEPT_ID}"


def domain_metadata(data_domain: str, item: dict[str, Any]) -> dict[str, Any]:
    """Shape a ``DOMAIN#/META`` DynamoDB item into the public domain dict.

    Shared by the Control API and the consumption MCP server so the two agree on
    the field set a declared domain exposes.
    """
    return {
        "data_domain": data_domain,
        "description": item.get("description", "") or "",
        "context": item.get("context", "") or "",
        "created_at": item.get("created_at", "") or "",
        "updated_at": item.get("updated_at", "") or "",
    }

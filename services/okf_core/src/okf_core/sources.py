"""Source-type vocabulary for dataset mappings.

A *source* describes WHERE a mapped dataset's data lives and how the harvester
reads it. Today the only supported source is the **AWS Glue Data Catalog**
(queried via Athena), but the data model is designed so new source types
(Redshift, BigQuery, …) can be added later WITHOUT a schema migration.

The mapping registry item therefore carries a **nested ``source`` object** whose
shape is ``{type: <source-type>, ...type-specific config}`` — e.g.
``{"type": "glue", "glue_database": "sales_db"}``. Nesting the per-source config
inside the object (rather than sprinkling flat attributes on the item) means a
future Redshift source can carry ``{"type": "redshift", "cluster": ..., ...}``
with no change to the item schema or to code that already stores a ``source``.

This module is pure (no AWS, no agent deps): it owns the source-type constants,
the config-key names, and the builder/normalizer/validator so the Control API
(writer), the incremental orchestrator (reader), and tests all agree on the
exact shape. Back-compat with pre-``source`` rows (which carry only a flat
top-level ``glue_database``) is handled by :func:`normalize_source`.
"""

from __future__ import annotations

from typing import Any

# -- source-type vocabulary --------------------------------------------------

#: The AWS Glue Data Catalog source (metadata in Glue, data queried via Athena).
SOURCE_TYPE_GLUE = "glue"

#: Every source type the platform recognizes. Add new types here as they land;
#: this is the single allowlist the Control API validates a mapping against and
#: the UI renders its (currently read-only) source dropdown from.
SUPPORTED_SOURCE_TYPES: tuple[str, ...] = (SOURCE_TYPE_GLUE,)

#: The default source when none is specified — the only one supported today, and
#: what the UI preselects. Keeping this a named constant means "the default
#: source" is defined in exactly one place.
DEFAULT_SOURCE_TYPE = SOURCE_TYPE_GLUE

#: The config key carrying the Glue database name inside a ``glue`` source object.
#: (For a Glue source the dataset name equals this by convention — see the
#: Control API's mapping guard.)
GLUE_DATABASE_KEY = "glue_database"


class SourceError(ValueError):
    """A source object is malformed or names an unsupported source type."""


def is_supported_source_type(source_type: str | None) -> bool:
    """True iff ``source_type`` is one this release supports."""
    return source_type in SUPPORTED_SOURCE_TYPES


def build_glue_source(glue_database: str) -> dict[str, Any]:
    """The canonical nested source object for a Glue-backed mapping.

    ``{"type": "glue", "glue_database": <db>}`` — the shape stored on the
    mapping registry item's ``source`` attribute.
    """
    if not glue_database:
        raise SourceError("glue source requires a non-empty glue_database")
    return {"type": SOURCE_TYPE_GLUE, GLUE_DATABASE_KEY: glue_database}


def normalize_source(
    source: dict[str, Any] | None = None,
    *,
    glue_database: str | None = None,
) -> dict[str, Any]:
    """Return a well-formed source object from either the new or legacy shape.

    Resolution order:

    * an explicit ``source`` dict (the new shape) — validated and returned; or
    * a legacy flat ``glue_database`` (pre-``source`` mapping rows carried only
      this top-level attribute) — lifted into ``build_glue_source``.

    Raises :class:`SourceError` if neither is usable or the type is unsupported.
    This is the single reader-side adapter, so every consumer sees one shape
    regardless of when the row was written.
    """
    if source:
        return validate_source(source)
    if glue_database:
        return build_glue_source(glue_database)
    raise SourceError("no source: provide a `source` object or a `glue_database`")


def validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate a source object and return it (possibly with the type filled in).

    Enforces a recognized ``type`` and the presence of that type's required
    config. Defaults a missing ``type`` to :data:`DEFAULT_SOURCE_TYPE` so a bare
    ``{"glue_database": ...}`` is accepted as a glue source.
    """
    if not isinstance(source, dict):
        raise SourceError(f"source must be an object, got {type(source).__name__}")
    source_type = source.get("type") or DEFAULT_SOURCE_TYPE
    if not is_supported_source_type(source_type):
        raise SourceError(
            f"unsupported source type {source_type!r}; "
            f"supported: {', '.join(SUPPORTED_SOURCE_TYPES)}"
        )
    if source_type == SOURCE_TYPE_GLUE:
        glue_database = source.get(GLUE_DATABASE_KEY)
        if not glue_database:
            raise SourceError("glue source requires a non-empty glue_database")
        return build_glue_source(glue_database)
    # Unreachable while SUPPORTED_SOURCE_TYPES == (glue,), but keeps the shape
    # for the next source type: return a copy with the type normalized in.
    return {**source, "type": source_type}


def source_glue_database(source: dict[str, Any] | None) -> str | None:
    """The Glue database from a glue source object, or None if not a glue source."""
    if not source or source.get("type") != SOURCE_TYPE_GLUE:
        return None
    return source.get(GLUE_DATABASE_KEY)

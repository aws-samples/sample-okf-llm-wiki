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

#: The Amazon Redshift source (metadata + data via the Redshift Data API over the
#: SVV_* catalog views). The descriptor is SELF-DESCRIBING: it carries the
#: connection routing (``cluster_identifier`` XOR ``workgroup_name``, plus the
#: ``secret_arn`` that authenticates to it) alongside the target
#: ``redshift_database``, so the harvest connects entirely from the descriptor —
#: there is no deploy-time connection config.
SOURCE_TYPE_REDSHIFT = "redshift"

#: Every source type the platform recognizes. Add new types here as they land;
#: this is the single allowlist the Control API validates a mapping against and
#: the UI renders its source dropdown from.
SUPPORTED_SOURCE_TYPES: tuple[str, ...] = (SOURCE_TYPE_GLUE, SOURCE_TYPE_REDSHIFT)

#: The default source when none is specified — and what the UI preselects. Keeping
#: this a named constant means "the default source" is defined in exactly one place.
DEFAULT_SOURCE_TYPE = SOURCE_TYPE_GLUE

#: The config key carrying the Glue database name inside a ``glue`` source object.
#: (For a Glue source the dataset name equals this by convention — see the
#: Control API's mapping guard.)
GLUE_DATABASE_KEY = "glue_database"

#: Config keys inside a ``redshift`` source object. ``redshift_database`` is
#: required; the connection-routing keys make the mapping self-describing so the
#: harvest connects with no deploy-time config. Exactly ONE of cluster/workgroup
#: identifies the target; ``secret_arn`` is the per-mapping Secrets Manager auth.
#: (They are typed optional so a stored db-only descriptor still NORMALIZES —
#: readers of legacy rows must not throw — but such a mapping has no target and
#: can't be harvested, so the Control API rejects it at registration; see
#: ``assert_source_registrable``.)
REDSHIFT_DATABASE_KEY = "redshift_database"
REDSHIFT_CLUSTER_KEY = "cluster_identifier"
REDSHIFT_WORKGROUP_KEY = "workgroup_name"
REDSHIFT_SECRET_ARN_KEY = "secret_arn"

#: Every optional connection key on a redshift source, in item-storage order.
REDSHIFT_CONNECTION_KEYS = (
    REDSHIFT_CLUSTER_KEY,
    REDSHIFT_WORKGROUP_KEY,
    REDSHIFT_SECRET_ARN_KEY,
)


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


def build_redshift_source(
    redshift_database: str,
    *,
    cluster_identifier: str | None = None,
    workgroup_name: str | None = None,
    secret_arn: str | None = None,
) -> dict[str, Any]:
    """The canonical nested source object for a Redshift-backed mapping.

    Required: ``redshift_database``. Connection routing makes the mapping
    self-describing so it can name ANY cluster/workgroup in the account with no
    deploy-time config:

    * exactly ONE of ``cluster_identifier`` (provisioned) / ``workgroup_name``
      (Serverless) names WHERE the database lives, and
    * ``secret_arn`` is the Secrets Manager secret used to authenticate to it.

    Omitting all three yields a bare db-only descriptor
    (``{"type": "redshift", "redshift_database": <db>}``) — it validates but has no
    target, so the harvest can't connect to it. When routing IS given, both a target
    and a secret are required (validated here), so a real mapping is always complete.
    """
    if not redshift_database:
        raise SourceError("redshift source requires a non-empty redshift_database")
    source: dict[str, Any] = {
        "type": SOURCE_TYPE_REDSHIFT,
        REDSHIFT_DATABASE_KEY: redshift_database,
    }
    if cluster_identifier and workgroup_name:
        raise SourceError(
            "redshift source: set only ONE of cluster_identifier / workgroup_name"
        )
    target = cluster_identifier or workgroup_name
    if target or secret_arn:
        # A self-describing mapping needs BOTH a target and a secret to connect.
        if not target:
            raise SourceError(
                "redshift source: cluster_identifier or workgroup_name is required "
                "when a secret_arn is given"
            )
        if not secret_arn:
            raise SourceError(
                "redshift source: secret_arn is required when a cluster/workgroup "
                "is given"
            )
        if cluster_identifier:
            source[REDSHIFT_CLUSTER_KEY] = cluster_identifier
        else:
            source[REDSHIFT_WORKGROUP_KEY] = workgroup_name
        source[REDSHIFT_SECRET_ARN_KEY] = secret_arn
    return source


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
    if source_type == SOURCE_TYPE_REDSHIFT:
        redshift_database = source.get(REDSHIFT_DATABASE_KEY)
        if not redshift_database:
            raise SourceError("redshift source requires a non-empty redshift_database")
        return build_redshift_source(
            redshift_database,
            cluster_identifier=source.get(REDSHIFT_CLUSTER_KEY),
            workgroup_name=source.get(REDSHIFT_WORKGROUP_KEY),
            secret_arn=source.get(REDSHIFT_SECRET_ARN_KEY),
        )
    # Unreachable (every SUPPORTED type has a branch above), but keeps the shape
    # for the next source type: return a copy with the type normalized in.
    return {**source, "type": source_type}


def source_glue_database(source: dict[str, Any] | None) -> str | None:
    """The Glue database from a glue source object, or None if not a glue source."""
    if not source or source.get("type") != SOURCE_TYPE_GLUE:
        return None
    return source.get(GLUE_DATABASE_KEY)

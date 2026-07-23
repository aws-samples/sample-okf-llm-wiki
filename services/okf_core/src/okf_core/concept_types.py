"""The OKF concept-``type`` vocabulary and the schema-bearing-type registry.

Every concept doc carries a frontmatter ``type`` (OKF SPEC). Downstream code
*routes* on a few of these strings — most importantly the augmentation guard
(:func:`okf_core.guard.check_augmentation`), which forbids a re-write from
dropping a source-derived ``# Schema`` field or ``# Citations`` entry. Those
type strings originate at the source (e.g. ``glue_source.list_concepts`` emits
``"Glue Table"`` / ``"Glue Database"``).

This module is the single home for those constants and for the predicate that
answers "is this a schema-bearing concept?" — so adding a new source (Redshift,
RDS, …) means registering its concept types here, not editing a literal tuple in
the guard. Pure: no AWS, no agent deps.
"""

from __future__ import annotations

# -- Glue Data Catalog concept types (emitted by harvest.glue_source) --------

#: The dataset concept for a Glue-backed dataset (one Glue database).
GLUE_DATABASE_TYPE = "Glue Database"
#: A table concept for a Glue-backed dataset (one Glue table).
GLUE_TABLE_TYPE = "Glue Table"

# -- Amazon Redshift concept types (emitted by harvest.redshift_source) -------
# Title-cased to match the Glue runtime convention (the runtime pins the actual
# frontmatter ``type`` strings; the skill adapter's dotted ``redshift.table`` form
# is its generic suggestion). An EXTERNAL table (Spectrum / federated / Glue-backed)
# is a distinct type because its cost + extraction semantics differ from a native
# table — see the redshift.md source adapter.

#: The dataset concept for a Redshift-backed dataset (a database in a cluster /
#: Serverless namespace).
REDSHIFT_DATABASE_TYPE = "Redshift Database"
#: A native table / view / materialized view in Redshift storage.
REDSHIFT_TABLE_TYPE = "Redshift Table"
#: A Spectrum / federated / Glue-backed external table (data outside Redshift).
REDSHIFT_EXTERNAL_TABLE_TYPE = "Redshift External Table"

# -- schema-bearing types ----------------------------------------------------

#: Concept types whose ``# Schema`` field set and ``# Citations`` entries are
#: populated from authoritative source metadata and must only ever be AUGMENTED,
#: never shrunk, by a later write (see :func:`okf_core.guard.check_augmentation`).
#: A new source adds its own table/database concept types here so the guard
#: protects them with no edit to the guard itself.
SCHEMA_BEARING_TYPES: frozenset[str] = frozenset(
    {
        GLUE_DATABASE_TYPE,
        GLUE_TABLE_TYPE,
        REDSHIFT_DATABASE_TYPE,
        REDSHIFT_TABLE_TYPE,
        REDSHIFT_EXTERNAL_TABLE_TYPE,
    }
)


def is_schema_bearing_type(concept_type: str | None) -> bool:
    """True iff ``concept_type`` is a source-derived, schema-bearing concept.

    The augmentation guard keys off this instead of hard-coding the source's
    concept-type strings, so protection extends to every registered source.
    """
    return concept_type in SCHEMA_BEARING_TYPES

"""The source-neutral contract every harvest data source implements.

A *source* is where a dataset's data lives and how the harvester reads it. Today
the only implementation is :class:`harvest.glue_source.GlueAthenaSource` (AWS Glue
Data Catalog + Athena), but the harvest runner, agent, metadata export, and tools
depend only on the :class:`Source` protocol below — not on the concrete class — so
a second source (Redshift, RDS, …) can be dropped in without touching them. See
``docs/DATA_SOURCES.md`` for the full recipe.

Two responsibilities a source must cover, both already source-neutral in shape:

* **Metadata reader** — enumerate the dataset's concepts (:meth:`list_concepts`,
  :meth:`find`, :meth:`table_names`) and return per-concept metadata as a plain
  dict (:meth:`read_concept`). The dict keys the downstream ``metadata_export``
  consumes (``resource``, ``flat_schema``, ``columns``, ``version_id``, …) are the
  contract, not any backend JSON shape.
* **Query engine** — sample rows (:meth:`sample_rows`) and run a verification query
  (:meth:`run_query`) against live data, returning rows as ``list[dict]`` with SQL
  ``NULL`` preserved as ``None`` (distinct from an empty string ``""``).

:class:`ConceptRef` is the shared concept-reference vocabulary, defined here (not in
``glue_source``) so both the protocol and every source implementation import it from
one neutral home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConceptRef:
    """A source-advertised concept. Mirrors the reference agent's ConceptRef.

    ``id`` is the concept-id tuple (e.g. ``("tables", "races")``); ``type`` is the
    frozen frontmatter ``type`` string the source emits (e.g. ``"Glue Table"``);
    ``resource`` is the backend URI/ARN; ``hint`` carries backend-specific lookup
    keys the source uses to resolve the concept (e.g. ``{"table": "races"}``).
    """

    id: tuple[str, ...]
    type: str
    resource: str | None = None
    hint: dict[str, Any] = field(default_factory=dict)

    @property
    def id_str(self) -> str:
        return "/".join(self.id)


@dataclass(frozen=True)
class SourceMetadataProfile:
    """Source-specific labels for the ``.metadata/`` snapshot the harvest writes.

    The snapshot *layout* — the dir structure, ``columns.tsv``, the schema/
    partition markdown tables, the manifest columns — is structural and shared by
    every source (``metadata_export`` owns it). Only the human LABELS differ per
    backend: the catalog's name, whether the resource identifier is an ARN or a
    URI, and which table-property keys hint at a row count. A source returns its
    profile via :attr:`Source.metadata_profile` so ``metadata_export`` stays
    source-neutral while the wording still reads correctly for the backend.

    Most headings are derived from :attr:`label` (the short source name shown in
    headings, e.g. ``"Glue"``) so a new source needs only these few fields.
    """

    #: Short source name used in ``.metadata/`` headings (e.g. ``"Glue"``).
    label: str
    #: Human name of the metadata catalog, used in the snapshot intro line
    #: (e.g. ``"Glue Data Catalog"``).
    catalog_name: str
    #: Label for a concept's resource-identifier line — Glue has ARNs
    #: (``"Resource (ARN)"``), a URI-based source would use ``"Resource (URI)"``.
    resource_label: str
    #: Table-property keys that hint at a row count WITHOUT a billed scan, in
    #: preference order (Glue crawler/ETL ``Parameters`` keys for a glue source).
    rowcount_param_keys: tuple[str, ...]


@dataclass(frozen=True)
class SourcePromptProfile:
    """Source-specific facts the harvest PROMPTS must state correctly per backend.

    The authoring methodology is source-generic (it lives in the okf-authoring
    skill); the prompts only carry the runtime facts that differ per source — which
    the agent must be TOLD, because getting them wrong produces a mislabeled bundle
    (e.g. a Redshift table doc tagged ``type: Glue Table``). A source returns its
    profile via :attr:`Source.prompt_profile` and the prompt builders fill their
    tokens from it, so the narration reads correctly for the run's actual source.

    Kept small: each field is a short phrase the prompt splices in.
    """

    #: One-clause description of the dataset for the intro line, e.g.
    #: "a single AWS Glue database queried via Amazon Athena".
    engine_sentence: str
    #: Short backend label reused across the narration, e.g. "Glue" / "Redshift".
    label: str
    #: The okf-authoring source adapter file the agent must read, e.g.
    #: "athena-glue.md" / "redshift.md" (under references/sources/).
    adapter_file: str
    #: Short dialect name for "write all SQL in the pinned <dialect> dialect", e.g.
    #: "Athena/Trino" / "amazon-redshift".
    dialect: str
    #: Frozen frontmatter ``type`` for the dataset concept (e.g. "Glue Database").
    database_type: str
    #: Full guidance clause for which ``type`` a table doc gets — a source may have
    #: more than one (Redshift: native vs external), e.g.
    #: "`Glue Table` for each table" /
    #: "`Redshift Table` for a native table/view, `Redshift External Table` for a
    #: Spectrum/external table".
    table_type_note: str
    #: How to fill a concept's ``resource`` frontmatter, e.g.
    #: "the Glue ARN from the table's `.metadata/tables/<table>.md` sheet" /
    #: "the `redshift://…#schema.table` connection URI from the table's sheet".
    resource_note: str
    #: What the column-type strings in `.metadata/` are called, e.g.
    #: "Hive types" / "Redshift column types".
    schema_type_term: str


@runtime_checkable
class Source(Protocol):  # pragma: no cover - typing only
    """The metadata-reader + query-engine contract the harvest depends on.

    ``runtime_checkable`` so tests/wiring can ``isinstance``-guard on it; the check
    is method-presence only (it does not verify signatures), which is enough for the
    duck-typed fakes the offline suite injects.
    """

    #: A short source-type identifier (e.g. ``"glue"``). Matches the ``type`` in the
    #: ``okf_core.sources`` descriptor for the source.
    name: str
    #: The dataset identifier this source instance is bound to (for Glue, the Glue
    #: database name). One source instance reads exactly one dataset.
    database: str
    #: Source-specific labels for the ``.metadata/`` snapshot (see
    #: :class:`SourceMetadataProfile`).
    metadata_profile: SourceMetadataProfile
    #: Source-specific facts the harvest prompts state (see
    #: :class:`SourcePromptProfile`).
    prompt_profile: SourcePromptProfile

    def list_concepts(self) -> list[ConceptRef]:
        """Every concept in the dataset — the database concept plus one per table."""
        ...

    def find(self, concept_id: tuple[str, ...]) -> ConceptRef | None:
        """The ConceptRef for ``concept_id``, or None if the source doesn't have it."""
        ...

    def table_names(self) -> list[str]:
        """The names of every table concept in the dataset."""
        ...

    def read_concept(self, ref: ConceptRef) -> dict[str, Any]:
        """Backend metadata for one concept as a plain dict (see module docstring)."""
        ...

    def sample_rows(
        self, ref: ConceptRef, n: int = 5, *, timeout_s: float = 60.0
    ) -> list[dict[str, str | None]] | None:
        """A small sample of real rows for a table concept, or None if unsupported."""
        ...

    def run_query(
        self, query: str, *, timeout_s: float = 60.0, poll_s: float = 1.0
    ) -> list[dict[str, str | None]]:
        """Run a read-only query and return rows as dicts (SQL NULL -> None)."""
        ...

"""okf_core — shared OKF primitives with no AWS or agent-framework dependencies.

Everything here is pure Python (``pyyaml`` + ``networkx`` only) so it can be
imported by every service (harvest, reindex, control API, incremental,
consumption) and unit-tested without live AWS.
"""

from okf_core.document import (
    REQUIRED_FRONTMATTER_KEYS,
    OKFDocument,
    OKFDocumentError,
)
from okf_core.domain import (
    DOMAIN_CONCEPT_ID,
    DOMAIN_DATASET,
    DOMAIN_DOC_TYPE,
    build_domain_document,
    domain_metadata,
    domain_vector_key,
    is_domain_dataset,
)
from okf_core.embedding import (
    ConceptCoordinates,
    build_embed_text,
    build_filterable_metadata,
    build_non_filterable_metadata,
    vector_key,
)
from okf_core.guard import (
    GuardResult,
    check_augmentation,
    check_frontmatter,
    ensure_timestamp,
    reorder_frontmatter,
)
from okf_core.hive_types import FlatField, flatten_hive_type
from okf_core.index_gen import regenerate_indexes
from okf_core.link_graph import LinkGraph
from okf_core.links import Link, extract_links, extract_links_with_headings
from okf_core.paths import (
    concept_id_to_path,
    parse_concept_id,
    path_to_concept_id,
)
from okf_core.session import runtime_session_id
from okf_core.sources import (
    DEFAULT_SOURCE_TYPE,
    GLUE_DATABASE_KEY,
    SOURCE_TYPE_GLUE,
    SUPPORTED_SOURCE_TYPES,
    SourceError,
    build_glue_source,
    is_supported_source_type,
    normalize_source,
    source_glue_database,
    validate_source,
)

__all__ = [
    "REQUIRED_FRONTMATTER_KEYS",
    "OKFDocument",
    "OKFDocumentError",
    "DOMAIN_CONCEPT_ID",
    "DOMAIN_DATASET",
    "DOMAIN_DOC_TYPE",
    "build_domain_document",
    "domain_metadata",
    "domain_vector_key",
    "is_domain_dataset",
    "ConceptCoordinates",
    "build_embed_text",
    "build_filterable_metadata",
    "build_non_filterable_metadata",
    "vector_key",
    "GuardResult",
    "check_augmentation",
    "check_frontmatter",
    "ensure_timestamp",
    "reorder_frontmatter",
    "FlatField",
    "flatten_hive_type",
    "regenerate_indexes",
    "LinkGraph",
    "Link",
    "extract_links",
    "extract_links_with_headings",
    "concept_id_to_path",
    "parse_concept_id",
    "path_to_concept_id",
    "runtime_session_id",
    "DEFAULT_SOURCE_TYPE",
    "GLUE_DATABASE_KEY",
    "SOURCE_TYPE_GLUE",
    "SUPPORTED_SOURCE_TYPES",
    "SourceError",
    "build_glue_source",
    "is_supported_source_type",
    "normalize_source",
    "source_glue_database",
    "validate_source",
]

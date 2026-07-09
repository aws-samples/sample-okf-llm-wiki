"""okf_aws — thin, shared boto3 helpers (Titan embeddings, S3 Vectors, S3
bundle-key parsing). Depends on boto3 + okf_core. Kept separate from okf_core so
the pure library stays dependency-light."""

from okf_aws.embeddings import (
    build_hierarchy_filter,
    create_index_if_absent,
    delete_vector,
    embed_text,
    put_vector,
    query_vectors,
)
from okf_aws.s3_bundle import (
    ConceptLocation,
    bundle_prefix,
    domain_doc_key,
    is_bundle_ready,
    parse_bundle_key,
    state_marker_key,
)

__all__ = [
    "build_hierarchy_filter",
    "create_index_if_absent",
    "delete_vector",
    "embed_text",
    "put_vector",
    "query_vectors",
    "ConceptLocation",
    "bundle_prefix",
    "domain_doc_key",
    "is_bundle_ready",
    "parse_bundle_key",
    "state_marker_key",
]

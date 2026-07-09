"""S3 bundle-bucket helpers: key parsing, concept classification, readiness.

Shared by the reindex worker (which reacts to .md object events) and the control
API / consumption server (which read bundle files). Keeps the S3 layout
conventions (docs/CONVENTIONS.md) in one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_BUNDLE_PREFIX = "okf/"
_RESERVED_FILES = {"index.md", "log.md"}


@dataclass
class ConceptLocation:
    """A parsed S3 object key that identifies a concept in the hierarchy."""

    data_domain: str
    dataset: str
    concept_id: str  # e.g. "tables/races"
    s3_key: str
    table: str | None  # table name if concept_id is tables/<table>

    @property
    def vector_key(self) -> str:
        return f"{self.data_domain}/{self.dataset}/{self.concept_id}"


def parse_bundle_key(s3_key: str) -> ConceptLocation | None:
    """Parse ``okf/<domain>/<dataset>/<concept_id>.md`` into a ConceptLocation.

    Returns None for keys that are not concept docs: wrong prefix, not ``.md``,
    reserved (index.md/log.md), or under a dot-prefixed dir (.context/.harvest).
    """
    if not s3_key.startswith(_BUNDLE_PREFIX) or not s3_key.endswith(".md"):
        return None
    rel = s3_key[len(_BUNDLE_PREFIX) :]
    parts = rel.split("/")
    # need at least domain/dataset/<something>.md
    if len(parts) < 3:
        return None
    data_domain, dataset, *concept_parts = parts
    if not data_domain or not dataset or not concept_parts:
        return None
    # ignore reserved files and any dot-prefixed segment (.context/.harvest/...)
    if concept_parts[-1] in _RESERVED_FILES:
        return None
    if any(seg.startswith(".") for seg in concept_parts):
        return None
    concept_id = "/".join(concept_parts)[: -len(".md")]
    table = None
    if len(concept_parts) == 2 and concept_parts[0] == "tables":
        table = concept_parts[1][: -len(".md")]
    return ConceptLocation(
        data_domain=data_domain,
        dataset=dataset,
        concept_id=concept_id,
        s3_key=s3_key,
        table=table,
    )


def bundle_prefix(data_domain: str, dataset: str) -> str:
    return f"{_BUNDLE_PREFIX}{data_domain}/{dataset}/"


def domain_doc_key(data_domain: str) -> str:
    """S3 object key for a declared domain's concept doc.

    ``okf/<domain>/_domain/overview.md`` — a reserved ``_domain`` pseudo-dataset
    segment so the key still has the 3 segments :func:`parse_bundle_key` requires
    and is indexed by the normal reindex pipeline (frontmatter ``type: Domain``).
    Being a *sibling* of every real ``okf/<domain>/<dataset>/`` tree, it never
    leaks into a dataset's index/listing. See ``okf_core.domain``.
    """
    from okf_core.domain import DOMAIN_CONCEPT_ID, DOMAIN_DATASET

    return f"{_BUNDLE_PREFIX}{data_domain}/{DOMAIN_DATASET}/{DOMAIN_CONCEPT_ID}.md"


def state_marker_key(data_domain: str, dataset: str) -> str:
    return f"{bundle_prefix(data_domain, dataset)}.harvest/state.json"


def is_bundle_ready(s3, bucket: str, data_domain: str, dataset: str) -> bool:
    """True if the commit marker exists and reports ``status == complete``."""
    key = state_marker_key(data_domain, dataset)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        state = json.loads(obj["Body"].read())
        return state.get("status") == "complete"
    except Exception:  # noqa: BLE001 - missing/parse error => not ready
        return False

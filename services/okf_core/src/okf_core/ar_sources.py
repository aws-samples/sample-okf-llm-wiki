"""AR policy source set — which bundle files feed a dataset's reasoning policy.

An Automated Reasoning (AR) policy is a **derived artifact of the bundle**,
exactly like the vector index: S3 markdown is the only truth, and a policy
built from anything but the CURRENT wiki state must never render a verdict.
This module owns the two invariants every service must agree on:

* **the source set** — which dataset-relative paths are policy material
  (:data:`AR_SOURCE_SELECTORS`, :func:`is_ar_source`). The selector is
  "enumerable conditions over booleans/numbers/enums" — usage guardrails,
  enums/named sets (sentinels), metric grain, recipes-as-dispositions, and the
  mechanical subset of known issues. Join clauses, metric SQL bodies, column
  descriptions, and table-local gotchas are deliberately NOT sources: raw
  strings with no home in AR's type system.
* **the source fingerprint** — :func:`compute_source_hash`, the content hash a
  built policy is stamped with and the freshly computed hash it is compared
  against at check time (the staleness gate). Content hashes, never S3 ETags:
  SSE-KMS makes ETags unreliable as content identity.

Pure Python (no AWS deps): the harvest finalize hook (build trigger), the
incremental service (rebuild authority), and the chat runtime (check-time
staleness gate) all import from here so the fingerprint can never drift
between the writer and the readers. The S3 walking lives in
``okf_aws.ar_policy``.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

#: Dataset-relative paths that are AR policy material. Entries ending in ``/``
#: select every object under that prefix; the rest match exactly. Adding a
#: selector changes every dataset's fingerprint on its next harvest — which is
#: correct (the policy must be rebuilt to ingest the new material), but treat
#: this tuple as a contract, not a tuning knob.
AR_SOURCE_SELECTORS: tuple[str, ...] = (
    "references/usage_guardrails.md",
    "references/enums/",
    "references/named_sets/",
    "references/metrics/",
    "references/recipes/",
    "references/known_issues/",
)


def is_ar_source(rel_path: str) -> bool:
    """True iff a dataset-relative path is AR policy material.

    ``rel_path`` is relative to the dataset root (forward slashes, no leading
    ``/``) — e.g. ``references/enums/status_codes.md``. Anything outside the
    selectors — table docs, joins, glossary, ``external/`` cross-references —
    is not a source and never perturbs the fingerprint.
    """
    for selector in AR_SOURCE_SELECTORS:
        if selector.endswith("/"):
            if rel_path.startswith(selector):
                return True
        elif rel_path == selector:
            return True
    return False


def select_ar_sources(rel_paths: Iterable[str]) -> list[str]:
    """The AR source subset of ``rel_paths``, sorted (the canonical order)."""
    return sorted(p for p in rel_paths if is_ar_source(p))


def compute_source_hash(pairs: Iterable[tuple[str, bytes]]) -> str:
    """The dataset's AR source fingerprint: one hex digest over the source set.

    ``pairs`` is ``(dataset-relative key, file content bytes)`` for every
    source file. The fingerprint is the SHA-256 of a canonical manifest —
    one ``"<key> <sha256(content)>"`` line per file, sorted by key — so it is
    insensitive to iteration order and sensitive to any key rename, addition,
    removal, or content change.

    Raises ``ValueError`` on an empty source set: "no sources" is a state the
    caller must represent explicitly (no policy to build), never as a hash of
    nothing that could accidentally compare equal.
    """
    lines = [
        f"{key} {hashlib.sha256(content).hexdigest()}"
        for key, content in sorted(pairs, key=lambda pair: pair[0])
    ]
    if not lines:
        raise ValueError("empty AR source set — no fingerprint exists")
    manifest = "\n".join(lines)
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()

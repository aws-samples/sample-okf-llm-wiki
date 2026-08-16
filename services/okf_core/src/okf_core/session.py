"""AgentCore runtime-session id construction.

``InvokeAgentRuntime.runtimeSessionId`` must be **33–256 chars** (AWS enforces a
min length of 33). The design wants ONE session per dataset, so the id must be
**deterministic** per (data_domain, dataset) — the same dataset always maps to
the same session for affinity.

We build a readable prefix (``okf-<domain>-<dataset>-``) and append a
deterministic sha256 hex of ``<domain>/<dataset>``. The hash both guarantees the
33-char minimum regardless of how short the names are AND keeps the id unique
per dataset. Only ``[A-Za-z0-9_-]`` characters are emitted (the prefix is
sanitized) to stay within a conservative id charset.
"""

from __future__ import annotations

import hashlib
import re

_SAFE = re.compile(r"[^A-Za-z0-9]+")
_MIN_LEN = 33
_MAX_LEN = 256
# 32 hex chars alone already exceeds the 33-min once combined with the prefix.
_HASH_LEN = 32

# A harvest holds a per-dataset lease (the HARVEST#.../STATUS row) while it is
# ``queued``/``running`` so concurrent harvests of the same dataset can't race on
# the shared bundle directory (see trigger_harvest / acquire_harvest_lease). A
# lease older than this is presumed DEAD and can be taken over: AgentCore caps a
# runtime session at 8h, so a job whose ``started_at`` is older than that is no
# longer running and would otherwise wedge the dataset (409 forever) if its
# terminal status write was lost. Shared here so the Control API and the
# incremental orchestrator use the SAME threshold.
HARVEST_LEASE_STALE_SECONDS = 8 * 60 * 60


def runtime_session_id(
    data_domain: str, dataset: str, *, unique_token: str | None = None
) -> str:
    """Length-valid AgentCore session id for a dataset.

    Default is DETERMINISTIC per (domain, dataset) — same dataset -> same session
    (affinity for the incremental path). Pass ``unique_token`` (e.g. a per-invoke
    uuid) to get a FRESH session per invocation: AgentCore reuses one microVM for
    a given session id until it's Stopped, so a one-shot full harvest wants a new
    session each run rather than reattaching to a warm (possibly stale-mounted)
    microVM from a prior attempt.
    """
    basis = f"{data_domain}/{dataset}"
    if unique_token:
        basis = f"{basis}/{unique_token}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_HASH_LEN]
    prefix = _SAFE.sub("-", f"okf-{data_domain}-{dataset}").strip("-")
    session_id = f"{prefix}-{digest}"
    # Prefix + '-' + 32 hex is always >= 33; only guard the max.
    return session_id[:_MAX_LEN]

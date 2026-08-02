"""``policies.yaml`` — the dataset's checkable policy document (pure format).

One YAML document, one individually-trackable entry per policy:

.. code-block:: yaml

    policies:
      - id: P001
        type: behavioural
        condition: >-
          a points request could mean race-result points or
          championship-standing points, and the term was not pinned to one
          reading
        action: ask for clarification instead of committing to a figure
        source: references/usage_guardrails.md

Authored by the harvest runtime's policy-author agent (diff-driven, surgical
updates — ``id`` is STABLE across edits so the UI can track each policy over
time); consumed by the chat policy check's judge fleet (each mini-judge
receives one shard of at most :data:`DEFAULT_SHARD_SIZE` entries) and by the
Reasoning page (which renders entries individually — the reason this is a
structured document rather than numbered prose).

Field semantics:

* ``id`` — ``P``-prefixed, unique, stable. An edited policy keeps its id; only
  a genuinely new policy mints a new one. Never reuse a retired id.
* ``type`` — which check TRACK judges the policy (exactly one):
  ``computational`` (a violation visible in a SQL query itself — additivity,
  grain/collapse, fan-out joins, sentinel decoding; judged query-time by the
  ``run_sql`` race) or ``behavioural`` (a process rule — ask-before-committing,
  refuse out-of-domain, require explicit scope; judged against the steps the
  agent has taken). Required: a document without types predates the v3 split
  and is REJECTED here, which is the whole migration path — the freshness
  gate flags the row stale and the rebuild re-authors with types, ids stable.
* ``condition`` — WHEN the policy applies, plain language over the turn's
  conduct (queries run, clarifications asked, the answer's statements).
* ``action`` — what the agent/answer MUST do when the condition holds (ask,
  refuse, include a caveat, apply a transform, never combine, …). Free text:
  the judges are language models, not a parser.
* ``source`` — the wiki page the policy traces to (``references/….md``); the
  UI links it and the author gate refuses entries pointing at dead pages.

Pure Python (pyyaml only): no AWS, no agent deps — the format's invariants
live here, next to the other OKF source-of-truth modules.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

#: Judges lose precision when a single rubric grows long; shards keep each
#: mini-judge's attention on a handful of policies (operator-tunable upstream).
DEFAULT_SHARD_SIZE = 10

#: Backstop against enumeration pathology, mirrored by the author gate.
DEFAULT_MAX_POLICIES = 60

POLICY_ID_RE = re.compile(r"^P\d{3,}$")
_SOURCE_RE = re.compile(r"^references/[^\s()]+\.md$")

#: The two check tracks a policy can belong to (exactly one per policy).
POLICY_TYPES = ("computational", "behavioural")

_REQUIRED = ("id", "type", "condition", "action", "source")


class PolicyDocError(ValueError):
    """A ``policies.yaml`` that cannot be used, with a fix-it message."""


def parse_policies(text: str | bytes) -> list[dict[str, str]]:
    """The document's policy entries, schema-checked. Raises PolicyDocError.

    Returns ``[{id, type, condition, action, source}, …]`` with every field a
    stripped non-empty string, ids unique and pattern-valid, types from
    :data:`POLICY_TYPES`. Anything else — unparseable YAML, a missing/blank
    field, a duplicate or malformed id, an unknown type, a source outside
    ``references/`` — raises with a message written for the AUTHOR (the
    validation gate forwards it verbatim to the agent).
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyDocError(f"the document is not valid YAML: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("policies"), list):
        raise PolicyDocError(
            "the document must be a YAML mapping with a top-level `policies` list"
        )
    entries: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(doc["policies"]):
        where = f"policies[{i}]"
        if not isinstance(raw, dict):
            raise PolicyDocError(f"{where} is not a mapping")
        entry: dict[str, str] = {}
        for field in _REQUIRED:
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PolicyDocError(
                    f"{where} is missing a non-empty `{field}` "
                    f"(required fields: {', '.join(_REQUIRED)})"
                )
            entry[field] = value.strip()
        if not POLICY_ID_RE.fullmatch(entry["id"]):
            raise PolicyDocError(
                f"{where} id {entry['id']!r} must match P<number> (e.g. P001)"
            )
        if entry["id"] in seen_ids:
            raise PolicyDocError(f"duplicate policy id {entry['id']!r}")
        seen_ids.add(entry["id"])
        if entry["type"] not in POLICY_TYPES:
            raise PolicyDocError(
                f"{where} type {entry['type']!r} must be one of: "
                + ", ".join(POLICY_TYPES)
            )
        if not _SOURCE_RE.fullmatch(entry["source"]):
            raise PolicyDocError(
                f"{where} source {entry['source']!r} must be a "
                "references/….md wiki page path"
            )
        entries.append(entry)
    return entries


def validate_policy_doc(
    text: str,
    *,
    known_sources: set[str] | None = None,
    max_policies: int = DEFAULT_MAX_POLICIES,
) -> str | None:
    """The author gate: None when acceptable, else what to fix.

    Wraps :func:`parse_policies` and adds the two checks only the caller can
    parameterize: entries must trace to LIVE sources (``known_sources``, when
    given — a policy citing a removed page must be deleted, not left
    dangling), and the count backstop (judges shard fine, but enumeration
    pathology still degrades precision and spends tokens).
    """
    try:
        entries = parse_policies(text)
    except PolicyDocError as e:
        return str(e)
    if not entries:
        return "the document contains no policies"
    if len(entries) > max_policies:
        return (
            f"{len(entries)} policies is too many (backstop {max_policies}). "
            "Keep the ones whose violation most damages a real answer; merge "
            "near-duplicates and drop entries that restate documentation "
            "without a checkable action."
        )
    if known_sources is not None:
        dead = sorted({e["source"] for e in entries} - known_sources)
        if dead:
            return (
                "these policy sources are not current wiki pages (removed or "
                "misspelled) — fix or delete their policies: " + ", ".join(dead)
            )
    return None


def policies_of_type(
    policies: list[dict[str, str]], policy_type: str
) -> list[dict[str, str]]:
    """The subset one check track judges, order-preserving.

    ``policy_type`` must be a member of :data:`POLICY_TYPES` — a typo'd track
    name silently judging nothing would be a hard bug to spot, so it raises.
    """
    if policy_type not in POLICY_TYPES:
        raise ValueError(f"unknown policy type {policy_type!r}")
    return [p for p in policies if p["type"] == policy_type]


def shard_policies(
    policies: list[dict[str, str]], size: int = DEFAULT_SHARD_SIZE
) -> list[list[dict[str, str]]]:
    """Split entries into judge-sized shards, order-preserving."""
    if size < 1:
        raise ValueError("shard size must be >= 1")
    return [policies[i : i + size] for i in range(0, len(policies), size)]


def render_policies_for_judge(policies: list[dict[str, str]]) -> str:
    """One shard as prompt text — id, condition, action, source, verbatim."""
    blocks = []
    for p in policies:
        blocks.append(
            f"- id: {p['id']}\n"
            f"  condition: {p['condition']}\n"
            f"  action: {p['action']}\n"
            f"  source: {p['source']}"
        )
    return "\n".join(blocks)

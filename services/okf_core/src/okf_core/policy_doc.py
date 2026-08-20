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
* ``rules`` — OPTIONAL, computational entries only: declarative SQL checks
  from the closed dimension catalog (``okf_core.policy_rules``), evaluated
  deterministically against the agent's query BEFORE the judge fleet. A
  policy whose rules decide (violation/pass, proven on the AST) never
  reaches the judges; undecidable rules fall through as today. Parsed and
  normalized here (shape only — the schema-contract and self-test layers
  live in :func:`validate_policy_doc`, which only the author gate exercises).

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


def parse_policies(
    text: str | bytes, *, drop_invalid_rules: bool = False
) -> list[dict[str, Any]]:
    """The document's policy entries, schema-checked. Raises PolicyDocError.

    Returns ``[{id, type, condition, action, source[, rules]}, …]`` with
    every prose field a stripped non-empty string, ids unique and
    pattern-valid, types from :data:`POLICY_TYPES`, and ``rules`` (when
    present on a computational entry) normalized via
    ``okf_core.policy_rules.parse_rules``. Anything else — unparseable YAML,
    a missing/blank field, a duplicate or malformed id, an unknown type, a
    source outside ``references/``, a malformed rule — raises with a message
    written for the AUTHOR (the validation gate forwards it verbatim to the
    agent).

    ``drop_invalid_rules=True`` is the RUNTIME reader's posture (chat gate,
    Reasoning status): a ``rules`` block this build cannot parse — e.g. a
    dimension a newer harvest image added before this runtime redeployed —
    degrades THAT policy to prose instead of failing the whole document.
    okf_core ships vendored in several separately-deployed artifacts, so
    catalog skew is a normal state; one unparseable rule must never silence
    both enforcement tiers for every policy of the dataset. The author gate
    keeps the strict default (bad rules are refused back to the author).
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
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(doc["policies"]):
        where = f"policies[{i}]"
        if not isinstance(raw, dict):
            raise PolicyDocError(f"{where} is not a mapping")
        entry: dict[str, Any] = {}
        for field in _REQUIRED:
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PolicyDocError(
                    f"{where} is missing a non-empty `{field}` "
                    f"(required fields: {', '.join(_REQUIRED)})"
                )
            # Collapse ALL internal whitespace (YAML block scalars legally
            # carry embedded newlines): every consumer — the judge shard
            # rendering, the reminder lines, the UI's line-oriented display
            # slice — treats a field as ONE line, so single-line is enforced
            # here, at the format's source of truth, not per consumer.
            entry[field] = " ".join(value.split())
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
        if raw.get("rules") is not None:
            from okf_core import policy_rules

            if entry["type"] != "computational":
                if not drop_invalid_rules:
                    raise PolicyDocError(
                        f"{where} carries `rules` but is {entry['type']!r} — "
                        "only computational policies may bind deterministic "
                        "rules"
                    )
            else:
                try:
                    entry["rules"] = policy_rules.parse_rules(
                        raw["rules"], where=f"{where}.rules"
                    )
                except policy_rules.RulesError as e:
                    if not drop_invalid_rules:
                        raise PolicyDocError(str(e)) from e
        entries.append(entry)
    return entries


def validate_policy_doc(
    text: str,
    *,
    known_sources: set[str] | None = None,
    max_policies: int = DEFAULT_MAX_POLICIES,
    rules_schema: dict[str, dict[str, list[str]]] | None = None,
) -> str | None:
    """The author gate: None when acceptable, else what to fix.

    Wraps :func:`parse_policies` and adds the checks only the caller can
    parameterize: entries must trace to LIVE sources (``known_sources``, when
    given — a policy citing a removed page must be deleted, not left
    dangling), the count backstop (judges shard fine, but enumeration
    pathology still degrades precision and spends tokens), and — for entries
    carrying ``rules`` — the two deterministic layers: the schema CONTRACT
    (every bound table/column exists in ``rules_schema``, the sidecar's
    ``databases`` mapping) and the SELF-TEST (each rule's violation/pass
    examples must evaluate to exactly those verdicts). Rules without a
    sidecar are refused outright: an unresolvable binding could never be
    evaluated, only mislead.
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
    ruled = [e for e in entries if e.get("rules")]
    if ruled:
        from okf_core import policy_rules

        if not rules_schema:
            return (
                "entries carry `rules` but no rules schema is available for "
                "this dataset — drop the `rules:` blocks (policies stay "
                "prose-only): " + ", ".join(e["id"] for e in ruled)
            )
        for e in ruled:
            err = policy_rules.check_rules_schema(e["rules"], rules_schema)
            if err:
                return f"{e['id']}: {err}"
            err = policy_rules.self_test(e["rules"], rules_schema)
            if err:
                return f"{e['id']}: {err}"
    return None


def policies_of_type(
    policies: list[dict[str, Any]], policy_type: str
) -> list[dict[str, Any]]:
    """The subset one check track judges, order-preserving.

    ``policy_type`` must be a member of :data:`POLICY_TYPES` — a typo'd track
    name silently judging nothing would be a hard bug to spot, so it raises.
    """
    if policy_type not in POLICY_TYPES:
        raise ValueError(f"unknown policy type {policy_type!r}")
    return [p for p in policies if p["type"] == policy_type]


def shard_policies(
    policies: list[dict[str, Any]], size: int = DEFAULT_SHARD_SIZE
) -> list[list[dict[str, Any]]]:
    """Split entries into judge-sized shards, keeping same-``source`` groups whole.

    Policies distilled from ONE wiki page share vocabulary and context (the
    same enum, the same recipe, the same known issue), so a judge reasons
    over them as a coherent set — a plain positional slice can scatter one
    page's policies across shards, wasting exactly that coherence. Shards
    are therefore packed from whole source-groups: groups keep the
    document's first-appearance order (stable within a group), a group
    moves to a fresh shard rather than straddling a boundary, and only a
    group larger than ``size`` itself is split. Packing whole groups can
    yield MORE shards than a positional ceil(n/size) slice — deliberate:
    an extra shard is one more parallel judge call; a fragmented source
    group is a judgment-quality loss on every call.
    """
    if size < 1:
        raise ValueError("shard size must be >= 1")
    groups: dict[str, list[dict[str, Any]]] = {}
    for policy in policies:
        groups.setdefault(str(policy.get("source") or ""), []).append(policy)
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for group in groups.values():
        # An oversized group is pre-chunked to the cap; each chunk then
        # places like a normal block (its siblings land in adjacent shards).
        for i in range(0, len(group), size):
            block = group[i : i + size]
            if current and len(current) + len(block) > size:
                shards.append(current)
                current = []
            current.extend(block)
    if current:
        shards.append(current)
    return shards


def render_policies_for_judge(policies: list[dict[str, Any]]) -> str:
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

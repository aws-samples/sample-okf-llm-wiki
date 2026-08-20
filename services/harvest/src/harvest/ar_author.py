"""The ``policies.yaml`` author — a single-purpose agent, not a one-shot prompt.

Turning wiki prose into individually-checkable policies is judgment work
(what is a real obligation, which scattered statements merge into one policy),
so it runs as a small ReAct agent with full reasoning rather than a
prompt-and-parse call: the agent explores the sources with read tools, then
SUBMITS the document through a validating ``write_policies`` tool — rejected
submissions (unparseable YAML, schema violations, dead source references,
over the count backstop) come back as tool errors the agent can fix, instead
of silently producing a document the judge fleet chokes on.

Two authoring postures, chosen by whether a prior document exists:

* **Full** — no prior ``policies.yaml``: author from the sources.
* **Update** — a prior document + its sources manifest exist: the agent gets
  per-file unified diffs of exactly what changed since the last authoring run
  (``diff_source``) and produces a minimally-changed, consistent update with
  STABLE policy ids — the same scoped-re-harvest philosophy the wiki itself
  uses, applied to the policy document. The diff base is the
  ``policy/<d>/<ds>/sources/`` copies persisted at the previous authoring
  (``okf_aws.ar_policy.persist_author_state``).

Structural confinement instead of guard middleware: the agent has no
filesystem — its only write is ``write_policies``, whose content the caller
persists. Every tool returns ``Error: …`` strings rather than raising (the
chat-tools convention), so one bad call never aborts the run.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
from typing import Any, Callable

from okf_aws.ar_policy import MAX_DOC_CHARS, POLICY_AUTHOR_PROMPT
from okf_core import policy_doc

log = logging.getLogger("harvest.ar_author")

#: Sonnet 5 with high-effort reasoning. An openai.* value is still supported
#: (needs the harvest role's Mantle grants, derived from
#: var.policy_preprocess_model).
DEFAULT_AUTHOR_MODEL = "global.anthropic.claude-sonnet-5"
#: Reasoning at HIGH: policy distillation is judgment work, and this path runs
#: rarely (per source change) and off every hot path.
DEFAULT_AUTHOR_EFFORT = "high"
#: Ceiling leaves room for reasoning + a full policy document (the document
#: itself is capped at MAX_DOC_CHARS ≈ 13k tokens).
_AUTHOR_MAX_TOKENS = 64000
#: LangGraph recursion budget for the loop (each model turn + tool batch ≈ 2).
_RECURSION_LIMIT = 40

#: Models occasionally fence the document despite instructions; be liberal in
#: what the gate accepts, strict in what it validates.
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n?```\s*$")


def _max_policies() -> int:
    """The policy-count BACKSTOP the submission gate enforces (env-tunable).

    Not a target — the prompt's proportionality guidance is what sizes the
    document to the dataset; this cap only catches enumeration pathology
    (live-observed in v1: ~97 rules for a semantically thin dataset, mostly
    per-value and per-reading expansions). Judges shard fine, but bloat still
    degrades precision and spends tokens on every check.
    """
    try:
        return int(
            os.environ.get("OKF_POLICY_MAX_RULES", "")
            or policy_doc.DEFAULT_MAX_POLICIES
        )
    except ValueError:
        return policy_doc.DEFAULT_MAX_POLICIES


_SYSTEM_PROMPT = (
    "You are the OKF Data Wiki's policy author. Your job is to produce "
    "policies.yaml — the document a fleet of LLM judges evaluates every chat "
    "turn against for this dataset. Work with your tools:\n"
    "- list_sources(): every policy-source file, marked new/changed/removed/"
    "unchanged relative to the previous authoring run.\n"
    "- read_source(path): a source file's current content.\n"
    "- diff_source(path): what changed in one file since the previous run "
    "(unified diff).\n"
    "- read_policies(): the current policy document (empty on a first run).\n"
    "- write_policies(content): SUBMIT the full document. It validates; fix "
    "any Error it returns and submit again. The LAST accepted submission "
    "wins.\n\n"
    "When a previous document exists, UPDATE it minimally: read it, diff the "
    "changed sources, and add/amend/delete only the policies those changes "
    "touch. Policies traced to removed files must go; a policy whose meaning "
    "survives KEEPS ITS ID (the UI tracks policies over time) and stays "
    "byte-identical unless the change genuinely affects it.\n\n"
    "The document contract:\n\n" + POLICY_AUTHOR_PROMPT + "\n\n"
    "Submit via write_policies — never paste the document into chat. Finish "
    "only after an accepted submission."
)


class AuthorContext:
    """The tool state: current sources, the diff base, and the staged document."""

    def __init__(
        self,
        sources: list[tuple[str, bytes]],
        *,
        prior_doc: str = "",
        prior_manifest: dict[str, Any] | None = None,
        fetch_old: Callable[[str], bytes | None] | None = None,
        rules_schema: dict[str, dict[str, list[str]]] | None = None,
    ):
        self.current: dict[str, bytes] = dict(sources)
        self.prior_doc = prior_doc or ""
        self._fetch_old = fetch_old or (lambda _rel: None)
        # The rules-schema sidecar ({db: {table: [columns]}}), when the build
        # snapshotted one — enables `rules:` blocks (contract + self-test in
        # the gate). None refuses them outright.
        self.rules_schema = rules_schema
        prior_files = (prior_manifest or {}).get("files") or {}

        self.status: dict[str, str] = {}
        for rel, content in self.current.items():
            if rel not in prior_files:
                self.status[rel] = "new"
            elif prior_files[rel] != hashlib.sha256(content).hexdigest():
                self.status[rel] = "changed"
            else:
                self.status[rel] = "unchanged"
        self.removed: list[str] = sorted(set(prior_files) - set(self.current))
        self.staged: str = ""
        # The coverage nudge fires at most once per run (see write_policies).
        self._nudged: bool = False

    @property
    def update_mode(self) -> bool:
        return bool(self.prior_doc)

    # -- tool bodies (returned strings go straight back to the model) ---------

    def list_sources(self) -> str:
        lines = [f"{rel} ({self.status[rel]})" for rel in sorted(self.current)]
        lines += [f"{rel} (removed)" for rel in self.removed]
        return "\n".join(lines) or "No policy-source files exist."

    def read_source(self, path: str) -> str:
        content = self.current.get(path)
        if content is None:
            return f"Error: unknown source {path!r} — see list_sources()"
        return content.decode("utf-8", errors="replace")

    def diff_source(self, path: str) -> str:
        if path in self.removed:
            return f"{path} was REMOVED — delete any policies traced to it."
        if path not in self.current:
            return f"Error: unknown source {path!r} — see list_sources()"
        if self.status[path] == "new":
            return f"{path} is NEW — read_source() for its full content."
        if self.status[path] == "unchanged":
            return f"{path} is unchanged."
        old = self._fetch_old(path)
        if old is None:
            return f"{path} changed but its previous copy is gone — treat as new."
        diff = "\n".join(
            difflib.unified_diff(
                old.decode("utf-8", errors="replace").splitlines(),
                self.current[path].decode("utf-8", errors="replace").splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )
        return diff or f"{path} is unchanged."

    def read_policies(self) -> str:
        return self.staged or self.prior_doc or "(no policy document yet)"

    def write_policies(self, content: str) -> str:
        text = _FENCE_RE.sub("", (content or "").strip()).strip()
        error = self.validate(text)
        if error:
            return f"Error: {error}"
        self.staged = text
        policies = policy_doc.parse_policies(text)
        accepted = f"Accepted: {len(policies)} policies staged."
        # Coverage nudge (full mode, once): whole pages silently yielding no
        # rule is the measured single-pass failure mode, so a valid submission
        # that leaves sources uncited gets ONE extract-or-affirm round trip.
        # The document is STAGED either way — the nudge can only improve it
        # (last accepted submission wins), never block it. index pages are
        # navigation, not assertions; they never trigger the nudge.
        if not self.update_mode and not self._nudged:
            cited = {p.get("source") for p in policies}
            uncited = sorted(
                rel
                for rel in self.current
                if not rel.endswith("index.md") and rel not in cited
            )
            if uncited:
                self._nudged = True
                return (
                    f"{accepted} NOTE: these sources yielded no policy: "
                    f"{', '.join(uncited)}. Re-read them and resubmit with any "
                    "decidable rules they assert — or leave the staged "
                    "document as is if they genuinely contain none."
                )
        return accepted

    def validate(self, content: str) -> str | None:
        """The submission gate; None when acceptable, else what to fix."""
        text = (content or "").strip()
        if not text:
            return "the document is empty"
        if len(text) > MAX_DOC_CHARS:
            return (
                f"the document is {len(text)} chars; the cap is "
                f"{MAX_DOC_CHARS} — merge or drop low-value policies"
            )
        return policy_doc.validate_policy_doc(
            text,
            known_sources=set(self.current),
            max_policies=_max_policies(),
            rules_schema=self.rules_schema,
        )

    def make_tools(self) -> list[Any]:
        from langchain_core.tools import StructuredTool

        return [
            StructuredTool.from_function(
                func=lambda: self.list_sources(),
                name="list_sources",
                description=(
                    "Every policy-source file, marked new/changed/removed/"
                    "unchanged since the previous authoring run."
                ),
            ),
            StructuredTool.from_function(
                func=lambda path: self.read_source(path),
                name="read_source",
                description="A source file's current full content.",
            ),
            StructuredTool.from_function(
                func=lambda path: self.diff_source(path),
                name="diff_source",
                description=(
                    "Unified diff of one source file since the previous "
                    "authoring run."
                ),
            ),
            StructuredTool.from_function(
                func=lambda: self.read_policies(),
                name="read_policies",
                description="The current policy document (staged, else previous).",
            ),
            StructuredTool.from_function(
                func=lambda content: self.write_policies(content),
                name="write_policies",
                description=(
                    "Submit the FULL policies.yaml document. Validates and "
                    "stages it; fix any Error and resubmit. The last accepted "
                    "submission wins."
                ),
            ),
        ]

    def _schema_note(self) -> str:
        if not self.rules_schema:
            return (
                " No rules schema is available for this dataset — write NO "
                "`rules:` blocks."
            )
        tables = sorted(
            t for tbls in self.rules_schema.values() for t in tbls
        )
        # Truncation must be VISIBLE: shown an ostensibly exhaustive list,
        # the agent declines to bind rules for real-but-unlisted tables —
        # silent under-authoring on large datasets. The gate validates
        # against the FULL schema either way.
        shown = ", ".join(tables[:40])
        if len(tables) > 40:
            shown += (
                f", … and {len(tables) - 40} more — every snapshot table is "
                "bindable, not just the ones listed"
            )
        return (
            " A rules schema is available (tables: "
            + shown
            + ") — bind deterministic `rules:` blocks where the document "
            "contract allows, each with its violation/pass examples."
        )

    def task_prompt(self, candidates: list[dict] | None = None) -> str:
        if not self.update_mode:
            if candidates:
                return (
                    "Author policies.yaml for this dataset. An extractor fleet "
                    "has already mined every source page in parallel; its "
                    "candidate rules are below with their source attributions. "
                    "You are the SYNTHESIZER: dedupe and merge overlapping "
                    "candidates (keep the most precise variant), drop anything "
                    "not decidable or over-enumerated (proportionality is YOUR "
                    "job — the fleet extracts maximally by design), verify any "
                    "candidate you doubt with read_source(), and add any "
                    "decidable rule the fleet missed. Then submit the FULL "
                    "document via write_policies."
                    + self._schema_note()
                    + "\n\nCandidate rules:\n"
                    + _render_candidates(candidates)
                )
            return (
                "Author policies.yaml for this dataset from scratch. Start with "
                "list_sources(), read every source, then submit via "
                "write_policies." + self._schema_note()
            )
        changed = [r for r, s in self.status.items() if s in ("new", "changed")]
        return (
            "Update the existing policies.yaml to stay consistent with the "
            "wiki. "
            f"Changed or new sources: {', '.join(sorted(changed)) or '(none)'}. "
            f"Removed sources: {', '.join(self.removed) or '(none)'}. "
            "read_policies() for the current document, diff_source() for each "
            "change, then submit the minimally-edited full document via "
            "write_policies — surviving policies KEEP their ids."
            + self._schema_note()
        )


def author_policy_doc(
    *,
    sources: list[tuple[str, bytes]],
    prior_doc: str = "",
    prior_manifest: dict[str, Any] | None = None,
    fetch_old: Callable[[str], bytes | None] | None = None,
    model: Any = None,
    extract: Callable[[list[tuple[str, bytes]]], list[dict]] | None = None,
    rules_schema: dict[str, dict[str, list[str]]] | None = None,
) -> str:
    """Run the authoring agent; returns the accepted document ("" on failure).

    Never raises: the caller (the build trigger) maps "" onto its existing
    ``no_rules`` failure stamp, and a recursion-limit exit returns whatever
    submission was accepted before the budget ran out.

    **From-scratch runs are map-reduce** (first authoring + forced Sync — any
    run with no ``prior_doc``): a fleet of per-cluster extractors mines
    candidate rules in parallel (``harvest.ar_clusters`` owns the
    deterministic clustering), and the authoring agent becomes the
    SYNTHESIZER — it receives the candidate union, dedupes/merges/prunes it,
    verifies doubtful candidates against the sources, and owns the one
    ``write_policies`` gate as before. Measured motivation (2026-08-03):
    three single-pass from-scratch runs over the same sources each produced a
    different incomplete rule set — recall is an attention problem, and
    per-cluster extraction makes coverage structural instead of lucky.
    Extraction is fail-open: no candidates -> plain single-pass authoring.
    UPDATE runs (``prior_doc`` present) never fan out — minimal diffs with
    stable ids are exactly right there. ``extract`` is the injectable fleet
    seam (tests); None -> the real fleet when fan-out is enabled.
    """
    from harvest.benchmark.react import is_recursion_limit, make_react_agent

    candidates: list[dict] = []
    if not prior_doc and _fanout_enabled():
        try:
            extract = extract or _extract_candidates
            candidates = extract(sources) or []
        except Exception:  # noqa: BLE001 - extraction must never fail authoring
            log.warning(
                "candidate extraction failed — authoring single-pass",
                exc_info=True,
            )
            candidates = []

    ctx = AuthorContext(
        sources,
        prior_doc=prior_doc,
        prior_manifest=prior_manifest,
        fetch_old=fetch_old,
        rules_schema=rules_schema,
    )
    agent = make_react_agent(
        model or _build_author_model(), ctx.make_tools(), _SYSTEM_PROMPT
    )
    from langchain_core.messages import HumanMessage

    try:
        agent.invoke(
            {"messages": [HumanMessage(content=ctx.task_prompt(candidates))]},
            config={"recursion_limit": _RECURSION_LIMIT},
        )
    except Exception as e:  # noqa: BLE001 - staged-so-far is still a result
        if not is_recursion_limit(e):
            log.error("policy author run failed", exc_info=True)
            if not ctx.staged:
                return ""
        else:
            log.warning(
                "policy author hit the step budget; using the last accepted "
                "submission (%s)", "present" if ctx.staged else "absent"
            )
    return ctx.staged


def _fanout_enabled() -> bool:
    """Kill switch for the extractor fleet (default ON). Read at call time."""
    return os.environ.get("OKF_POLICY_FANOUT", "").lower() not in (
        "false",
        "0",
        "no",
    )


def _build_author_model() -> Any:
    """Reasoning-ON author model.

    ``OKF_POLICY_AUTHOR_THINKING_BUDGET`` selects the pre-adaptive budget
    encoding for models that need it (Haiku 4.5: 48000 is a sensible value);
    adaptive effort otherwise. GPT ids take the effort ladder directly.

    The Converse client MUST get the harvest runtime's botocore config: a
    high-effort authoring turn reasons for minutes before the first response
    byte, and botocore's default 60s read timeout kills it with
    ReadTimeoutError (live 2026-08-03: every Converse-backed authoring run
    for bird/european_football timed out and the dataset marked failed —
    the GPT/Mantle path never hit this because it doesn't ride botocore).
    """
    from okf_aws.model_factory import (
        DEFAULT_MANTLE_REGION,
        build_bedrock_converse,
        build_mantle_openai,
        is_openai_model,
    )

    from harvest.agent import _bedrock_config

    model_id = os.environ.get("OKF_POLICY_PREPROCESS_MODEL", DEFAULT_AUTHOR_MODEL)
    effort = os.environ.get("OKF_POLICY_AUTHOR_EFFORT", DEFAULT_AUTHOR_EFFORT)
    if is_openai_model(model_id):
        return build_mantle_openai(
            model_id,
            effort,
            _AUTHOR_MAX_TOKENS,
            region=os.environ.get("OKF_HARVEST_MANTLE_REGION", DEFAULT_MANTLE_REGION),
        )
    budget_raw = os.environ.get("OKF_POLICY_AUTHOR_THINKING_BUDGET", "")
    return build_bedrock_converse(
        model_id,
        effort,
        _AUTHOR_MAX_TOKENS,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        botocore_config=_bedrock_config(),
        thinking_budget=int(budget_raw) if budget_raw else None,
    )


# --- the extractor fleet (map side of from-scratch authoring) -----------------

#: A cluster's rules are a page of YAML at most; extraction is not authoring.
_EXTRACT_MAX_TOKENS = 8000
#: Forced-call attempts per cluster before the fleet fails that cluster open.
_EXTRACT_ATTEMPTS = 3
#: Concurrent extractors (env-tunable; the fleet is minutes-rare, so this only
#: bounds Bedrock burst, not steady-state cost).
_EXTRACT_CONCURRENCY_ENV = "OKF_POLICY_EXTRACT_CONCURRENCY"
_DEFAULT_EXTRACT_CONCURRENCY = 4

_EXTRACT_PROMPT = """\
You are one extractor in a fleet distilling DECIDABLE guardrail rules from a \
dataset's wiki. You received ONE topic cluster of pages (topic: {topic}); the \
other clusters are handled by other extractors — mine YOURS exhaustively.

A rule is DECIDABLE when an independent judge could check it against a SQL \
query (`computational`) or against an agent's conduct and final answer \
(`behavioural`). For each rule: `condition` = precisely when it applies, \
`action` = what must (or must never) be done — carry the page's exact \
columns, tables, values and predicates; never soften a specific into a \
generality, and never invent a rule the pages do not assert. `source` = the \
cluster page that asserts the rule (one of: {allowed}).

{contract_block}Cluster pages:

{pages}

Submit EVERY rule in ONE submit_rules call."""

_CONTRACT_BLOCK = """\
The dataset's operating contract is attached for CONTEXT (interpreting your \
pages); cite your own cluster's pages, not the contract:

{contract}

"""


def _render_candidates(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        lines.append(
            f"- type: {c.get('type')}\n"
            f"  condition: {c.get('condition')}\n"
            f"  action: {c.get('action')}\n"
            f"  source: {c.get('source')}"
        )
    return "\n".join(lines)


def _render_pages(files: list[tuple[str, bytes]]) -> str:
    return "\n\n".join(
        f"===== {rel}\n{content.decode('utf-8', errors='replace')}"
        for rel, content in files
    )


def _build_extractor_model() -> Any:
    """The extractor is a CLASSIFIER-contract client, not a reasoner.

    Same model id as the author (``OKF_POLICY_PREPROCESS_MODEL``) but thinking
    OFF + temperature 0 on Converse (which is exactly what makes the FORCED
    ``submit_rules`` tool choice legal on Anthropic) and reasoning ``"none"``
    on an openai.* id — the judge fleet's proven dual-family contract. The
    deep judgment (dedupe, proportionality, verification) belongs to the
    synthesizer, not here.
    """
    from okf_aws.model_factory import (
        DEFAULT_MANTLE_REGION,
        build_bedrock_converse,
        build_mantle_openai,
        is_openai_model,
    )

    from harvest.agent import _bedrock_config

    model_id = os.environ.get("OKF_POLICY_PREPROCESS_MODEL", DEFAULT_AUTHOR_MODEL)
    if is_openai_model(model_id):
        return build_mantle_openai(
            model_id,
            "none",
            _EXTRACT_MAX_TOKENS,
            region=os.environ.get("OKF_HARVEST_MANTLE_REGION", DEFAULT_MANTLE_REGION),
        )
    return build_bedrock_converse(
        model_id,
        "none",  # unused: thinking is off
        _EXTRACT_MAX_TOKENS,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        botocore_config=_bedrock_config(),
        thinking=False,
        temperature=0,
    )


def _coerce_rules(rules: Any) -> Any:
    """Recover the common malformed shapes before validating.

    Live 2026-08-03 (fpl's measures cluster): a model returned ``rules`` as a
    JSON-ENCODED STRING three attempts straight, salvaging nothing — 8 pages
    fell back to the synthesizer's nudge. Also accepts a bare single rule
    object and a dict-of-rules keyed by index; anything else passes through to
    the validator's normal rejection.
    """
    if isinstance(rules, str):
        import json

        try:
            rules = json.loads(rules)
        except ValueError:
            return rules
    if isinstance(rules, dict):
        if {"type", "condition", "action", "source"} <= set(rules):
            return [rules]
        values = list(rules.values())
        if values and all(isinstance(v, dict) for v in values):
            return values
    return rules


def _validate_rules(
    rules: Any, allowed_sources: set[str]
) -> tuple[list[dict], list[str]]:
    """``(valid, errors)`` — schema + source-attribution checks per candidate."""
    rules = _coerce_rules(rules)
    if not isinstance(rules, list):
        return [], ["`rules` must be a list of rule objects"]
    valid: list[dict] = []
    errors: list[str] = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rules[{i}] is not an object")
            continue
        rtype = rule.get("type")
        condition = (rule.get("condition") or "").strip()
        action = (rule.get("action") or "").strip()
        source = (rule.get("source") or "").strip()
        problems = []
        if rtype not in ("computational", "behavioural"):
            problems.append("type must be computational|behavioural")
        if len(condition) < 10:
            problems.append("condition is missing or too vague")
        if len(action) < 10:
            problems.append("action is missing or too vague")
        if source not in allowed_sources:
            problems.append(
                f"source {source!r} is not one of this cluster's pages"
            )
        if problems:
            errors.append(f"rules[{i}]: " + "; ".join(problems))
        else:
            valid.append(
                {
                    "type": rtype,
                    "condition": condition,
                    "action": action,
                    "source": source,
                }
            )
    return valid, errors


def _submit_rules_tool():
    """The extraction tool, with a FULLY TYPED args schema.

    The schema is the fix for the dead-extractor failure (live 2026-08-03,
    fpl's measures cluster): built from an untyped lambda, the tool advertised
    `rules` with no item structure, so the model had to infer the shape from
    prose — and stringified the list three attempts straight. A typed pydantic
    schema puts the exact object shape (and the type enum) in the toolSpec the
    API renders, so the model is constrained instead of guessing;
    ``_coerce_rules`` remains the belt-and-braces behind it.
    """
    from typing import Literal

    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class ExtractedRule(BaseModel):
        """One decidable rule asserted by a cluster page."""

        type: Literal["computational", "behavioural"]
        condition: str = Field(description="Precisely when the rule applies")
        action: str = Field(
            description=(
                "What must (or must never) be done — carry the page's exact "
                "columns, tables, values and predicates"
            )
        )
        source: str = Field(description="The cluster page that asserts the rule")

    class SubmitRules(BaseModel):
        """Submit every decidable rule extracted from the cluster."""

        rules: list[ExtractedRule]

    return StructuredTool.from_function(
        func=lambda rules: "ok",
        name="submit_rules",
        description="Submit every decidable rule extracted from the cluster.",
        args_schema=SubmitRules,
    )


def _forced_extract(model: Any, prompt: str, allowed_sources: set[str]) -> list[dict]:
    """One cluster's extraction: forced submit_rules call + gate-retry.

    The CALL is guaranteed by the API (forced tool choice); the PAYLOAD by the
    validation round trip; the PIPELINE by salvage-on-exhaustion — the valid
    subset of the final attempt still counts, and an empty result fails open
    to the synthesizer (whose coverage nudge names the hole).
    """
    from langchain_core.messages import HumanMessage, ToolMessage

    bound = model.bind_tools([_submit_rules_tool()], tool_choice="submit_rules")
    messages: list[Any] = [HumanMessage(content=prompt)]
    valid: list[dict] = []
    for attempt in range(1, _EXTRACT_ATTEMPTS + 1):
        reply = bound.invoke(messages)
        calls = getattr(reply, "tool_calls", None) or []
        if not calls:
            # Unreachable under a working forced tool choice; belt-and-braces.
            messages += [
                reply,
                HumanMessage(content="Reply ONLY with a submit_rules call."),
            ]
            continue
        args = calls[0].get("args") or {}
        valid, errors = _validate_rules(args.get("rules"), allowed_sources)
        if not errors or attempt == _EXTRACT_ATTEMPTS:
            if errors:
                log.warning(
                    "extractor exhausted retries; salvaging %d valid rules "
                    "(dropped: %s)", len(valid), "; ".join(errors[:5]),
                )
            return valid
        messages += [
            reply,
            ToolMessage(
                content=(
                    "Error: " + "; ".join(errors[:10]) + " — resubmit ONE "
                    "submit_rules call shaped exactly like: {\"rules\": "
                    "[{\"type\": \"computational\", \"condition\": \"…\", "
                    "\"action\": \"…\", \"source\": \"<a cluster page>\"}]}"
                ),
                tool_call_id=calls[0].get("id") or "submit_rules",
            ),
        ]
    return valid


def _extract_candidates(sources: list[tuple[str, bytes]]) -> list[dict]:
    """The fleet: cluster deterministically, extract in parallel, return the union.

    Returns [] when fan-out isn't worthwhile (fewer than two clusters) — the
    caller then authors single-pass exactly as before. Per-cluster failures
    log and drop (fail-open); candidate order is cluster order, so the
    synthesizer's input is as deterministic as the model output allows.
    """
    from concurrent.futures import ThreadPoolExecutor

    from harvest.ar_clusters import cluster_sources

    clusters, shared = cluster_sources(sources)
    if len(clusters) < 2:
        return []
    model = _build_extractor_model()
    contract_text = _render_pages(shared) if shared else ""

    def _one(cluster) -> list[dict]:
        allowed = {rel for rel, _c in cluster.files}
        contract_block = (
            _CONTRACT_BLOCK.format(contract=contract_text)
            if contract_text and not allowed.intersection({s[0] for s in shared})
            else ""
        )
        prompt = _EXTRACT_PROMPT.format(
            topic=cluster.topic,
            allowed=", ".join(sorted(allowed)),
            contract_block=contract_block,
            pages=_render_pages(cluster.files),
        )
        try:
            rules = _forced_extract(model, prompt, allowed)
            log.info(
                "extractor %s: %d rules from %d pages",
                cluster.topic, len(rules), len(cluster.files),
            )
            return rules
        except Exception:  # noqa: BLE001 - one cluster must not sink the fleet
            log.warning("extractor %s failed (skipped)", cluster.topic, exc_info=True)
            return []

    workers = min(
        int(os.environ.get(_EXTRACT_CONCURRENCY_ENV) or _DEFAULT_EXTRACT_CONCURRENCY),
        len(clusters),
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, clusters))
    candidates = [rule for cluster_rules in results for rule in cluster_rules]
    log.info(
        "extractor fleet: %d candidate rules from %d clusters",
        len(candidates), len(clusters),
    )
    return candidates

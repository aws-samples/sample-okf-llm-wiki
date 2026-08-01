"""The ``ar_rules.md`` author — a single-purpose agent, not a one-shot prompt.

Turning wiki prose into decidable IF-THEN rules is judgment work (what is
expressible in the vocabulary, which scattered statements merge into one rule),
so it runs as a small ReAct agent with full reasoning rather than a
prompt-and-parse call: the agent explores the sources with read tools, then
SUBMITS the document through a validating ``write_rules`` tool — rejected
submissions (unnumbered, over the ingest cap, references to files that aren't
sources) come back as tool errors the agent can fix, instead of silently
producing a document the Bedrock build chokes on.

Two authoring postures, chosen by whether a prior document exists:

* **Full** — no prior ``ar_rules.md``: author from the sources.
* **Update** — a prior document + its sources manifest exist: the agent gets
  per-file unified diffs of exactly what changed since the last authoring run
  (``diff_source``) and produces a minimally-changed, consistent update — the
  same scoped-re-harvest philosophy the wiki itself uses, applied to the rules
  document. The diff base is the ``policy/<d>/<ds>/sources/`` copies persisted
  at the previous authoring (``okf_aws.ar_policy.persist_author_state``).

Structural confinement instead of guard middleware: the agent has no
filesystem — its only write is ``write_rules``, whose content the caller
persists. Every tool returns ``Error: …`` strings rather than raising (the
chat-tools convention), so one bad call never aborts the run.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from typing import Any, Callable

from okf_aws.ar_policy import AR_RULES_PROMPT, MAX_INGEST_CHARS

log = logging.getLogger("harvest.ar_author")

#: Same knob as the one-shot pass this module replaced; an openai.* value needs
#: the harvest role's Mantle grants (derived from var.policy_preprocess_model).
DEFAULT_AUTHOR_MODEL = "openai.gpt-5.6-luna"
#: Full reasoning: rules distillation is judgment work, and this path runs
#: rarely (per source change) and off every hot path — depth is free here.
DEFAULT_AUTHOR_EFFORT = "xhigh"
#: Ceiling leaves room for reasoning + a full rules document (the document
#: itself is capped at MAX_INGEST_CHARS ≈ 13k tokens).
_AUTHOR_MAX_TOKENS = 64000
#: LangGraph recursion budget for the loop (each model turn + tool batch ≈ 2).
_RECURSION_LIMIT = 40

#: At least one "N. …" rule line is what makes the document a rules document.
_RULE_LINE_RE = re.compile(r"^\s*\d+\.\s+\S", re.MULTILINE)


def _max_rules() -> int:
    """The rule-count BACKSTOP the submission gate enforces (env-tunable).

    Not a target — the prompt's proportionality guidance is what sizes the
    document to the dataset; this cap only catches enumeration pathology
    (live-observed: ~97 rules / 139 variables for a semantically thin
    dataset, mostly per-value and per-reading expansions). Every rule mints
    solver variables, and variable count degrades the AR translator's
    precision and the check's latency. Set OKF_POLICY_MAX_RULES per
    deployment when a genuinely rich dataset needs more headroom.
    """
    try:
        return int(os.environ.get("OKF_POLICY_MAX_RULES", "") or 60)
    except ValueError:
        return 60
#: Source-traceability suffixes: every referenced path must be a live source.
_SOURCE_REF_RE = re.compile(r"\((references/[^)]+)\)")

_SYSTEM_PROMPT = (
    "You are the OKF Data Wiki's reasoning-rules author. Your job is to produce "
    "ar_rules.md — the ONLY document a formal reasoner ingests to build this "
    "dataset's policy. Work with your tools:\n"
    "- list_sources(): every policy-source file, marked new/changed/removed/"
    "unchanged relative to the previous authoring run.\n"
    "- read_source(path): a source file's current content.\n"
    "- diff_source(path): what changed in one file since the previous run "
    "(unified diff).\n"
    "- read_rules(): the current rules document (empty on a first run).\n"
    "- write_rules(content): SUBMIT the full document. It validates; fix any "
    "Error it returns and submit again. The LAST accepted submission wins.\n\n"
    "When a previous document exists, UPDATE it minimally: read it, diff the "
    "changed sources, and add/amend/delete only the rules those changes touch — "
    "rules traced to removed files must go, unchanged rules keep their text "
    "(and stay byte-identical) so the document history stays reviewable.\n\n"
    "The document contract:\n\n" + AR_RULES_PROMPT + "\n\n"
    "Submit via write_rules — never paste the document into chat. Finish only "
    "after an accepted submission."
)


class AuthorContext:
    """The tool state: current sources, the diff base, and the staged document."""

    def __init__(
        self,
        sources: list[tuple[str, bytes]],
        *,
        prior_rules: str = "",
        prior_manifest: dict[str, Any] | None = None,
        fetch_old: Callable[[str], bytes | None] | None = None,
    ):
        self.current: dict[str, bytes] = dict(sources)
        self.prior_rules = prior_rules or ""
        self._fetch_old = fetch_old or (lambda _rel: None)
        prior_files = (prior_manifest or {}).get("files") or {}
        import hashlib

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

    @property
    def update_mode(self) -> bool:
        return bool(self.prior_rules)

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
            return f"{path} was REMOVED — delete any rules traced to it."
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

    def read_rules(self) -> str:
        return self.staged or self.prior_rules or "(no rules document yet)"

    def write_rules(self, content: str) -> str:
        error = self.validate(content)
        if error:
            return f"Error: {error}"
        self.staged = content.strip()
        count = len(_RULE_LINE_RE.findall(self.staged))
        return f"Accepted: {count} rules staged."

    def validate(self, content: str) -> str | None:
        """The submission gate; None when acceptable, else what to fix."""
        text = (content or "").strip()
        if not text:
            return "the document is empty"
        if len(text) > MAX_INGEST_CHARS:
            return (
                f"the document is {len(text)} chars; the ingest cap is "
                f"{MAX_INGEST_CHARS} — merge or drop low-value rules"
            )
        if not _RULE_LINE_RE.search(text):
            return "no numbered rule lines found (write '1. IF … THEN … (path)')"
        rule_count = len(_RULE_LINE_RE.findall(text))
        if rule_count > _max_rules():
            return (
                f"{rule_count} rules is too many (cap {_max_rules()}). Every "
                "rule mints solver variables, and variable count degrades "
                "translation quality and check latency. Keep the rules whose "
                "violation most damages a real answer; merge near-duplicates "
                "(one ASK rule per ambiguous term, one rule per enum pitfall) "
                "and drop rules that restate documentation without an answer "
                "obligation."
            )
        unknown = sorted(
            {
                ref
                for ref in _SOURCE_REF_RE.findall(text)
                if ref not in self.current
            }
        )
        if unknown:
            return (
                "these referenced paths are not current sources (removed or "
                "misspelled) — fix or delete their rules: " + ", ".join(unknown)
            )
        return None

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
                func=lambda: self.read_rules(),
                name="read_rules",
                description="The current rules document (staged, else previous).",
            ),
            StructuredTool.from_function(
                func=lambda content: self.write_rules(content),
                name="write_rules",
                description=(
                    "Submit the FULL ar_rules.md document. Validates and stages "
                    "it; fix any Error and resubmit. The last accepted "
                    "submission wins."
                ),
            ),
        ]

    def task_prompt(self) -> str:
        if not self.update_mode:
            return (
                "Author ar_rules.md for this dataset from scratch. Start with "
                "list_sources(), read every source, then submit via write_rules."
            )
        changed = [r for r, s in self.status.items() if s in ("new", "changed")]
        return (
            "Update the existing ar_rules.md to stay consistent with the wiki. "
            f"Changed or new sources: {', '.join(sorted(changed)) or '(none)'}. "
            f"Removed sources: {', '.join(self.removed) or '(none)'}. "
            "read_rules() for the current document, diff_source() for each "
            "change, then submit the minimally-edited full document via "
            "write_rules."
        )


def author_rules(
    *,
    sources: list[tuple[str, bytes]],
    prior_rules: str = "",
    prior_manifest: dict[str, Any] | None = None,
    fetch_old: Callable[[str], bytes | None] | None = None,
    model: Any = None,
) -> str:
    """Run the authoring agent; returns the accepted document ("" on failure).

    Never raises: the caller (the build trigger) maps "" onto its existing
    ``no_rules`` failure stamp, and a recursion-limit exit returns whatever
    submission was accepted before the budget ran out.
    """
    from harvest.benchmark.react import is_recursion_limit, make_react_agent

    ctx = AuthorContext(
        sources,
        prior_rules=prior_rules,
        prior_manifest=prior_manifest,
        fetch_old=fetch_old,
    )
    agent = make_react_agent(
        model or _build_author_model(), ctx.make_tools(), _SYSTEM_PROMPT
    )
    from langchain_core.messages import HumanMessage

    try:
        agent.invoke(
            {"messages": [HumanMessage(content=ctx.task_prompt())]},
            config={"recursion_limit": _RECURSION_LIMIT},
        )
    except Exception as e:  # noqa: BLE001 - staged-so-far is still a result
        if not is_recursion_limit(e):
            log.error("ar_rules author run failed", exc_info=True)
            if not ctx.staged:
                return ""
        else:
            log.warning(
                "ar_rules author hit the step budget; using the last accepted "
                "submission (%s)", "present" if ctx.staged else "absent"
            )
    return ctx.staged


def _build_author_model() -> Any:
    """Reasoning-ON author model (contrast: the chat pre-pass keeps it off).

    ``OKF_POLICY_AUTHOR_THINKING_BUDGET`` selects the pre-adaptive budget
    encoding for models that need it (Haiku 4.5: 48000 is a sensible value);
    adaptive effort otherwise. GPT ids take the effort ladder directly.
    """
    from okf_aws.model_factory import (
        DEFAULT_MANTLE_REGION,
        build_bedrock_converse,
        build_mantle_openai,
        is_openai_model,
    )

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
        thinking_budget=int(budget_raw) if budget_raw else None,
    )

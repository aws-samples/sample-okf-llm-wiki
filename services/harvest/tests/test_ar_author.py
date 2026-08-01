"""The ar_rules author agent: tool semantics, validation gate, loop wiring.

The tools ARE the confinement (no filesystem — the only write is the staged
document), so their contracts get direct unit tests; the ReAct loop machinery
itself is ``make_react_agent``'s (covered by the benchmark suite), so
``author_rules`` is tested through a monkeypatched agent that drives the real
tools the way a model would — including the fix-after-rejection round trip.
"""

from __future__ import annotations

import pytest

from harvest import ar_author
from harvest.ar_author import AuthorContext
from okf_aws.ar_policy import MAX_INGEST_CHARS

_SOURCES = [
    ("references/usage_guardrails.md", b"never sum booked and billed"),
    ("references/enums/status.md", b"-1 means unknown"),
]


def _manifest(sources):
    import hashlib

    return {"files": {rel: hashlib.sha256(c).hexdigest() for rel, c in sources}}


# --- classification + read tools ------------------------------------------------


def test_full_mode_lists_everything_and_reads_sources():
    ctx = AuthorContext(_SOURCES)
    assert not ctx.update_mode
    listing = ctx.list_sources()
    assert "references/usage_guardrails.md (new)" in listing
    assert ctx.read_source("references/enums/status.md") == "-1 means unknown"
    assert ctx.read_source("nope.md").startswith("Error:")
    assert ctx.read_rules() == "(no rules document yet)"


def test_update_mode_classifies_changed_new_and_removed():
    prior = _manifest(
        [
            ("references/usage_guardrails.md", b"OLD guardrails"),
            ("references/named_sets/gone.md", b"was here"),
        ]
    )
    ctx = AuthorContext(
        _SOURCES,
        prior_rules="1. IF a THEN b (references/usage_guardrails.md)",
        prior_manifest=prior,
        fetch_old=lambda rel: b"OLD guardrails" if "usage_guardrails" in rel else None,
    )
    assert ctx.update_mode
    listing = ctx.list_sources()
    assert "references/usage_guardrails.md (changed)" in listing
    assert "references/enums/status.md (new)" in listing
    assert "references/named_sets/gone.md (removed)" in listing
    assert "removed" in ctx.task_prompt().lower()

    diff = ctx.diff_source("references/usage_guardrails.md")
    assert "-OLD guardrails" in diff
    assert "+never sum booked and billed" in diff
    assert "REMOVED" in ctx.diff_source("references/named_sets/gone.md")
    assert "NEW" in ctx.diff_source("references/enums/status.md")


def test_diff_of_an_unchanged_file_says_so():
    ctx = AuthorContext(_SOURCES, prior_rules="1. x", prior_manifest=_manifest(_SOURCES))
    assert "unchanged" in ctx.diff_source("references/enums/status.md")


# --- the validation gate -----------------------------------------------------------


def test_write_rules_accepts_a_valid_document():
    ctx = AuthorContext(_SOURCES)
    out = ctx.write_rules(
        "1. IF zeroRowsReturned THEN no figures (references/usage_guardrails.md)\n"
        "2. IF sentinel present THEN sentinelExcluded (references/enums/status.md)"
    )
    assert out == "Accepted: 2 rules staged."
    assert ctx.staged.startswith("1. IF")
    assert ctx.read_rules() == ctx.staged  # staged wins over prior


@pytest.mark.parametrize(
    "content,fragment",
    [
        ("", "empty"),
        ("no numbering at all", "numbered"),
        ("1. IF x THEN y (references/ghost.md)", "references/ghost.md"),
        ("1. " + "x" * MAX_INGEST_CHARS, "ingest cap"),
        # Over the rule backstop: enumeration pathology (per-value, per-reading
        # rule expansion) — the gate makes the agent consolidate.
        (
            "\n".join(f"{i}. IF x THEN y" for i in range(1, 62)),
            "too many",
        ),
    ],
)
def test_write_rules_rejects_and_names_the_problem(content, fragment):
    ctx = AuthorContext(_SOURCES)
    out = ctx.write_rules(content)
    assert out.startswith("Error:") and fragment in out
    assert ctx.staged == ""


def test_rule_cap_is_env_tunable(monkeypatch):
    # A genuinely rich dataset can be given headroom per deployment — the cap
    # is a backstop, never a fixed truth about datasets.
    monkeypatch.setenv("OKF_POLICY_MAX_RULES", "100")
    ctx = AuthorContext(_SOURCES)
    doc = "\n".join(f"{i}. IF x THEN y" for i in range(1, 62))
    assert not ctx.write_rules(doc).startswith("Error:")


def test_stale_source_reference_forces_cleanup_in_update_mode():
    # The rule traced to a removed file must go — the validator is what makes
    # the agent actually delete it rather than leave a dangling reference.
    prior = _manifest(_SOURCES + [("references/named_sets/gone.md", b"x")])
    ctx = AuthorContext(_SOURCES, prior_rules="…", prior_manifest=prior)
    out = ctx.write_rules("1. IF a THEN b (references/named_sets/gone.md)")
    assert out.startswith("Error:") and "gone.md" in out


# --- author_rules wiring -------------------------------------------------------------


class _ScriptedAgent:
    """Drives the REAL tools the way a model would: reject → fix → accept."""

    def __init__(self, tools, submissions):
        self.tools = {t.name: t for t in tools}
        self.submissions = submissions
        self.results: list[str] = []

    def invoke(self, _payload, config=None):
        self.results.append(self.tools["list_sources"].func())
        for content in self.submissions:
            self.results.append(self.tools["write_rules"].func(content=content))
        return {"messages": []}


def test_author_rules_returns_the_last_accepted_submission(monkeypatch):
    made: dict = {}

    def fake_make_react_agent(model, tools, system_prompt):
        made["prompt"] = system_prompt
        return _ScriptedAgent(
            tools,
            [
                "1. IF x THEN y (references/ghost.md)",  # rejected
                "1. IF x THEN y (references/usage_guardrails.md)",  # accepted
            ],
        )

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", fake_make_react_agent
    )
    out = ar_author.author_rules(sources=_SOURCES, model=object())
    assert out == "1. IF x THEN y (references/usage_guardrails.md)"
    # The system prompt embeds the one-shot contract verbatim (vocabulary, the
    # conditional form, the two hard rule notes ride along with it).
    assert "queryExecuted" in made["prompt"]
    assert "write_rules" in made["prompt"]


def test_author_rules_returns_empty_when_nothing_was_accepted(monkeypatch):
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _ScriptedAgent(tools, ["not numbered"]),
    )
    assert ar_author.author_rules(sources=_SOURCES, model=object()) == ""


def test_author_rules_survives_a_recursion_limit_with_the_staged_doc(monkeypatch):
    class _BudgetedAgent(_ScriptedAgent):
        def invoke(self, payload, config=None):
            super().invoke(payload, config)

            class GraphRecursionError(Exception):
                pass

            raise GraphRecursionError("Recursion limit of 40 reached")

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _BudgetedAgent(
            tools, ["1. IF x THEN y (references/enums/status.md)"]
        ),
    )
    out = ar_author.author_rules(sources=_SOURCES, model=object())
    assert out == "1. IF x THEN y (references/enums/status.md)"


def test_author_rules_never_raises(monkeypatch):
    class _Boom:
        def invoke(self, *a, **k):
            raise RuntimeError("bedrock exploded")

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Boom()
    )
    assert ar_author.author_rules(sources=_SOURCES, model=object()) == ""

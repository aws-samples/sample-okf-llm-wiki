"""The policy-document author agent: tool semantics, validation gate, loop wiring.

The tools ARE the confinement (no filesystem — the only write is the staged
document), so their contracts get direct unit tests; the ReAct loop machinery
itself is ``make_react_agent``'s (covered by the benchmark suite), so
``author_policy_doc`` is tested through a monkeypatched agent that drives the
real tools the way a model would — including the fix-after-rejection round
trip.
"""

from __future__ import annotations

import pytest

from harvest import ar_author
from harvest.ar_author import AuthorContext
from okf_aws.ar_policy import MAX_DOC_CHARS

_SOURCES = [
    ("references/usage_guardrails.md", b"never sum booked and billed"),
    ("references/enums/status.md", b"-1 means unknown"),
]

GOOD_DOC = """\
policies:
  - id: P001
    type: behavioural
    condition: figures are requested from a query that returned zero rows
    action: never state figures derived from that query
    source: references/usage_guardrails.md
  - id: P002
    type: computational
    condition: a status aggregate could include the -1 sentinel
    action: exclude -1 before aggregating
    source: references/enums/status.md
"""


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
    assert ctx.read_policies() == "(no policy document yet)"


def test_update_mode_classifies_changed_new_and_removed():
    prior = _manifest(
        [
            ("references/usage_guardrails.md", b"OLD guardrails"),
            ("references/named_sets/gone.md", b"was here"),
        ]
    )
    ctx = AuthorContext(
        _SOURCES,
        prior_doc=GOOD_DOC,
        prior_manifest=prior,
        fetch_old=lambda rel: b"OLD guardrails" if "usage_guardrails" in rel else None,
    )
    assert ctx.update_mode
    listing = ctx.list_sources()
    assert "references/usage_guardrails.md (changed)" in listing
    assert "references/enums/status.md (new)" in listing
    assert "references/named_sets/gone.md (removed)" in listing
    prompt = ctx.task_prompt()
    assert "removed" in prompt.lower()
    # Stable ids are the whole reason the document is structured.
    assert "KEEP their ids" in prompt

    diff = ctx.diff_source("references/usage_guardrails.md")
    assert "-OLD guardrails" in diff
    assert "+never sum booked and billed" in diff
    assert "REMOVED" in ctx.diff_source("references/named_sets/gone.md")
    assert "NEW" in ctx.diff_source("references/enums/status.md")


def test_diff_of_an_unchanged_file_says_so():
    ctx = AuthorContext(_SOURCES, prior_doc="policies: []", prior_manifest=_manifest(_SOURCES))
    assert "unchanged" in ctx.diff_source("references/enums/status.md")


# --- the validation gate -----------------------------------------------------------


def test_write_policies_accepts_a_valid_document():
    ctx = AuthorContext(_SOURCES)
    out = ctx.write_policies(GOOD_DOC)
    assert out == "Accepted: 2 policies staged."
    assert ctx.staged.startswith("policies:")
    assert ctx.read_policies() == ctx.staged  # staged wins over prior


def test_write_policies_strips_a_code_fence_before_validating():
    ctx = AuthorContext(_SOURCES)
    out = ctx.write_policies(f"```yaml\n{GOOD_DOC}```")
    assert out.startswith("Accepted:")
    assert not ctx.staged.startswith("```")


@pytest.mark.parametrize(
    "content,fragment",
    [
        ("", "empty"),
        ("just some prose", "top-level `policies` list"),
        (GOOD_DOC.replace("P002", "P001"), "duplicate policy id"),
        # A policy citing a file that is not a live source must be fixed.
        (GOOD_DOC.replace(
            "references/enums/status.md", "references/ghost.md"
        ), "references/ghost.md"),
        ("policies:\n  - id: P001\n    type: computational\n    condition: c\n"
         "    action: a\n    source: references/usage_guardrails.md\n"
         + "# pad\n" * 20000,
         "the cap is"),
        # A pre-v3 document (no `type`) is a schema violation the agent must fix.
        ("policies:\n  - id: P001\n    condition: c\n    action: a\n"
         "    source: references/usage_guardrails.md\n", "`type`"),
    ],
)
def test_write_policies_rejects_and_names_the_problem(content, fragment):
    ctx = AuthorContext(_SOURCES)
    out = ctx.write_policies(content)
    assert out.startswith("Error:") and fragment in out
    assert ctx.staged == ""


def test_policy_count_backstop_is_env_tunable(monkeypatch):
    entry = (
        "  - id: P{i:03d}\n    type: computational\n    condition: c{i}\n"
        "    action: a{i}\n    source: references/usage_guardrails.md\n"
    )
    doc = "policies:\n" + "".join(entry.format(i=i) for i in range(1, 62))
    ctx = AuthorContext(_SOURCES)
    assert "too many" in ctx.write_policies(doc)
    monkeypatch.setenv("OKF_POLICY_MAX_RULES", "100")
    assert ctx.write_policies(doc).startswith("Accepted:")


def test_stale_source_reference_forces_cleanup_in_update_mode():
    # The policy traced to a removed file must go — the validator is what makes
    # the agent actually delete it rather than leave a dangling reference.
    prior = _manifest(_SOURCES + [("references/named_sets/gone.md", b"x")])
    ctx = AuthorContext(_SOURCES, prior_doc="policies: []", prior_manifest=prior)
    doc = GOOD_DOC.replace(
        "references/enums/status.md", "references/named_sets/gone.md"
    )
    out = ctx.write_policies(doc)
    assert out.startswith("Error:") and "gone.md" in out


# --- author_policy_doc wiring --------------------------------------------------------


class _ScriptedAgent:
    """Drives the REAL tools the way a model would: reject → fix → accept."""

    def __init__(self, tools, submissions):
        self.tools = {t.name: t for t in tools}
        self.submissions = submissions
        self.results: list[str] = []

    def invoke(self, _payload, config=None):
        self.results.append(self.tools["list_sources"].func())
        for content in self.submissions:
            self.results.append(self.tools["write_policies"].func(content=content))
        return {"messages": []}


def test_author_returns_the_last_accepted_submission(monkeypatch):
    made: dict = {}

    def fake_make_react_agent(model, tools, system_prompt):
        made["prompt"] = system_prompt
        return _ScriptedAgent(
            tools,
            [
                GOOD_DOC.replace(
                    "references/usage_guardrails.md", "references/ghost.md"
                ),  # rejected: dead source
                GOOD_DOC,  # accepted
            ],
        )

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", fake_make_react_agent
    )
    out = ar_author.author_policy_doc(sources=_SOURCES, model=object())
    assert out == GOOD_DOC.strip()
    # The system prompt embeds the document contract verbatim.
    assert "policies:" in made["prompt"]
    assert "write_policies" in made["prompt"]
    assert "STABLE" in made["prompt"]


def test_author_returns_empty_when_nothing_was_accepted(monkeypatch):
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _ScriptedAgent(tools, ["not yaml at all"]),
    )
    assert ar_author.author_policy_doc(sources=_SOURCES, model=object()) == ""


def test_author_survives_a_recursion_limit_with_the_staged_doc(monkeypatch):
    class _BudgetedAgent(_ScriptedAgent):
        def invoke(self, payload, config=None):
            super().invoke(payload, config)

            class GraphRecursionError(Exception):
                pass

            raise GraphRecursionError("Recursion limit of 40 reached")

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _BudgetedAgent(tools, [GOOD_DOC]),
    )
    out = ar_author.author_policy_doc(sources=_SOURCES, model=object())
    assert out == GOOD_DOC.strip()


def test_author_never_raises(monkeypatch):
    class _Boom:
        def invoke(self, *a, **k):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Boom()
    )
    assert ar_author.author_policy_doc(sources=_SOURCES, model=object()) == ""


def test_doc_cap_constant_is_generous_but_real():
    assert MAX_DOC_CHARS == 50_000

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


def test_author_converse_client_gets_the_long_read_timeout(monkeypatch):
    # The regression that failed bird/european_football live (2026-08-03): a
    # Converse-backed author reasons for minutes before the first response
    # byte, so the client must carry harvest's botocore config (600s read
    # timeout + adaptive retries), not botocore's 60s default — which is what
    # an omitted `config` silently gives.
    import sys
    import types

    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)
    monkeypatch.setenv(
        "OKF_POLICY_PREPROCESS_MODEL", "global.anthropic.claude-sonnet-5"
    )

    ar_author._build_author_model()

    assert captured["config"] is not None
    assert captured["config"].read_timeout >= 300
    assert captured["config"].retries["mode"] == "adaptive"


# --- the extractor fleet (map side) ------------------------------------------


def _rule(source="references/usage_guardrails.md", **over):
    rule = {
        "type": "computational",
        "condition": "when a query aggregates the status column of any table",
        "action": "always filter with status = 1 before counting",
        "source": source,
    }
    rule.update(over)
    return rule


def test_validate_rules_schema_and_attribution():
    allowed = {"references/enums/status.md"}
    valid, errors = ar_author._validate_rules(
        [
            _rule(source="references/enums/status.md"),
            _rule(source="references/enums/status.md", type="banana"),
            _rule(source="references/ghost.md"),
            _rule(source="references/enums/status.md", condition="x"),
            "not-an-object",
        ],
        allowed,
    )
    assert len(valid) == 1
    assert len(errors) == 4
    assert any("banana" not in e and "type" in e for e in errors)
    assert any("ghost" in e for e in errors)


class _ScriptedExtractorModel:
    """bind_tools/invoke double: records the forced tool choice, plays replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.tool_choices = []

    def bind_tools(self, tools, tool_choice=None):
        self.tool_choices.append((tuple(t.name for t in tools), tool_choice))
        return self

    def invoke(self, _messages):
        from types import SimpleNamespace

        return SimpleNamespace(tool_calls=self.replies.pop(0))


def test_forced_extract_retries_invalid_payloads_then_returns_valid():
    allowed = {"references/enums/status.md"}
    model = _ScriptedExtractorModel(
        [
            [{"id": "t1", "args": {"rules": [_rule(source="references/ghost.md")]}}],
            [{"id": "t2", "args": {"rules": [_rule(source="references/enums/status.md")]}}],
        ]
    )
    out = ar_author._forced_extract(model, "prompt", allowed)
    assert [r["source"] for r in out] == ["references/enums/status.md"]
    # The call is FORCED at the API layer — the guarantee, not a prompt hope.
    assert model.tool_choices == [(("submit_rules",), "submit_rules")]


def test_forced_extract_salvages_the_valid_subset_on_exhaustion():
    allowed = {"references/enums/status.md"}
    bad_and_good = {
        "rules": [
            _rule(source="references/enums/status.md"),
            _rule(source="references/ghost.md"),
        ]
    }
    model = _ScriptedExtractorModel(
        [[{"id": f"t{i}", "args": bad_and_good}] for i in range(3)]
    )
    out = ar_author._forced_extract(model, "prompt", allowed)
    assert [r["source"] for r in out] == ["references/enums/status.md"]


def test_extract_candidates_unions_the_clusters(monkeypatch):
    import re

    class _EchoModel:
        """Returns one rule per allowed page — thread-safe, prompt-derived."""

        def bind_tools(self, tools, tool_choice=None):
            return self

        def invoke(self, messages):
            from types import SimpleNamespace

            match = re.search(r"one of: (.+?)\)\.", messages[0].content, re.S)
            rels = [r.strip() for r in match.group(1).split(",")]
            return SimpleNamespace(
                tool_calls=[
                    {"id": "t", "args": {"rules": [_rule(source=r) for r in rels]}}
                ]
            )

    monkeypatch.setattr(ar_author, "_build_extractor_model", lambda: _EchoModel())
    big = b"x" * 20_000
    sources = [
        ("references/usage_guardrails.md", big),
        ("references/enums/status.md", big),
        ("references/enums/bestellart.md", big),
        ("references/metrics/stock_level.md", big),
        ("references/known_issues/free_text_and_confidentiality.md", big),
    ]
    out = ar_author._extract_candidates(sources)
    # Every source page produced a candidate — coverage is structural.
    assert sorted({r["source"] for r in out}) == sorted(s[0] for s in sources)


def test_extract_candidates_skips_fanout_below_two_clusters(monkeypatch):
    monkeypatch.setattr(
        ar_author, "_build_extractor_model",
        lambda: (_ for _ in ()).throw(AssertionError("must not build a model")),
    )
    assert ar_author._extract_candidates(
        [("references/enums/status.md", b"x" * 100)]
    ) == []


# --- synthesizer wiring (reduce side) ----------------------------------------


def test_from_scratch_authoring_synthesizes_from_the_fleet(monkeypatch):
    captured = {}

    class _CapturingAgent:
        def __init__(self, tools):
            self.tools = {t.name: t for t in tools}

        def invoke(self, payload, config=None):
            captured["task"] = payload["messages"][0].content
            self.tools["write_policies"].func(content=GOOD_DOC)
            return {"messages": []}

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _CapturingAgent(tools),
    )
    monkeypatch.setenv("OKF_POLICY_FANOUT", "true")
    candidates = [_rule(source="references/usage_guardrails.md")]
    out = ar_author.author_policy_doc(
        sources=_SOURCES, model=object(), extract=lambda s: candidates
    )
    assert out == GOOD_DOC.strip()
    assert "SYNTHESIZER" in captured["task"]
    assert "always filter with status = 1" in captured["task"]


def test_update_mode_never_fans_out(monkeypatch):
    calls = []
    monkeypatch.setenv("OKF_POLICY_FANOUT", "true")
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _ScriptedAgent(tools, [GOOD_DOC]),
    )
    out = ar_author.author_policy_doc(
        sources=_SOURCES,
        prior_doc=GOOD_DOC,
        prior_manifest=_manifest(_SOURCES),
        model=object(),
        extract=lambda s: calls.append(s) or [],
    )
    assert out == GOOD_DOC.strip()
    assert calls == []  # increments go through the old path, untouched


def test_extraction_failure_falls_back_to_single_pass(monkeypatch):
    captured = {}

    class _CapturingAgent:
        def __init__(self, tools):
            self.tools = {t.name: t for t in tools}

        def invoke(self, payload, config=None):
            captured["task"] = payload["messages"][0].content
            self.tools["write_policies"].func(content=GOOD_DOC)
            return {"messages": []}

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _CapturingAgent(tools),
    )
    monkeypatch.setenv("OKF_POLICY_FANOUT", "true")

    def _boom(_sources):
        raise RuntimeError("fleet exploded")

    out = ar_author.author_policy_doc(sources=_SOURCES, model=object(), extract=_boom)
    assert out == GOOD_DOC.strip()
    assert "from scratch" in captured["task"]  # the plain single-pass prompt


def test_fanout_kill_switch(monkeypatch):
    monkeypatch.setenv("OKF_POLICY_FANOUT", "false")
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent",
        lambda model, tools, prompt: _ScriptedAgent(tools, [GOOD_DOC]),
    )
    out = ar_author.author_policy_doc(
        sources=_SOURCES,
        model=object(),
        extract=lambda s: (_ for _ in ()).throw(AssertionError("must not extract")),
    )
    assert out == GOOD_DOC.strip()


# --- the coverage nudge -------------------------------------------------------


def test_coverage_nudge_names_uncited_sources_once_and_still_stages():
    ctx = AuthorContext(
        _SOURCES
        + [
            ("references/known_issues/never_cited.md", b"a decidable trap"),
            ("references/enums/index.md", b"navigation only"),
        ]
    )
    first = ctx.write_policies(GOOD_DOC)
    # Staged AND nudged: the note can only improve the document, never block it.
    assert first.startswith("Accepted: 2 policies staged.")
    assert "never_cited.md" in first
    assert "index.md" not in first  # navigation pages never trigger the nudge
    assert ctx.staged  # the document stands even if the agent stops here
    second = ctx.write_policies(GOOD_DOC)
    assert second == "Accepted: 2 policies staged."  # once per run


def test_coverage_nudge_stays_out_of_update_mode():
    ctx = AuthorContext(
        _SOURCES + [("references/known_issues/never_cited.md", b"x")],
        prior_doc=GOOD_DOC,
        prior_manifest=_manifest(_SOURCES),
    )
    assert ctx.write_policies(GOOD_DOC) == "Accepted: 2 policies staged."


def test_validate_rules_coerces_common_malformed_shapes():
    # Live failure: rules arrived as a JSON-encoded string (3 attempts
    # straight); also cover the bare-object and dict-of-rules shapes.
    import json

    allowed = {"references/enums/status.md"}
    good = _rule(source="references/enums/status.md")
    for shape in (json.dumps([good]), good, {"0": good}):
        valid, errors = ar_author._validate_rules(shape, allowed)
        assert [r["source"] for r in valid] == ["references/enums/status.md"], shape
        assert errors == []
    valid, errors = ar_author._validate_rules("not json at all", allowed)
    assert valid == [] and "list" in errors[0]



def test_submit_rules_tool_advertises_the_full_item_schema():
    # The dead-extractor fix: the toolSpec must carry the typed item shape
    # (an untyped lambda advertised `rules: anything`, and the model
    # stringified the list — live 2026-08-03).
    tool = ar_author._submit_rules_tool()
    schema = tool.args_schema.model_json_schema()
    rule_schema = schema["$defs"]["ExtractedRule"]["properties"]
    assert rule_schema["type"]["enum"] == ["computational", "behavioural"]
    assert set(rule_schema) == {"type", "condition", "action", "source"}

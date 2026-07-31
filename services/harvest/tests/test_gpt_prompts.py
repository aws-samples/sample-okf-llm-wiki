"""The GPT-family prompt addendum (per the GPT-5.x prompting guidance).

When an agent's resolved model is an OpenAI GPT, its prompt gains three blocks
targeting the GPT behaviors that are wrong for this headless job out of the
box: <persistence> (no user to hand back to), <context_gathering> (explicit
stop criteria), and <output_discipline> (no tool preambles; Markdown output —
GPT defaults to NOT formatting final answers as Markdown). The flag is keyed
PER SCOPE: the supervisor and the sub-agents can run different model families.
The benchmark solver/judge prompts deliberately do NOT carry it — their fenced
SQL/JSON output contracts must not be contradicted.
"""

from harvest import prompts
from harvest.benchmark.checks import BEHAVIOR_SOLVER_PROMPT, SQL_SOLVER_PROMPT
from harvest.benchmark.judge import JUDGE_SYSTEM_PROMPT

_BUILDERS = (
    prompts.build_table_author_prompt,
    prompts.build_reference_author_prompt,
    prompts.build_reviewer_prompt,
    prompts.build_context_extractor_prompt,
    # Cross-dataset mode's trio: keyed to the same per-scope flags in agent.py
    # (supervisor_gpt for the supervisor, subagent_gpt for cross-author,
    # reviewer_gpt for the cross reviewer).
    prompts.build_cross_supervisor_prompt,
    prompts.build_cross_author_prompt,
    prompts.build_cross_reviewer_prompt,
)

_BLOCKS = ("<persistence>", "<context_gathering>", "<output_discipline>")


def test_default_prompts_carry_no_gpt_addendum():
    # Claude runs are unchanged: gpt defaults to False everywhere.
    for build in _BUILDERS:
        p = build()
        for block in _BLOCKS:
            assert block not in p
    assert "<persistence>" not in prompts.build_supervisor_prompt()


def test_gpt_flag_appends_the_addendum_to_every_agent_prompt():
    for build in _BUILDERS:
        p = build(gpt=True)
        for block in _BLOCKS:
            assert block in p
    p = prompts.build_supervisor_prompt(gpt=True)
    for block in _BLOCKS:
        assert block in p


def test_supervisor_prompt_has_no_benchmark_loop():
    # The RI loop is retired: a harvest supervisor is never told to benchmark.
    for gpt in (False, True):
        p = prompts.build_supervisor_prompt(gpt=gpt)
        assert "run_benchmark" not in p
        assert "Recursive improvement" not in p


def test_gpt_annotation_prompt_carries_the_addendum():
    p = prompts.build_annotation_supervisor_prompt(
        results_rel=".harvest/x.json", gpt=True
    )
    for block in _BLOCKS:
        assert block in p
    # And not without the flag.
    p_plain = prompts.build_annotation_supervisor_prompt(results_rel=".harvest/x.json")
    assert "<persistence>" not in p_plain


def test_gpt_addendum_covers_the_key_gpt_behaviors():
    a = prompts._GPT_ADDENDUM
    # Persistence: no user mid-run, never stop at uncertainty.
    assert "NO user" in a
    assert "Never stop at uncertainty" in a
    # Context gathering: explicit stop criterion + no re-reads.
    assert "stop gathering as soon as you can" in a
    assert "never re-read" in a
    # Output discipline: no tool preambles; Markdown, not JSON/XML.
    assert "tool preambles" in a
    assert "Markdown" in a


def test_benchmark_prompts_never_carry_the_addendum():
    # The SQL solver returns a fenced ```sql payload and the judge a fenced
    # JSON verdict — the addendum's Markdown-not-JSON rule would contradict
    # them, so they are exempt by design (the Behavior solver answers in prose
    # but stays exempt for symmetry: its output feeds the judge, not a human).
    for p in (SQL_SOLVER_PROMPT, BEHAVIOR_SOLVER_PROMPT, JUDGE_SYSTEM_PROMPT):
        for block in _BLOCKS:
            assert block not in p

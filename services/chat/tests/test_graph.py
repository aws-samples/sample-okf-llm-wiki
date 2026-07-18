"""The curated system prompt: the load-bearing instructions must be present, the
prompt must stay STATIC (cacheable — no per-turn interpolation), and the SQL
variant must extend (not replace) the base and mention run_sql.
"""

from __future__ import annotations

from chat.graph import SYSTEM_PROMPT, SYSTEM_PROMPT_WITH_SQL


def test_prompt_is_static_no_interpolation():
    # A cacheable prefix must not vary per turn — so no unfilled format fields.
    assert "{" not in SYSTEM_PROMPT and "}" not in SYSTEM_PROMPT


def test_prompt_covers_the_load_bearing_instructions():
    p = SYSTEM_PROMPT.lower()
    # grounding in the wiki + the discovery tools
    assert "wiki" in p and "read_page" in p and "semantic_search" in p
    # the cardinal no-hallucination rule
    assert "invent" in p or "fabricate" in p
    # citation format the UI parses: <cite src="...">
    assert '<cite src="' in SYSTEM_PROMPT
    # read-only posture
    assert "read-only" in p


def test_prompt_forbids_content_bearing_cite_tags():
    # The <cite> tag MUST be empty — the model wrapping gloss text inside it
    # (`<cite src="x">gloss</cite>`) breaks the UI's citation renderer (leaks a
    # stray </cite>). The prompt must state the tag is empty and show the form.
    assert "ALWAYS EMPTY" in SYSTEM_PROMPT
    assert '<cite src="..."></cite>' in SYSTEM_PROMPT


def test_prompt_mentions_charts_without_the_authoring_details():
    # The base prompt covers WHEN to chart (a <charts> block naming render_chart)
    # but keeps the detailed authoring format in the tool description — so the
    # prompt stays a short, static, cacheable prefix.
    assert "<charts>" in SYSTEM_PROMPT
    assert "render_chart" in SYSTEM_PROMPT
    # The load-bearing chart guardrail (real numbers, not invented) is stated.
    assert "real" in SYSTEM_PROMPT.lower()
    # The verbose authoring API lives in charts.RENDER_CHART_DESC, not the prompt.
    assert "renderChart(el, spec)" not in SYSTEM_PROMPT


def test_sql_variant_extends_base_and_mentions_run_sql():
    # The SQL prompt is the base plus a run_sql block — so the base agent never
    # advertises a tool it doesn't have, and the SQL agent keeps every base rule.
    assert SYSTEM_PROMPT_WITH_SQL.startswith(SYSTEM_PROMPT)
    assert "run_sql" in SYSTEM_PROMPT_WITH_SQL
    assert "run_sql" not in SYSTEM_PROMPT
    # read-only SQL is spelled out (the write verbs are explicitly forbidden)
    assert "SELECT" in SYSTEM_PROMPT_WITH_SQL and "never" in SYSTEM_PROMPT_WITH_SQL.lower()

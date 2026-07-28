"""The curated system prompt: the load-bearing instructions must be present, the
prompt must stay STATIC (cacheable — no per-turn interpolation), and the SQL
variant must extend (not replace) the base and mention run_sql.
"""

from __future__ import annotations

from chat.graph import (
    SQL_BLOCK,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_WITH_SQL,
    WEB_SEARCH_BLOCK,
    compose_system_prompt,
)


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


def test_prompt_groups_sources_into_one_tag_and_allows_urls():
    # The UI renders ONE badge per <cite> tag (paged when it holds several), so a
    # claim with two sources must be one tag — adjacent tags would litter the
    # sentence with pills. URLs are first-class src entries alongside concept ids.
    low = SYSTEM_PROMPT.lower()
    assert "never emit two tags back to back" in low
    assert "http(s)://" in SYSTEM_PROMPT
    assert 'src="tables/orders,https://' in SYSTEM_PROMPT


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


def test_prompt_carries_opus5_verbosity_and_correction_tuning():
    # Opus 5 defaults to longer user-facing responses and narrates corrections
    # more; the prompt must calibrate both — a conciseness instruction paired
    # with a short reminder near the end (<tone_preference>), and correction
    # narration limited to errors that change the reader's conclusions.
    assert "<tone_preference>" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.rstrip().endswith("</tone_preference>")
    low = SYSTEM_PROMPT.lower()
    assert "concise" in low
    assert "correct an earlier statement" in low


def test_sql_variant_extends_base_and_mentions_run_sql():
    # The SQL prompt is the base plus a run_sql block — so the base agent never
    # advertises a tool it doesn't have, and the SQL agent keeps every base rule.
    assert SYSTEM_PROMPT_WITH_SQL.startswith(SYSTEM_PROMPT)
    assert "run_sql" in SYSTEM_PROMPT_WITH_SQL
    assert "run_sql" not in SYSTEM_PROMPT
    # read-only SQL is spelled out (the write verbs are explicitly forbidden)
    assert "SELECT" in SYSTEM_PROMPT_WITH_SQL and "never" in SYSTEM_PROMPT_WITH_SQL.lower()


def test_web_search_block_documents_when_to_reach_for_it():
    # The base agent must not advertise a tool a deployment may not have.
    assert "web_search" not in SYSTEM_PROMPT
    low = WEB_SEARCH_BLOCK.lower()
    assert "web_search" in low
    # The reason it exists: external context around internal numbers (the
    # "is this decline actually bad / what caused it" case, incl. tariffs).
    assert "tariff" in low
    assert "external" in low
    # It must NOT be used for what the data means — the wiki owns that.
    assert "wiki is authoritative" in low
    # Time is steered by the QUERY (the tool has no date filter — deliberately),
    # and attribution rides the SAME <cite src> tag as wiki docs (the UI renders
    # one grouped badge per tag), not loose markdown links.
    assert "no date filter" in low
    assert "put the period in the query" in low
    assert '<cite src="https://' in WEB_SEARCH_BLOCK
    assert "one tag" in low
    # Correlation is not causation: candidate explanations, not proven causes.
    assert "speculative" in low


def test_compose_system_prompt_appends_blocks_in_order():
    assert compose_system_prompt() == SYSTEM_PROMPT
    both = compose_system_prompt(WEB_SEARCH_BLOCK, SQL_BLOCK)
    assert both.startswith(SYSTEM_PROMPT)
    assert both.index(WEB_SEARCH_BLOCK) < both.index(SQL_BLOCK)
    # Empty/absent blocks are skipped, not rendered as gaps.
    assert compose_system_prompt("", SQL_BLOCK) == SYSTEM_PROMPT_WITH_SQL


def test_prompt_teaches_cross_dataset_references():
    # The agent must know the cross-dataset capability: the one-home layout,
    # BOTH list_domains signal fields, prefer-verified-pair-docs over derived
    # joins, hypothesis discipline for unverified candidates, and how to direct
    # the user to run a cross harvest (it cannot trigger one itself).
    assert "<cross_dataset_references>" in SYSTEM_PROMPT
    assert "external/<domain>/<dataset>/" in SYSTEM_PROMPT
    assert "cross_references" in SYSTEM_PROMPT
    assert "cross_referenced_by" in SYSTEM_PROMPT
    low = SYSTEM_PROMPT.lower()
    assert "one bundle only" in low
    assert "unverified hypothesis" in low
    assert "cannot run harvests" in low
    assert "Cross-dataset discovery…" in SYSTEM_PROMPT
    assert "zero docs" in low  # no-convergence is a valid outcome to explain

"""The curated system prompt: the load-bearing instructions must be present, the
prompt must stay STATIC (cacheable — no per-turn interpolation), and the SQL
variant must extend (not replace) the base and mention run_sql.
"""

from __future__ import annotations

from chat.graph import (
    SQL_BLOCK,
    SQL_REDSHIFT_BLOCK,
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
    # citation format the UI parses: the SHORT tag <c src="..."> (the long
    # <cite ...> form is legacy — parsed from stored history, never prompted)
    assert '<c src="' in SYSTEM_PROMPT
    assert "<cite" not in SYSTEM_PROMPT
    # read-only posture
    assert "read-only" in p


def test_prompt_teaches_result_skepticism_bounded():
    # An implausible tool result is a bug in the QUERY until checked (fan-out,
    # skipped dedup recipe, sentinel, non-additive sum) — but skepticism must be
    # bounded and aimed at the query, never the data: fix once, re-run, then
    # report with the anomaly named. No result-shopping toward a prior.
    assert "<result_skepticism>" in SYSTEM_PROMPT
    block = SYSTEM_PROMPT.split("<result_skepticism>")[1].split(
        "</result_skepticism>"
    )[0]
    low = block.lower()
    assert "fans out" in low
    assert "references/recipes/" in block
    assert "sentinel" in low
    assert "fix the query once" in low
    # the overcorrection guard: a verified surprising number is the answer
    assert "never" in low and "expectation" in low


def test_prompt_names_recipes_folder_conditionally():
    # <wiki_structure> lists references/recipes/ so the agent knows the folder
    # exists — but flagged as present only when a dataset needs one (most
    # bundles have no recipe; the mention must not imply otherwise).
    block = SYSTEM_PROMPT.split("<wiki_structure>")[1].split("</wiki_structure>")[0]
    assert "`recipes/`" in block
    assert "present only when" in block


def test_prompt_groups_sources_into_one_tag_and_allows_urls():
    # The UI renders ONE badge per <cite> tag (paged when it holds several), so a
    # claim with two sources must be one tag — adjacent tags would litter the
    # sentence with pills. URLs are first-class src entries alongside concept ids.
    low = SYSTEM_PROMPT.lower()
    assert "never emit two tags back to back" in low
    assert "http(s)://" in SYSTEM_PROMPT
    assert 'src="sales/orders_db/tables/orders,https://' in SYSTEM_PROMPT


def test_prompt_requires_fully_qualified_wiki_citations():
    # A bare concept id (`tables/races`) is only resolvable when the UI happens to
    # have seen that doc in the turn's tool traffic — hit-or-miss, and the badge
    # renders without an "Open page" action when it misses. The prompt must demand
    # the full `<data_domain>/<dataset>/<concept id>` address and show the form.
    assert "<data_domain>/<dataset>/<concept id>" in SYSTEM_PROMPT
    assert '<c src="bird/formula_1/tables/races"></c>' in SYSTEM_PROMPT
    assert "NEVER cite a bare concept id" in SYSTEM_PROMPT


def test_prompt_forbids_content_bearing_cite_tags():
    # The <c> tag MUST be empty — the model wrapping gloss text inside it
    # (`<c src="x">gloss</c>`) breaks the UI's citation renderer (leaks a
    # stray </c>). The prompt must state the tag is empty and show the form.
    assert "ALWAYS EMPTY" in SYSTEM_PROMPT
    assert '<c src="..."></c>' in SYSTEM_PROMPT


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
    block = SYSTEM_PROMPT.split("<charts>")[1].split("</charts>")[0]
    low = block.lower()
    # Budget-pinned: the tool description is UNCONDITIONAL (render_chart is always
    # wired), so anything it already says is pure duplication here. The block keeps
    # only when-to-reach-for-a-chart + never-invent-numbers.
    assert len(block.split()) < 80, f"charts block grew to {len(block.split())} words"
    assert "shape of the data" in low
    assert "never invented" in low
    # Rules the tool description owns must NOT be restated here.
    assert "palette" not in low and "theme" not in low


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
    # The read-only framing stays in the block; the VERB ALLOWLIST and the
    # qualification/LIMIT mechanics are the tool description's job (they differ
    # per engine, so a prompt copy can only drift — see test_sql.py).
    assert "READ-ONLY" in SYSTEM_PROMPT_WITH_SQL
    for mechanic in ("SELECT", "INSERT", '"database"."table"', "LIMIT"):
        assert mechanic not in SYSTEM_PROMPT_WITH_SQL, mechanic


def test_sql_blocks_keep_when_to_reach_for_it_and_read_first():
    # Both engine variants must carry the same two policies (the parts the tool
    # description does NOT own): when to reach for SQL at all, and read the doc
    # before writing the query.
    for block in (SQL_BLOCK, SQL_REDSHIFT_BLOCK):
        low = block.lower()
        assert "prefer the wiki" in low
        assert "live data or aggregates the docs don't state" in low
        assert "read the relevant table doc first" in low
        assert "never fabricate" in low


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
    # Attribution rides the SAME <c src> tag as wiki docs (the UI renders one
    # grouped badge per tag), not loose markdown links.
    assert '<c src="https://' in WEB_SEARCH_BLOCK
    assert "one comma-separated tag" in low
    # Correlation is not causation: candidate explanations, not proven causes.
    assert "speculative" in low
    # The date guidance is ARGUMENT-level and lives in the tool description (which
    # also carries today's date — see test_web_search.py); this static block must
    # not duplicate it.
    assert "no date filter" not in low
    assert "put the period in the query" not in low
    assert "q2 2026" not in low


def test_compose_system_prompt_appends_blocks_in_order():
    assert compose_system_prompt() == SYSTEM_PROMPT
    both = compose_system_prompt(WEB_SEARCH_BLOCK, SQL_BLOCK)
    assert both.startswith(SYSTEM_PROMPT)
    assert both.index(WEB_SEARCH_BLOCK) < both.index(SQL_BLOCK)
    # Empty/absent blocks are skipped, not rendered as gaps.
    assert compose_system_prompt("", SQL_BLOCK) == SYSTEM_PROMPT_WITH_SQL


def test_with_current_date_appends_dated_block():
    from datetime import date

    from chat.graph import with_current_date

    out = with_current_date(SYSTEM_PROMPT, today=date(2026, 7, 22))
    assert out.startswith(SYSTEM_PROMPT)  # appended last — prefix untouched
    assert "<current_date>" in out
    assert "Wednesday, 2026-07-22" in out
    # never assume data reaches today — the horizons caveat rides along
    assert "Never assume data exists up to the current date" in out


def test_with_current_date_defaults_to_today_utc():
    from datetime import datetime, timezone

    from chat.graph import with_current_date

    out = with_current_date(SYSTEM_PROMPT)
    assert datetime.now(timezone.utc).date().strftime("%Y-%m-%d") in out


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


def test_prompt_teaches_the_wiki_structure_leanly():
    # The primer content (bundle shape, trap locations, the backlinks habit)
    # rides the STATIC prompt — chat has one shared prompt for Opus and GPT,
    # so both families get it, and it sits in the cached prefix. Budget-pinned
    # so it stays lean: the agent should get the map, not a manual.
    assert "<wiki_structure>" in SYSTEM_PROMPT
    for token in (
        "index.md",
        "usage_guardrails",
        "known_issues",
        "get_backlinks",
        "grain",
    ):
        assert token in SYSTEM_PROMPT
    block = SYSTEM_PROMPT.split("<wiki_structure>")[1].split("</wiki_structure>")[0]
    assert len(block) < 1500, f"wiki_structure block grew to {len(block)} chars"


def test_wiki_structure_block_agrees_with_the_wiki_primer():
    # <wiki_structure> is the chat-tuned prose rendering of the same contract
    # okf_core.wiki_primer serves external MCP agents via read_me. The two are
    # deliberately separate texts (each tuned for its surface; chat gets no
    # read_me tool — its system prompt already carries the block, cached), so
    # this pin is the drift guard: a bundle-layout or navigation-contract
    # change must land in BOTH or fail here.
    from okf_core.wiki_primer import MCP_PRIMER

    shared_facts = (
        "references/usage_guardrails.md",  # the traps doc, by exact path
        "known_issues",                    # ...and the known-issues folder
        "index.md",                        # index-first navigation
        "get_backlinks",                   # the "fastest route" move
        "annotations",                     # curator annotations override prose
        "tables/",                         # per-table docs location
        "references/",                     # cross-cutting docs location
    )
    for fact in shared_facts:
        assert fact in SYSTEM_PROMPT, f"chat <wiki_structure> lost: {fact}"
        assert fact in MCP_PRIMER, f"wiki_primer lost: {fact}"

"""Report contract: block lint, id/key conventions, composer.

The composer is DETERMINISTIC (no clock, no randomness, injected markdown),
so these tests assert real output content — escaping, badge rendering, KPI
grouping — not just "it returned a string". Markdown rendering uses the
markdown-it dev extra when present and a trivial injected renderer otherwise,
so the suite passes in the dep-minimal venv too.
"""

from __future__ import annotations

import json

import pytest

from okf_core import reports as rs


def _md_render():
    try:
        from markdown_it import MarkdownIt

        return MarkdownIt("js-default").render
    except ImportError:
        return lambda md: f"<p>{md}</p>"


PNG = "data:image/png;base64,iVBORw0KGgo="

BLOCKS = [
    {"type": "markdown", "md": "## Findings\n\nLap times cluster at 83s."},
    {
        "type": "kpi",
        "label": "Total laps",
        "value": "1,204",
        "delta": -3.2,
        "delta_label": "vs 2003",
        "provenance": {"kind": "computation", "slug": "laps_by_season", "content_hash": "abc123def456"},
    },
    {"type": "kpi", "label": "Fastest lap", "value": "81.4s"},
    {
        "type": "chart",
        "title": "Lap time distribution",
        "spec": {
            "type": "histogram",
            "labels": ["81s", "82s", "83s"],
            "series": [{"name": "Laps", "data": [12, 87, 261]}],
        },
        "provenance": {"kind": "adhoc_sql", "sql": "SELECT bucket, count(*) FROM laps GROUP BY 1"},
    },
    {
        "type": "table",
        "title": "Top drivers",
        "columns": ["driver", "wins"],
        "rows": [["Hamilton", 11], ["Massa", 10]],
    },
]


# --- lint_blocks -------------------------------------------------------------


def test_valid_blocks_lint_clean():
    assert rs.lint_blocks("2004 Monza lap analysis", BLOCKS) == []


def test_lint_refuses_shapeless_input():
    assert rs.lint_blocks("", []) == [
        "report title is required",
        "blocks must be a non-empty array",
    ]
    errors = rs.lint_blocks("t", [{"type": "nope"}, "not-a-dict"])
    assert any("blocks[0]: type must be one of" in e for e in errors)
    assert any("blocks[1]: must be an object" in e for e in errors)


def test_lint_names_the_offending_block():
    errors = rs.lint_blocks(
        "t",
        [
            {"type": "markdown", "md": "   "},
            {"type": "chart", "title": "c", "spec": {"type": "bar", "series": []}},
            {"type": "table", "columns": ["a"], "rows": [[1, 2]]},
            {"type": "kpi", "label": "", "value": None},
        ],
    )
    assert any(e.startswith("blocks[0]:") and "md text" in e for e in errors)
    assert any(e.startswith("blocks[1]:") and "series" in e for e in errors)
    assert any(e.startswith("blocks[2]:") and "1 cells" in e for e in errors)
    assert any(e.startswith("blocks[3]:") for e in errors)


def test_lint_enforces_caps():
    too_many = [{"type": "kpi", "label": "x", "value": 1}] * (rs.MAX_BLOCKS + 1)
    assert any("exceed" in e for e in rs.lint_blocks("t", too_many))
    fat_chart = {
        "type": "chart",
        "title": "c",
        "spec": {"type": "line", "series": [{"data": [0] * (rs.MAX_CHART_POINTS + 1)}]},
    }
    assert any("data points exceed" in e for e in rs.lint_blocks("t", [fat_chart]))
    long_table = {
        "type": "table",
        "columns": ["a"],
        "rows": [[1]] * (rs.MAX_TABLE_ROWS + 1),
    }
    assert any("rows exceed" in e for e in rs.lint_blocks("t", [long_table]))


def test_lint_checks_chart_spec_type_against_the_renderer_set():
    bad = {"type": "chart", "title": "c", "spec": {"type": "pie3d", "series": [{"data": [1]}]}}
    assert any("spec.type" in e for e in rs.lint_blocks("t", [bad]))
    # The renderer set matches the chat contract's 18 types.
    assert len(rs.CHART_SPEC_TYPES) == 18


def test_lint_validates_provenance_shapes():
    bad_kind = {"type": "kpi", "label": "x", "value": 1, "provenance": {"kind": "vibes"}}
    assert any("provenance.kind" in e for e in rs.lint_blocks("t", [bad_kind]))
    no_slug = {"type": "kpi", "label": "x", "value": 1, "provenance": {"kind": "computation"}}
    assert any("requires a slug" in e for e in rs.lint_blocks("t", [no_slug]))
    no_sql = {"type": "kpi", "label": "x", "value": 1, "provenance": {"kind": "adhoc_sql"}}
    assert any("requires the sql" in e for e in rs.lint_blocks("t", [no_sql]))


# --- report id / key conventions ---------------------------------------------


def test_report_id_round_trips():
    rid = rs.make_report_id("motorsport", "f1", "20260815T120000Z", "a1b2c3d4")
    parsed = rs.parse_report_id(rid)
    assert parsed == {
        "domain": "motorsport",
        "dataset": "f1",
        "stamp": "20260815T120000Z",
        "suffix": "a1b2c3d4",
    }


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "rep~only~three~parts",
        "nope~d~ds~20260815T120000Z~a1b2c3d4",
        "rep~d!oh~ds~20260815T120000Z~a1b2c3d4",  # bad domain slug
        "rep~d~ds~2026-08-15T12:00:00Z~a1b2c3d4",  # non-compact stamp
        "rep~d~ds~20260815T120000Z~xyz",  # bad suffix
    ],
)
def test_malformed_report_ids_parse_to_none(bad):
    assert rs.parse_report_id(bad) is None


def test_s3_keys_are_coherent():
    prefix = rs.report_s3_prefix("d", "ds", "20260815T120000Z", "a1b2c3d4")
    assert prefix == "reports/d/ds/20260815T120000Z-a1b2c3d4"
    assert rs.report_html_key(prefix).endswith("/report.html")
    assert rs.report_pdf_key(prefix).endswith("/report.pdf")
    assert rs.report_blocks_key(prefix).endswith("/blocks.json")


# --- composer ------------------------------------------------------------------


def _compose(blocks=BLOCKS, images=None, **kw):
    images = {3: PNG} if images is None else images
    args = dict(
        domain="motorsport",
        dataset="f1",
        generated_at="2026-08-15T12:00:00+00:00",
        request="How did lap times distribute at Monza 2004?",
        report_id="rep~motorsport~f1~20260815T120000Z~a1b2c3d4",
        md_render=_md_render(),
    )
    args.update(kw)
    return rs.compose_html("2004 Monza lap analysis", blocks, images, **args)


def test_compose_is_deterministic_and_self_contained():
    a = _compose()
    b = _compose()
    assert a == b
    assert a.startswith("<!doctype html>")
    # Self-contained: no external fetches, no scripts (the viewer iframe has
    # no allow-scripts and the PDF pass must not touch the network).
    low = a.lower()
    assert "<script" not in low
    assert "http://" not in low.replace("http://www.w3.org", "")
    assert 'src="data:image/png' in a


def test_compose_escapes_model_text_everywhere():
    blocks = [
        {"type": "kpi", "label": "<img src=x onerror=alert(1)>", "value": "<b>7</b>"},
        {
            "type": "table",
            "title": 'a"b<c>',
            "columns": ["<col>"],
            "rows": [["<cell>"]],
        },
    ]
    html_out = _compose(blocks=blocks, images={})
    assert "<img src=x" not in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out
    assert "&lt;b&gt;7&lt;/b&gt;" in html_out
    assert "&lt;col&gt;" in html_out and "&lt;cell&gt;" in html_out


def test_markdown_raw_html_stays_disabled_via_default_renderer():
    pytest.importorskip("markdown_it")
    out = rs.compose_html(
        "t",
        [{"type": "markdown", "md": "hello <script>alert(1)</script>"}],
        {},
        domain="d",
        dataset="ds",
        generated_at="2026-08-15T12:00:00+00:00",
    )
    assert "<script>" not in out


def test_compose_provenance_renders_as_appendix_notes_not_body_badges():
    out = _compose()
    # The BODY is executive-clean: no badges, no inline SQL — figures carry
    # only a small footnote marker.
    assert "badge" not in out and "EXPLORATORY" not in out
    assert '<sup class="note"><a href="#note-1">1</a></sup>' in out
    # The appendix carries the audit trail: computation slug + hash, and the
    # ad-hoc SQL disclosed verbatim.
    assert "Notes &amp; methodology" in out
    assert "verified computation <code>laps_by_season</code>" in out
    assert "content hash abc123def456" in out
    assert "direct read-only query" in out
    assert "SELECT bucket, count(*)" in out
    body, appendix = out.split('<section class="appendix">')
    assert "SELECT bucket" not in body  # SQL lives ONLY in the appendix


def test_compose_document_chrome_contracts():
    out = _compose()
    # The PDF prints from this same HTML — the methodology appendix (and the
    # footnote marks pointing into it) must be print-hidden so the shareable
    # copy stays executive-clean while the viewer keeps the audit trail.
    assert ".appendix, sup.note { display: none; }" in out
    # Inter leads the font stack (the chat image installs it so headless
    # Chromium's PDF pass doesn't fall back to DejaVu Sans).
    assert "font: 15px/1.65 Inter," in out
    # The viewer iframe is its own document: it embeds the app's thin
    # transparent-track scrollbar rather than the heavy native bar.
    assert "*::-webkit-scrollbar" in out and "scrollbar-width: thin" in out
    assert "Generated by Data Wiki" in out


def test_compose_groups_consecutive_kpis_into_one_row():
    out = _compose()
    assert out.count('class="kpi-row"') == 1
    assert out.count('class="kpi"') == 2
    # Delta styling: negative renders the down arrow + class.
    assert "▼" in out and 'class="delta down"' in out and "vs 2003" in out


def test_compose_renders_dual_theme_chart_images():
    dark = "data:image/png;base64,ZGFyaw=="
    out = _compose(images={3: {"light": PNG, "dark": dark}})
    # Both PNGs ship in the figure; the stylesheet's data-theme rules pick one
    # (light is the no-attribute/PDF default).
    assert f'<img class="chart-light" src="{PNG}"' in out
    assert f'<img class="chart-dark" src="{dark}"' in out
    assert "img.chart-dark { display: none; }" in out
    assert '[data-theme="dark"] img.chart-light { display: none; }' in out
    # A bare string (light-only back-compat) serves both themes.
    both = _compose()
    assert both.count(f'src="{PNG}"') == 2
    # A dict missing/failing the light URI refuses like a missing image.
    with pytest.raises(ValueError, match="no rendered image"):
        _compose(images={3: {"dark": dark}})
    with pytest.raises(ValueError, match="no rendered image"):
        _compose(images={3: {"light": PNG, "dark": "https://not-a-data-uri"}})


def test_compose_refuses_invalid_blocks_and_missing_chart_images():
    with pytest.raises(ValueError, match="report title is required"):
        rs.compose_html("", BLOCKS, {3: PNG}, domain="d", dataset="ds", generated_at="t")
    with pytest.raises(ValueError, match="no rendered image"):
        _compose(images={})
    with pytest.raises(ValueError, match="no rendered image"):
        _compose(images={3: "https://not-a-data-uri"})


def test_blocks_document_is_self_describing():
    doc = json.loads(
        rs.blocks_document(
            "t",
            BLOCKS,
            request="q",
            domain="d",
            dataset="ds",
            created_by="sub-1",
            generated_at="2026-08-15T12:00:00+00:00",
        )
    )
    assert doc["version"] == rs.REPORTS_VERSION
    assert doc["blocks"] == BLOCKS
    assert doc["request"] == "q"
    # No database row exists — the artifact carries its own metadata.
    assert doc["domain"] == "d" and doc["created_by"] == "sub-1"

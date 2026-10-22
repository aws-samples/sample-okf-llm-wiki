"""Canvas config contract: static schema + widget slots, contract-vs-
computation checks, live data checks through a fake executor.

Computations are built from real doc text through ``parse_computation_text``
so the contract layer is exercised against the same parse the serving paths
use, not hand-rolled dataclasses.
"""

from __future__ import annotations

import pytest

from okf_core import canvas as cv
from okf_core.computations import parse_computation_text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _comp_text(params_yaml: str, sql: str) -> str:
    return (
        "---\n"
        "type: Attested Computation\n"
        "title: T\n"
        "description: d\n"
        "timestamp: t\n"
        "runtime: athena\n"
        f"parameters:\n{params_yaml}"
        "verified: null\n"
        "verified_by: null\n"
        "verified_sha256: null\n"
        "---\n\n"
        "# Computation\n\n"
        f"```sql\n{sql}\n```\n"
    )


REV_BY_YEAR = _comp_text(
    "  - name: year\n"
    "    type: integer\n"
    "    example: 2024\n"
    "    min: 2000\n"
    "    max: 2030\n"
    "  - name: brand\n"
    "    type: string\n"
    "    example: Audi\n"
    "    default: Audi\n"
    "    enum: [Audi, VW]\n",
    "SELECT month, revenue, brand FROM sales\n"
    "WHERE year = @year AND brand = @brand",
)

TOTAL_KPI = _comp_text(
    "  - name: year\n"
    "    type: integer\n"
    "    example: 2024\n",
    "SELECT total FROM totals WHERE year = @year",
)


@pytest.fixture()
def computations():
    comp1, errs1 = parse_computation_text(
        "references/computations/rev_by_year.md", REV_BY_YEAR
    )
    comp2, errs2 = parse_computation_text(
        "references/computations/total_kpi.md", TOTAL_KPI
    )
    assert not errs1 and not errs2
    return {"rev_by_year": comp1, "total_kpi": comp2}


def _config(**overrides):
    """A valid two-tile, one-dial config; kwargs override top-level keys."""
    base = {
        "canvas_version": 1,
        "title": "DtC revenue",
        "data_domain": "sport",
        "dataset": "formula_1",
        "dials": [{"name": "year", "label": "Year", "default": 2024}],
        "tiles": [
            {
                "id": "trend",
                "title": "Revenue by month",
                "widget": "line",
                "data": {
                    "computation": "rev_by_year",
                    "bind": {
                        "year": {"dial": "year"},
                        "brand": {"value": "Audi"},
                    },
                },
                "shape": [{"op": "sort", "by": "month"}],
                "encoding": {
                    "x": {"field": "month"},
                    "y": {"field": "revenue", "format": "currency:EUR"},
                },
                "layout": {"x": 0, "y": 0, "w": 8, "h": 4},
            },
            {
                "id": "total",
                "title": "Total",
                "widget": "kpi",
                "data": {
                    "computation": "total_kpi",
                    "bind": {"year": {"dial": "year"}},
                },
                "encoding": {"value": {"field": "total", "format": "currency:EUR"}},
                "layout": {"x": 8, "y": 0, "w": 4, "h": 4},
            },
        ],
    }
    base.update(overrides)
    return base


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


# ---------------------------------------------------------------------------
# Static layer
# ---------------------------------------------------------------------------


def test_valid_config_parses_and_normalizes(computations):
    canvas, findings = cv.parse_canvas(_config())
    assert not findings
    assert canvas is not None
    assert [t.id for t in canvas.tiles] == ["trend", "total"]
    # Single-field channels normalize to one-element fields lists.
    assert canvas.tiles[0].encoding["y"] == {
        "fields": ["revenue"], "label": None, "format": "currency:EUR",
    }
    # Dial state + per-tile values: fixed binds verbatim, dial binds resolved.
    dials = cv.dial_values(canvas)
    assert dials == {"year": 2024}
    assert cv.tile_values(canvas.tiles[0], dials) == {"year": 2024, "brand": "Audi"}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda c: c.update(canvas_version=2), "canvas-schema"),
        (lambda c: c.update(title=""), "canvas-schema"),
        (lambda c: c.update(dataset=""), "canvas-schema"),
        (lambda c: c.update(surprise=1), "canvas-schema"),
        (lambda c: c.update(tiles=[]), "canvas-tile"),
        (lambda c: c["tiles"][0].update(widget="hologram"), "canvas-widget"),
        # line requires x: drop it.
        (lambda c: c["tiles"][0]["encoding"].pop("x"), "canvas-encoding"),
        # kpi has no `series` channel.
        (
            lambda c: c["tiles"][1]["encoding"].update(series={"field": "b"}),
            "canvas-encoding",
        ),
        # series + multi-y is two competing series dimensions.
        (
            lambda c: c["tiles"][0]["encoding"].update(
                y={"fields": ["revenue", "cost"]}, series={"field": "brand"}
            ),
            "canvas-encoding",
        ),
        # kpi takes no options at all.
        (lambda c: c["tiles"][1].update(options={"stacked": True}), "canvas-options"),
        (lambda c: c["tiles"][1].update(id="trend"), "canvas-tile"),
        (
            lambda c: c["tiles"][0]["data"]["bind"].update(year={"dial": "ghost"}),
            "canvas-bind",
        ),
        (
            lambda c: c["tiles"][0]["data"]["bind"].update(
                year={"dial": "year", "value": 1}
            ),
            "canvas-bind",
        ),
        (
            lambda c: c["tiles"][0].update(shape=[{"op": "pivot", "by": "m"}]),
            "canvas-shape",
        ),
        (
            lambda c: c["tiles"][0].update(shape=[{"op": "topN", "n": 0, "by": "m"}]),
            "canvas-shape",
        ),
        # x + w beyond the 12-column grid.
        (
            lambda c: c["tiles"][0].update(layout={"x": 8, "y": 0, "w": 8, "h": 4}),
            "canvas-layout",
        ),
        (
            lambda c: c["tiles"][0]["encoding"]["y"].update(format="money"),
            "canvas-encoding",
        ),
        (lambda c: c["dials"][0].pop("default"), "canvas-dial"),
        (
            lambda c: c.update(dials=c["dials"] + [dict(c["dials"][0])]),
            "canvas-dial",
        ),
    ],
)
def test_static_refusals(mutate, code):
    config = _config()
    mutate(config)
    canvas, findings = cv.parse_canvas(config)
    assert canvas is None
    assert code in {f.code for f in _errors(findings)}


def test_unbound_dial_is_an_error():
    config = _config(
        dials=[
            {"name": "year", "default": 2024},
            {"name": "lonely", "default": 1},
        ]
    )
    canvas, findings = cv.parse_canvas(config)
    assert canvas is None
    assert any(
        f.code == "canvas-dial" and f.path == "dial `lonely`"
        for f in _errors(findings)
    )


def test_layout_overlap_warns_but_parses():
    config = _config()
    config["tiles"][1]["layout"] = {"x": 4, "y": 0, "w": 4, "h": 4}  # under tile 1
    canvas, findings = cv.parse_canvas(config)
    assert canvas is not None
    assert [f.code for f in findings] == ["canvas-layout"]
    assert findings[0].severity == "warning"



def test_layout_overlap_is_refused_on_every_write_path():
    # parse tolerates it (stored configs must keep reading), but the page
    # places tiles at explicit cells — overlapping cards hide each other — so
    # the CI's save gate and the human layout patch both refuse.
    config = _config()
    config["tiles"][1]["layout"] = {"x": 4, "y": 0, "w": 4, "h": 4}
    report = cv.lint_canvas(config)
    static = report.steps[0]
    assert [f.severity for f in static.findings] == ["error"]
    assert "may not overlap" in static.findings[0].message

    clean = _config()
    first = clean["tiles"][0]
    patched, errors = cv.apply_layout_patch(
        clean, {clean["tiles"][1]["id"]: dict(first["layout"])}
    )
    assert patched is None and "overlaps" in errors[0]


# ---------------------------------------------------------------------------
# Contract layer
# ---------------------------------------------------------------------------


def test_contract_clean(computations):
    canvas, _ = cv.parse_canvas(_config())
    assert cv.check_contract(canvas, computations) == []


def test_contract_unknown_computation(computations):
    config = _config()
    config["tiles"][0]["data"]["computation"] = "nope"
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any(f.code == "canvas-computation" and "nope" in f.message for f in findings)


def test_contract_bind_violations(computations):
    config = _config()
    # Unknown parameter, and a fixed value outside the declared enum.
    config["tiles"][0]["data"]["bind"]["ghost"] = {"value": 1}
    config["tiles"][0]["data"]["bind"]["brand"] = {"value": "Porsche"}
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    messages = " | ".join(f.message for f in findings)
    assert "no parameter `ghost`" in messages
    assert "Porsche" in messages


def test_contract_missing_required_bind(computations):
    config = _config()
    # `year` on rev_by_year is required (no default) — unbind it.
    del config["tiles"][0]["data"]["bind"]["year"]
    config["dials"] = [{"name": "year", "default": 2024}]
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any(
        "required parameter `year`" in f.message and f.path == "tile `trend`"
        for f in findings
    )


def test_contract_dial_default_and_mixed_types(computations):
    # Default 1999 sits below rev_by_year's declared min 2000.
    config = _config()
    config["dials"][0]["default"] = 1999
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any(f.code == "canvas-dial" and "1999" in f.message for f in findings)

    # One dial driving an integer param AND a string param is refused.
    config = _config()
    config["tiles"][0]["data"]["bind"]["brand"] = {"dial": "year"}
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any("mixed types" in f.message for f in findings)


# ---------------------------------------------------------------------------
# Shape ops
# ---------------------------------------------------------------------------


def test_apply_shape_semantics():
    columns = ["Month", "revenue"]
    rows = [
        ["b", "45"],   # numeric-looking strings sort numerically (Athena
        ["a", "123"],  # returns strings) — '45' must never beat '123'.
        ["c", None],
        ["d", "7"],
    ]
    top = cv.apply_shape([{"op": "topN", "n": 2, "by": "revenue"}], columns, rows)
    assert [r[0] for r in top] == ["a", "b"]
    # Sort asc: None LAST, not smallest; column name matches case-insensitively.
    asc = cv.apply_shape([{"op": "sort", "by": "REVENUE"}], columns, rows)
    assert [r[0] for r in asc] == ["d", "b", "a", "c"]
    desc = cv.apply_shape([{"op": "sort", "by": "revenue", "dir": "desc"}], columns, rows)
    assert [r[0] for r in desc] == ["a", "b", "d", "c"]
    assert len(cv.apply_shape([{"op": "limit", "n": 3}], columns, rows)) == 3


# ---------------------------------------------------------------------------
# Live layer
# ---------------------------------------------------------------------------


def _executor(results, calls=None):
    # Object-based (comp, values): the same executor shape runs bundle slugs
    # and synthesized ad-hoc tiles; tests key canned results by comp.slug.
    def execute(comp, values):
        if calls is not None:
            calls.append((comp.slug, dict(values)))
        r = results[comp.slug]
        if isinstance(r, Exception):
            raise r
        return r
    return execute


GOOD_RESULTS = {
    "rev_by_year": {
        "columns": ["month", "revenue", "brand"],
        "rows": [["01", "10.5", "Audi"], ["02", "11", "Audi"]],
    },
    "total_kpi": {"columns": ["total"], "rows": [["21.5"]]},
}


def test_live_clean_probes_defaults_and_extremes(computations):
    canvas, _ = cv.parse_canvas(_config())
    calls = []
    findings, runs = cv.check_live(
        canvas, computations, _executor(GOOD_RESULTS, calls)
    )
    assert findings == []
    # 2 tiles at defaults + the year dial's min/max probed on both bound tiles.
    years = sorted(v["year"] for _, v in calls)
    assert years == [2000, 2000, 2024, 2024, 2030, 2030]
    assert runs == len(calls) == 6
    # Fixed binds ride along on every run.
    assert all(v["brand"] == "Audi" for s, v in calls if s == "rev_by_year")


def test_live_missing_column_and_empty(computations):
    canvas, _ = cv.parse_canvas(_config())
    results = {
        "rev_by_year": {"columns": ["month", "umsatz"], "rows": [["01", "1"]]},
        "total_kpi": {"columns": ["total"], "rows": []},
    }
    findings, _ = cv.check_live(canvas, computations, _executor(results))
    by_code = {f.code for f in findings}
    assert "canvas-columns" in by_code  # revenue missing
    assert any(f.code == "canvas-empty" and f.severity == "warning" for f in findings)


def test_live_row_caps_and_numeric(computations):
    config = _config()
    config["tiles"][0].update(
        widget="pie",
        encoding={"category": {"field": "month"}, "value": {"field": "revenue"}},
        shape=[],
    )
    canvas, _ = cv.parse_canvas(config)
    results = {
        # 51 rows breaches pie's hard cap of 50; negative share warns.
        "rev_by_year": {
            "columns": ["month", "revenue"],
            "rows": [[f"m{i}", "-1" if i == 0 else "1"] for i in range(51)],
        },
        # KPI value is not a number at all.
        "total_kpi": {"columns": ["total"], "rows": [["n/a"]]},
    }
    findings, _ = cv.check_live(canvas, computations, _executor(results))
    assert any(f.code == "canvas-rows" and f.severity == "error" for f in findings)
    assert any(f.code == "canvas-numeric" and "negative" in f.message for f in findings)
    assert any(
        f.code == "canvas-numeric" and "'n/a'" in f.message and f.severity == "error"
        for f in findings
    )


def test_live_executor_failure_is_a_finding_not_a_crash(computations):
    canvas, _ = cv.parse_canvas(_config())
    results = dict(GOOD_RESULTS)
    results["total_kpi"] = RuntimeError("engine on fire")
    findings, _ = cv.check_live(canvas, computations, _executor(results))
    assert any(
        f.code == "canvas-execute" and "engine on fire" in f.message
        and f.path == "tile `total`"
        for f in findings
    )
    # The other tile was still checked (its runs happened).


def test_live_budget_truncates(computations):
    canvas, _ = cv.parse_canvas(_config())
    findings, runs = cv.check_live(
        canvas, computations, _executor(GOOD_RESULTS), max_runs=1
    )
    assert runs == 1
    assert any(f.code == "canvas-budget" for f in findings)


# ---------------------------------------------------------------------------
# Ad-hoc SQL tiles (inline, unattested — synthesized through the SAME
# computation contract: read-only single statement, holes == parameters)
# ---------------------------------------------------------------------------


def _adhoc_config():
    config = _config()
    config["tiles"][0]["data"] = {
        "sql": "SELECT month, revenue FROM sales WHERE year = @year",
        "parameters": [
            {"name": "year", "type": "integer", "example": 2024,
             "min": 2000, "max": 2030},
        ],
        "bind": {"year": {"dial": "year"}},
    }
    return config


def test_adhoc_tile_full_green(computations):
    config = _adhoc_config()
    calls = []
    results = {**GOOD_RESULTS, "_adhoc_trend": {
        "columns": ["month", "revenue"], "rows": [["01", "10"]],
    }}
    report = cv.lint_canvas(config, computations, _executor(results, calls))
    assert report.ok, [f.to_dict() for f in report.findings]
    # The inline contract drives dial extremes exactly like a doc'd one.
    years = sorted(v["year"] for s, v in calls if s == "_adhoc_trend")
    assert years == [2000, 2024, 2030]


def test_adhoc_tile_refusals(computations):
    # Read-only is enforced through the synthesized computation: DDL refused.
    config = _adhoc_config()
    config["tiles"][0]["data"]["sql"] = "DROP TABLE sales"
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any(f.code == "canvas-adhoc" for f in findings)

    # An undeclared @hole is refused (holes == declared parameters).
    config = _adhoc_config()
    config["tiles"][0]["data"]["sql"] = (
        "SELECT month FROM sales WHERE year = @year AND brand = @brand"
    )
    canvas, _ = cv.parse_canvas(config)
    findings = cv.check_contract(canvas, computations)
    assert any("@brand" in f.message for f in findings)

    # Statically: sql XOR computation, and parameters only with sql.
    config = _adhoc_config()
    config["tiles"][0]["data"]["computation"] = "rev_by_year"
    canvas, findings = cv.parse_canvas(config)
    assert canvas is None
    assert any("exactly one of" in f.message for f in findings)


# ---------------------------------------------------------------------------
# The layered report
# ---------------------------------------------------------------------------


def test_lint_canvas_full_green(computations):
    report = cv.lint_canvas(
        _config(), computations, _executor(GOOD_RESULTS)
    )
    assert [s.name for s in report.steps] == ["static", "contract", "live"]
    assert [s.status for s in report.steps] == ["ok", "ok", "ok"]
    assert report.ok


def test_lint_canvas_static_errors_skip_the_rest(computations):
    report = cv.lint_canvas({"canvas_version": 1}, computations)
    assert report.steps[0].status == "issues"
    assert [s.status for s in report.steps[1:]] == ["skipped", "skipped"]
    assert not report.ok


def test_lint_canvas_contract_errors_skip_live(computations):
    config = _config()
    config["tiles"][0]["data"]["computation"] = "nope"
    report = cv.lint_canvas(config, computations, _executor(GOOD_RESULTS))
    assert report.steps[1].status == "issues"
    assert report.steps[2].status == "skipped"
    assert not report.ok


def test_lint_canvas_without_executor_skips_live(computations):
    report = cv.lint_canvas(_config(), computations)
    assert report.steps[2].status == "skipped"
    # Static + contract clean; a skipped live step is not a failure.
    assert report.ok


# ---------------------------------------------------------------------------
# Skeleton (the requirements-time tile plan)
# ---------------------------------------------------------------------------


def _skeleton():
    return {
        "title": "Season overview",
        "widgets": [
            {
                "title": "Wins by constructor",
                "widget": "bar",
                "represents": "total wins per constructor for the selected season",
            },
            {
                "title": "Points trend",
                "widget": "line",
                "represents": "cumulative points per round",
                "layout": {"w": 12, "h": 5},
            },
        ],
        "dials": [{"name": "season", "label": "Season", "filters": "which season all tiles show"}],
    }


def test_skeleton_normalizes_green():
    spec, errors = cv.normalize_skeleton(_skeleton())
    assert errors == []
    # Layout defaults fill; explicit layout survives; unknown keys are gone.
    assert spec["widgets"][0]["layout"] == {"w": 6, "h": 4}
    assert spec["widgets"][1]["layout"] == {"w": 12, "h": 5}
    assert spec["dials"][0]["label"] == "Season"
    assert set(spec) == {"title", "widgets", "dials"}


def test_skeleton_dial_label_defaults_to_name():
    raw = _skeleton()
    raw["dials"] = [{"name": "season"}]
    spec, errors = cv.normalize_skeleton(raw)
    assert errors == []
    assert spec["dials"][0] == {"name": "season", "label": "season", "filters": ""}


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda s: s.pop("title"), "spec.title"),
        (lambda s: s.update(widgets=[]), "spec.widgets"),
        (lambda s: s["widgets"][0].pop("represents"), "widgets[0].represents"),
        (lambda s: s["widgets"][0].update(widget="hologram"), "not in the catalog"),
        (lambda s: s["widgets"][1].update(layout={"w": 40}), "1..12"),
        (lambda s: s.update(dials=[{"name": "a"}, {"name": "a"}]), "duplicate dial"),
    ],
)
def test_skeleton_refusals(mutate, needle):
    raw = _skeleton()
    mutate(raw)
    _spec, errors = cv.normalize_skeleton(raw)
    assert any(needle in e for e in errors), errors


def test_skeleton_not_a_dict():
    _spec, errors = cv.normalize_skeleton("plan")
    assert errors and "object" in errors[0]


# ---------------------------------------------------------------------------
# Layout patch (the one direct human config mutation — drag/resize)
# ---------------------------------------------------------------------------


def test_layout_patch_green():
    config = _config()
    original_data = config["tiles"][0]["data"].copy()
    patched, errors = cv.apply_layout_patch(
        config, {config["tiles"][0]["id"]: {"x": 0, "y": 4, "w": 6, "h": 3}}
    )
    assert errors == []
    assert patched["tiles"][0]["layout"] == {"x": 0, "y": 4, "w": 6, "h": 3}
    # Nothing but geometry moved — and the input config was not mutated.
    assert patched["tiles"][0]["data"] == original_data
    assert config["tiles"][0]["layout"] != patched["tiles"][0]["layout"]


@pytest.mark.parametrize(
    ("layouts", "needle"),
    [
        ({}, "non-empty"),
        ({"ghost": {"x": 0, "y": 0, "w": 6, "h": 4}}, "unknown tile"),
        # Same geometry rules as the lint: fit the 12-column grid, full shape.
        ({"t1": {"x": 8, "y": 0, "w": 6, "h": 4}}, "12-column"),
        ({"t1": {"x": 0, "y": 0, "w": 6}}, "{x, y, w, h}"),
    ],
)
def test_layout_patch_refusals(layouts, needle):
    config = _config()
    config["tiles"][0]["id"] = "t1"
    patched, errors = cv.apply_layout_patch(config, layouts)
    assert patched is None
    assert any(needle in e for e in errors), errors


# ---------------------------------------------------------------------------
# The BI-staple widgets (combo/pivot/heatmap/waterfall/funnel/gauge/
# histogram/boxplot) + structured options (references, rules, gauge bounds)
# ---------------------------------------------------------------------------


def _tile(widget, encoding, options=None):
    tile = {
        "id": "t9", "title": "T", "widget": widget,
        "data": {"computation": "revenue_by_region", "bind": {"region": {"value": "EMEA"}}},
        "encoding": encoding,
        "layout": {"x": 0, "y": 20, "w": 6, "h": 4},
    }
    if options:
        tile["options"] = options
    return tile


NEW_WIDGET_TILES = {
    "combo": (
        {"x": {"field": "m"}, "bars": {"fields": ["rev"]}, "lines": {"fields": ["margin"]}},
        {"y2Label": "margin", "references": [{"axis": "y", "value": 100, "label": "target"}]},
    ),
    "heatmap": ({"x": {"field": "d"}, "y": {"field": "h"}, "value": {"field": "v"}}, None),
    "waterfall": (
        {"x": {"field": "step"}, "value": {"field": "delta"}, "total": {"field": "is_total"}},
        None,
    ),
    "funnel": ({"stage": {"field": "s"}, "value": {"field": "n"}}, None),
    "gauge": ({"value": {"field": "pct"}}, {"min": 0, "max": 100}),
    "histogram": ({"bin": {"field": "b"}, "count": {"field": "n"}}, None),
    "boxplot": (
        {"category": {"field": "c"}, "min": {"field": "lo"}, "q1": {"field": "q1"},
         "median": {"field": "md"}, "q3": {"field": "q3"}, "max": {"field": "hi"}},
        None,
    ),
    "pivot": (
        {"rows": {"fields": ["r"]}, "columns": {"field": "c"}, "values": {"fields": ["v"]}},
        {"rules": [{"field": "v", "min": 100, "color": "emerald"}]},
    ),
}


@pytest.mark.parametrize("widget", sorted(NEW_WIDGET_TILES))
def test_new_widgets_parse_green(widget):
    config = _config()
    enc, options = NEW_WIDGET_TILES[widget]
    config["tiles"].append(_tile(widget, enc, options))
    canvas, findings = cv.parse_canvas(config)
    assert canvas is not None, findings
    assert not [f for f in findings if f.severity == "error"], findings


@pytest.mark.parametrize(
    ("widget", "enc", "options", "needle"),
    [
        ("gauge", {"value": {"field": "pct"}}, None, "numeric `max`"),
        ("gauge", {"value": {"field": "pct"}}, {"min": 5, "max": 5}, "`min` < `max`"),
        ("boxplot", {"category": {"field": "c"}, "min": {"field": "lo"}}, None, "q1"),
        ("bar", {"x": {"field": "a"}, "y": {"fields": ["b"]}},
         {"references": [{"axis": "z", "value": 3}]}, "`axis` is"),
        ("bar", {"x": {"field": "a"}, "y": {"fields": ["b"]}},
         {"references": [{"value": 3, "from": 1, "to": 2}]}, "OR"),
        ("pivot", {"rows": {"fields": ["r"]}, "values": {"fields": ["v"]}},
         {"rules": [{"field": "v", "min": 1, "color": "magenta"}]}, "color"),
    ],
)
def test_new_widget_option_refusals(widget, enc, options, needle):
    config = _config()
    config["tiles"].append(_tile(widget, enc, options))
    _canvas, findings = cv.parse_canvas(config)
    errs = [f.message for f in findings if f.severity == "error"]
    assert any(needle in e for e in errs), errs


# ---------------------------------------------------------------------------
# Ad-hoc runtime routing + dial-extreme intersection (code-review fixes)
# ---------------------------------------------------------------------------


def test_adhoc_runtime_follows_the_source():
    # source_runtime single-sources the engine choice for inline SQL; the
    # synthesized computation must carry it (a hardcoded athena made every
    # ad-hoc tile refuse to execute on redshift-mapped datasets).
    assert cv.source_runtime(None) == "athena"
    assert cv.source_runtime({"type": "glue", "glue_database": "x"}) == "athena"
    assert cv.source_runtime({"type": "redshift", "redshift_database": "db"}) == "redshift"
    comp, errs = cv.adhoc_computation("t1", "SELECT 1 AS one", [], "redshift")
    assert not errs and comp.runtime == "redshift"
    comp, errs = cv.adhoc_computation("t1", "SELECT 1 AS one", [])
    assert not errs and comp.runtime == "athena"
    # Unrecognized runtimes fall back to athena rather than synthesizing an
    # unexecutable doc.
    comp, errs = cv.adhoc_computation("t1", "SELECT 1 AS one", [], "duckdb")
    assert not errs and comp.runtime == "athena"


def test_dial_extremes_intersect_across_bound_params():
    # A dial may legally drive computations with DIFFERENT declared ranges —
    # only the default is contract-checked. Probing must use the tightest
    # bounds every bound parameter accepts (max of mins, min of maxes);
    # pooling the raw extremes ran the narrower tile at the wider tile's min,
    # a guaranteed resolve failure the author could never lint away.
    wide, e1 = parse_computation_text(
        "references/computations/rev_by_year.md", REV_BY_YEAR  # min 2000 max 2030
    )
    narrow_text = _comp_text(
        "  - name: year\n"
        "    type: integer\n"
        "    example: 2024\n"
        "    min: 2010\n"
        "    max: 2024\n",
        "SELECT total FROM totals WHERE year = @year",
    )
    narrow, e2 = parse_computation_text(
        "references/computations/total_kpi.md", narrow_text
    )
    assert not e1 and not e2
    comps = {"rev_by_year": wide, "total_kpi": narrow}
    canvas, findings = cv.parse_canvas(
        _config(dials=[{"name": "year", "label": "Year", "default": 2015}])
    )
    assert canvas is not None and not _errors(findings)
    assert not _errors(cv.check_contract(canvas, comps))  # 2015 satisfies both
    calls = []
    results = {
        "rev_by_year": GOOD_RESULTS["rev_by_year"],
        "total_kpi": GOOD_RESULTS["total_kpi"],
    }
    live_findings, runs = cv.check_live(canvas, comps, _executor(results, calls))
    assert live_findings == []
    years = sorted(v["year"] for _, v in calls)
    # Defaults (2015 ×2) + intersection extremes 2010/2024 (×2 tiles each) —
    # never 2000 or 2030, which the narrow contract would refuse.
    assert years == [2010, 2010, 2015, 2015, 2024, 2024]
    assert runs == 6

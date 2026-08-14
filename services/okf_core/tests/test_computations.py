"""okf_core.computations — parsing, hashing, contracts, substitution, and the
lint/guard integration of Attested Computations (docs/ATTESTED_COMPUTATIONS.md)."""

from __future__ import annotations

import json

import pytest

from okf_core import computations as comp_mod
from okf_core.computations import (
    COMPUTATION_TYPE,
    COMPUTATIONS_PREFIX,
    Computation,
    ComputationError,
    computation_sha256,
    domain_warnings,
    flatten_domains,
    fold_verification_entry,
    hole_names,
    is_computation_path,
    lookup_domain,
    normalize_value,
    parse_computation_text,
    parse_parameters,
    render_literal,
    resolve_values,
    substitute,
    verification_state,
)
from okf_core.guard import check_computation_doc, check_verification_fields
from okf_core.lint import collect_sql_fences, lint_bundle


def comp_doc(
    *,
    sql: str = (
        "SELECT region, SUM(amount) AS revenue\n"
        "FROM orders\n"
        "WHERE region = @region AND order_date >= @since\n"
        "GROUP BY 1"
    ),
    params: str = (
        "parameters:\n"
        '  - {name: region, type: string, required: true, example: "EMEA"}\n'
        '  - {name: since, type: date, required: true, example: "2026-01-01"}\n'
    ),
    runtime: str = "athena",
    verified: str = "verified: null\nverified_by: null\n",
    ctype: str = COMPUTATION_TYPE,
) -> str:
    return (
        "---\n"
        f"type: {ctype}\n"
        "title: Revenue by region since a date\n"
        "description: Recognized revenue for one region from a start date.\n"
        f"runtime: {runtime}\n"
        f"{params}"
        f"{verified}"
        "timestamp: 2026-08-13T00:00:00Z\n"
        "---\n\n"
        "Reads [orders](../../tables/orders.md).\n\n"
        "# Computation\n\n"
        "```sql\n"
        f"{sql}\n"
        "```\n"
    )


REL = COMPUTATIONS_PREFIX + "revenue_by_region.md"


# ---------------------------------------------------------------------------
# Parsing + the content hash
# ---------------------------------------------------------------------------


def test_valid_doc_parses():
    comp, errors = parse_computation_text(REL, comp_doc())
    assert errors == []
    assert comp is not None
    assert comp.slug == "revenue_by_region"
    assert comp.runtime == "athena"
    assert [p["name"] for p in comp.parameters] == ["region", "since"]
    assert comp.statement.startswith("SELECT region")
    assert comp.sha256 == computation_sha256(comp.sql, comp.parameters, "athena")


def test_hash_ignores_trailing_whitespace_but_not_content():
    comp1, _ = parse_computation_text(REL, comp_doc())
    comp2, _ = parse_computation_text(
        REL, comp_doc(sql="SELECT region, SUM(amount) AS revenue   \nFROM orders\nWHERE region = @region AND order_date >= @since\nGROUP BY 1")
    )
    assert comp1 is not None and comp2 is not None
    assert comp1.sha256 == comp2.sha256  # trailing spaces never void a stamp
    comp3, _ = parse_computation_text(
        REL,
        comp_doc(sql="SELECT region, SUM(amount) AS rev\nFROM orders\nWHERE region = @region AND order_date >= @since\nGROUP BY 1"),
    )
    assert comp3 is not None and comp3.sha256 != comp1.sha256


def test_hash_covers_parameters_and_runtime():
    comp1, _ = parse_computation_text(REL, comp_doc())
    widened = comp_doc(
        params=(
            "parameters:\n"
            '  - {name: region, type: string, required: true, example: "EMEA", enum: [EMEA, NA]}\n'
            '  - {name: since, type: date, required: true, example: "2026-01-01"}\n'
        )
    )
    comp2, errors = parse_computation_text(REL, widened)
    assert errors == []
    assert comp2 is not None and comp2.sha256 != comp1.sha256  # bounds are signed
    comp3, _ = parse_computation_text(REL, comp_doc(runtime="redshift"))
    assert comp3 is not None and comp3.sha256 != comp1.sha256


@pytest.mark.parametrize(
    "mutation, expect",
    [
        (dict(sql="DELETE FROM orders WHERE region = @region AND x = @since"), "exactly ONE"),
        (dict(sql="SELECT 1; SELECT region FROM orders WHERE region=@region AND d=@since"), "exactly ONE"),
        (dict(sql="SELECT * FROM <table> WHERE region=@region AND d=@since"), "placeholders"),
        (dict(sql="SELECT 1 FROM orders WHERE region=@region AND d=@since AND x=@extra"), "not declared"),
        (dict(sql="SELECT 1 FROM orders WHERE region=@region"), "never appears"),
        (dict(runtime="bigquery"), "runtime"),
        (dict(ctype="Reference"), "type"),
    ],
)
def test_shape_errors(mutation, expect):
    comp, errors = parse_computation_text(REL, comp_doc(**mutation))
    assert comp is None
    assert any(expect in e for e in errors), errors


def test_missing_and_multiple_fences():
    no_section = comp_doc().replace("# Computation", "# Notes")
    comp, errors = parse_computation_text(REL, no_section)
    assert comp is None and any("no ```sql fence" in e for e in errors)

    two = comp_doc() + "\n```sql\nSELECT 2\n```\n"
    comp, errors = parse_computation_text(REL, two)
    assert comp is None and any("one canonical statement" in e for e in errors)


def test_fence_outside_section_is_prose_not_computation():
    # A ```sql fence under another heading (an example in prose) is fine —
    # only the # Computation section's fence is the frozen statement.
    text = comp_doc() + "\n# Usage\n\n```sql\nSELECT 'call run_computation'\n```\n"
    comp, errors = parse_computation_text(REL, text)
    assert errors == []
    assert comp is not None and "@region" in comp.sql


def test_is_computation_path():
    assert is_computation_path(REL)
    assert not is_computation_path("references/computations/sub/x.md")
    assert not is_computation_path("references/metrics/x.md")
    assert not is_computation_path(COMPUTATIONS_PREFIX)


# ---------------------------------------------------------------------------
# Parameter contracts
# ---------------------------------------------------------------------------


def P(**kw):
    base = {"name": "p", "type": "string", "required": True, "example": "x"}
    base.update(kw)
    return base


@pytest.mark.parametrize(
    "param, expect",
    [
        (P(name="1bad"), "identifier"),
        (P(type="text"), "`type` must be one of"),
        (P(exmaple="x"), "unknown key"),
        (P(required=False), "needs a `default`"),
        (P(required=True, default="x"), "cannot have a `default`"),
        ({"name": "p", "type": "string"}, "`example` is required"),
        (P(type="boolean", example=True, enum=[True]), "meaningless for a boolean"),
        (P(type="integer", example=5, min=10, max=1), "`min` exceeds `max`"),
        (P(type="integer", example=99, min=1, max=10), "violates the contract"),
        (P(enum=["a"], example="b"), "violates the contract"),
        (P(column="justtable"), "`column` must be"),
        (P(type="string", example="x", min="a"), "orderable type"),
    ],
)
def test_parameter_contract_errors(param, expect):
    normalized, errors = parse_parameters([param])
    assert normalized == []
    assert any(expect in e for e in errors), errors


def test_duplicate_and_too_many_parameters():
    _, errors = parse_parameters([P(), P()])
    assert any("duplicate" in e for e in errors)
    _, errors = parse_parameters([P(name=f"p{i}") for i in range(20)])
    assert any("at most" in e for e in errors)


def test_normalize_value_matrix():
    assert normalize_value("integer", "42") == (42, None)
    assert normalize_value("integer", True)[1] is not None
    assert normalize_value("number", "1e3") == ("1e3", None)
    assert normalize_value("number", "nan")[1] is not None
    assert normalize_value("number", "1_000")[1] is not None
    assert normalize_value("boolean", "TRUE") == (True, None)
    assert normalize_value("date", "2026-07-01") == ("2026-07-01", None)
    assert normalize_value("date", "07/01/2026")[1] is not None
    assert normalize_value("timestamp", "2026-07-01T10:30") == ("2026-07-01 10:30", None)
    assert normalize_value("string", "O'Neil") == ("O'Neil", None)
    assert normalize_value("string", "a\x00b")[1] is not None
    assert normalize_value("string", ["x"])[1] is not None
    assert normalize_value("string", None)[1] is not None


def test_resolve_values_fills_defaults_and_names_every_problem():
    params, errors = parse_parameters(
        [
            {"name": "region", "type": "string", "example": "EMEA",
             "default": "EMEA", "enum": ["EMEA", "NA"]},
            {"name": "n", "type": "integer", "example": 5, "min": 1, "max": 10},
        ]
    )
    assert errors == []
    assert resolve_values(params, {"n": 3}) == {"region": "EMEA", "n": 3}
    with pytest.raises(ComputationError) as ei:
        resolve_values(params, {"region": "APAC", "typo": 1})
    msg = str(ei.value)
    assert "unknown parameter `typo`" in msg
    assert "not among the declared values" in msg
    assert "missing required parameter `n`" in msg
    with pytest.raises(ComputationError, match="above the declared max"):
        resolve_values(params, {"n": 99})


# ---------------------------------------------------------------------------
# Rendering + substitution (the injection guard)
# ---------------------------------------------------------------------------


def test_render_literal_dialects():
    assert render_literal("string", "O'Neil", "athena") == "'O''Neil'"
    # Redshift treats backslash as an escape char; Athena must NOT double it.
    assert render_literal("string", "x\\", "redshift") == "'x\\\\'"
    assert render_literal("string", "x\\", "athena") == "'x\\'"
    assert render_literal("integer", 42, "athena") == "42"
    assert render_literal("number", "1.5", "athena") == "1.5"
    assert render_literal("boolean", True, "athena") == "TRUE"
    assert render_literal("date", "2026-07-01", "athena") == "DATE '2026-07-01'"
    assert (
        render_literal("timestamp", "2026-07-01 10:30", "athena")
        == "TIMESTAMP '2026-07-01 10:30'"
    )


def test_substitute_masks_literals_and_comments():
    sql = (
        "SELECT '@region literal', col -- @region comment\n"
        "FROM t WHERE r = @region AND r2 = @region"
    )
    out = substitute(sql, {"region": "'EMEA'"})
    assert out.count("'EMEA'") == 2
    assert "'@region literal'" in out
    assert "-- @region comment" in out
    with pytest.raises(ComputationError, match="no value for hole"):
        substitute("SELECT @other", {})


def test_hole_names_and_injection_end_to_end():
    comp, _ = parse_computation_text(REL, comp_doc())
    assert comp is not None
    assert hole_names(comp.statement) == {"region", "since"}
    # A quote-smuggling value arrives as a harmless literal.
    sql = comp.rendered({"region": "EMEA' OR '1'='1", "since": "2026-01-01"})
    assert "'EMEA'' OR ''1''=''1'" in sql
    assert "@" not in sql.replace("@", "@")  # no unfilled holes
    assert "DATE '2026-01-01'" in sql


def test_rendered_uses_examples_for_lint():
    comp, _ = parse_computation_text(REL, comp_doc())
    assert comp is not None
    sql = comp.rendered(comp.example_values())
    assert "'EMEA'" in sql and "DATE '2026-01-01'" in sql


# ---------------------------------------------------------------------------
# Verification state + fold-in
# ---------------------------------------------------------------------------


def test_verification_state_overlay_wins_and_stale_surfaces():
    sha = "a" * 64
    unv = verification_state(sha, frontmatter={"verified": None})
    assert unv["verification"] == "unverified"
    fm = {"verified": "2026-08-14T09:30:00Z", "verified_by": "a@x", "verified_sha256": sha}
    assert verification_state(sha, frontmatter=fm)["verification"] == "verified"
    assert verification_state("b" * 64, frontmatter=fm)["verification"] == "stale"
    overlay = {"slug": "s", "sha256": sha, "verified": "2026-08-15T00:00:00Z", "verified_by": "b@x"}
    merged = verification_state(sha, frontmatter=fm, overlay_entry=overlay)
    assert merged == {
        "verification": "verified",
        "verified": "2026-08-15T00:00:00Z",
        "verified_by": "b@x",
    }
    assert (
        verification_state("b" * 64, frontmatter=fm, overlay_entry=overlay)[
            "verification"
        ]
        == "stale"
    )


def test_fold_verification_entry_binds_only_on_hash_match():
    text = comp_doc()
    comp, _ = parse_computation_text(REL, text)
    assert comp is not None
    entry = {
        "slug": comp.slug,
        "sha256": comp.sha256,
        "verified": "2026-08-14T09:30:00Z",
        "verified_by": "analyst@example.com",
    }
    folded = fold_verification_entry(text, entry)
    assert folded is not None
    comp2, errors = parse_computation_text(REL, folded)
    assert errors == []
    assert comp2 is not None
    assert comp2.verified == "2026-08-14T09:30:00Z"
    assert comp2.verified_by == "analyst@example.com"
    assert comp2.verified_sha256 == comp.sha256
    assert comp2.sha256 == comp.sha256  # folding never changes the hash
    # The doc changed since the click: the stamp must NOT be written in.
    assert fold_verification_entry(text, {**entry, "sha256": "b" * 64}) is None
    assert fold_verification_entry("not a doc", entry) is None


# ---------------------------------------------------------------------------
# Profile evidence (advisory layer)
# ---------------------------------------------------------------------------


DOMAINS = {
    "version": 1,
    "tables": {
        "customers": {
            "profiled_at": "2026-08-01T00:00:00Z",
            "columns": {
                "region": {"values": ["EMEA", "NA", "APAC"], "distinct": 3, "exhaustive": True}
            },
        }
    },
}


def test_domain_lookup_exact_and_suffix():
    flat = flatten_domains(DOMAINS)
    assert lookup_domain(flat, "customers.region") is not None
    # Redshift-style schema-qualified snapshot key, short binding in the doc.
    flat2 = {"public.customers.region": flat["customers.region"]}
    assert lookup_domain(flat2, "customers.region") is not None
    assert lookup_domain(flat, "customers.missing") is None


def test_domain_warnings_warn_and_run():
    params, errors = parse_parameters(
        [{"name": "region", "type": "string", "example": "EMEA", "column": "customers.region"}]
    )
    assert errors == []
    flat = flatten_domains(DOMAINS)
    assert domain_warnings(params, {"region": "EMEA"}, flat) == []
    warnings = domain_warnings(params, {"region": "EMAE"}, flat)
    assert len(warnings) == 1 and "EMAE" in warnings[0] and "typo" in warnings[0]


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def test_guard_verification_fields_null_preserve_invent():
    ok = check_verification_fields({"verified": None, "verified_by": None}, None)
    assert ok.ok
    invent = check_verification_fields({"verified": "2026-08-14T00:00:00Z"}, None)
    assert not invent.ok and "HUMAN" in invent.error
    existing = {"verified": "2026-08-14T00:00:00Z", "verified_by": "a@x", "verified_sha256": "c" * 64}
    assert check_verification_fields(dict(existing), existing).ok  # preserve
    altered = {**existing, "verified_by": "attacker@x"}
    assert not check_verification_fields(altered, existing).ok


def test_guard_computation_doc_shape_and_placement():
    from okf_core.document import OKFDocument

    doc = OKFDocument.parse(comp_doc())
    assert check_computation_doc(REL, doc.frontmatter, doc.body).ok
    # A computation-typed doc outside the folder is invisible to consumers.
    misplaced = check_computation_doc(
        "references/metrics/x.md", doc.frontmatter, doc.body
    )
    assert not misplaced.ok and COMPUTATIONS_PREFIX in misplaced.error
    bad = OKFDocument.parse(comp_doc(sql="SELECT 1 FROM t WHERE a=@region"))
    refused = check_computation_doc(REL, bad.frontmatter, bad.body)
    assert not refused.ok and "never appears" in refused.error
    # Non-computation docs pass through untouched.
    assert check_computation_doc(
        "tables/orders.md", {"type": "Glue Table"}, "# Schema\n"
    ).ok


# ---------------------------------------------------------------------------
# Lint integration
# ---------------------------------------------------------------------------


def _write_bundle(tmp_path, comp_text=None, domains=DOMAINS):
    (tmp_path / ".metadata" / "tables").mkdir(parents=True)
    (tmp_path / ".metadata" / "tables" / "orders.md").write_text("# orders")
    (tmp_path / ".metadata" / "columns.tsv").write_text(
        "table\tcolumn\ttype\tcomment\n"
        "orders\tregion\tstring\t\n"
        "orders\tamount\tdouble\t\n"
        "orders\torder_date\tdate\t\n"
        "customers\tregion\tstring\t\n"
    )
    if domains is not None:
        prof = tmp_path / ".metadata" / "profile"
        prof.mkdir(parents=True)
        (prof / "domains.json").write_text(json.dumps(domains))
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "sales.md").write_text(
        "---\ntype: Glue Database\ntitle: t\ndescription: d\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n\nSee "
        "[guardrails](../references/usage_guardrails.md) and "
        "[orders](../tables/orders.md) and "
        "[revenue](../references/computations/revenue_by_region.md).\n"
    )
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.md").write_text(
        "---\ntype: Glue Table\ntitle: t\ndescription: d\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n\nbody\n"
    )
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "usage_guardrails.md").write_text(
        "---\ntype: Reference\ntitle: t\ndescription: d\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n\nbody\n"
    )
    if comp_text is not None:
        cdir = tmp_path / "references" / "computations"
        cdir.mkdir()
        (cdir / "revenue_by_region.md").write_text(comp_text)


def _step(report, name):
    return next(s for s in report.steps if s.name == name)


def test_lint_valid_computation_passes(tmp_path):
    _write_bundle(tmp_path, comp_doc())
    report = lint_bundle(tmp_path)
    step = _step(report, "computations")
    assert step.status == "ok", [f.message for f in step.findings]
    assert report.ok


def test_lint_flags_invalid_computation_and_misplaced_type(tmp_path):
    _write_bundle(tmp_path, comp_doc(sql="SELECT 1 FROM t WHERE a=@region"))
    (tmp_path / "references" / "misplaced.md").write_text(comp_doc())
    report = lint_bundle(tmp_path)
    step = _step(report, "computations")
    codes = {f.code for f in step.findings}
    assert "invalid-computation" in codes
    assert "computation-outside-folder" in codes


def test_lint_checks_column_bindings_and_enum_evidence(tmp_path):
    text = comp_doc(
        params=(
            "parameters:\n"
            '  - {name: region, type: string, required: true, example: "EMEA",\n'
            "     enum: [EMEA, MOON], column: customers.region}\n"
            '  - {name: since, type: date, required: true, example: "2026-01-01",\n'
            "     column: orders.nosuch}\n"
        )
    )
    _write_bundle(tmp_path, text)
    report = lint_bundle(tmp_path)
    step = _step(report, "computations")
    by_code = {f.code: f for f in step.findings}
    # Exhaustive scan proved MOON absent -> error (invention, not evidence).
    assert by_code["computation-enum-not-observed"].severity == "error"
    assert "MOON" in by_code["computation-enum-not-observed"].message
    assert by_code["computation-unknown-column"].severity == "error"


def test_collect_fences_substitutes_examples_for_explain(tmp_path):
    _write_bundle(tmp_path, comp_doc())
    fences = collect_sql_fences(tmp_path)
    comp_fences = [f for f in fences if f.path == REL]
    assert len(comp_fences) == 1
    (stmt,) = comp_fences[0].statements
    assert "@region" not in stmt and "'EMEA'" in stmt
    assert not comp_fences[0].templated


def test_at_holes_classify_as_templated_elsewhere(tmp_path):
    _write_bundle(tmp_path, None)
    (tmp_path / "references" / "recipes").mkdir()
    (tmp_path / "references" / "recipes" / "r.md").write_text(
        "---\ntype: Reference\ntitle: t\ndescription: d\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n\n```sql\n"
        "SELECT * FROM orders WHERE region = @region\n```\n"
    )
    fences = [f for f in collect_sql_fences(tmp_path) if f.path.endswith("r.md")]
    assert len(fences) == 1 and fences[0].templated


def test_computation(tmp_path):
    # dataclass smoke: rendered() refuses a constraint violation end-to-end.
    comp, _ = parse_computation_text(
        REL,
        comp_doc(
            params=(
                "parameters:\n"
                '  - {name: region, type: string, required: true, example: "EMEA", enum: [EMEA, NA]}\n'
                '  - {name: since, type: date, required: true, example: "2026-01-01"}\n'
            )
        ),
    )
    assert isinstance(comp, Computation)
    with pytest.raises(ComputationError, match="declared values"):
        comp.rendered({"region": "MARS", "since": "2026-01-01"})


def test_is_frozen_requires_matching_folded_stamp():
    from okf_core.computations import is_frozen

    comp, _ = parse_computation_text(REL, comp_doc())
    assert not is_frozen(comp)  # unverified
    stamped = comp_doc(
        verified=f"verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n"
    )
    comp2, errors = parse_computation_text(REL, stamped)
    assert errors == [] and is_frozen(comp2)
    # A stale stamp (content changed since) is NOT frozen — it needs repair.
    stale = comp_doc(
        sql="SELECT region FROM orders WHERE region = @region AND d >= @since",
        verified=f"verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    )
    comp3, errors = parse_computation_text(REL, stale)
    assert errors == [] and not is_frozen(comp3)


def test_lint_downgrades_findings_on_frozen_computations(tmp_path):
    # Schema drift under a FROZEN (human-verified, hash-matching) computation:
    # real finding, but unfixable by an agent — must be a WARNING that routes
    # to the human, never an error that wedges the fix-to-zero gate.
    text = comp_doc(
        params=(
            "parameters:\n"
            '  - {name: region, type: string, required: true, example: "EMEA",\n'
            "     column: orders.nosuch}\n"
            '  - {name: since, type: date, required: true, example: "2026-01-01"}\n'
        )
    )
    comp, errors = parse_computation_text(REL, text)
    assert errors == []
    frozen_text = text.replace(
        "verified: null\nverified_by: null\n",
        f"verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    )
    frozen_root = tmp_path / "frozen"
    frozen_root.mkdir()
    _write_bundle(frozen_root, frozen_text)
    report = lint_bundle(frozen_root)
    step = next(s for s in report.steps if s.name == "computations")
    (finding,) = [f for f in step.findings if f.code == "computation-unknown-column"]
    assert finding.severity == "warning"
    assert "FROZEN" in finding.message and "unverify" in finding.message
    # The same drift on an UNVERIFIED doc stays an error.
    draft_root = tmp_path / "draft"
    draft_root.mkdir()
    _write_bundle(draft_root, text)
    report = lint_bundle(draft_root)
    step = next(s for s in report.steps if s.name == "computations")
    (finding,) = [f for f in step.findings if f.code == "computation-unknown-column"]
    assert finding.severity == "error"


# -- code-review regressions (2026-08-13 xhigh pass) ---------------------------


def test_negative_values_parenthesize_never_forming_comments():
    # A bare -5 spliced after a minus forms the line-comment token `--` and
    # silently truncates the statement on every engine we run.
    assert render_literal("integer", -5, "athena") == "(-5)"
    assert render_literal("number", "-1.5", "athena") == "(-1.5)"
    assert render_literal("integer", 5, "athena") == "5"
    out = substitute("SELECT a -@adj AS x FROM t", {"adj": render_literal("integer", -5, "athena")})
    assert "--" not in out and "(-5)" in out
    out = substitute("SELECT @a-@b AS d FROM t", {
        "a": render_literal("integer", 10, "athena"),
        "b": render_literal("integer", -5, "athena"),
    })
    assert "--" not in out


def test_fence_with_trailing_ddl_is_refused():
    # _classify_fence COLLECTS only SELECT/WITH parts — the DROP must still
    # fail the one-statement gate (it rides the hashed, human-reviewed fence).
    comp, errors = parse_computation_text(
        REL,
        comp_doc(sql="SELECT 1 FROM t WHERE r=@region AND d=@since;\nDROP TABLE t"),
    )
    assert comp is None
    assert any("exactly ONE" in e for e in errors), errors
    # A single trailing semicolon stays legal.
    comp, errors = parse_computation_text(
        REL, comp_doc(sql="SELECT 1 FROM t WHERE r=@region AND d=@since;")
    )
    assert errors == []


def test_string_params_reject_yaml_coerced_scalars():
    # YAML 1.1 parses unquoted yes/no as booleans and 007 as an int — a silent
    # str() would hash values ('True', '7') the human never saw in the doc.
    normalized, err = normalize_value("string", True)
    assert normalized is None and "quote" in err
    normalized, err = normalize_value("string", 7)
    assert normalized is None and "quote" in err
    _, errors = parse_parameters(
        [{"name": "p", "type": "string", "example": "yes", "enum": [True, False]}]
    )
    assert any("quote" in e for e in errors)


def test_value_observed_canonicalizes_types():
    from okf_core.computations import value_observed

    assert value_observed("boolean", True, ["true", "false"])
    assert value_observed("number", "10.0", ["10", "20"])
    assert value_observed("integer", 10, ["10"])
    assert not value_observed("number", "30", ["10", "20"])
    assert value_observed("string", "EMEA", ["EMEA"])
    # And the advisory layer stops false-warning on booleans.
    params, errors = parse_parameters(
        [{"name": "active", "type": "boolean", "example": True, "column": "users.is_active"}]
    )
    assert errors == []
    flat = {"users.is_active": {"values": ["true", "false"], "exhaustive": True}}
    assert domain_warnings(params, {"active": True}, flat) == []


def test_lint_numeric_enum_compares_numerically(tmp_path):
    # `enum: [2023.0, 2024.0]` against an exhaustively-profiled ['2023','2024']
    # must NOT false-error a correct doc (the fix-to-zero gate would wedge).
    text = comp_doc(
        params=(
            "parameters:\n"
            "  - {name: region, type: number, required: true, example: 2023.0,\n"
            "     enum: [2023.0, 2024.0], column: customers.region}\n"
            '  - {name: since, type: date, required: true, example: "2026-01-01"}\n'
        )
    )
    domains = {
        "version": 1,
        "tables": {
            "customers": {
                "profiled_at": "t",
                "columns": {"region": {"values": ["2023", "2024"], "distinct": 2, "exhaustive": True}},
            }
        },
    }
    _write_bundle(tmp_path, text, domains=domains)
    report = lint_bundle(tmp_path)
    step = next(s for s in report.steps if s.name == "computations")
    assert not [f for f in step.findings if f.code == "computation-enum-not-observed"]

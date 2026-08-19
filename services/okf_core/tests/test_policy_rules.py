"""policy_rules: the dimension catalog, evaluator tri-state, hash, self-test.

The evaluator corpus is F1-derived — the live dataset whose policies this
feature was sized against — with the intended verdict known per case. The
contract under test everywhere: VIOLATION only when the full resolution chain
is proven; anything unprovable is UNKNOWN (never a wrong refusal, never a
false pass through a derived source).
"""

from __future__ import annotations

import pytest

from okf_core import policy_doc, policy_rules

sqlglot = pytest.importorskip("sqlglot")


F1 = {
    "f1": {
        "driverstandings": ["driverstandingsid", "raceid", "driverid", "points",
                            "position", "wins"],
        "constructorstandings": ["constructorstandingsid", "raceid",
                                 "constructorid", "points", "wins"],
        "constructorresults": ["constructorresultsid", "raceid",
                               "constructorid", "points", "status"],
        "results": ["resultid", "raceid", "driverid", "constructorid",
                    "positiontext", "positionorder", "points", "time",
                    "milliseconds", "rank"],
        "races": ["raceid", "year", "round", "name", "date"],
        "laptimes": ["raceid", "driverid", "lap", "time", "milliseconds"],
        "drivers": ["driverid", "surname", "dob"],
    }
}


def _rule(**kw):
    base = {
        "examples": {
            "violation": kw.pop("ex_violation", "SELECT 1"),
            "pass": kw.pop("ex_pass", "SELECT 2"),
        }
    }
    base.update(kw)
    return policy_rules.parse_rules([base])


def _evaluate(sql, rules, *, schema=F1["f1"], default_db="f1"):
    return policy_rules.evaluate_policies(
        sql,
        [("P", rules)],
        {"f1": schema},
        default_database=default_db,
    )["P"]


# -- parse / shape -------------------------------------------------------------------


def test_parse_rules_fills_defaults_and_lowercases():
    rules = _rule(
        dimension="Forbidden_Aggregation",
        targets=["DriverStandings.Points"],
    )
    assert rules[0]["dimension"] == "forbidden_aggregation"
    assert rules[0]["targets"] == ["driverstandings.points"]
    assert rules[0]["aggs"] == ["sum", "avg"]


def test_parse_rules_rejects_unknown_dimension():
    with pytest.raises(policy_rules.RulesError, match="catalog"):
        _rule(dimension="forbidden_vibes", targets=["a.b"])


def test_parse_rules_rejects_bad_target_and_missing_examples():
    with pytest.raises(policy_rules.RulesError, match="table.column"):
        _rule(dimension="forbidden_aggregation", targets=["no_dot_here"])
    with pytest.raises(policy_rules.RulesError, match="examples"):
        policy_rules.parse_rules(
            [{"dimension": "forbidden_aggregation", "targets": ["a.b"]}]
        )


def test_parse_rules_required_predicate_needs_value_for_eq():
    with pytest.raises(policy_rules.RulesError, match="require.value"):
        _rule(
            dimension="required_predicate",
            table="results",
            require={"column": "status", "op": "eq"},
        )


def test_check_rules_schema_catches_dead_bindings():
    rules = _rule(dimension="forbidden_aggregation", targets=["driverstandings.points"])
    assert policy_rules.check_rules_schema(rules, F1) is None
    bad_col = _rule(dimension="forbidden_aggregation", targets=["driverstandings.nope"])
    assert "unknown column" in policy_rules.check_rules_schema(bad_col, F1)
    bad_table = _rule(dimension="required_distinct", table="ghosts",
                      group_by="a", count_distinct="b")
    assert "unknown table" in policy_rules.check_rules_schema(bad_table, F1)


# -- evaluator: forbidden_aggregation (P020 / P011-P012 family) -----------------------


CUMULATIVE = dict(
    dimension="forbidden_aggregation",
    targets=["driverstandings.points", "driverstandings.wins",
             "constructorstandings.points", "constructorstandings.wins"],
)


def test_sum_of_cumulative_snapshot_violates_direct_and_aliased():
    rules = _rule(**CUMULATIVE)
    assert _evaluate("SELECT SUM(points) FROM driverstandings", rules)[
        "verdict"
    ] == "violation"
    joined = (
        "SELECT d.surname, SUM(ds.points) FROM driverstandings ds "
        "JOIN drivers d ON d.driverid = ds.driverid GROUP BY 1"
    )
    assert _evaluate(joined, rules)["verdict"] == "violation"


def test_non_cumulative_twin_and_plain_read_pass():
    rules = _rule(**CUMULATIVE)
    assert _evaluate("SELECT SUM(points) FROM results", rules)["verdict"] == "pass"
    assert _evaluate(
        "SELECT points FROM driverstandings WHERE raceid = 900 AND driverid = 1",
        rules,
    )["verdict"] == "pass"


def test_cte_flow_is_unknown_never_false_pass():
    rules = _rule(**CUMULATIVE)
    sql = (
        "WITH s AS (SELECT driverid, points FROM driverstandings) "
        "SELECT driverid, SUM(points) FROM s GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_max_points_variant_for_standings_antipattern():
    rules = _rule(dimension="forbidden_aggregation",
                  targets=["driverstandings.points"], aggs=["max"])
    assert _evaluate(
        "SELECT driverid, MAX(points) FROM driverstandings GROUP BY 1", rules
    )["verdict"] == "violation"
    # SUM is not in this rule's aggs — decisively clean for THIS rule.
    assert _evaluate("SELECT SUM(points) FROM driverstandings", rules)[
        "verdict"
    ] == "pass"


# -- evaluator: forbidden_usage (P022/P080/P023 family) -------------------------------


TEXT_TIME = dict(dimension="forbidden_usage",
                 targets=["results.time", "laptimes.time"])


def test_text_time_in_order_agg_compare_violates():
    rules = _rule(**TEXT_TIME)
    assert _evaluate(
        "SELECT driverid, time FROM results WHERE raceid = 900 ORDER BY time",
        rules,
    )["verdict"] == "violation"
    assert _evaluate("SELECT MIN(time) FROM laptimes WHERE raceid = 1", rules)[
        "verdict"
    ] == "violation"
    assert _evaluate(
        "SELECT * FROM laptimes WHERE time < '1:30.000'", rules
    )["verdict"] == "violation"


def test_milliseconds_route_and_cast_lift_pass():
    rules = _rule(**TEXT_TIME)
    assert _evaluate("SELECT MIN(milliseconds) FROM laptimes", rules)[
        "verdict"
    ] == "pass"
    # An explicit CAST lifts the column out of its stored text form.
    assert _evaluate(
        "SELECT MIN(CAST(time AS time)) FROM laptimes", rules
    )["verdict"] == "pass"


def test_equality_compare_is_not_flagged():
    rules = _rule(**TEXT_TIME)
    assert _evaluate(
        "SELECT * FROM results WHERE time = '1:30:00.000'", rules
    )["verdict"] == "pass"


# -- evaluator: forbidden_function (P077) --------------------------------------------


def test_round_and_cast_to_int_on_points_violates():
    rules = _rule(dimension="forbidden_function",
                  targets=["results.points"], functions=["round", "floor"],
                  cast_types=["int", "bigint"])
    assert _evaluate("SELECT ROUND(points) FROM results", rules)[
        "verdict"
    ] == "violation"
    assert _evaluate("SELECT CAST(points AS INTEGER) FROM results", rules)[
        "verdict"
    ] == "violation"
    assert _evaluate("SELECT SUM(points) FROM results", rules)["verdict"] == "pass"
    # Casting to DOUBLE is not in cast_types — precision-preserving, fine.
    assert _evaluate("SELECT CAST(points AS DOUBLE) FROM results", rules)[
        "verdict"
    ] == "pass"


# -- evaluator: required_predicate (P085 / P080-milliseconds) -------------------------


STATUS_FILTER = dict(
    dimension="required_predicate",
    table="constructorresults",
    when="aggregate",
    when_columns=["points"],
    require={"column": "status", "op": "is_null"},
)


def test_sum_without_status_filter_violates_with_filter_passes():
    rules = _rule(**STATUS_FILTER)
    assert _evaluate(
        "SELECT constructorid, SUM(points) FROM constructorresults GROUP BY 1",
        rules,
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT constructorid, SUM(points) FROM constructorresults "
        "WHERE status IS NULL GROUP BY 1",
        rules,
    )["verdict"] == "pass"


def test_or_at_top_of_where_is_unknown():
    rules = _rule(**STATUS_FILTER)
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults "
        "WHERE status IS NULL OR raceid = 1",
        rules,
    )["verdict"] == "unknown"


def test_filter_in_inner_join_on_counts_as_conjunct():
    rules = _rule(**STATUS_FILTER)
    sql = (
        "SELECT SUM(cr.points) FROM constructorresults cr "
        "JOIN races r ON r.raceid = cr.raceid AND cr.status IS NULL"
    )
    assert _evaluate(sql, rules)["verdict"] == "pass"


def test_left_join_on_does_not_satisfy_the_filter():
    rules = _rule(**STATUS_FILTER)
    sql = (
        "SELECT SUM(cr.points) FROM constructorresults cr "
        "LEFT JOIN races r ON r.raceid = cr.raceid AND cr.status IS NULL"
    )
    assert _evaluate(sql, rules)["verdict"] == "violation"


def test_or_group_by_satisfies_currency_style_rule():
    rules = _rule(
        dimension="required_predicate",
        table="results",
        when="aggregate",
        when_columns=["points"],
        require={"column": "constructorid", "op": "eq", "value": "1"},
        or_group_by="constructorid",
    )
    assert _evaluate(
        "SELECT constructorid, SUM(points) FROM results GROUP BY constructorid",
        rules,
    )["verdict"] == "pass"
    assert _evaluate("SELECT SUM(points) FROM results", rules)[
        "verdict"
    ] == "violation"


def test_sentinel_exclusion_via_when_use():
    rules = _rule(
        dimension="required_predicate",
        table="results",
        when="use",
        when_columns=["rank"],
        require={"column": "rank", "op": "neq", "value": "0"},
    )
    assert _evaluate(
        "SELECT driverid FROM results WHERE rank = 1", rules
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT driverid FROM results WHERE rank <> 0 AND rank = 1", rules
    )["verdict"] == "pass"
    assert _evaluate("SELECT driverid, points FROM results", rules)[
        "verdict"
    ] == "pass"


# -- evaluator: required_guard (P088) -------------------------------------------------


GUARD = dict(dimension="required_guard", targets=["results.positiontext"])


def test_unguarded_cast_violates_guarded_and_trycast_pass():
    rules = _rule(**GUARD)
    assert _evaluate(
        "SELECT CAST(positiontext AS INTEGER) FROM results", rules
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT CAST(positiontext AS INTEGER) FROM results "
        "WHERE regexp_like(positiontext, '^[0-9]+$')",
        rules,
    )["verdict"] == "pass"
    assert _evaluate(
        "SELECT TRY_CAST(positiontext AS INTEGER) FROM results", rules
    )["verdict"] == "pass"


def test_guard_inside_case_passes():
    rules = _rule(**GUARD)
    sql = (
        "SELECT CASE WHEN regexp_like(positiontext, '^[0-9]+$') "
        "THEN CAST(positiontext AS INTEGER) END FROM results"
    )
    assert _evaluate(sql, rules)["verdict"] == "pass"


# -- evaluator: forbidden_sequencing_key (P089) ---------------------------------------


SEQ = dict(
    dimension="forbidden_sequencing_key",
    targets=["driverstandings.raceid", "results.raceid"],
)


def test_window_over_raceid_violates_year_round_passes():
    rules = _rule(**SEQ)
    assert _evaluate(
        "SELECT driverid, points - LAG(points) OVER "
        "(PARTITION BY driverid ORDER BY raceid) FROM driverstandings",
        rules,
    )["verdict"] == "violation"
    ok = (
        "SELECT r2.driverid, LAG(r2.points) OVER "
        "(PARTITION BY r2.driverid ORDER BY r.year, r.round) "
        "FROM results r2 JOIN races r ON r.raceid = r2.raceid"
    )
    assert _evaluate(ok, rules)["verdict"] == "pass"


def test_order_by_raceid_limit_violates_plain_order_passes():
    rules = _rule(**SEQ)
    assert _evaluate(
        "SELECT * FROM results ORDER BY raceid DESC LIMIT 1", rules
    )["verdict"] == "violation"
    # Presentational ordering without LIMIT is not a "latest" probe.
    assert _evaluate(
        "SELECT raceid, points FROM results WHERE driverid = 1 ORDER BY raceid",
        rules,
    )["verdict"] == "pass"


# -- evaluator: required_distinct (P058) ----------------------------------------------


WINS = dict(dimension="required_distinct", table="results",
            group_by="constructorid", count_distinct="raceid")


def test_count_star_by_constructor_violates_count_distinct_passes():
    rules = _rule(**WINS)
    assert _evaluate(
        "SELECT constructorid, COUNT(*) FROM results "
        "WHERE positionorder = 1 GROUP BY constructorid",
        rules,
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT constructorid, COUNT(DISTINCT raceid) FROM results "
        "WHERE positionorder = 1 GROUP BY constructorid",
        rules,
    )["verdict"] == "pass"
    # Grouped by something else entirely — the rule does not apply.
    assert _evaluate(
        "SELECT driverid, COUNT(*) FROM results GROUP BY driverid", rules
    )["verdict"] == "pass"


# -- cross-database namespacing -------------------------------------------------------


def test_rules_never_fire_on_another_databases_same_named_table():
    other = {"other": {"driverstandings": ["raceid", "points"]}}
    merged = {**F1, **other}
    rules = _rule(**CUMULATIVE)
    result = policy_rules.evaluate_policies(
        'SELECT SUM(points) FROM "other"."driverstandings"',
        [("P", rules)],
        merged,
        databases={"f1"},
        default_database=None,
    )["P"]
    assert result["verdict"] == "pass"
    # Same SQL against the f1 table IS a violation.
    result = policy_rules.evaluate_policies(
        'SELECT SUM(points) FROM "f1"."driverstandings"',
        [("P", rules)],
        merged,
        databases={"f1"},
        default_database=None,
    )["P"]
    assert result["verdict"] == "violation"


def test_ambiguous_bare_column_is_unknown():
    rules = _rule(**TEXT_TIME)
    # `time` exists in both results and laptimes — qualify refuses to guess.
    sql = "SELECT time FROM results JOIN laptimes ON results.raceid = laptimes.raceid"
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_unparseable_sql_is_unknown_for_every_policy():
    rules = _rule(**CUMULATIVE)
    out = policy_rules.evaluate_policies(
        "SELECT FROM WHERE", [("A", rules), ("B", rules)], F1
    )
    assert out["A"]["verdict"] == "unknown"
    assert out["B"]["verdict"] == "unknown"


# -- self-test -------------------------------------------------------------------------


def test_self_test_passes_for_honest_examples():
    rules = _rule(
        **CUMULATIVE,
        ex_violation="SELECT SUM(points) FROM driverstandings",
        ex_pass="SELECT points FROM driverstandings WHERE raceid = 900",
    )
    assert policy_rules.self_test(rules, F1) is None


def test_self_test_fails_when_violation_example_does_not_fire():
    rules = _rule(
        **CUMULATIVE,
        ex_violation="SELECT points FROM driverstandings",  # doesn't violate
        ex_pass="SELECT points FROM driverstandings",
    )
    err = policy_rules.self_test(rules, F1)
    assert err is not None and "self-test failed" in err


# -- policy_doc integration ------------------------------------------------------------


RULED_DOC = """\
policies:
  - id: P020
    type: computational
    condition: aggregating standings points across rounds
    action: never SUM cumulative snapshot columns
    source: references/usage_guardrails.md
    rules:
      - dimension: forbidden_aggregation
        targets: [driverstandings.points]
        examples:
          violation: SELECT SUM(points) FROM driverstandings
          pass: SELECT points FROM driverstandings WHERE raceid = 900
"""


def test_policy_doc_parses_and_validates_rules():
    entries = policy_doc.parse_policies(RULED_DOC)
    assert entries[0]["rules"][0]["dimension"] == "forbidden_aggregation"
    assert policy_doc.validate_policy_doc(
        RULED_DOC,
        known_sources={"references/usage_guardrails.md"},
        rules_schema=F1,
    ) is None


def test_policy_doc_refuses_rules_without_schema():
    err = policy_doc.validate_policy_doc(
        RULED_DOC, known_sources={"references/usage_guardrails.md"}
    )
    assert err is not None and "no rules schema" in err


def test_policy_doc_refuses_rules_on_behavioural_entries():
    doc = RULED_DOC.replace("type: computational", "type: behavioural")
    with pytest.raises(policy_doc.PolicyDocError, match="behavioural"):
        policy_doc.parse_policies(doc)


def test_policy_doc_surfaces_failed_self_test():
    doc = RULED_DOC.replace(
        "violation: SELECT SUM(points) FROM driverstandings",
        "violation: SELECT points FROM driverstandings",
    )
    err = policy_doc.validate_policy_doc(
        doc,
        known_sources={"references/usage_guardrails.md"},
        rules_schema=F1,
    )
    assert err is not None and "self-test failed" in err


# -- structural robustness (shapes the F1 corpus doesn't cover) -----------------------


def test_union_flags_a_violating_branch():
    rules = _rule(**CUMULATIVE)
    sql = (
        "SELECT SUM(points) FROM results "
        "UNION ALL SELECT SUM(points) FROM driverstandings"
    )
    assert _evaluate(sql, rules)["verdict"] == "violation"


def test_violation_inside_a_where_subquery_is_found():
    rules = _rule(**CUMULATIVE)
    sql = (
        "SELECT 1 FROM races WHERE raceid IN ("
        "SELECT raceid FROM driverstandings GROUP BY raceid "
        "HAVING SUM(points) > 5)"
    )
    assert _evaluate(sql, rules)["verdict"] == "violation"


def test_required_predicate_holds_through_a_subquery_wrapper():
    rules = _rule(**STATUS_FILTER)
    ok = (
        "SELECT * FROM (SELECT constructorid, SUM(points) p "
        "FROM constructorresults WHERE status IS NULL GROUP BY 1) t"
    )
    assert _evaluate(ok, rules)["verdict"] == "pass"
    missing = (
        "SELECT * FROM (SELECT constructorid, SUM(points) p "
        "FROM constructorresults GROUP BY 1) t"
    )
    assert _evaluate(missing, rules)["verdict"] == "violation"


def test_negated_required_filter_is_a_violation():
    rules = _rule(**STATUS_FILTER)
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults WHERE NOT (status IS NULL)",
        rules,
    )["verdict"] == "violation"


def test_opaque_commands_read_unknown_not_pass():
    rules = _rule(**CUMULATIVE)
    # EXPLAIN/SHOW parse as an opaque Command — nothing can be proven, and a
    # silent `pass` would read as "checked and clean".
    for sql in (
        "EXPLAIN SELECT SUM(points) FROM driverstandings",
        "SHOW TABLES",
    ):
        assert _evaluate(sql, rules)["verdict"] == "unknown"


# -- regressions from the adversarial review (all were false refusals or
# -- decisive passes that escaped BOTH tiers) ----------------------------------------


def test_parenthesized_filter_satisfies_a_required_predicate():
    rules = _rule(**STATUS_FILTER)
    # Agents parenthesize constantly; a Paren wrapper hiding the filter was a
    # false refusal on a fully compliant query.
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults WHERE (status IS NULL)",
        rules,
    )["verdict"] == "pass"
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults "
        "WHERE (raceid > 0 AND status IS NULL) AND constructorid = 1",
        rules,
    )["verdict"] == "pass"


def test_parenthesized_or_is_unknown_not_a_violation():
    rules = _rule(**STATUS_FILTER)
    # The Paren defeated the OR detection, turning an UNDECIDABLE query into a
    # refusal with a false "filter absent" proof.
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults "
        "WHERE (status IS NULL OR raceid = 1)",
        rules,
    )["verdict"] == "unknown"


def test_partial_conjuncts_still_prove_a_present_filter():
    rules = _rule(**STATUS_FILTER)
    # A disjunction elsewhere must not discard a conjunct that genuinely holds.
    assert _evaluate(
        "SELECT SUM(points) FROM constructorresults "
        "WHERE status IS NULL AND (raceid = 1 OR raceid = 2)",
        rules,
    )["verdict"] == "pass"


def test_ordinary_limit_is_not_a_sequencing_probe():
    rules = _rule(**SEQ)
    # run_sql's own tool description tells the model to LIMIT every query, so
    # any-LIMIT + ORDER BY would refuse ordinary paged listings.
    assert _evaluate(
        "SELECT raceid, points FROM results WHERE driverid = 1 "
        "ORDER BY raceid LIMIT 200",
        rules,
    )["verdict"] == "pass"
    # LIMIT 1 remains the "latest row" probe.
    assert _evaluate(
        "SELECT * FROM results ORDER BY raceid DESC LIMIT 1", rules
    )["verdict"] == "violation"


def test_count_distinct_of_any_column_is_never_a_fanout_violation():
    rules = _rule(**WINS)
    # COUNT(DISTINCT <anything>) is fan-out-safe by construction — and
    # counting distinct drivers per constructor is a different, legitimate
    # question, not this rule's error.
    assert _evaluate(
        "SELECT constructorid, COUNT(DISTINCT driverid) FROM results "
        "WHERE positionorder = 1 GROUP BY constructorid",
        rules,
    )["verdict"] == "pass"


def test_required_distinct_trigger_scopes_the_rule_to_its_reading():
    rules = _rule(
        **WINS,
        when_filtered={"column": "positionorder", "op": "eq", "value": "1"},
    )
    # With the winner predicate: the fan-out error applies.
    assert _evaluate(
        "SELECT constructorid, COUNT(*) FROM results "
        "WHERE positionorder = 1 GROUP BY constructorid",
        rules,
    )["verdict"] == "violation"
    # Without it, a plain row count per constructor is a different question.
    assert _evaluate(
        "SELECT constructorid, COUNT(*) FROM results GROUP BY constructorid",
        rules,
    )["verdict"] == "pass"


def test_cast_lift_is_proximity_based_not_any_ancestor():
    rules = _rule(**TEXT_TIME)
    # The raw text column is still AGGREGATED here; an any-ancestor cast check
    # read this as a decisive pass, which also excluded the policy from the
    # judge shard — escaping both tiers.
    assert _evaluate(
        "SELECT CAST(MIN(time) AS varchar) FROM laptimes", rules
    )["verdict"] == "violation"
    # Casting the column itself remains the sanctioned route.
    assert _evaluate(
        "SELECT MIN(CAST(time AS time)) FROM laptimes", rules
    )["verdict"] == "pass"


def test_set_operation_order_by_is_unknown_not_pass():
    rules = _rule(**TEXT_TIME)
    # A union's ORDER BY hangs off the SetOperation scope and can't be
    # attributed to a branch — defer instead of passing decisively.
    sql = (
        "SELECT time FROM results WHERE raceid = 1 "
        "UNION ALL SELECT time FROM results WHERE raceid = 2 "
        "ORDER BY time"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


# -- regression: renamed derived-source exports must not read as a decisive pass ------
# A CTE/subquery projection that RENAMES a target (`points AS pts`, then
# SUM(pts) outside) is invisible to a name-only overlap check: the old code
# returned `pass`, which ALSO excluded the policy from the judge shard —
# escaping both tiers. Export-alias tracking demotes these to `unknown`.


def test_cte_renamed_target_is_unknown_not_pass():
    rules = _rule(**CUMULATIVE)
    sql = (
        "WITH x AS (SELECT driverid, points AS pts FROM driverstandings) "
        "SELECT driverid, SUM(pts) FROM x GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_subquery_renamed_target_is_unknown_not_pass():
    rules = _rule(**CUMULATIVE)
    sql = (
        "SELECT driverid, SUM(pts) FROM "
        "(SELECT driverid, points AS pts FROM driverstandings) t GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_multi_hop_rename_chain_is_unknown():
    rules = _rule(**CUMULATIVE)
    # The intermediate reference (pts) is itself unresolved and collides with
    # the first hop's export alias — the chain needs no lineage walk.
    sql = (
        "WITH a AS (SELECT driverid, points AS pts FROM driverstandings), "
        "b AS (SELECT driverid, pts AS p2 FROM a) "
        "SELECT driverid, SUM(p2) FROM b GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_expression_wrapped_rename_is_unknown():
    rules = _rule(**CUMULATIVE)
    # Every column FEEDING the aliased expression counts, not just bare
    # Alias(Column) — `points + 0 AS pts` still carries the target.
    sql = (
        "WITH x AS (SELECT driverid, points + 0 AS pts FROM driverstandings) "
        "SELECT driverid, SUM(pts) FROM x GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


def test_rename_of_non_target_still_passes():
    rules = _rule(**CUMULATIVE)
    # `position` is not a target of this rule: its renamed export must not
    # contaminate the verdict — provably clean stays a decisive pass.
    sql = (
        "WITH x AS (SELECT driverid, position AS pos FROM driverstandings) "
        "SELECT driverid, SUM(pos) FROM x GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "pass"


def test_renamed_export_never_referenced_still_passes():
    rules = _rule(**CUMULATIVE)
    # The rename exists but the outer query never touches it — nothing to hide.
    sql = (
        "WITH x AS (SELECT driverid, points AS pts, raceid "
        "FROM driverstandings) "
        "SELECT driverid, COUNT(raceid) FROM x GROUP BY 1"
    )
    assert _evaluate(sql, rules)["verdict"] == "pass"


def test_forbidden_usage_renamed_through_cte_is_unknown():
    rules = _rule(**TEXT_TIME)
    sql = (
        "WITH x AS (SELECT raceid, time AS t FROM results) "
        "SELECT raceid FROM x ORDER BY t"
    )
    assert _evaluate(sql, rules)["verdict"] == "unknown"


# -- regressions from the xhigh review (false decisive passes / false flags) ---------


GUARD = dict(dimension="required_guard", targets=["results.positiontext"])


def test_guard_in_or_branch_or_under_not_is_unknown_not_pass():
    rules = _rule(**GUARD)
    # A guard inside an OR branch does not hold for every row reaching the
    # cast; NOT(guard) anti-guards. Neither may read as a decisive pass.
    assert _evaluate(
        "SELECT CAST(positiontext AS INTEGER) FROM results "
        "WHERE regexp_like(positiontext, '^[0-9]+$') OR raceid = 1",
        rules,
    )["verdict"] == "unknown"
    assert _evaluate(
        "SELECT CAST(positiontext AS INTEGER) FROM results "
        "WHERE NOT regexp_like(positiontext, '^[0-9]+$')",
        rules,
    )["verdict"] == "unknown"


def test_guard_in_inner_join_on_counts_as_proof():
    # The same filter material required_predicate trusts — an INNER JOIN ON
    # conjunct genuinely restricts the rows reaching the cast.
    rules = _rule(**GUARD)
    sql = (
        "SELECT CAST(r.positiontext AS INTEGER) FROM results r "
        "JOIN races x ON x.raceid = r.raceid "
        "AND regexp_like(r.positiontext, '^[0-9]+$')"
    )
    assert _evaluate(sql, rules)["verdict"] == "pass"


def test_cast_as_real_is_in_the_default_guard_set():
    rules = _rule(**GUARD)
    assert _evaluate(
        "SELECT CAST(positiontext AS REAL) FROM results", rules
    )["verdict"] == "violation"


def test_intermediate_varchar_cast_does_not_shield_the_numeric_one():
    rules = _rule(**GUARD)
    assert _evaluate(
        "SELECT CAST(CAST(positiontext AS VARCHAR) AS INTEGER) FROM results",
        rules,
    )["verdict"] == "violation"


def test_inner_trycast_is_the_guard_for_required_guard():
    rules = _rule(**GUARD)
    assert _evaluate(
        "SELECT CAST(TRY_CAST(positiontext AS DOUBLE) AS INTEGER) FROM results",
        rules,
    )["verdict"] == "pass"


def test_inner_trycast_does_not_shield_a_forbidden_cast():
    rules = _rule(dimension="forbidden_function",
                  targets=["results.points"], cast_types=["int"])
    # The precision-destroying CAST-to-int still applies; only its INPUT was
    # sanitized by the TRY_CAST.
    assert _evaluate(
        "SELECT CAST(TRY_CAST(points AS DOUBLE) AS INTEGER) FROM results",
        rules,
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT TRY_CAST(points AS INTEGER) FROM results", rules
    )["verdict"] == "pass"


def test_count_star_triggers_a_use_rule_without_column_occurrences():
    rules = _rule(dimension="required_predicate", table="constructorresults",
                  when="use", require={"column": "status", "op": "is_null"})
    assert _evaluate(
        "SELECT COUNT(*) FROM constructorresults", rules
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT COUNT(*) FROM constructorresults WHERE status IS NULL", rules
    )["verdict"] == "pass"


def test_when_columns_keeps_the_narrow_trigger_for_count_star():
    # An authored when_columns trigger is deliberately narrower: COUNT(*)
    # reads none of the trigger columns, so the rule does not apply.
    rules = _rule(dimension="required_predicate", table="constructorresults",
                  when="use", when_columns=["points"],
                  require={"column": "status", "op": "is_null"})
    assert _evaluate(
        "SELECT COUNT(*) FROM constructorresults", rules
    )["verdict"] == "pass"


def test_literal_matching_is_engine_faithful():
    rules = _rule(dimension="required_predicate", table="constructorresults",
                  when="use",
                  require={"column": "status", "op": "eq", "value": "D"})
    # Athena string equality is case-sensitive: 'd' does not satisfy 'D'.
    assert _evaluate(
        "SELECT points FROM constructorresults WHERE status = 'd'", rules
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT points FROM constructorresults WHERE status = 'D'", rules
    )["verdict"] == "pass"
    # Numbers compare numerically, including negative literals.
    neq = _rule(dimension="required_predicate", table="results", when="use",
                require={"column": "rank", "op": "neq", "value": "-1"})
    assert _evaluate(
        "SELECT rank FROM results WHERE rank <> -1", neq
    )["verdict"] == "pass"
    zero = _rule(dimension="required_predicate", table="results", when="use",
                 require={"column": "rank", "op": "neq", "value": "0"})
    assert _evaluate(
        "SELECT rank FROM results WHERE rank <> 0.0", zero
    )["verdict"] == "pass"


def test_not_wrapped_predicates_match_their_equivalents():
    neq = _rule(dimension="required_predicate", table="results", when="use",
                require={"column": "rank", "op": "neq", "value": "0"})
    assert _evaluate(
        "SELECT rank FROM results WHERE NOT (rank = 0)", neq
    )["verdict"] == "pass"
    nn = _rule(dimension="required_predicate", table="results", when="use",
               require={"column": "time", "op": "is_not_null"})
    assert _evaluate(
        "SELECT time FROM results WHERE NOT (time IS NULL)", nn
    )["verdict"] == "pass"
    assert _evaluate(
        "SELECT time FROM results WHERE time IS NOT NULL", nn
    )["verdict"] == "pass"


def test_having_on_a_grouped_key_satisfies_a_required_predicate():
    rules = _rule(dimension="required_predicate", table="results",
                  when="aggregate", when_columns=["points"],
                  require={"column": "constructorid", "op": "eq", "value": "1"})
    assert _evaluate(
        "SELECT constructorid, SUM(points) FROM results "
        "GROUP BY constructorid HAVING constructorid = 1",
        rules,
    )["verdict"] == "pass"
    assert _evaluate(
        "SELECT constructorid, SUM(points) FROM results GROUP BY constructorid",
        rules,
    )["verdict"] == "violation"


def test_string_concat_is_transparent_to_usage_contexts():
    rules = _rule(**TEXT_TIME)
    # `||` transforms text to text — the enclosing aggregate/order context
    # still applies to the stored form.
    assert _evaluate(
        "SELECT MIN(time || 'x') FROM laptimes", rules
    )["verdict"] == "violation"
    assert _evaluate(
        "SELECT time FROM laptimes WHERE raceid = 1 ORDER BY time || ''", rules
    )["verdict"] == "violation"
    # Equality remains sanctioned (not a `compare` context).
    assert _evaluate(
        "SELECT time FROM laptimes WHERE time = '1:23.456'", rules
    )["verdict"] == "pass"


def test_multiword_function_names_and_cast_aliases_bind():
    rules = policy_rules.parse_rules([{
        "dimension": "forbidden_function",
        "targets": ["results.time"],
        "functions": ["date_diff"],
        "examples": {
            "violation": (
                "SELECT date_diff('second', TRY_CAST(time AS timestamp), "
                "now()) FROM results"
            ),
            "pass": "SELECT time FROM results",
        },
    }])
    assert policy_rules.self_test(rules, F1) is None
    aliased = policy_rules.parse_rules([{
        "dimension": "forbidden_function",
        "targets": ["results.points"],
        "cast_types": ["integer"],
        "examples": {
            "violation": "SELECT CAST(points AS INTEGER) FROM results",
            "pass": "SELECT points FROM results",
        },
    }])
    assert aliased[0]["cast_types"] == ["int"]
    assert policy_rules.self_test(aliased, F1) is None


def test_runtime_reader_degrades_bad_rules_to_prose():
    doc = """\
policies:
  - id: P001
    type: computational
    condition: c
    action: a
    source: references/usage_guardrails.md
    rules:
      - dimension: forbidden_teleportation
        targets: [results.points]
        examples: {violation: SELECT 1, pass: SELECT 2}
  - id: P002
    type: behavioural
    condition: c
    action: a
    source: references/usage_guardrails.md
"""
    # The author gate stays strict…
    with pytest.raises(policy_doc.PolicyDocError, match="catalog"):
        policy_doc.parse_policies(doc)
    # …but a runtime with an older DIMENSIONS catalog must not silence the
    # whole document: the unparseable rules degrade THAT policy to prose.
    entries = policy_doc.parse_policies(doc, drop_invalid_rules=True)
    assert [e["id"] for e in entries] == ["P001", "P002"]
    assert "rules" not in entries[0]

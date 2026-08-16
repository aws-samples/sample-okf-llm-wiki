#!/usr/bin/env python3
"""Convert BIRD mini_dev questions into Benchmark Studio question-set CSVs.

One CSV per database (named by its GLUE database, so the file matches the
dataset you pick in the Studio), format per okf_core.benchmark_questions:

  question,gold_sql            (+ question_id/difficulty for traceability —
                                unknown headers are ignored by the parser)

The gold SQL ships in mini_dev as SQLite; the Studio's Accuracy check executes
gold through Athena (Trino) against the Glue copies that load_bird_to_glue.py
produced, so each gold is:

  1. transpiled sqlite -> trino with sqlglot,
  2. identifier-lowercased (the loader lowercases every table/column name),
  3. de-quirked: a double-quoted token that names no real column was a STRING
     LITERAL under SQLite's lenient quoting — converted to one (checked against
     the live Glue schema, fetched per database),
  4. optionally --validate: executed on Athena so a broken gold never poisons
     a benchmark run (failures land in studio_questions/validation_failures.json).

The question text carries the official "evidence" hint (leaderboard-faithful:
mini_dev inference runs with External Knowledge ON — see README.md).

Usage:
  python3 make_studio_questions.py            # write studio_questions/*.csv
  python3 make_studio_questions.py --validate # + run every gold on Athena
  python3 make_studio_questions.py --db formula_1 --validate   # one database
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import sqlglot
from sqlglot import exp

from behavior_questions import BEHAVIOR
from config import DATABASES, REGION

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_JSON = os.path.join(HERE, "data", "mini_dev_sqlite.json")
OUT_DIR = os.path.join(HERE, "studio_questions")

ATHENA_WORKGROUP = "primary"
VALIDATE_THREADS = 8


# ---- Behavior-question grounding checks --------------------------------------
def check_behavior_asserts(db_id: str, items: list[dict]) -> list[str]:
    """Run each behavior item's grounding asserts against the local SQLite DB.

    A failed assert means the question's premise about the data is WRONG
    (e.g. a 'nonexistent' column that exists) — generation must stop.
    """
    path = os.path.join(HERE, "data", "dev_databases", db_id, f"{db_id}.sqlite")
    con = sqlite3.connect(path)
    cur = con.cursor()
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    all_cols = {
        c[1].lower()
        for t in tables
        for c in cur.execute(f'PRAGMA table_info("{t}")')
    }
    problems = []
    for i, it in enumerate(items):
        for a in it.get("asserts", []):
            kind, arg = a[0], a[1]
            if kind == "no_column":
                if any(arg.lower() in c for c in all_cols):
                    problems.append(f"{db_id}[{i}] no_column '{arg}' MATCHES a real column")
            elif kind == "sql":
                try:
                    ok = cur.execute(arg).fetchone()[0]
                except Exception as e:
                    problems.append(f"{db_id}[{i}] sql assert errored: {e} :: {arg}")
                    continue
                if not ok:
                    problems.append(f"{db_id}[{i}] sql assert FALSE :: {arg}")
    con.close()
    return problems


# ---- Glue schema (the ground truth the loader wrote) ------------------------
def glue_schema(glue, db: str) -> dict[str, dict[str, str]]:
    """{table_lc: {column_lc: hive_type}} for one Glue database."""
    out: dict[str, dict[str, str]] = {}
    paginator = glue.get_paginator("get_tables")
    for page in paginator.paginate(DatabaseName=db):
        for t in page["TableList"]:
            cols = {
                c["Name"].lower(): c["Type"]
                for c in t["StorageDescriptor"]["Columns"]
            }
            out[t["Name"].lower()] = cols
    return out


# ---- SQLite gold -> Athena gold ---------------------------------------------
_BOOL_EXPRS = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.Is, exp.In, exp.Like, exp.And, exp.Or, exp.Not, exp.Between,
)
_DATE_EXPRS = (
    exp.Date, exp.TsOrDsToDate, exp.CurrentDate,
    exp.DateAdd, exp.DateSub, exp.DateTrunc,
)
_CMP_EXPRS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_ARITH_EXPRS = (exp.Add, exp.Sub, exp.Mul, exp.Div)
_NUM_TYPES = {"bigint", "double"}

# Hand-translated golds where a mechanical transform would be dishonest.
# 1031 relies on SQLite prefix-parsing DATETIME() minus a birthday string down
# to a difference of YEARS — transcribed to the arithmetic it actually did.
# (Note: it is clock-dependent by construction, in SQLite and here alike.)
OVERRIDES: dict[int, str] = {
    1031: (
        "SELECT DISTINCT YEAR(CURRENT_DATE) - "
        "CAST(SUBSTR(t2.birthday, 1, 4) AS INTEGER) AS age "
        "FROM player_attributes AS t1 INNER JOIN player AS t2 "
        "ON t1.player_api_id = t2.player_api_id "
        "WHERE SUBSTR(t1.date, 1, 4) >= '2013' "
        "AND SUBSTR(t1.date, 1, 4) <= '2015' AND t1.sprint_speed >= 97"
    ),
}


def _is_date_expr(node) -> bool:
    if isinstance(node, _DATE_EXPRS):
        return True
    return isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DATE


def _sqlite_text(lit: exp.Literal) -> exp.Literal:
    """The TEXT form SQLite would coerce a numeric literal into."""
    return exp.Literal.string(lit.name)


def _is_time_to_str(node) -> bool:
    """A strftime-style call (renders as DATE_FORMAT, always digit output)."""
    return isinstance(node, exp.TimeToStr) or (
        isinstance(node, exp.Anonymous) and node.name.lower() == "date_format"
    )


def _sqlite_num(node: exp.Expression, *, zero_default: bool) -> exp.Expression:
    """SQLite's text->number coercion: longest numeric prefix ('13:30.454' ->
    13.0, '1:' -> 1), junk -> 0 (``zero_default``) or NULL."""
    parsed = exp.Cast(
        this=exp.func(
            "REGEXP_EXTRACT",
            node.copy(),
            exp.Literal.string(r"^[+-]?[0-9]+(\.[0-9]+)?"),
        ),
        to=exp.DataType.build("double"),
        safe=True,
    )
    if zero_default:
        return exp.func("COALESCE", parsed, exp.Literal.number(0))
    return parsed


def to_athena_sql(
    sqlite_sql: str, schema: dict[str, dict[str, str]], question_id: int | None = None
) -> str:
    if question_id in OVERRIDES:
        return OVERRIDES[question_id]
    tree = sqlglot.parse_one(sqlite_sql, read="sqlite")
    all_columns: set[str] = set()
    for cols in schema.values():
        all_columns |= set(cols)

    # SQLite quirk: "double quoted" resolves as a string literal when it names
    # no column. sqlglot reads it as an Identifier; rewrite the impostors to
    # literals BEFORE lowercasing (checked case-insensitively).
    def dequirk(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, exp.Column)
            and node.this
            and isinstance(node.this, exp.Identifier)
            and node.this.args.get("quoted")
            and node.this.name.lower() not in all_columns
        ):
            return exp.Literal.string(node.this.name)
        return node

    tree = tree.transform(dequirk)

    # The loader lowercased every table and column name; aliases are lowered
    # too (definition and reference lower together, so they stay consistent).
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.this.lower())

    # alias/name -> real table, for resolving a column's Glue type.
    alias_to_table = {
        (t.alias or t.name).lower(): t.name.lower() for t in tree.find_all(exp.Table)
    }

    def column_type(node) -> str | None:
        """Glue type of a bare column reference, or None if not resolvable."""
        if not isinstance(node, exp.Column):
            return None
        name = node.name.lower()
        if node.table:
            table = alias_to_table.get(node.table.lower())
            return schema.get(table, {}).get(name) if table else None
        types = {cols[name] for cols in schema.values() if name in cols}
        return types.pop() if len(types) == 1 else None

    def is_string_col(node) -> bool:
        return column_type(node) == "string"

    def is_stringy(node) -> bool:
        """The expression itself is string-VALUED (not merely contains one)."""
        if isinstance(node, exp.Column):
            return is_string_col(node)
        if isinstance(node, exp.Literal):
            return node.is_string
        if isinstance(node, (exp.Substring, exp.Concat, exp.DPipe, exp.Trim)):
            return True
        if isinstance(node, exp.Anonymous) and node.name.lower() in (
            "substr", "substring", "trim", "upper", "lower", "replace",
        ):
            return True
        return _is_time_to_str(node)

    # ---- structural repairs (SQLite's lenient SELECT shapes) ----------------
    for select in list(tree.find_all(exp.Select)):
        projections = select.expressions
        group = select.args.get("group")

        def bare_col(e):
            inner = e.this if isinstance(e, exp.Alias) else e
            return inner if isinstance(inner, exp.Column) else None

        # A window function is per-row, not an aggregate: RANK() OVER (...)
        # must neither count as an aggregate nor have its inputs MAX-wrapped.
        if list(select.find_all(exp.Window)):
            continue
        def own_aggs(e):
            """Aggregates belonging to THIS select (not a nested subquery's)."""
            return [
                a
                for a in e.find_all(exp.AggFunc)
                if a.find_ancestor(exp.Select) is select
            ]

        aggs = [e for e in projections if own_aggs(e)]
        bares = [e for e in projections if bare_col(e) is not None]

        if group is None and aggs and bares:
            only_agg = aggs[0].this if isinstance(aggs[0], exp.Alias) else aggs[0]
            if len(aggs) == 1 and isinstance(only_agg, (exp.Max, exp.Min)) and not (
                select.args.get("order") or select.args.get("limit")
            ):
                # SQLite's argmax: bare columns come FROM THE ROW of the
                # MAX/MIN. Trino shape: order by the argument, keep row 1.
                arg = only_agg.this
                aggs[0].replace(
                    exp.Alias(this=arg.copy(), alias=exp.to_identifier(aggs[0].alias_or_name))
                    if aggs[0].alias_or_name
                    else arg.copy()
                )
                select.order_by(
                    exp.Ordered(
                        this=arg.copy(), desc=isinstance(only_agg, exp.Max)
                    ),
                    copy=False,
                )
                select.limit(1, copy=False)
            else:
                # Aggregate + bare columns without GROUP BY: SQLite returns an
                # arbitrary row's value (the golds rely on it being unique) —
                # MAX() is the deterministic equivalent.
                for e in bares:
                    col = bare_col(e)
                    col.replace(exp.Max(this=col.copy()))
        elif group is not None:
            grouped = {g.sql() for g in group.expressions} | {
                g.name.lower() for g in group.expressions if isinstance(g, exp.Column)
            }

            def in_group(col):
                return col.sql() in grouped or col.name.lower() in grouped

            for e in bares:
                col = bare_col(e)
                if not in_group(col):
                    col.replace(exp.Max(this=col.copy()))
            order = select.args.get("order")
            if order:
                proj_aliases = {e.alias_or_name.lower() for e in projections}
                for o in order.expressions:
                    if (
                        isinstance(o.this, exp.Column)
                        and not in_group(o.this)
                        and o.this.name.lower() not in proj_aliases
                    ):
                        o.this.replace(exp.Max(this=o.this.copy()))

        # SELECT DISTINCT ... ORDER BY <not in the list>: Trino rejects it and
        # the grader compares result SETS anyway — drop the ORDER BY.
        if select.args.get("distinct") and select.args.get("order"):
            proj_sqls = {
                (e.this if isinstance(e, exp.Alias) else e).sql()
                for e in select.expressions
            }
            if any(o.this.sql() not in proj_sqls for o in select.args["order"].expressions):
                select.set("order", None)

    # ORDER BY JULIANDAY(x): for ISO date strings the string itself sorts
    # identically — drop the (unregistered) function.
    for o in tree.find_all(exp.Ordered):
        f = o.this
        if isinstance(f, exp.Anonymous) and f.name.lower() == "julianday":
            o.set("this", f.expressions[0].copy())

    # DATE(<string column>) chokes on 'YYYY-MM-DD HH:MM:SS.f' values; SQLite's
    # date() just yields the day part — SUBSTR(x, 1, 10) is that, exactly.
    def fix_date_fn(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Date, exp.TsOrDsToDate)):
            # SQLite's DATE('now') is today's date.
            if (
                isinstance(node.this, exp.Literal)
                and node.this.is_string
                and node.this.name.lower() == "now"
            ):
                return exp.CurrentDate()
            arg = node.this if isinstance(node.this, exp.Column) else None
            if arg is not None and column_type(arg) == "string":
                return exp.func(
                    "SUBSTR", arg.copy(), exp.Literal.number(1), exp.Literal.number(10)
                )
        return node

    tree = tree.transform(fix_date_fn)

    # SQLite dynamic-typing repairs, mirroring what SQLite itself computed for
    # the leaderboard gold (so EX equality is preserved, not reinterpreted):
    def repair(node: exp.Expression) -> exp.Expression:
        # SUM/AVG over a boolean expression: SQLite treats TRUE as 1.
        if isinstance(node, (exp.Sum, exp.Avg)) and isinstance(node.this, _BOOL_EXPRS):
            node.set(
                "this",
                exp.func("IF", node.this, exp.Literal.number(1), exp.Literal.number(0)),
            )
            return node
        # Comparison of a string-typed column with a numeric literal: SQLite
        # coerces the literal to TEXT (the column keeps text semantics).
        if isinstance(node, _CMP_EXPRS):
            left, right = node.this, node.expression
            for col, lit, side in ((left, right, "expression"), (right, left, "this")):
                if (
                    isinstance(lit, exp.Literal)
                    and not lit.is_string
                    and column_type(col) == "string"
                ):
                    node.set(side, _sqlite_text(lit))
                elif (
                    isinstance(lit, exp.Literal)
                    and lit.is_string
                    and column_type(col) in _NUM_TYPES
                ):
                    # numeric-affinity column vs 'text': SQLite converts the
                    # text to a number.
                    try:
                        float(lit.name)
                    except ValueError:
                        pass
                    else:
                        node.set(side, exp.Literal.number(lit.name))
            # DATE-valued side vs string literal/column: compare as TEXT the
            # way SQLite did ('YYYY-MM-DD' is lexicographically date-ordered).
            for dt, side in ((left, "this"), (right, "expression")):
                if _is_date_expr(dt):
                    node.set(side, exp.cast(dt, "varchar"))
            return node
        if isinstance(node, exp.Between):
            if _is_date_expr(node.this):
                node.set("this", exp.cast(node.this, "varchar"))
            elif column_type(node.this) == "string":
                for side in ("low", "high"):
                    lit = node.args.get(side)
                    if isinstance(lit, exp.Literal) and not lit.is_string:
                        node.set(side, _sqlite_text(lit))
            return node
        if isinstance(node, exp.In) and column_type(node.this) == "string":
            node.set(
                "expressions",
                [
                    _sqlite_text(e)
                    if isinstance(e, exp.Literal) and not e.is_string
                    else e
                    for e in node.expressions
                ],
            )
            return node
        return node

    tree = tree.transform(repair)

    # SQLite arithmetic/aggregation over TEXT coerces via numeric-prefix parse
    # (junk -> 0). Reproduce it where Trino would type-error instead:
    def coerce(node: exp.Expression) -> exp.Expression:
        # CAST(<string expr> AS numeric): SQLite prefix-parses ('1:' -> 1).
        if (
            isinstance(node, exp.Cast)
            and not node.args.get("safe")
            and node.to.this
            in (
                exp.DataType.Type.INT,
                exp.DataType.Type.BIGINT,
                exp.DataType.Type.FLOAT,
                exp.DataType.Type.DOUBLE,
            )
            and is_stringy(node.this)
        ):
            num = _sqlite_num(node.this, zero_default=True)
            if node.to.this in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT):
                # SQLite CAST AS INTEGER truncates toward zero.
                return exp.cast(exp.func("TRUNCATE", num), "bigint")
            return num
        # SUM/AVG over a string column: coerce each value like SQLite did
        # ('13:30.454' contributes 13.0, not an error).
        if isinstance(node, (exp.Sum, exp.Avg)) and is_string_col(node.this):
            node.set("this", _sqlite_num(node.this, zero_default=False))
            return node
        # IF branches mixing a string column with a numeric literal (the
        # SUM(IIF(cond, text_col, 0)) idiom).
        if isinstance(node, exp.If):
            true, false = node.args.get("true"), node.args.get("false")
            branches = [b for b in (true, false) if b is not None]
            if any(isinstance(b, exp.Literal) and not b.is_string for b in branches):
                for key in ("true", "false"):
                    b = node.args.get(key)
                    if b is not None and is_string_col(b):
                        node.set(key, _sqlite_num(b, zero_default=True))
            return node
        # Arithmetic with strftime output or string columns as operands.
        if isinstance(node, _ARITH_EXPRS):
            for side in ("this", "expression"):
                operand = node.args.get(side)
                if _is_time_to_str(operand):
                    node.set(side, exp.cast(operand.copy(), "integer"))
                elif isinstance(operand, exp.Column) and is_string_col(operand):
                    node.set(side, _sqlite_num(operand, zero_default=True))
            return node
        return node

    tree = tree.transform(coerce)

    # Trino rejects a projection alias referenced in HAVING (SQLite allows
    # it): inline the aliased expression at the reference site.
    for select in tree.find_all(exp.Select):
        having = select.args.get("having")
        if having is None:
            continue
        aliases = {
            a.alias.lower(): a.this
            for a in select.expressions
            if isinstance(a, exp.Alias)
        }

        def inline(node, aliases=aliases):
            if isinstance(node, exp.Column) and not node.table:
                target = aliases.get(node.name.lower())
                if target is not None:
                    return target.copy()
            return node

        having.set("this", having.this.transform(inline))

    return tree.sql(dialect="trino")


# ---- Athena validation -------------------------------------------------------
def run_on_athena(athena, db: str, output_loc: str, sql: str) -> str | None:
    """None on success, else the Athena error string."""
    try:
        qid = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": db},
            WorkGroup=ATHENA_WORKGROUP,
            ResultConfiguration={"OutputLocation": output_loc},
        )["QueryExecutionId"]
    except Exception as e:  # malformed enough to be rejected at submit
        return str(e)
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"][
            "Status"
        ]
        state = status["State"]
        if state == "SUCCEEDED":
            return None
        if state in ("FAILED", "CANCELLED"):
            return status.get("StateChangeReason", state)
        time.sleep(0.6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="only this mini_dev db_id (or Glue name)")
    ap.add_argument("--validate", action="store_true", help="execute every gold on Athena")
    args = ap.parse_args()

    with open(QUESTIONS_JSON) as f:
        questions = json.load(f)

    session = boto3.Session(region_name=REGION)
    glue = session.client("glue")
    account = session.client("sts").get_caller_identity()["Account"]

    os.makedirs(OUT_DIR, exist_ok=True)
    failures: dict[str, list[dict]] = {}

    for db_id, (glue_db, slug) in DATABASES.items():
        if args.db and args.db not in (db_id, glue_db):
            continue
        qs = [q for q in questions if q["db_id"] == db_id]
        schema = glue_schema(glue, glue_db)

        rows = []
        for q in qs:
            text = q["question"].strip()
            evidence = (q.get("evidence") or "").strip()
            if evidence:
                text = f"{text}\nHint: {evidence}"
            try:
                gold = to_athena_sql(q["SQL"], schema, q["question_id"])
            except Exception as e:
                failures.setdefault(glue_db, []).append(
                    {"question_id": q["question_id"], "stage": "transpile", "error": str(e)}
                )
                continue
            rows.append(
                {
                    "question_id": q["question_id"],
                    "difficulty": q.get("difficulty", ""),
                    "question": text,
                    "gold_sql": gold,
                    "expected_behavior": "",
                }
            )

        # Behavior-check questions (judge-graded; blank gold_sql keeps them
        # out of the Accuracy check). Grounding asserts run first — a false
        # premise about the data aborts generation.
        behavior = BEHAVIOR.get(glue_db, [])
        problems = check_behavior_asserts(db_id, behavior)
        if problems:
            for p in problems:
                print(f"[ASSERT] {p}")
            return 1
        n_acc = len(rows)
        for i, it in enumerate(behavior, start=1):
            rows.append(
                {
                    "question_id": f"B{i:02d}",
                    "difficulty": "behavior",
                    "question": it["question"],
                    "gold_sql": "",
                    "expected_behavior": it["expected_behavior"],
                }
            )

        out_path = os.path.join(OUT_DIR, f"{glue_db}.csv")
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "question_id", "difficulty", "question", "gold_sql",
                    "expected_behavior",
                ],
            )
            w.writeheader()
            w.writerows(rows)
        print(
            f"[write] {out_path}: {len(rows)} questions "
            f"({n_acc} accuracy + {len(rows) - n_acc} behavior)"
        )
        rows = rows[:n_acc]  # Athena validation below only runs gold SQL

        if args.validate:
            athena = session.client("athena")
            output_loc = f"s3://okf-bird-{slug}-{account}/athena-results/"

            def check(row):
                err = run_on_athena(athena, glue_db, output_loc, row["gold_sql"])
                return (row, err)

            with ThreadPoolExecutor(max_workers=VALIDATE_THREADS) as pool:
                results = list(pool.map(check, rows))
            bad = [(r, e) for r, e in results if e]
            print(f"[validate] {glue_db}: {len(rows) - len(bad)}/{len(rows)} gold OK")
            for r, e in bad:
                failures.setdefault(glue_db, []).append(
                    {
                        "question_id": r["question_id"],
                        "stage": "athena",
                        "error": e,
                        "gold_sql": r["gold_sql"],
                    }
                )

    if failures:
        report = os.path.join(OUT_DIR, "validation_failures.json")
        with open(report, "w") as f:
            json.dump(failures, f, indent=2)
        n = sum(len(v) for v in failures.values())
        print(f"[FAIL] {n} golds need attention -> {report}")
        return 1
    print("[done] all golds written" + (" and validated on Athena" if args.validate else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

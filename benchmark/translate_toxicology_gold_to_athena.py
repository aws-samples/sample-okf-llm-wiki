#!/usr/bin/env python3
"""Translate the BIRD toxicology gold SQL from SQLite dialect to Athena/Trino.

Companion to ``translate_gold_to_athena.py`` (formula_1). Toxicology is a much
gentler port than F1 — all four tables are pure varchar, so none of F1's
lenient-numeric-cast machinery is needed. Two rewrites carry the whole set:

1. ``CAST(x AS REAL)`` → ``CAST(x AS DOUBLE)``. **This one is a correctness fix,
   not cosmetics.** Trino's REAL is float32 and Athena renders it with float32
   precision, so a percentage gold prints ``38.698505`` where SQLite's REAL
   (float64) prints ``38.69850528618301``. The Benchmark Studio grader compares
   *stringified* cells (``harvest/benchmark/grader.py:_canonical``), so a float32
   gold can never set-equal a solver's ordinary float64 answer even when the
   solver is perfectly right — the question would fail for every wiki forever.
   5 of the 11 CAST-bearing golds print a different value under REAL vs DOUBLE
   (q4, q12, q34, q36, q38); the other 6 round to a short decimal and are
   unaffected, but all 11 are converted for uniformity.

2. ``a || b`` → ``concat(a, b)`` (q20). Trino's ``||`` does work on varchar, so
   this is a portability nicety rather than a fix; it is verified either way.

Deliberately NOT rewritten (verified against Athena, they already match SQLite):

* ``SUBSTR(x, -2)`` negative offsets — Trino counts from the end identically
  (``substr('TR004_19',-2)`` → ``'19'`` on both). q22/q24/q37 need no change.
* ``LENGTH`` / ``ROUND`` / ``AVG`` / ``COUNT(DISTINCT ...)`` / ``WITH`` /
  ``CASE`` — same spelling and semantics in Trino.
* Every gold's GROUP BY already lists its non-aggregated columns, so none hit
  Trino's EXPRESSION_NOT_AGGREGATE strictness (F1's q60 problem).

VERIFICATION is two-stage, same philosophy as the F1 script — a translation is
never trusted blindly:

1. LOCAL equivalence, against ``toxicology.sqlite``: run the ORIGINAL gold in
   real SQLite, and the TRANSLATED query in SQLite too with Trino's spellings
   registered as functions (``concat``), comparing result sets as unordered
   multisets. This proves the rewrite preserves the ANSWER, independent of AWS.
   Float cells are compared with a tolerance because the whole point of rewrite
   #1 is to change float PRECISION (SQLite float64 vs the float32 the gold asked
   for) — an exact string compare would reject the fix it is meant to validate.

2. REMOTE execution, against Athena (``--athena``): every translated query must
   actually run on Trino, since a gold that fails to execute is DISCARDED from
   the benchmark. Results are NOT compared to SQLite here, because the deployed
   Glue tables legitimately hold MORE data than the mini-dev SQLite file (12,333
   atoms vs 9,111 — the dump keeps ~101 molecules' atoms/bonds whose parent rows
   are absent from ``molecule``). That difference is a property of the data, not
   of the translation, and it is harmless: the grader executes gold AND predicted
   against the same live Athena, so both sides see the same rows.

Usage:
  python benchmark/translate_toxicology_gold_to_athena.py           # translate + local verify + write
  python benchmark/translate_toxicology_gold_to_athena.py --check   # verify only, write nothing
  python benchmark/translate_toxicology_gold_to_athena.py --athena  # also execute each on Athena

Writes four CSVs — Accuracy-only and combined, each with and without hints:

* ``toxicology_questions_athena.csv``            — question,gold_sql (40 rows)
* ``toxicology_questions_athena_hints.csv``      — same + BIRD ``evidence``
  appended to each question
* ``toxicology_questions_athena_full.csv``       — question,gold_sql,
  expected_behavior: the 40 Accuracy rows plus the 20 hand-authored Behavior
  rows from ``toxicology_questions_behavior.csv`` (60 rows). **This is the
  one to upload** for a run exercising both checks.
* ``toxicology_questions_athena_full_hints.csv`` — the combined set with the
  hinted Accuracy questions.

Exits non-zero if any translation fails to verify — nothing partial ships.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_CSV = os.path.join(HERE, "toxicology_questions.csv")
OUT_CSV = os.path.join(HERE, "toxicology_questions_athena.csv")
OUT_HINTS_CSV = os.path.join(HERE, "toxicology_questions_athena_hints.csv")
# Hand-authored Behavior questions (question,expected_behavior), merged into the
# combined sets below so ONE upload drives both checks.
BEHAVIOR_CSV = os.path.join(HERE, "toxicology_questions_behavior.csv")
OUT_FULL_CSV = os.path.join(HERE, "toxicology_questions_athena_full.csv")
OUT_FULL_HINTS_CSV = os.path.join(HERE, "toxicology_questions_athena_full_hints.csv")
MINI_DEV_JSON = os.path.join(HERE, "mini_dev", "data", "mini_dev_sqlite.json")
SQLITE_DB = os.path.join(
    HERE, "mini_dev", "data", "dev_databases", "toxicology", "toxicology.sqlite"
)
DB_ID = "toxicology"

# Athena/Glue coordinates of the deployed dump (for --athena).
ATHENA_REGION = "eu-west-1"
ATHENA_DATABASE = "toxicology"
ATHENA_OUTPUT = "s3://okf-bird-athena-results-158204760618/gold-verify/"


# -- translation -------------------------------------------------------------


def translate(sql: str) -> str:
    """Rewrite one SQLite toxicology gold into Athena/Trino dialect."""
    s = sql

    # 1. REAL (float32 in Trino) → DOUBLE (float64), matching SQLite's REAL.
    #    Scoped to `AS REAL` inside CAST so nothing else can be caught.
    s = re.sub(r"\bAS\s+REAL\b", "AS DOUBLE", s, flags=re.I)

    # 2. String concatenation: `X || Y` → concat(X, Y). Only q20 uses it, in the
    #    form `T1.molecule_id || '_1'`; keep the pattern tight (identifier or
    #    quoted literal on each side) rather than trying to parse expressions.
    operand = r"""(?:[A-Za-z_][A-Za-z0-9_.]*|'(?:[^']|'')*')"""
    s = re.sub(
        rf"({operand})\s*\|\|\s*({operand})",
        r"concat(\1, \2)",
        s,
    )

    return s


# -- Trino-function shims for local verification in SQLite -------------------


def _register_trino_shims(conn: sqlite3.Connection) -> None:
    """Register the Trino spellings our translation introduces as SQLite
    functions, so a TRANSLATED query runs in SQLite with Trino semantics."""

    # concat(a, b): Trino returns NULL if any argument is NULL — same as SQLite's
    # `||`. SQLite's built-in concat() only exists in 3.44+, so define it always
    # (create_function overrides the built-in when present).
    def _concat(*args):
        if any(a is None for a in args):
            return None
        return "".join(str(a) for a in args)

    conn.create_function("concat", 2, _concat)
    # substr with a negative offset, LENGTH, ROUND, AVG, COUNT(DISTINCT), CASE
    # and WITH are all native to SQLite with matching Trino semantics (verified
    # on Athena) — no shim needed. CAST(... AS DOUBLE) is accepted by SQLite as
    # REAL affinity, which is exactly the float64 semantics we want.


def _run(conn: sqlite3.Connection, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def _canonical(rows) -> Counter:
    """Order-insensitive multiset of rows, floats rounded to a tolerance.

    Mirrors the grader's set-comparison intent, but compares floats at ~9
    significant digits instead of exact text: rewrite #1 deliberately changes
    float PRECISION, so an exact compare would reject the very fix being
    verified. 9 digits is far tighter than any float32/float64 gap we are
    papering over, so a genuine arithmetic error still fails.
    """

    def norm(v):
        if v is None:
            return "\x00NULL"
        if isinstance(v, float):
            return f"{v:.9g}"
        if isinstance(v, int):
            return str(v)
        return str(v)

    return Counter(tuple(norm(v) for v in r) for r in rows)


def verify_one(original: str, translated: str) -> tuple[bool, str]:
    """Run original + translated in SQLite (translated with shims) and compare."""
    plain = sqlite3.connect(SQLITE_DB)
    try:
        gold_rows = _run(plain, original)
    except Exception as e:  # noqa: BLE001
        return False, f"ORIGINAL gold failed in sqlite (unexpected): {e}"
    finally:
        plain.close()

    shim = sqlite3.connect(SQLITE_DB)
    _register_trino_shims(shim)
    try:
        trans_rows = _run(shim, translated)
    except Exception as e:  # noqa: BLE001
        return False, f"TRANSLATED failed in sqlite-with-shims: {e}"
    finally:
        shim.close()

    if _canonical(gold_rows) == _canonical(trans_rows):
        return True, f"match ({len(gold_rows)} rows)"
    return False, (
        f"RESULT MISMATCH: gold={len(gold_rows)} rows, "
        f"translated={len(trans_rows)} rows"
    )


# -- Athena execution check --------------------------------------------------


def verify_on_athena(queries: list[str]) -> list[tuple[int, str]]:
    """Execute each translated query on Athena. Returns [(idx, error)] failures."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    import boto3

    ath = boto3.client("athena", region_name=ATHENA_REGION)

    def one(i_sql):
        i, sql = i_sql
        try:
            qid = ath.start_query_execution(
                QueryString=sql,
                QueryExecutionContext={"Database": ATHENA_DATABASE},
                ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
            )["QueryExecutionId"]
        except Exception as e:  # noqa: BLE001
            return i, f"start failed: {e}"
        deadline = time.time() + 180
        while time.time() < deadline:
            ex = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            st = ex["Status"]["State"]
            if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
                if st == "SUCCEEDED":
                    return i, ""
                reason = ex["Status"].get("StateChangeReason", st)
                return i, " ".join(str(reason).split())[:200]
            time.sleep(1)
        return i, "TIMEOUT after 180s"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = sorted(pool.map(one, enumerate(queries)), key=lambda t: t[0])
    return [(i, err) for i, err in results if err]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    ap.add_argument(
        "--athena", action="store_true", help="also execute each translated gold on Athena"
    )
    args = ap.parse_args()

    if not os.path.exists(SQLITE_DB):
        print(f"missing SQLite DB: {SQLITE_DB}", file=sys.stderr)
        return 2
    if not os.path.exists(SRC_CSV):
        print(f"missing source CSV: {SRC_CSV}", file=sys.stderr)
        return 2

    rows = list(csv.DictReader(open(SRC_CSV)))
    out_rows = []
    failures = []
    changed = 0
    for i, r in enumerate(rows):
        original = r["gold_sql"]
        translated = translate(original)
        if translated != original:
            changed += 1
        ok, detail = verify_one(original, translated)
        if not ok:
            failures.append((i, detail, translated))
        print(f"[{'OK ' if ok else 'FAIL'}] q{i}: {detail}")
        out_rows.append({"question": r["question"], "gold_sql": translated})

    print(f"\n{len(rows) - len(failures)}/{len(rows)} locally verified; {changed} rewritten.")
    if failures:
        print(f"\n{len(failures)} FAILED — nothing written:", file=sys.stderr)
        for i, detail, t in failures:
            print(f"  q{i}: {detail}\n     {t}", file=sys.stderr)
        return 1

    if args.athena:
        print("\nexecuting all translated golds on Athena…")
        bad = verify_on_athena([r["gold_sql"] for r in out_rows])
        for i, err in bad:
            print(f"[FAIL] q{i}: {err}", file=sys.stderr)
        print(f"{len(out_rows) - len(bad)}/{len(out_rows)} executed on Athena.")
        if bad:
            print("Athena execution failures — nothing written.", file=sys.stderr)
            return 1

    if args.check:
        return 0

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(["question", "gold_sql"])
        for r in out_rows:
            w.writerow([r["question"], r["gold_sql"]])
    print(f"\nwrote {OUT_CSV}")

    # Same rows with the BIRD `evidence` hint folded into the question, for runs
    # where the agent should see the external knowledge BIRD's authors assumed.
    evidence = {
        d["question"].strip(): d.get("evidence", "").strip()
        for d in json.load(open(MINI_DEV_JSON))
        if d["db_id"] == DB_ID
    }
    unmatched = [r["question"] for r in out_rows if r["question"].strip() not in evidence]
    if unmatched:
        print(
            f"{len(unmatched)} question(s) not found in {MINI_DEV_JSON}; "
            "hints CSV not written:",
            file=sys.stderr,
        )
        for q in unmatched:
            print(f"  {q}", file=sys.stderr)
        return 1
    hinted_rows = []
    for r in out_rows:
        hint = evidence[r["question"].strip()]
        q = f"{r['question']} (Hint: {hint})" if hint else r["question"]
        hinted_rows.append({"question": q, "gold_sql": r["gold_sql"]})
    with open(OUT_HINTS_CSV, "w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(["question", "gold_sql"])
        for r in hinted_rows:
            w.writerow([r["question"], r["gold_sql"]])
    print(f"wrote {OUT_HINTS_CSV}")

    # -- combined sets: one upload driving BOTH checks ------------------------
    # A question participates in a check iff its gold cell is non-blank, so the
    # SQL rows carry an empty expected_behavior and the Behavior rows an empty
    # gold_sql. Two variants, differing ONLY in the SQL half's question text:
    # the hints variant appends BIRD's `evidence` (the external knowledge BIRD's
    # authors assumed) to each Accuracy question. Behavior questions are
    # hand-authored and self-contained, so they are identical in both.
    if not os.path.exists(BEHAVIOR_CSV):
        print(
            f"note: {BEHAVIOR_CSV} not found — combined CSVs not written "
            "(the SQL-only sets above are complete).",
            file=sys.stderr,
        )
        return 0
    behavior = list(csv.DictReader(open(BEHAVIOR_CSV)))
    missing = [
        i for i, b in enumerate(behavior) if not (b.get("expected_behavior") or "").strip()
    ]
    if missing:
        print(
            f"{BEHAVIOR_CSV} has {len(missing)} row(s) with a blank "
            f"expected_behavior (rows {missing}); combined CSVs not written.",
            file=sys.stderr,
        )
        return 1

    for path, sql_rows in ((OUT_FULL_CSV, out_rows), (OUT_FULL_HINTS_CSV, hinted_rows)):
        with open(path, "w", newline="") as f:
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            w.writerow(["question", "gold_sql", "expected_behavior"])
            for r in sql_rows:
                w.writerow([r["question"], r["gold_sql"], ""])
            for b in behavior:
                w.writerow([b["question"], "", b["expected_behavior"]])
        print(
            f"wrote {path} "
            f"({len(sql_rows)} sql + {len(behavior)} behavior = "
            f"{len(sql_rows) + len(behavior)} rows)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Build an eval workspace from the fetched BIRD mini_dev data.

fetch_data.py places, under data/:
  mini_dev_sqlite.json       [{question_id, db_id, question, evidence, SQL, difficulty}]
  mini_dev_sqlite_gold.sql   one line per question: "<gold sql>\\t<db_id>"

The gold file line order matches the JSON record order (BIRD's stable indexing),
so we zip them by position and cross-check db_id agreement.

Emits, into --out (default: ./_ws):
  gen_questions.json    {idx: {question, evidence, db_id, dataset}}   generators — NO gold
  grade_questions.json  {idx: {question, evidence, gold, db_id, difficulty}}  grader/report only
  all_idxs.json         [0..N-1]  (optionally filtered to one db)
  meta.json             [{idx, db_id, difficulty}]

LEAKAGE GUARD: the gold SQL lives ONLY in grade_questions.json, which the
generator agents never open. They read gen_questions.json (question + evidence +
which dataset) and must derive the query from the OKF bundle. Grading uses the
original gold .sql file directly (run_eval.sh), not these workspace files.

Usage:
  python3 prep.py [--db-id formula_1] [--out _ws]
"""

from __future__ import annotations

import argparse
import json
import os

from config import glue_db

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def load_pairs(json_path: str, gold_path: str):
    questions = json.load(open(json_path))
    gold_lines = [ln for ln in open(gold_path).read().splitlines() if ln.strip()]
    if len(questions) != len(gold_lines):
        raise SystemExit(
            f"count mismatch: {len(questions)} questions vs {len(gold_lines)} gold lines"
        )
    out = []
    for i, (q, ln) in enumerate(zip(questions, gold_lines)):
        # gold line is "<sql>\t<db_id>"; split on the LAST tab (sql may contain none)
        sql, _, db_from_gold = ln.rpartition("\t")
        sql, db_from_gold = sql.strip(), db_from_gold.strip()
        if db_from_gold != q["db_id"]:
            raise SystemExit(
                f"idx {i}: db_id mismatch json={q['db_id']!r} gold={db_from_gold!r}"
            )
        out.append((i, q, sql))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(DATA, "mini_dev_sqlite.json"))
    ap.add_argument("--gold", default=os.path.join(DATA, "mini_dev_sqlite_gold.sql"))
    ap.add_argument("--db-id", default=None, help="restrict to one mini_dev db_id")
    ap.add_argument("--out", default=os.path.join(HERE, "_ws"))
    args = ap.parse_args()

    for p in (args.json, args.gold):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p} — run fetch_data.py first")

    os.makedirs(args.out, exist_ok=True)
    pairs = load_pairs(args.json, args.gold)

    grade: dict[str, dict] = {}   # graders/report ONLY — contains gold; NEVER shown to generators
    gen: dict[str, dict] = {}     # generators — question+evidence+dataset, NO gold
    idxs: list[int] = []
    meta: list[dict] = []

    for idx, q, gold_sql in pairs:
        if args.db_id and q["db_id"] != args.db_id:
            continue
        grade[str(idx)] = {
            "question": q["question"],
            "evidence": q.get("evidence", ""),
            "gold": gold_sql,
            "db_id": q["db_id"],
            "difficulty": q.get("difficulty", "?"),
        }
        # Generator-visible view: everything the leaderboard model gets (question +
        # evidence + which db), but NO gold. Agents open THIS file, never grade_*.
        gen[str(idx)] = {
            "question": q["question"],
            "evidence": q.get("evidence", ""),
            "db_id": q["db_id"],
            "dataset": glue_db(q["db_id"]),  # OKF dataset == Glue db name
        }
        idxs.append(idx)
        meta.append({"idx": idx, "db_id": q["db_id"], "difficulty": q.get("difficulty", "?")})

    json.dump(grade, open(os.path.join(args.out, "grade_questions.json"), "w"), indent=1)
    json.dump(gen, open(os.path.join(args.out, "gen_questions.json"), "w"), indent=1)
    json.dump(idxs, open(os.path.join(args.out, "all_idxs.json"), "w"))
    json.dump(meta, open(os.path.join(args.out, "meta.json"), "w"), indent=1)

    print(f"workspace: {args.out}")
    print(f"  questions: {len(idxs)}" + (f" (db={args.db_id})" if args.db_id else " (all 11 dbs)"))
    print("  gen_questions.json (gold-free, for generators) + grade_questions.json (gold, graders only)")


if __name__ == "__main__":
    main()

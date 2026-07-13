"""Assemble per-question agent SQL into the bird-bench predictions JSON.

The official evaluator (evaluation_ex.py -> package_sqls, mode='pred') expects a
JSON dict keyed by question index string, value = the SQL (optionally
"<sql>\\t----- bird -----\\t<db_id>"). Keys must cover 0..N-1 in gold order; a
missing/empty prediction becomes a harmless non-executable string that scores 0
(the same as the leaderboard treats a bad generation).

We append the "\\t----- bird -----\\t<db_id>" tail using the mini_dev db_id so
package_sqls routes each query to the right SQLite database.

Usage:
  python3 assemble_predictions.py --out _ws [--pred-dir preds]
      [--json evaluation/predict_mini_dev.json]
"""

from __future__ import annotations

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = "\t----- bird -----\t"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "_ws"))
    ap.add_argument("--pred-dir", default="preds")
    ap.add_argument("--json", default=os.path.join(HERE, "evaluation", "predict_mini_dev.json"))
    args = ap.parse_args()

    grade = json.load(open(os.path.join(args.out, "grade_questions.json")))
    idxs = [str(i) for i in json.load(open(os.path.join(args.out, "all_idxs.json")))]
    pred_dir = os.path.join(args.out, args.pred_dir)

    out: dict[str, str] = {}
    missing = 0
    for i in idxs:
        p = os.path.join(pred_dir, f"q{i}.sql")
        sql = open(p).read().strip() if os.path.exists(p) else ""
        if not sql:
            sql = "SELECT 1 WHERE 1=0"  # runs, returns nothing -> scores 0, never crashes eval
            missing += 1
        db_id = grade[i]["db_id"]
        out[i] = f"{sql}{SEP}{db_id}"

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump(out, open(args.json, "w"), indent=1)
    print(f"wrote {len(out)} predictions -> {args.json}")
    if missing:
        print(f"  WARNING: {missing} questions had no prediction file (scored 0)")


if __name__ == "__main__":
    main()

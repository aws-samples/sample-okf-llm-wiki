#!/usr/bin/env bash
# Grade the agent+OKF predictions with the UNMODIFIED bird-bench evaluators, on
# the original BIRD SQLite databases — i.e. the exact leaderboard grading.
# EX (Execution Accuracy) is the headline; Soft-F1 is also reported.
#
# Prereqs (run in order):
#   python3 fetch_data.py              # downloads SQLite DBs + the evaluators
#   python3 prep.py --out _ws          # builds the workspace
#   # generate predictions via generate_workflow.js -> _ws/preds/q<idx>.sql
#   python3 assemble_predictions.py --out _ws   # -> evaluation/predict_mini_dev.json
#
# Usage: ./run_eval.sh [pred_json]
#   pred_json defaults to evaluation/predict_mini_dev.json
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

DB_ROOT="$HERE/data/dev_databases/"
PRED="${1:-$HERE/evaluation/predict_mini_dev.json}"
GOLD="$HERE/data/mini_dev_sqlite_gold.sql"
DIFF="$HERE/data/mini_dev_sqlite.jsonl"
LOG="$HERE/evaluation/okf_eval_result.txt"

[[ -d "$HERE/evaluation" ]] || { echo "missing evaluation/ — run: python3 fetch_data.py"; exit 1; }
[[ -f "$PRED" ]] || { echo "missing $PRED — run: python3 assemble_predictions.py --out _ws"; exit 1; }
[[ -d "$DB_ROOT" ]] || { echo "missing $DB_ROOT — run: python3 fetch_data.py"; exit 1; }
[[ -f "$GOLD" ]] || { echo "missing $GOLD — run: python3 fetch_data.py"; exit 1; }

cd "$HERE/evaluation"
common=(--db_root_path "$DB_ROOT" --predicted_sql_path "$PRED"
        --ground_truth_path "$GOLD" --num_cpus 12 --meta_time_out 30.0
        --diff_json_path "$DIFF" --sql_dialect SQLite --output_log_path "$LOG")

echo "===== EX (Execution Accuracy) ====="
"$PY" -u evaluation_ex.py "${common[@]}"
echo "===== Soft F1 ====="
"$PY" -u evaluation_f1.py "${common[@]}" || echo "(Soft-F1 failed; EX is the headline metric)"
echo
echo "Log written to $LOG"
echo "Reference (published mini_dev SQLite EX): gpt-4 47.80 | gpt-4-turbo 45.80 | llama3-70b 40.80 | gpt-3.5-turbo 38.00 | TA+gpt-4o 63.00"

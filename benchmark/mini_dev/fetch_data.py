#!/usr/bin/env python3
"""Download the BIRD mini_dev dataset + the bird-bench evaluators.

Nothing from BIRD is vendored into this repo. This script fetches, into local
git-ignored directories:

  data/
    dev_databases/<db_id>/<db_id>.sqlite   the 11 SQLite databases
    mini_dev_sqlite.json                    500 questions (question/evidence/SQL/difficulty)
    mini_dev_sqlite_gold.sql                gold SQL, one line per question
    mini_dev_sqlite.jsonl                   per-question difficulty (grader input)
  evaluation/
    evaluation_ex.py evaluation_f1.py evaluation_ves.py evaluation_utils.py

Sources (both CC-BY-SA 4.0, bird-bench/mini_dev):
  - dataset zip:  https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip
                  (mirrors: Google Drive + HuggingFace birdsql/bird_mini_dev)
  - evaluators:   https://github.com/bird-bench/mini_dev  (evaluation/*.py)

The zip layout has shifted between BIRD releases, so we do NOT hardcode paths —
we extract the archive and *discover* each file by walking the tree. If the
official URL ever moves, download the zip yourself and pass --zip <path>.

Usage:
  python3 fetch_data.py                    # download + extract everything
  python3 fetch_data.py --zip minidev.zip  # use an already-downloaded archive
  python3 fetch_data.py --skip-evaluators  # data only (already have evaluators)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
EVAL_DIR = os.path.join(HERE, "evaluation")

ZIP_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"

# bird-bench evaluators, fetched verbatim from the repo (pinned to main). These
# are the UNMODIFIED official grader — we never edit them, so the score is
# leaderboard-comparable.
EVAL_BASE = "https://raw.githubusercontent.com/bird-bench/mini_dev/main/evaluation"
EVAL_FILES = [
    "evaluation_ex.py",
    "evaluation_f1.py",
    "evaluation_ves.py",
    "evaluation_utils.py",
]

# Files we need out of the archive, keyed by the basename to search for.
WANTED = {
    "mini_dev_sqlite.json": "mini_dev_sqlite.json",
    "mini_dev_sqlite_gold.sql": "mini_dev_sqlite_gold.sql",
    "mini_dev_sqlite.jsonl": "mini_dev_sqlite.jsonl",
}


def _progress(count: int, block: int, total: int) -> None:
    if total <= 0:
        return
    pct = min(100, count * block * 100 // total)
    mb = count * block / 1e6
    sys.stdout.write(f"\r  downloading… {pct:3d}%  ({mb:,.0f} MB)")
    sys.stdout.flush()


def download_zip(url: str, dest: str) -> None:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  archive already present: {dest} ({os.path.getsize(dest)/1e6:,.0f} MB)")
        return
    print(f"  {url}")
    urllib.request.urlretrieve(url, dest, _progress)
    print()  # newline after the progress line


def extract_and_place(zip_path: str) -> None:
    """Extract the archive to a temp dir and copy the wanted files into data/."""
    work = os.path.join(DATA_DIR, "_extract")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    print(f"  extracting {zip_path} …")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work)

    # 1) the loose data files, found by basename anywhere in the tree.
    for basename, target in WANTED.items():
        src = _find_file(work, basename)
        if not src:
            print(f"  WARNING: {basename} not found in archive")
            continue
        shutil.copy2(src, os.path.join(DATA_DIR, target))
        print(f"  {target:32s} <- {os.path.relpath(src, work)}")

    # 2) the dev_databases dir — the folder that holds the <db>/<db>.sqlite files.
    db_root = _find_dev_databases(work)
    if not db_root:
        raise SystemExit(
            "could not locate dev_databases (a dir with <db>/<db>.sqlite) in the archive"
        )
    dst = os.path.join(DATA_DIR, "dev_databases")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(db_root, dst)
    n = sum(1 for _ in _iter_sqlite(dst))
    print(f"  dev_databases/                   <- {os.path.relpath(db_root, work)}  ({n} .sqlite)")

    shutil.rmtree(work)


def _find_file(root: str, basename: str) -> str | None:
    for dirpath, _dirs, files in os.walk(root):
        if basename in files:
            return os.path.join(dirpath, basename)
    return None


def _iter_sqlite(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".sqlite"):
                yield os.path.join(dirpath, f)


def _find_dev_databases(root: str) -> str | None:
    """Return the directory that directly parents the per-db SQLite folders.

    We prefer a dir literally named ``dev_databases``; otherwise we fall back to
    the common parent of the discovered ``*.sqlite`` files.
    """
    for dirpath, dirs, _files in os.walk(root):
        if os.path.basename(dirpath) == "dev_databases":
            return dirpath
    sqlites = list(_iter_sqlite(root))
    if not sqlites:
        return None
    # each sqlite is <root>/<db>/<db>.sqlite -> parent-of-parent is the db root
    return os.path.dirname(os.path.dirname(sqlites[0]))


def download_evaluators() -> None:
    os.makedirs(EVAL_DIR, exist_ok=True)
    for fn in EVAL_FILES:
        dst = os.path.join(EVAL_DIR, fn)
        url = f"{EVAL_BASE}/{fn}"
        urllib.request.urlretrieve(url, dst)
        print(f"  {fn:28s} <- {url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=ZIP_URL, help="dataset zip URL")
    ap.add_argument("--zip", default=None, help="use an already-downloaded zip instead of downloading")
    ap.add_argument("--skip-evaluators", action="store_true", help="do not fetch the bird-bench evaluators")
    ap.add_argument("--skip-data", action="store_true", help="do not fetch the dataset zip")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if not args.skip_data:
        print("== BIRD mini_dev dataset ==")
        zip_path = args.zip or os.path.join(DATA_DIR, "minidev.zip")
        if not args.zip:
            download_zip(args.url, zip_path)
        elif not os.path.exists(zip_path):
            raise SystemExit(f"--zip path not found: {zip_path}")
        extract_and_place(zip_path)

    if not args.skip_evaluators:
        print("\n== bird-bench evaluators (unmodified) ==")
        download_evaluators()

    print("\nDone. Next: python3 load_all.py   (load the 11 databases into Glue)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Load all 11 BIRD mini_dev databases into Glue (one per config.DATABASES).

Reads the SQLite files fetched by fetch_data.py under data/dev_databases/, and
loads each into its own per-database S3 bucket (okf-bird-<slug>-<account>) +
Glue database. This is the "create Glue tables" step of the benchmark — the OKF
harvester authors bundles from these Glue databases afterward.

Usage:
  python3 load_all.py                       # load all 11, with Athena verify
  python3 load_all.py --db formula_1        # just one (by mini_dev db_id)
  python3 load_all.py --no-verify           # skip the Athena COUNT(*) checks
  python3 load_all.py --profile my-profile  # use a named AWS profile
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

from config import DATABASES, REGION, bucket_name, glue_db
from load_bird_to_glue import load

HERE = os.path.dirname(os.path.abspath(__file__))
DB_ROOT = os.path.join(HERE, "data", "dev_databases")


def sqlite_path(db_id: str) -> str:
    return os.path.join(DB_ROOT, db_id, f"{db_id}.sqlite")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="load only this mini_dev db_id")
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--profile", default=None, help="AWS named profile")
    ap.add_argument("--no-verify", action="store_true", help="skip Athena COUNT(*) verify")
    args = ap.parse_args()

    db_ids = [args.db] if args.db else list(DATABASES)
    unknown = [d for d in db_ids if d not in DATABASES]
    if unknown:
        raise SystemExit(f"unknown db_id(s): {unknown}; known: {sorted(DATABASES)}")

    account = boto3.Session(profile_name=args.profile).client("sts").get_caller_identity()["Account"]
    print(f"account={account} region={args.region} "
          f"loading {len(db_ids)} database(s) into Glue\n")

    results: dict[str, str] = {}
    for db_id in db_ids:
        src = sqlite_path(db_id)
        gdb = glue_db(db_id)
        bucket = bucket_name(db_id, account)
        print(f"== {db_id}  ->  glue db '{gdb}'  bucket '{bucket}' ==")
        if not os.path.exists(src):
            print(f"  SKIP: sqlite not found at {src} — run fetch_data.py first\n")
            results[db_id] = "MISSING"
            continue
        try:
            ok = load(src, gdb, bucket, region=args.region,
                      profile=args.profile, verify=not args.no_verify)
            results[db_id] = "OK" if ok else "VERIFY_MISMATCH"
        except Exception as e:  # noqa: BLE001 - report per-db, keep going
            print(f"  ERROR: {type(e).__name__}: {e}")
            results[db_id] = f"ERROR: {type(e).__name__}"
        print()

    print("== summary ==")
    for db_id in db_ids:
        print(f"  {db_id:26s} {results[db_id]}")
    failed = [d for d, s in results.items() if s != "OK"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

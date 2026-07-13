#!/usr/bin/env python3
"""Load one BIRD SQLite database into S3-Parquet + a Glue Data Catalog database.

This is how a mini_dev database is made discoverable to the OKF harvester: the
agent authors a knowledge bundle by reading the Glue catalog + sampling via
Athena, so the data has to live in Glue first. Layout produced:

  s3://<bucket>/<db>/<table>/<table>.000.parquet   (one prefix per table, no partitions)
  Glue database <db> with one EXTERNAL_TABLE per source table (lowercased names).

Idempotent: create-or-update only; never deletes anything. Column types are
mapped from SQLite's declared types (INTEGER->bigint, REAL->double, else string).

Bucket name defaults to okf-bird-<slug>-<account> (per config.DATABASES); the
account id is read from the caller's own credentials, so nothing here is tied to
a specific AWS account.

Requires: boto3, pyarrow.

Example (usually invoked via load_all.py):
  python3 load_bird_to_glue.py --sqlite data/dev_databases/formula_1/formula_1.sqlite \
      --db formula_1 [--bucket okf-bird-formula1-123456789012] [--verify]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile

import boto3
from botocore.exceptions import ClientError

from config import REGION, bucket_name

ATHENA_WORKGROUP = "primary"


# ---- SQLite declared-type -> Hive type -------------------------------------
def sqlite_type_to_hive(decl_type: str) -> str:
    t = (decl_type or "").strip().upper()
    if t == "INTEGER":
        return "bigint"
    if t == "REAL":
        return "double"
    return "string"  # TEXT, BLOB, NUMERIC, empty, ... -> string


def _coerce(value, hive: str):
    if value is None:
        return None
    try:
        if hive == "bigint":
            return int(value)
        if hive == "double":
            return float(value)
        return str(value)
    except (ValueError, TypeError):
        return None


def get_tables(cur) -> list[str]:
    rows = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if not r[0].startswith("sqlite_")]


def get_columns(cur, table: str) -> list[tuple[str, str]]:
    """Return [(orig_name, declared_type), ...] for a table."""
    return [
        (name, ctype)
        for _cid, name, ctype, _nn, _dflt, _pk in cur.execute(
            f'PRAGMA table_info("{table}")'
        )
    ]


def build_parquet(cur, table: str, columns: list[tuple[str, str]], out_path: str) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    orig_names = [n for n, _ in columns]
    hive_types = [sqlite_type_to_hive(ct) for _, ct in columns]
    rows = cur.execute(
        'SELECT {} FROM "{}"'.format(", ".join(f'"{n}"' for n in orig_names), table)
    ).fetchall()

    arrays, fields = [], []
    for idx, (name, hive) in enumerate(zip(orig_names, hive_types)):
        col = [_coerce(r[idx], hive) for r in rows]
        arrow_t = {"bigint": pa.int64(), "double": pa.float64()}.get(hive, pa.string())
        arr = pa.array(col, type=arrow_t)
        arrays.append(arr)
        fields.append(pa.field(name.lower(), arr.type))

    pq.write_table(pa.Table.from_arrays(arrays, schema=pa.schema(fields)),
                   out_path, compression="snappy")
    return len(rows)


def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  [s3] bucket exists: {bucket}")
    except ClientError:
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":  # us-east-1 rejects a LocationConstraint
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            s3.create_bucket(**kwargs)
            print(f"  [s3] created bucket: {bucket}")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
    # Block public access (idempotent).
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )


def ensure_glue_db(glue, db: str) -> None:
    try:
        glue.create_database(DatabaseInput={"Name": db})
        print(f"  [glue] created database: {db}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            print(f"  [glue] database exists: {db}")
        else:
            raise


def upsert_glue_table(glue, db: str, table_lc: str, columns, location: str) -> None:
    glue_cols = [{"Name": n.lower(), "Type": sqlite_type_to_hive(ct)} for n, ct in columns]
    table_input = {
        "Name": table_lc,
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {"classification": "parquet", "EXTERNAL": "TRUE"},
        "StorageDescriptor": {
            "Columns": glue_cols,
            "Location": location,
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
            },
        },
    }
    try:
        glue.create_table(DatabaseName=db, TableInput=table_input)
        print(f"  [glue] created table: {db}.{table_lc} ({len(glue_cols)} cols)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            glue.update_table(DatabaseName=db, TableInput=table_input)
            print(f"  [glue] updated table: {db}.{table_lc} ({len(glue_cols)} cols)")
        else:
            raise


def athena_count(session, region, db: str, table: str, output_loc: str) -> int | None:
    """Generic COUNT(*) verify — discovers nothing F1-specific."""
    import time

    athena = session.client("athena", region_name=region)
    qid = athena.start_query_execution(
        QueryString=f'SELECT COUNT(*) AS n FROM "{db}"."{table}"',  # noqa: S608 - quoted
        QueryExecutionContext={"Database": db},
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": output_loc},
    )["QueryExecutionId"]
    for _ in range(120):
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st != "SUCCEEDED":
        return None
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return int(rows[1]["Data"][0]["VarCharValue"])


def load(sqlite_path: str, db: str, bucket: str, *, region: str, profile: str | None,
         verify: bool) -> bool:
    session = boto3.Session(profile_name=profile, region_name=region)
    s3 = session.client("s3", region_name=region)
    glue = session.client("glue", region_name=region)

    ensure_bucket(s3, bucket, region)
    ensure_glue_db(glue, db)

    con = sqlite3.connect(sqlite_path)
    cur = con.cursor()
    tables = get_tables(cur)
    print(f"  [src] {len(tables)} tables: {tables}")

    src_counts: dict[str, int] = {}
    tmpdir = tempfile.mkdtemp(prefix="birdload_")
    for table in tables:
        columns = get_columns(cur, table)
        table_lc = table.lower()
        local = os.path.join(tmpdir, f"{table_lc}.000.parquet")
        n = build_parquet(cur, table, columns, local)
        src_counts[table_lc] = n

        key = f"{db}/{table_lc}/{table_lc}.000.parquet"
        s3.upload_file(local, bucket, key)
        upsert_glue_table(glue, db, table_lc, columns,
                          location=f"s3://{bucket}/{db}/{table_lc}/")
    con.close()

    if not verify:
        print(f"  [done] {db}: {sum(src_counts.values())} rows across {len(tables)} tables")
        return True

    print("  [verify] Athena COUNT(*) per table")
    output_loc = f"s3://{bucket}/athena-results/"
    ok = True
    for table_lc, expected in src_counts.items():
        got = athena_count(session, region, db, table_lc, output_loc)
        match = got == expected
        ok = ok and match
        print(f"    {table_lc:24s} sqlite={expected:<8d} athena={got} {'OK' if match else 'MISMATCH'}")
    print(f"  VERIFY {db}: {'OK' if ok else 'MISMATCH'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Load one BIRD SQLite DB into S3 Parquet + Glue.")
    ap.add_argument("--sqlite", required=True, help="path to source <db>.sqlite")
    ap.add_argument("--db", required=True, help="Glue database name (also S3 prefix)")
    ap.add_argument("--bucket", default=None,
                    help="target S3 bucket (default: okf-bird-<slug>-<account>)")
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--profile", default=None, help="AWS named profile (default: credential chain)")
    ap.add_argument("--verify", action="store_true", help="Athena COUNT(*) sanity check per table")
    args = ap.parse_args()

    bucket = args.bucket
    if not bucket:
        account = boto3.Session(profile_name=args.profile).client("sts").get_caller_identity()["Account"]
        # db here is the Glue db; the bucket slug is keyed by mini_dev db_id, but
        # for the standard 11 they coincide except football. Look up by matching.
        from config import DATABASES

        slug = next((v[1] for v in DATABASES.values() if v[0] == args.db), args.db.replace("_", ""))
        bucket = f"okf-bird-{slug}-{account}"

    ok = load(args.sqlite, args.db, bucket, region=args.region,
              profile=args.profile, verify=args.verify)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One-time backfill: stamp the by-entity GSI keys onto existing registry rows.

The ``by-entity`` index (CONVENTIONS.md "Registry entity index") lets listings
Query instead of Scan, but a GSI only contains items that CARRY its key
attributes — rows written before the index existed don't. This walks the
registry once and stamps:

- ``DATASET#`` mapping rows  -> entity="dataset", pair="<domain>/<dataset>"
- ``META`` declared domains  -> entity="domain",  pair="<domain>"
- ``XREF#`` cross-references -> entity="xref",    pair="<target d>/<target ds>"

When the walk completes it writes the READINESS MARKER row (pk="REGISTRY",
sk="ENTITY_INDEX_READY") — readers Query the index only once that marker
exists and use the legacy scan until then (a partially-stamped registry would
otherwise Query back a partial catalog), so ordering vs. the terraform apply
doesn't matter. Idempotent (already-stamped rows are skipped); deploy.sh runs
it after every durable apply.

Usage:  python3 scripts/backfill_registry_entity.py [table-name] [region]
        (defaults: okf-registry, $AWS_REGION or eu-west-1)
"""

import os
import sys
from datetime import datetime, timezone

import boto3


def main() -> None:
    table = sys.argv[1] if len(sys.argv) > 1 else "okf-registry"
    region = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.environ.get("AWS_REGION", "eu-west-1")
    )
    ddb = boto3.client("dynamodb", region_name=region)

    stamped = skipped = 0
    kwargs = {"TableName": table}
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = (item.get("pk") or {}).get("S", "")
            sk = (item.get("sk") or {}).get("S", "")
            if not pk.startswith("DOMAIN#"):
                continue
            domain = pk.removeprefix("DOMAIN#")
            if sk.startswith("DATASET#"):
                entity, pair = "dataset", f"{domain}/{sk.removeprefix('DATASET#')}"
            elif sk == "META":
                entity, pair = "domain", domain
            elif sk.startswith("XREF#"):
                tds = sk.removeprefix("XREF#").split("#", 1)[0]
                entity, pair = "xref", f"{domain}/{tds}"
            else:
                continue
            if (item.get("entity") or {}).get("S") == entity and (
                item.get("pair") or {}
            ).get("S") == pair:
                skipped += 1
                continue
            ddb.update_item(
                TableName=table,
                Key={"pk": {"S": pk}, "sk": {"S": sk}},
                UpdateExpression="SET entity = :e, #pr = :p",
                ExpressionAttributeNames={"#pr": "pair"},
                ExpressionAttributeValues={
                    ":e": {"S": entity},
                    ":p": {"S": pair},
                },
            )
            stamped += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    # Every row is stamped — record that fact durably. Readers trust the index
    # only once this marker exists (okf_aws/registry_entity.py reads this exact
    # key; the literals are duplicated so this script stays boto3-only).
    ddb.put_item(
        TableName=table,
        Item={
            "pk": {"S": "REGISTRY"},
            "sk": {"S": "ENTITY_INDEX_READY"},
            "stamped_at": {
                "S": datetime.now(timezone.utc).isoformat(timespec="seconds")
            },
        },
    )
    print(
        f"stamped {stamped} row(s), {skipped} already current ({table}, {region}); "
        "entity index marked ready"
    )


if __name__ == "__main__":
    main()

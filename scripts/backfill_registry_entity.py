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
otherwise Query back a partial catalog), so ordering vs. the GSI's terraform
apply doesn't matter. Ordering vs. the WRITERS does: the marker vouches that
every row is in the index, so this must run only after the deployed services
stamp entity/pair on the rows they write — deploy.sh runs it at the END of the
compute stage, once terraform has replaced the code. Idempotent
(already-stamped rows are skipped).

Usage:  python3 scripts/backfill_registry_entity.py [table-name] [region]
        (defaults: okf-registry, $AWS_REGION or eu-west-1)
"""

import os
import sys
from pathlib import Path

import boto3

# The entity vocabulary + marker live in okf_aws.registry_entity so writers
# and readers can't drift. When the package isn't installed (deploy.sh's bare
# python3 fallback), import it from the repo checkout this script ships in.
try:
    from okf_aws import registry_entity
except ImportError:
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "services" / "okf_aws" / "src")
    )
    from okf_aws import registry_entity


def main() -> None:
    table = sys.argv[1] if len(sys.argv) > 1 else "okf-registry"
    region = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.environ.get("AWS_REGION", "eu-west-1")
    )
    ddb = boto3.client("dynamodb", region_name=region)

    stamped = skipped = vanished = 0
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
                entity = registry_entity.ENTITY_DATASET
                pair = registry_entity.entity_pair(
                    domain, sk.removeprefix("DATASET#")
                )
            elif sk == "META":
                entity = registry_entity.ENTITY_DOMAIN
                pair = registry_entity.entity_pair(domain)
            elif sk.startswith("XREF#"):
                tds = sk.removeprefix("XREF#").split("#", 1)[0]
                entity = registry_entity.ENTITY_XREF
                pair = registry_entity.entity_pair(domain, tds)
            else:
                continue
            if (item.get("entity") or {}).get("S") == entity and (
                item.get("pair") or {}
            ).get("S") == pair:
                skipped += 1
                continue
            try:
                ddb.update_item(
                    TableName=table,
                    Key={"pk": {"S": pk}, "sk": {"S": sk}},
                    UpdateExpression="SET entity = :e, #pr = :p",
                    # A row deleted between the Scan page and this stamp must
                    # STAY deleted: an unconditional update_item would
                    # resurrect it as a phantom {pk, sk, entity, pair} item
                    # that the ALL-projection GSI serves to listings forever.
                    ConditionExpression="attribute_exists(pk)",
                    ExpressionAttributeNames={"#pr": "pair"},
                    ExpressionAttributeValues={
                        ":e": {"S": entity},
                        ":p": {"S": pair},
                    },
                )
            except ddb.exceptions.ConditionalCheckFailedException:
                vanished += 1
                continue
            stamped += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    # Every row is stamped — record that fact durably. Readers trust the index
    # only once this marker exists (okf_aws.registry_entity owns the key).
    registry_entity.mark_entity_index_ready(ddb, table)
    print(
        f"stamped {stamped} row(s), {skipped} already current, "
        f"{vanished} deleted mid-scan ({table}, {region}); "
        "entity index marked ready"
    )


if __name__ == "__main__":
    main()

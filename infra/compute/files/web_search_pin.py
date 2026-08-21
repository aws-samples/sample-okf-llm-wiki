"""Pin the web-search gateway target to a connector version (UpdateGatewayTarget).

Sent as a RAW SigV4-signed request rather than through the AWS CLI/SDK: the
shipped service models lag the API — as of aws-cli 2.35.8 / botocore 1.43.44
their ConnectorSource shape has only `connectorId`, so a CLI call carrying
`source.version` fails CLIENT-SIDE with ParamValidation while the service
itself accepts it (returns 202 and the tool schema gains `filters`). Pure
stdlib on purpose: the deploy host is only guaranteed the AWS CLI + python3
(deploy.sh), not boto3 — and curl --aws-sigv4 is not portable (macOS's
SecureTransport curl silently downgrades it to Basic auth). Retire this file
for a plain `aws bedrock-agentcore-control update-gateway-target` call once
the released models carry `source.version` (and fold the pin into the
declarative target once the CFN registry does).

stdin: the JSON from `aws configure export-credentials`.
env:   GATEWAY_ID, TARGET_ID, GW_REGION, TARGET_NAME, TARGET_DESCRIPTION,
       TARGET_CONFIGURATION (camelCase control-plane JSON), CONNECTOR_VERSION.
Exits non-zero (with the HTTP body on stderr) on any failure, including the
pin not landing in the response — the caller retries.
"""

import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request


def sign(headers: dict, method: str, path: str, body: bytes, creds: dict, region: str) -> None:
    """Add a SigV4 `authorization` header (service bedrock-agentcore)."""
    amz_date = headers["x-amz-date"]
    signed = ";".join(sorted(headers))
    canonical = (
        method + "\n" + path + "\n\n"
        + "".join(k + ":" + headers[k] + "\n" for k in sorted(headers))
        + "\n" + signed + "\n" + hashlib.sha256(body).hexdigest()
    )
    scope = amz_date[:8] + "/" + region + "/bedrock-agentcore/aws4_request"
    to_sign = (
        "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )
    key = b"AWS4" + creds["SecretAccessKey"].encode()
    for part in (amz_date[:8], region, "bedrock-agentcore", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 Credential=" + creds["AccessKeyId"] + "/" + scope
        + ", SignedHeaders=" + signed
        + ", Signature=" + hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    )


def main() -> int:
    creds = json.load(sys.stdin)
    region = os.environ["GW_REGION"]
    host = "bedrock-agentcore-control." + region + ".amazonaws.com"
    # The trailing slash is part of the operation's requestUri — keep it.
    path = "/gateways/" + os.environ["GATEWAY_ID"] + "/targets/" + os.environ["TARGET_ID"] + "/"
    body = json.dumps(
        {
            "name": os.environ["TARGET_NAME"],
            "description": os.environ["TARGET_DESCRIPTION"],
            "targetConfiguration": json.loads(os.environ["TARGET_CONFIGURATION"]),
            "credentialProviderConfigurations": [
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
        }
    ).encode()

    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    if creds.get("SessionToken"):
        headers["x-amz-security-token"] = creds["SessionToken"]
    sign(headers, "PUT", path, body, creds, region)

    request = urllib.request.Request(
        "https://" + host + path, data=body, method="PUT", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            out = json.load(response)
    except urllib.error.HTTPError as e:
        print(
            "UpdateGatewayTarget HTTP " + str(e.code) + ": "
            + e.read().decode("utf-8", "replace")[:300],
            file=sys.stderr,
        )
        return 1

    source = (
        out.get("targetConfiguration", {}).get("mcp", {}).get("connector", {}).get("source", {})
    )
    if source.get("version") != os.environ["CONNECTOR_VERSION"]:
        print("pin did not stick, source is: " + json.dumps(source), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

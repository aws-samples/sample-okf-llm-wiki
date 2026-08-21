# Public web search for the chat agent (var.enable_web_search).
#
# Web Search on Bedrock AgentCore is only reachable as a built-in CONNECTOR
# TARGET on an AgentCore Gateway, which speaks MCP — there is no direct
# data-plane search API. So this file provisions:
#
#   1. a gateway service role (what the gateway itself uses to reach the
#      connector: bedrock-agentcore:InvokeWebSearch on the service-owned tool ARN);
#   2. an MCP gateway with AWS_IAM inbound auth, so the CHAT RUNTIME authenticates
#      with SigV4 from its own execution role — no JWT, no client secret to vend;
#   3. the web-search connector target;
#   4. a version-pin step that upgrades the target to connector 1.2.0 (request-
#      level date/domain filters + the target-level include list).
#
# THREE non-obvious constraints are baked in here:
#
# * REGION — the connector is offered in us-east-1, eu-west-1, and ap-northeast-1
#   only, so all resources here use the aws.web_search provider alias
#   (var.web_search_region, default us-east-1) rather than var.region. A query
#   still never leaves AWS (the gateway serves it internally), but it can leave
#   the deployment's region; that's the trade-off for the capability, and it's why
#   the flag exists. The chat runtime signs for that region (OKF_WEB_SEARCH_REGION).
#
# * The target is an aws_cloudcontrolapi_resource, NOT
#   aws_bedrockagentcore_gateway_target. As of hashicorp/aws 6.56 that resource's
#   target_configuration.mcp block supports lambda / api_gateway / mcp_server /
#   open_api_schema / smithy_model — but NOT `connector`, which is the one shape
#   web search needs. AWS::BedrockAgentCore::GatewayTarget in the CloudFormation
#   registry does support Connector and is FULLY_MUTABLE, so Cloud Control gives
#   us the same declarative create/update/destroy through the SAME aws provider
#   (no second provider, still no console steps). Swap this for the native
#   resource once the provider grows a `connector` block.
#
# * The CONNECTOR VERSION PIN cannot be expressed declaratively AT ALL yet. The
#   request-level filters exist only when the target's source pins connector
#   version >= 1.2.0 (`source.version` in the control-plane API) — a gateway
#   snapshots the tool schema at target creation, and an unpinned target keeps
#   the pre-1.2.0 schema and *silently ignores* a `filters` argument (verified
#   live, all three regions). But the CFN registry's ConnectorSource carries ONLY
#   ConnectorId (additionalProperties: false — verified live Aug 2026), so
#   neither Cloud Control nor awscc can pin. Until it can, the terraform_data
#   step below pins via one `update-gateway-target` CLI call after the target
#   exists (the AWS CLI is already a documented deploy prerequisite). It is also
#   the SINGLE WRITER for the connector's parameterValues (both domain lists):
#   the Cloud Control desired_state keeps ParameterValues empty and constant, so
#   a domain-list change never routes an update through the CFN handler — whose
#   schema-conformant model would strip the pin. Fold the pin (and the lists)
#   back into the declarative resource when ConnectorSource grows Version.

locals {
  web_search_enabled = var.enable_web_search
  # The gateway-side tool name is `<target_name>___<tool>` (three underscores) —
  # AgentCore prefixes every tool with its target's name to keep names unique
  # across targets. The runtime can discover this via tools/list, but passing it
  # explicitly saves a round trip on the first search of each process.
  web_search_target_name = "${var.name_prefix}-web-search"
  web_search_tool_name   = "${var.name_prefix}-web-search___WebSearch"
  web_search_target_desc = "Amazon-operated web index + knowledge graph, MCP tool WebSearch"

  # First connector version with request-level filters and the include list.
  web_search_connector_version = "1.2.0"

  # Operator-side domain filtering, enforced server-side and invisible to the
  # model (the model's own per-call lists can only narrow further). Applied by
  # the pin step, not the Cloud Control resource — see the header comment.
  web_search_domain_filter = merge(
    length(var.web_search_included_domains) > 0 ? { include = var.web_search_included_domains } : {},
    length(var.web_search_excluded_domains) > 0 ? { exclude = var.web_search_excluded_domains } : {},
  )

  # The control-plane (camelCase) target configuration the pin step asserts.
  web_search_pinned_configuration = jsonencode({
    mcp = {
      connector = {
        source = {
          connectorId = "web-search"
          version     = local.web_search_connector_version
        }
        configurations = [
          {
            name = "WebSearch"
            parameterValues = (
              length(local.web_search_domain_filter) > 0
              ? { domainFilter = local.web_search_domain_filter }
              : {}
            )
          }
        ]
      }
    }
  })
}

# --- the gateway's own service role -------------------------------------------

data "aws_iam_policy_document" "web_search_gateway_assume" {
  count = local.web_search_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "web_search_gateway" {
  count = local.web_search_enabled ? 1 : 0

  # The gateway invokes itself on the caller's behalf...
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${var.web_search_region}:${local.account_id}:gateway/*"]
  }

  # ...and this is what authorizes the search itself. The resource is a
  # SERVICE-OWNED ARN (account field is literally "aws"), checked per request.
  statement {
    sid       = "InvokeWebSearch"
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:${var.web_search_region}:aws:tool/web-search.v1"]
  }
}

resource "aws_iam_role" "web_search_gateway" {
  count = local.web_search_enabled ? 1 : 0

  name               = "${var.name_prefix}-web-search-gateway"
  assume_role_policy = data.aws_iam_policy_document.web_search_gateway_assume[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "web_search_gateway" {
  count = local.web_search_enabled ? 1 : 0

  name   = "web-search-gateway-policy"
  role   = aws_iam_role.web_search_gateway[0].id
  policy = data.aws_iam_policy_document.web_search_gateway[0].json
}

# --- the gateway --------------------------------------------------------------

# AWS_IAM inbound auth: the only caller is the chat runtime, which signs with
# SigV4 from its execution role (see the ChatWebSearchGateway statement in
# agentcore_iam.tf). CUSTOM_JWT would mean vending and rotating a Cognito M2M
# client for a purely server-to-server hop — IAM is both simpler and tighter.
resource "aws_bedrockagentcore_gateway" "web_search" {
  count    = local.web_search_enabled ? 1 : 0
  provider = aws.web_search

  name            = "${var.name_prefix}-web-search"
  description     = "Web search connector for the OKF chat agent"
  role_arn        = aws_iam_role.web_search_gateway[0].arn
  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"

  tags = var.tags

  # Same IAM-propagation race as the runtimes: CreateGateway validates the
  # service role, which can be a stale snapshot seconds after PutRolePolicy.
  depends_on = [aws_iam_role_policy.web_search_gateway]
}

# --- the web-search connector target ------------------------------------------

resource "aws_cloudcontrolapi_resource" "web_search_target" {
  count    = local.web_search_enabled ? 1 : 0
  provider = aws.web_search

  type_name = "AWS::BedrockAgentCore::GatewayTarget"

  desired_state = jsonencode({
    Name              = local.web_search_target_name
    GatewayIdentifier = aws_bedrockagentcore_gateway.web_search[0].gateway_id
    Description       = local.web_search_target_desc
    TargetConfiguration = {
      Mcp = {
        Connector = {
          Source = { ConnectorId = "web-search" }
          Configurations = [
            {
              Name = "WebSearch"
              # Deliberately empty and CONSTANT: the pin step below owns the
              # version and the domain lists (see the header comment). Keeping
              # them out of desired_state means a domain-list change never
              # produces a Cloud Control patch that would strip the pin.
              ParameterValues = {}
            }
          ]
        }
      }
    }
    # Connector targets need no iamCredentialProvider — the connector's service
    # name is already known to the gateway, so the role alone is the config.
    CredentialProviderConfigurations = [
      { CredentialProviderType = "GATEWAY_IAM_ROLE" }
    ]
  })
}

# --- the connector version pin (+ domain lists) --------------------------------

# Re-runs whenever the target is recreated or the pinned configuration changes
# (version bump, either domain list). Uses the deployer's own credentials, like
# every other step of `terraform apply`.
resource "terraform_data" "web_search_target_pin" {
  count = local.web_search_enabled ? 1 : 0

  triggers_replace = [
    aws_cloudcontrolapi_resource.web_search_target[0].id,
    local.web_search_pinned_configuration,
    var.web_search_region,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    # Values travel as env vars, not interpolated shell text, so a quote in a
    # domain name can't break out of the command.
    environment = {
      GATEWAY_ID           = aws_bedrockagentcore_gateway.web_search[0].gateway_id
      GW_REGION            = var.web_search_region
      TARGET_NAME          = local.web_search_target_name
      TARGET_DESCRIPTION   = local.web_search_target_desc
      TARGET_CONFIGURATION = local.web_search_pinned_configuration
    }
    command = <<-EOT
      set -euo pipefail
      TARGET_ID=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$GATEWAY_ID" --region "$GW_REGION" \
        --query "items[?name=='$TARGET_NAME'].targetId" --output text)
      if [ -z "$TARGET_ID" ] || [ "$TARGET_ID" = "None" ]; then
        echo "web-search target '$TARGET_NAME' not found on gateway $GATEWAY_ID" >&2
        exit 1
      fi
      # The target can briefly report UPDATING right after create; retry.
      for attempt in 1 2 3 4 5; do
        if aws bedrock-agentcore-control update-gateway-target \
          --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID" \
          --name "$TARGET_NAME" --description "$TARGET_DESCRIPTION" \
          --target-configuration "$TARGET_CONFIGURATION" \
          --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
          --region "$GW_REGION" > /dev/null; then
          echo "web-search target pinned to connector ${local.web_search_connector_version}"
          exit 0
        fi
        echo "update-gateway-target attempt $attempt failed - retrying" >&2
        sleep 5
      done
      exit 1
    EOT
  }
}

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
#   3. the web-search connector target.
#
# TWO non-obvious constraints are baked in here:
#
# * REGION — the connector is offered ONLY in us-east-1, so all three resources
#   use the existing aws.us_east_1 alias regardless of var.region. A query still
#   never leaves AWS (the gateway serves it internally), but it does leave the
#   deployment's region; that's the trade-off for the capability, and it's why the
#   flag exists. The chat runtime signs for us-east-1 (OKF_WEB_SEARCH_REGION).
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

locals {
  web_search_enabled = var.enable_web_search
  # The gateway-side tool name is `<target_name>___<tool>` (three underscores) —
  # AgentCore prefixes every tool with its target's name to keep names unique
  # across targets. The runtime can discover this via tools/list, but passing it
  # explicitly saves a round trip on the first search of each process.
  web_search_target_name = "${var.name_prefix}-web-search"
  web_search_tool_name   = "${var.name_prefix}-web-search___WebSearch"
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
    resources = ["arn:aws:bedrock-agentcore:us-east-1:${local.account_id}:gateway/*"]
  }

  # ...and this is what authorizes the search itself. The resource is a
  # SERVICE-OWNED ARN (account field is literally "aws"), checked per request.
  statement {
    sid       = "InvokeWebSearch"
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1"]
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
  provider = aws.us_east_1

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
  provider = aws.us_east_1

  type_name = "AWS::BedrockAgentCore::GatewayTarget"

  desired_state = jsonencode({
    Name              = local.web_search_target_name
    GatewayIdentifier = aws_bedrockagentcore_gateway.web_search[0].gateway_id
    Description       = "Amazon-operated web index + knowledge graph, MCP tool WebSearch"
    TargetConfiguration = {
      Mcp = {
        Connector = {
          Source = { ConnectorId = "web-search" }
          Configurations = [
            {
              Name = "WebSearch"
              # Fixed provisioning values for the tool. An operator-supplied
              # domain denylist is enforced SERVER-SIDE and is invisible to the
              # model — it just never sees results from those hosts.
              ParameterValues = (
                length(var.web_search_excluded_domains) > 0
                ? { domainFilter = { exclude = var.web_search_excluded_domains } }
                : {}
              )
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

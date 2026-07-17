# Control API: API Gateway HTTP API (v2) + Cognito JWT authorizer -> a single
# Lambda whose internal router dispatches all endpoints. The browser talks only
# to this.

module "control_api_fn" {
  source      = "../modules/lambda"
  name        = "${var.name_prefix}-control-api"
  handler     = "control_api.app.lambda_handler"
  source_dir  = "${local.build_root}/control_api"
  policy_json = data.aws_iam_policy_document.control_api.json
  timeout     = 30
  memory_size = 512
  # Keep N environments pre-warmed to eliminate cold starts on the browser-facing
  # control plane. 0 disables it. See modules/lambda for how the alias is wired.
  provisioned_concurrency = var.control_api_provisioned_concurrency
  environment = merge(local.common_env, {
    OKF_HARVEST_RUNTIME_ARN = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
    OKF_ATHENA_WORKGROUP    = var.athena_workgroup
    # MCP credential vending: which pool to create M2M clients in + the scope to
    # grant them (must match the consumption authorizer's allowed_scopes).
    OKF_USER_POOL_ID = local.d.user_pool_id
    OKF_MCP_SCOPE    = local.d.mcp_scope
    # The harvest runtime's CloudWatch log group — read back for the live step
    # feed (GET /harvest/.../events). Derived from the runtime id (the ARN's last
    # path segment) as /aws/bedrock-agentcore/runtimes/<id>-DEFAULT. Overridable
    # via var.harvest_log_group if the account's naming differs; empty disables
    # the feed (the handler then returns an empty batch — status still works).
    OKF_HARVEST_LOG_GROUP = local.harvest_log_group
    # The (model, effort) catalog the per-harvest picker offers; the Control API
    # validates a chosen model/effort against this before invoking the runtime.
    # Same value the UI receives via VITE_HARVEST_MODEL_CATALOG (see outputs.tf).
    OKF_HARVEST_MODEL_CATALOG = jsonencode(var.harvest_model_catalog)
    # Chat conversation index (sidebar list) + the LangGraph checkpoint table the
    # delete route purges. The chat runtime writes both; the Control API only
    # reads/renames/deletes for the UI (GET/PUT/DELETE /chat/threads).
    OKF_CHAT_THREADS_TABLE    = local.d.chat_table
    OKF_CHAT_CHECKPOINT_TABLE = local.d.chat_checkpoints_table
  })
  tags = var.tags
}

resource "aws_apigatewayv2_api" "control" {
  name          = "${var.name_prefix}-control-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"] # tighten to the CloudFront URL post-deploy
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }

  tags = var.tags
}

# Cognito JWT authorizer: audience = the SPA app client id, issuer = the pool.
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.control.id
  name             = "cognito-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [local.d.user_pool_client_id]
    issuer   = local.d.oidc_issuer
  }
}

resource "aws_apigatewayv2_integration" "control" {
  api_id                 = aws_apigatewayv2_api.control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.control_api_fn.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

# A single $default route (JWT-protected) forwards everything to the Lambda,
# whose internal router matches method+path.
resource "aws_apigatewayv2_route" "default" {
  api_id             = aws_apigatewayv2_api.control.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.control.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# CRITICAL for CORS: with a JWT-authorized $default route, $default ALSO catches
# the browser's preflight OPTIONS — which then hits the authorizer and is
# rejected (no token on a preflight), surfacing as a CORS failure. Per AWS docs,
# add an explicit UNAUTHENTICATED `OPTIONS /{proxy+}` route (higher priority than
# $default) so preflight is answered by the HTTP API's managed CORS handler.
resource "aws_apigatewayv2_route" "options_preflight" {
  # checkov:skip=CKV_AWS_309:CORS preflight (OPTIONS) requests carry NO Authorization header by spec, so this route MUST be authorization_type = NONE — a JWT authorizer here would reject every preflight and break the SPA. It only answers preflight; all real methods hit the JWT-protected $default route. This is the AWS-documented pattern (see the comment above).
  api_id             = aws_apigatewayv2_api.control.id
  route_key          = "OPTIONS /{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.control.id}"
  authorization_type = "NONE"
}

# Access logging for the HTTP API stage (CKV_AWS_76): a request-level audit trail
# of who called what, from where, and how the authorizer/integration responded —
# needed to investigate abuse or a leaked JWT. 30-day retention keeps cost bounded
# for an admin tool; the log group is encrypted with the account's default key.
resource "aws_cloudwatch_log_group" "control_api_access" {
  name              = "/aws/apigateway/${var.name_prefix}-control-api/access"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.control.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.control_api_access.arn
    # Compact JSON access log: CALLER IDENTITY, request line, auth + integration
    # status, and latency — enough to trace a request end-to-end (who did what,
    # from where, with what result) without logging bodies. callerSub binds each
    # sensitive control-plane action (POST/DELETE /credentials, PUT/DELETE
    # /domains, POST /harvest) to the Cognito user's immutable subject claim so
    # the trail is a caller->action->target audit record, not just traffic (#44).
    format = jsonencode({
      requestId               = "$context.requestId"
      ip                      = "$context.identity.sourceIp"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      routeKey                = "$context.routeKey"
      path                    = "$context.path"
      status                  = "$context.status"
      callerSub               = "$context.authorizer.claims.sub"
      integrationStatus       = "$context.integrationStatus"
      integrationErrorMessage = "$context.integrationErrorMessage"
      authorizerError         = "$context.authorizer.error"
      responseLatency         = "$context.responseLatency"
    })
  }

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 50
  }

  tags = var.tags
}

resource "aws_lambda_permission" "control_api" {
  statement_id  = "AllowAPIGWInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.control_api_fn.function_name
  # When provisioned concurrency is on, integration_uri targets the "live" alias,
  # so the invoke permission must be scoped to that qualifier or API GW gets 403.
  qualifier  = module.control_api_fn.qualifier
  principal  = "apigateway.amazonaws.com"
  source_arn = "${aws_apigatewayv2_api.control.execution_arn}/*/*"
}

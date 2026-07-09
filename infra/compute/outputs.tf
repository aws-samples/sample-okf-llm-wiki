output "control_api_endpoint" {
  description = "Base URL the UI calls (API Gateway HTTP API invoke URL)."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "ui_bucket" {
  value = aws_s3_bucket.ui.id
}

output "ui_cloudfront_domain" {
  value = "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "ui_cloudfront_distribution_id" {
  description = "CloudFront distribution fronting the UI bucket; used to invalidate the cache after a UI deploy."
  value       = aws_cloudfront_distribution.ui.id
}

output "harvest_runtime_arn" {
  value = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
}

output "s3files_access_point_arn" {
  description = "The okf/-rooted S3 Files access point mounted by the harvest runtime (empty if VPC subnets not provided)."
  value       = local.harvest_access_point_arn
}

output "athena_output_location" {
  description = "S3 location where the harvest agent's Athena queries write results."
  value       = local.athena_output
}

output "consumption_runtime_arn" {
  value = try(aws_bedrockagentcore_agent_runtime.consumption[0].agent_runtime_arn, "")
}

output "reindex_queue_url" {
  value = aws_sqs_queue.reindex.id
}

output "incremental_queue_url" {
  value = aws_sqs_queue.incremental.id
}

# Everything the UI's build-time config (.env) needs, in one place.
output "ui_env" {
  value = {
    VITE_AWS_REGION        = var.region
    VITE_COGNITO_AUTHORITY = local.d.oidc_issuer
    VITE_COGNITO_CLIENT_ID = local.d.user_pool_client_id
    VITE_COGNITO_DOMAIN    = local.d.cognito_hosted_ui_domain
    VITE_API_BASE_URL      = aws_apigatewayv2_stage.default.invoke_url
    # MCP credential vending: shown on the Credentials page so a vended
    # client is immediately actionable (token endpoint + scope + MCP runtime).
    VITE_COGNITO_TOKEN_ENDPOINT = local.d.cognito_token_endpoint
    VITE_MCP_SCOPE              = local.d.mcp_scope
    VITE_MCP_RUNTIME_ARN        = try(aws_bedrockagentcore_agent_runtime.consumption[0].agent_runtime_arn, "")
    # The harvest model/effort picker options (see var.harvest_model_catalog).
    # BASE64-encoded, NOT raw JSON: deploy.sh's stage_ui `eval "export k=v"`s each
    # ui_env entry, and raw JSON's braces/quotes/spaces would be mangled by shell
    # brace-expansion + word-splitting. base64 is [A-Za-z0-9+/=] — safe through
    # both the eval and the .env.local dotenv path. The UI atob()+JSON.parses it.
    # (The Control API gets the same catalog as RAW JSON via OKF_HARVEST_MODEL_
    # CATALOG — that's a Lambda env var set directly by TF, never shell-eval'd.)
    VITE_HARVEST_MODEL_CATALOG = base64encode(jsonencode(var.harvest_model_catalog))
  }
}

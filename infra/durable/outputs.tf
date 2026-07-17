# Outputs consumed by the compute stack via terraform_remote_state, and by the
# UI build. Only root-level outputs are exposed to remote-state readers.

output "region" {
  value = var.region
}

output "account_id" {
  value = local.account_id
}

output "bundle_bucket" {
  value = aws_s3_bucket.bundles.id
}

output "bundle_bucket_arn" {
  value = aws_s3_bucket.bundles.arn
}

output "vector_bucket" {
  value = aws_s3vectors_vector_bucket.vectors.vector_bucket_name
}

output "vector_bucket_arn" {
  value = aws_s3vectors_vector_bucket.vectors.vector_bucket_arn
}

output "vector_index" {
  value = aws_s3vectors_index.concepts.index_name
}

output "vector_index_arn" {
  value = aws_s3vectors_index.concepts.index_arn
}

output "registry_table" {
  value = aws_dynamodb_table.registry.name
}

output "registry_table_arn" {
  value = aws_dynamodb_table.registry.arn
}

output "freshness_table" {
  value = aws_dynamodb_table.freshness.name
}

output "freshness_table_arn" {
  value = aws_dynamodb_table.freshness.arn
}

output "annotations_table" {
  value = aws_dynamodb_table.annotations.name
}

output "annotations_table_arn" {
  value = aws_dynamodb_table.annotations.arn
}

output "chat_checkpoints_table" {
  value = aws_dynamodb_table.chat_checkpoints.name
}

output "chat_checkpoints_table_arn" {
  value = aws_dynamodb_table.chat_checkpoints.arn
}

output "chat_table" {
  value = aws_dynamodb_table.chat.name
}

output "chat_table_arn" {
  value = aws_dynamodb_table.chat.arn
}

output "user_pool_id" {
  value = aws_cognito_user_pool.pool.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.pool.arn
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

# OIDC issuer — endpoint has no scheme, so prepend https://. Feeds the API GW +
# AgentCore JWT authorizers and the React app's `authority`.
output "oidc_issuer" {
  value = "https://${aws_cognito_user_pool.pool.endpoint}"
}

output "oidc_discovery_url" {
  value = "https://${aws_cognito_user_pool.pool.endpoint}/.well-known/openid-configuration"
}

output "cognito_hosted_ui_domain" {
  value = "${aws_cognito_user_pool_domain.domain.domain}.auth.${var.region}.amazoncognito.com"
}

# The full custom scope string (`okf-mcp/invoke`) that both the MCP authorizer
# trusts and every vended M2M client is granted.
output "mcp_scope" {
  value = aws_cognito_resource_server.mcp.scope_identifiers[0]
}

# The full custom scope string (`okf-chat/invoke`) the chat runtime's AgentCore
# JWT authorizer trusts; the SPA carries it so its access token can call chat.
output "chat_scope" {
  value = aws_cognito_resource_server.chat.scope_identifiers[0]
}

# OAuth2 token endpoint for the client_credentials (M2M) grant. Machine clients
# POST here with their client_id/secret to get a bearer token for the MCP server.
output "cognito_token_endpoint" {
  value = "https://${aws_cognito_user_pool_domain.domain.domain}.auth.${var.region}.amazoncognito.com/oauth2/token"
}

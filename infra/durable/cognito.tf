# Amazon Cognito (OIDC) — one user pool. The UI logs in here, and the SAME OIDC
# discovery URL feeds both the API Gateway JWT authorizer (compute stack) and the
# AgentCore JWT authorizer for the consumption MCP server.

resource "aws_cognito_user_pool" "pool" {
  name = "${var.name_prefix}-users"

  admin_create_user_config {
    allow_admin_create_user_only = true # invite-only; no open self-signup
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  tags = var.tags
}

# Hosted-UI domain (prefix). Needed for the OAuth authorize/logout endpoints the
# SPA redirects to.
resource "aws_cognito_user_pool_domain" "domain" {
  domain       = "${var.name_prefix}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.pool.id
}

# Resource server for the consumption MCP server. Its custom scope
# (`okf-mcp/invoke`) is the single grant the AgentCore JWT authorizer trusts
# (allowed_scopes) — so any client bearing this scope can call MCP, and NEW
# machine clients need NO authorizer/infra change (only the scope grant, done in
# Cognito). Chosen over allowed_clients (per-client allowlist -> Terraform drift)
# and allowed_audience (Cognito M2M client_credentials tokens carry no `aud`).
resource "aws_cognito_resource_server" "mcp" {
  user_pool_id = aws_cognito_user_pool.pool.id
  identifier   = "okf-mcp"
  name         = "${var.name_prefix}-mcp"

  scope {
    scope_name        = "invoke"
    scope_description = "Invoke the OKF consumption MCP server"
  }
}

# Public SPA client: authorization-code + PKCE, NO client secret.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name_prefix}-web"
  user_pool_id = aws_cognito_user_pool.pool.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  # openid/email/profile for login + the MCP scope so human sessions also satisfy
  # the scopes-based MCP authorizer (the SPA and M2M clients pass the same check).
  allowed_oauth_scopes         = ["openid", "email", "profile", aws_cognito_resource_server.mcp.scope_identifiers[0]]
  supported_identity_providers = ["COGNITO"]

  callback_urls = var.ui_callback_urls
  logout_urls   = var.ui_logout_urls

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Return a uniform "incorrect username or password" for BOTH a wrong password
  # and a non-existent user, so the hosted UI / auth API can't be used to
  # enumerate valid accounts (threat #64). Applies to login, forgot-password, and
  # confirm flows.
  prevent_user_existence_errors = "ENABLED"

  # Refresh long enough to survive a working session; access/id short.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# The initial console user, created from deploy inputs. Cognito emails a
# temporary password (the pool is invite-only). Created only when an email is
# supplied. No password is ever stored in Terraform state.
resource "aws_cognito_user" "admin" {
  count        = var.admin_email != "" ? 1 : 0
  user_pool_id = aws_cognito_user_pool.pool.id
  username     = var.admin_username != "" ? var.admin_username : var.admin_email

  attributes = {
    email          = var.admin_email
    email_verified = true
    given_name     = var.admin_given_name
    family_name    = var.admin_family_name
  }

  desired_delivery_mediums = ["EMAIL"]
}

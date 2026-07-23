variable "region" {
  type        = string
  description = "AWS region. Must have S3 Vectors + AgentCore + Titan V2 co-located."
  default     = "us-east-1"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for resource names, keeps a deployment self-contained."
  default     = "okf"
}

variable "dynamodb_customer_managed_cmk" {
  type        = bool
  default     = true
  description = <<-EOT
    Encrypt the DynamoDB tables with a customer-managed KMS CMK created here
    (CKV_AWS_119) instead of the default AWS-managed aws/dynamodb key. The CMK key
    policy includes a kms:ViaService grant for dynamodb.<region> scoped to this
    account, mirroring the AWS-managed key — so the existing table-consumer roles
    (dynamodb:* in the compute stack) keep working transparently with NO kms:*
    grant added to them (crypto ops are allowed only when made through DynamoDB).
    Set false to fall back to the AWS-managed key (still encrypted at rest, but the
    CKV_AWS_119 finding will re-appear).
  EOT
}

variable "bundle_bucket_name" {
  type        = string
  description = "Globally-unique name for the S3 bundle bucket (source of truth). Empty = derive from prefix + account id."
  default     = ""
}

variable "restrict_bundle_writers" {
  type        = bool
  default     = false
  description = <<-EOT
    Attach a bucket policy that DENIES s3:PutObject/DeleteObject on the authored
    bundle prefixes (okf/*) to every principal EXCEPT the harvest + control-api
    roles (threat #26: a stray writer poisoning the index). OFF by default and
    MUST be validated before enabling: bundle writes flow through the S3 Files
    mount, and CloudTrail S3 data events are typically OFF, so the exact writing
    principal (the harvest role vs the s3files service principal) cannot be
    confirmed from logs alone. Enabling with the wrong principal list Denies the
    live harvest's own writes. Before setting true: enable CloudTrail data events
    on the bucket, run one harvest, confirm the PutObject principal, and add it to
    var.bundle_writer_role_arns. See storage.tf aws_s3_bucket_policy.bundles.
  EOT
}

variable "bundle_writer_role_arns" {
  type        = list(string)
  default     = []
  description = "Role ARNs allowed to write okf/* when var.restrict_bundle_writers is true (harvest-runtime + control-api). Populated by the compute stack / operator after validating the S3 Files mount write principal."
}

variable "vector_bucket_name" {
  type        = string
  description = "Name for the S3 Vectors bucket. Empty = derive."
  default     = ""
}

variable "vector_index_name" {
  type    = string
  default = "okf-concepts"
}

variable "ui_callback_urls" {
  type        = list(string)
  description = "Cognito app-client OAuth callback URLs (the CloudFront URL(s) + http://localhost for dev)."
  # Must match auth.js redirect_uri (window.location.origin + "/callback.html").
  default = ["http://localhost:5173/callback.html"]
}

variable "ui_logout_urls" {
  type        = list(string)
  description = "Cognito app-client sign-out URLs."
  default     = ["http://localhost:5173/"]
}

# --- Initial console user (Cognito emails a temp password) -------------------

variable "admin_email" {
  type        = string
  description = "Email for the initial Cognito console user. Empty = create no user."
  default     = ""
}

variable "admin_username" {
  type        = string
  description = "Username for the initial user. Empty = use the email as username."
  default     = ""
}

variable "admin_given_name" {
  type    = string
  default = "OKF"
}

variable "admin_family_name" {
  type    = string
  default = "Admin"
}

variable "tags" {
  type    = map(string)
  default = { project = "okf-on-aws", managed_by = "terraform" }
}

# --- Observability (CloudWatch Transaction Search) ---------------------------

variable "enable_transaction_search" {
  type        = bool
  description = <<-EOT
    Enable CloudWatch Transaction Search (account+region-wide). Required for the
    AgentCore trace/span trajectory to appear in the GenAI Observability console:
    it routes X-Ray span ingestion into CloudWatch Logs (aws/spans). Set false if
    another stack/account owner already manages this setting, to avoid two owners
    fighting over one account-global config. Lives in the DURABLE stack because
    it is long-lived and account-global, not per-deploy.
  EOT
  default     = true
}

variable "transaction_search_indexing_percentage" {
  type        = number
  description = "Percentage of spans indexed as trace summaries (0-100). 1 keeps cost minimal; raise for denser search. Full span data is always in aws/spans regardless."
  default     = 1
}


variable "chat_checkpoint_offload_expire_days" {
  type        = number
  description = "Days before offloaded chat-checkpoint blobs in S3 expire. Keep >= the chat checkpoint TTL (compute var.chat_checkpoint_ttl_seconds) so live threads never lose offloaded state; orphans from deleted threads age out with the same rule."
  default     = 90
}

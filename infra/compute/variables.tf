variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  type    = string
  default = "okf"
}

variable "durable_state_bucket" {
  type        = string
  description = "S3 bucket holding the durable stack's remote state."
}

variable "durable_state_key" {
  type    = string
  default = "okf/durable/terraform.tfstate"
}

# The harvest + consumption container images (pushed to ECR by scripts/deploy).
variable "harvest_image_uri" {
  type        = string
  description = "ECR image URI (ARM64) for the harvest agent runtime."
  default     = ""
}

variable "consumption_image_uri" {
  type        = string
  description = "ECR image URI (ARM64) for the consumption MCP runtime."
  default     = ""
}

variable "chat_image_uri" {
  type        = string
  description = "ECR image URI (ARM64) for the chat agent runtime. Empty = don't create the runtime (lets validate/plan run before the image exists)."
  default     = ""
}

# S3 Files access point mounted (shared, runtime-scoped) at /mnt/data, rooted at
# okf/. There is no native Terraform resource for an S3 Files access point yet,
# so the ARN is provided out-of-band (created via CLI/console) and passed here.
# When empty, the harvest runtime is created without a filesystem mount (useful
# for validate/plan before the access point exists).
variable "s3_files_access_point_arn" {
  type    = string
  default = ""
}

variable "harvest_vpc_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the harvest runtime (VPC required for S3 Files)."
  default     = []
}

variable "harvest_vpc_security_group_ids" {
  type    = list(string)
  default = []
}

variable "harvest_model" {
  type        = string
  description = "Harvest model id. An anthropic.* Converse inference profile (e.g. global.anthropic.claude-opus-4-8) runs on the bedrock-runtime Converse API; an openai.* id (e.g. openai.gpt-5.6-sol) runs on the Bedrock Mantle OpenAI-compatible endpoint in harvest_mantle_region."
  default     = "global.anthropic.claude-opus-4-8"
}

variable "harvest_mantle_region" {
  type        = string
  description = "AWS region for the Bedrock Mantle endpoint when harvest_model is an openai.* GPT id. Independent of var.region because GPT-5.x on Mantle is only in us-east-2/us-west-2 while the harvest runtime may deploy elsewhere. Ignored for Converse (anthropic.*) models."
  default     = "us-east-2"
}

variable "harvest_effort" {
  type        = string
  description = "Adaptive-thinking effort for the harvest model, passed through to Bedrock output_config.effort. Valid values are model-specific and validated by Bedrock (e.g. Opus 4.8 supports xhigh|max|high|medium|low), so no TF-side allow-list."
  default     = "xhigh"
}

variable "harvest_max_tokens" {
  type        = number
  description = "Max output tokens for the harvest model (reasoning tokens count against this; Opus 4.8 allows up to 128000)."
  default     = 128000
}

variable "harvest_max_subagent_concurrency" {
  type        = number
  description = "Max dynamic subagents (reviewer/table-author) run concurrently when the supervisor fans out via the QuickJS task() global. Bounds in-flight Opus 4.8 crawls (the rest queue) to limit Bedrock throttling + peak cost."
  default     = 8
}

# The models + reasoning-effort levels the harvest UI's per-run picker offers.
# This is the single source of truth for the picker: it's jsonencode'd into the
# Control API's OKF_HARVEST_MODEL_CATALOG (server-side validation of a chosen
# model/effort before it reaches Bedrock) AND into the UI's
# VITE_HARVEST_MODEL_CATALOG (the dropdown options). Each entry lists the model
# id, a display label, the allowed efforts, and the default effort. `harvest_model`
# above remains the deploy-time DEFAULT used when a harvest request omits a model.
# GPT-5.6 added "max" as a distinct native level above "xhigh" (harvest.agent.
# _GPT_EFFORT passes it through verbatim), so the GPT entry offers the full ladder.
# Efforts are per-model: an older GPT id (e.g. gpt-5.4) that rejects "max" must NOT
# list it here — the catalog is the trust boundary Bedrock validation backs up.
variable "harvest_model_catalog" {
  type = list(object({
    model          = string
    label          = string
    efforts        = list(string)
    default_effort = string
  }))
  description = "Catalog of (model, allowed efforts) the harvest UI offers and the Control API validates against."
  default = [
    {
      model          = "global.anthropic.claude-opus-4-8"
      label          = "Claude Opus 4.8"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "openai.gpt-5.6-sol"
      label          = "GPT-5.6 Sol"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "global.anthropic.claude-opus-5"
      label          = "Claude Opus 5"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "global.anthropic.claude-sonnet-5"
      label          = "Claude Sonnet 5"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "openai.gpt-5.6-terra"
      label          = "GPT-5.6 Terra"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "global.anthropic.claude-fable-5"
      label          = "Claude Fable 5"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
    {
      model          = "openai.gpt-5.6-luna"
      label          = "GPT-5.6 Luna"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "xhigh"
    },
  ]
}

# --- Chat agent --------------------------------------------------------------

variable "chat_model" {
  type        = string
  description = "Deploy-time DEFAULT chat model, used when a conversation omits a model. An anthropic.* Converse profile runs on the Converse API; an openai.* id runs on Bedrock Mantle in chat_mantle_region."
  default     = "global.anthropic.claude-opus-5"
}

variable "chat_effort" {
  type        = string
  description = "Default adaptive-thinking effort / reasoning_effort for the chat model, passed through to Bedrock (validated per-model by Bedrock + the catalog, no TF allow-list)."
  default     = "high"
}

variable "chat_max_tokens" {
  type        = number
  description = "Default max output tokens for a chat turn. Lower than harvest's — interactive chat wants snappier turns."
  default     = 32000
}

variable "chat_mantle_region" {
  type        = string
  description = "AWS region for the Bedrock Mantle endpoint when a chat model is an openai.* GPT id. Independent of var.region (GPT-5.x on Mantle is only in us-east-2/us-west-2)."
  default     = "us-east-2"
}

variable "chat_checkpoint_ttl_seconds" {
  type        = number
  description = "TTL (seconds) for chat conversation checkpoints; 0 = no expiry. When >0 the DynamoDBSaver stamps an epoch `ttl` attr and DynamoDB reaps idle conversations. Default 30 days."
  default     = 2592000
}

variable "chat_idle_runtime_session_timeout" {
  type        = number
  description = "AgentCore idle-session timeout (seconds) for the chat runtime. A conversation resumes from the DynamoDB checkpointer after an idle stop, so this only trades cold-start latency vs warm-session cost. Default 30 min."
  default     = 1800
}

# The models + reasoning-effort levels the chat UI's picker offers. Single source
# of truth for the picker, SEPARATE from harvest_model_catalog so chat can offer a
# lighter/faster model set than the heavyweight authoring agent. Fans out the same
# three ways harvest's does: jsonencode'd into the chat runtime's
# OKF_CHAT_MODEL_CATALOG (the runtime validates a per-conversation model/effort
# before it reaches Bedrock — the trust boundary, since the browser calls the
# runtime directly with no proxy) and base64(json) into the UI's
# VITE_CHAT_MODEL_CATALOG (the dropdown). `chat_model` above is the deploy-time
# DEFAULT used when a conversation omits a model. Efforts are per-model.
variable "chat_model_catalog" {
  type = list(object({
    model          = string
    label          = string
    efforts        = list(string)
    default_effort = string
  }))
  description = "Catalog of (model, allowed efforts) the chat UI offers and the chat runtime validates against."
  # Chat is pinned to Opus 5 — a single-entry catalog (no model choice in the
  # UI; the runtime rejects anything else). Needs langchain-aws >= 1.6.4, the
  # first release that streams Opus 5. GPT-5.6 on Bedrock Mantle didn't return
  # reasoning summaries and behaved inconsistently, so it's dropped here.
  default = [
    {
      model          = "global.anthropic.claude-opus-5"
      label          = "Claude Opus 5"
      efforts        = ["low", "medium", "high", "xhigh", "max"]
      default_effort = "high"
    },
  ]
}

# --- Chat read-only SQL (optional) -------------------------------------------

variable "enable_chat_sql" {
  type        = bool
  default     = true
  description = <<-EOT
    Give the BROWSER-FACING chat agent a read-only SQL tool over the Glue catalog
    (Athena). This is the ONE chat tool that touches source data, so it expands the
    attack surface of a runtime users call directly — set to false to withhold the
    Glue/Athena grants entirely if that's not acceptable. When true, the
    chat runtime's IAM role gains catalog-wide READ-ONLY Glue/Athena + Athena
    results-bucket write (NO write to source data), and OKF_CHAT_SQL_ENABLED is set
    so the runtime offers run_sql. It still requires a per-conversation opt-in from
    the UI (the composer "+" menu sends features:["sql"]) — the flag alone doesn't
    force SQL on every chat. Read-only is enforced by IAM (no write grants) AND a
    query guard in the runtime (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN, single statement).
    Unlike harvest (pinned to one DB per invocation via a scoped STS session), chat
    SQL is catalog-wide; scope it tighter by leaving this off if that's too broad.
  EOT
}

variable "chat_sql_max_rows" {
  type        = number
  default     = 200
  description = "Max rows the chat run_sql tool returns per query (the rest are truncated to bound a turn's token cost). Only used when var.enable_chat_sql = true."
}

# --- Chat web search (optional) ----------------------------------------------

variable "enable_web_search" {
  type        = bool
  default     = true
  description = <<-EOT
    Give the chat agent a public web_search tool, so it can put internal numbers in
    external context (is a decline unusual for the sector? did a tariff, regulation,
    or supply event land in the same quarter?) and answer questions about facts newer
    than the model's training. Provisions an AgentCore Gateway with the built-in
    web-search connector plus its service role, and grants the chat runtime
    bedrock-agentcore:InvokeGateway on it.

    Three things to weigh before leaving this on:
      * REGION — the connector exists only in us-east-1, so the gateway is created
        there whatever var.region is. Queries are served inside AWS (never handed to
        a third-party search engine) but they DO leave the deployment's region.
      * COST — searches are billed per query, on top of the tokens the results add
        to a turn's context.
      * ATTRIBUTION — AWS's acceptable use requires that source citations and links
        be retained in anything shown to end users; the agent's prompt makes linking
        every used result mandatory, and the UI renders those links.

    Unlike run_sql this needs NO per-conversation opt-in: it reads no source data,
    and the turns it helps with are exactly the ones a user can't predict when
    choosing tools. Set to false to withhold the tool and create nothing.
  EOT
}

variable "web_search_excluded_domains" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Optional domain denylist for web_search (e.g. ["example.com"]). Enforced
    SERVER-SIDE by the connector and invisible to the model — the agent isn't told
    about the restriction, it simply never receives results from these hosts. Only
    used when var.enable_web_search = true.
  EOT
}

variable "web_search_max_results" {
  type        = number
  default     = 10
  description = "Default number of web_search results per query when the agent doesn't ask for a specific count (the agent can pick 1-25 per call). Higher = more context per search and more tokens per turn."
}

variable "enable_code_interpreter" {
  type        = bool
  default     = true
  description = <<-EOT
    Provision a network-isolated (SANDBOX-mode) AgentCore Code Interpreter and
    grant the harvest runtime access to it. This is what lets the harvest agent
    extract text from uploaded binary .context/ docs (PDF/DOCX/PPTX/XLSX) via its
    run_code tool. When false, no interpreter is created and OKF_CODE_INTERPRETER_ID
    is left unset, so the harvest degrades gracefully to text-only .context reading.
  EOT
}

variable "enable_lakeformation" {
  type        = bool
  default     = false
  description = <<-EOT
    Set true if the Glue catalog being harvested is governed by AWS Lake Formation.
    It adds lakeformation:GetDataAccess to the harvest DATA role (identity policy
    AND the per-invocation session policy) so Athena/Glue can obtain LF-vended,
    short-lived S3 credentials for governed table data. This is the ONLY change the
    solution can make on its own — the adopter must ALSO, per mapped dataset: grant
    the okf-harvest-data role LF SELECT+DESCRIBE (and DESCRIBE to the control-api /
    incremental / reconcile roles), and register the table data location with LF.
    See docs/LAKE_FORMATION.md. Default off — with plain (non-LF) catalogs the
    role's existing glue:Get*/athena:* + TableDataRead S3 grants are sufficient.
  EOT
}

variable "athena_workgroup" {
  type    = string
  default = "primary"
}

variable "enable_redshift" {
  type        = bool
  default     = false
  description = <<-EOT
    Set true to allow harvests to read an Amazon Redshift data source via the
    Redshift Data API. It adds the Redshift IAM grants — to the harvest DATA role
    (redshift-data + secretsmanager:GetSecretValue), to the Control API role
    (DescribeClusters / ListWorkgroups / ListDatabases + GetSecretValue for the
    UI's cluster/database pickers), and to the chat role when var.enable_chat_sql
    is also on (redshift-data + GetSecretValue for the chat run_sql tool on
    Redshift-scoped conversations). Default off — a Glue-only deployment needs
    none of this.

    NO connection config is set at deploy time: each Redshift mapping is
    self-describing — the operator picks the cluster/workgroup and its Secrets
    Manager secret in the UI, and the harvest reads the connection from the
    mapping's source descriptor ({type: redshift, cluster_identifier|workgroup_name,
    secret_arn}). Per-mapping secrets can't be enumerated at deploy time, so the
    GetSecretValue grants are scoped by NAME PREFIX instead — see
    var.redshift_secret_name_prefix.

    NOTE ON READ-ONLY: Redshift SQL executes with the SQL privileges of the
    secret's DB user — IAM cannot make it read-only the way it bounds Athena.
    Create the connection secrets with a least-privilege, read-only database user
    (see docs/DATA_SOURCES.md).
  EOT
}

variable "redshift_secret_name_prefix" {
  type        = string
  default     = "okf-"
  description = <<-EOT
    Secrets Manager NAME PREFIX the Redshift GetSecretValue grants are scoped to
    (harvest data role, Control API role, and — with enable_chat_sql — the chat
    role). Only secrets named with this prefix are usable as Redshift connection
    secrets; this bounds which credentials the browser-facing pickers and the
    chat SQL tool can exercise, instead of granting account-wide secret access.
    Name your Redshift connection secrets accordingly (e.g. okf-warehouse-ro), or
    change the prefix. Set to "" to allow any secret in the account (the pre-
    hardening behavior; not recommended).
  EOT
}

variable "control_api_provisioned_concurrency" {
  type        = number
  description = "Pre-warmed execution environments for the control API Lambda, to minimize cold starts on the browser-facing plane. 0 disables it. Deployments override via CONTROL_API_PROVISIONED_CONCURRENCY in scripts/.deployment.config (deploy.sh passes the -var only when set)."
  default     = 4
}

variable "athena_output_location" {
  type        = string
  description = "s3://.../ location for Athena query results (harvest sampling)."
  default     = ""
}

variable "ui_bucket_name" {
  type        = string
  description = "S3 bucket for the built React SPA. Empty = derive."
  default     = ""
}

# --- Observability -----------------------------------------------------------

variable "capture_trace_content" {
  type        = bool
  description = <<-EOT
    Capture LLM message CONTENT in traces — the adaptive-thinking reasoning text,
    prompts, and tool I/O — for the harvest agent (via the OpenInference LangChain
    instrumentor). true = full trajectory visible in CloudWatch GenAI Observability
    (default; this is an admin-only internal tool). Set false to REDACT content and
    keep only metadata (model id, token counts, span timing). NOTE: captured text
    lands in CloudWatch Logs (aws/spans) — a PII/secret surface and log-cost driver.
  EOT
  default     = true
}

# --- Incremental freshness ---------------------------------------------------

variable "enable_reconcile" {
  type        = bool
  default     = false
  description = <<-EOT
    Enable the NIGHTLY reconcile job (default OFF). When true, a scheduled Lambda
    re-scans every mapped dataset's Glue table versions once a day and triggers an
    incremental re-harvest for any table that drifted — the safety net for
    EventBridge Glue events that were dropped. OFF by default because it periodically
    invokes the harvest runtime (Bedrock/Athena cost) on a schedule with no human in
    the loop; the event-driven incremental path (Glue change -> SQS -> orchestrator)
    still runs regardless. Turn on if you want guaranteed eventual freshness even
    when live events are missed. Schedule is var.reconcile_schedule.
  EOT
}

variable "reconcile_schedule" {
  type        = string
  default     = "cron(0 7 * * ? *)" # 07:00 UTC daily
  description = "EventBridge schedule expression for the nightly reconcile job (only used when var.enable_reconcile = true)."
}

# --- CloudFront / edge security ----------------------------------------------

variable "enable_waf" {
  type        = bool
  default     = true
  description = <<-EOT
    Attach an AWS WAFv2 web ACL to the CloudFront distribution (threat #50):
    managed common-rule-set + IP-reputation + a rate-based rule. The web ACL is
    created in us-east-1 (required for CLOUDFRONT scope) via the us_east_1
    provider alias. Set false only if an org-level WAF already fronts the CDN.
  EOT
}

variable "waf_rate_limit_per_5min" {
  type        = number
  default     = 2000
  description = "WAFv2 rate-based rule threshold: max requests per rolling 5 min per client IP before the CDN blocks it (threat #50). Tune to expected legitimate SPA traffic."
}

variable "csp_override" {
  type        = string
  default     = ""
  description = <<-EOT
    Full Content-Security-Policy header value for the SPA (threat #51). Empty =
    use the built-in default (script-src 'self'; style-src 'self' 'unsafe-inline'
    for React/shadcn runtime styles; connect-src 'self' + Cognito + AWS APIs).
    Override if you serve the SPA from a custom domain or add third-party origins.
  EOT
}

variable "harvest_log_group" {
  type        = string
  default     = ""
  description = <<-EOT
    Override for the harvest runtime's CloudWatch log group that the Control API
    reads to serve the live harvest step feed (GET /harvest/.../events). Empty =
    derive it from the harvest runtime ARN as
    /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT. Set this only if your
    account's AgentCore runtime log-group naming differs from that convention.
    An empty/incorrect value simply disables the feed (status polling still works).
  EOT
}

# --- Automated Reasoning policy checks ---------------------------------------

variable "enable_policy_checks" {
  type        = bool
  default     = false
  description = <<-EOT
    Master deploy switch for the mid-turn policy checks — ADVISORY checks of a
    chat run's SQL queries (computational policies) and steps (behavioural
    policies) against the dataset's authored policy document, judged by a
    map-reduce fleet of LLM judges (chat/policy_check.py). Flags ride back
    into the model's own context as hedged system reminders — the model
    decides the correction; nothing blocks or gates. Per-run opt-in sits
    below this gate: the composer's Policy feature (requires the SQL tool).

    When true: the chat runtime's role gains the registry stale-flag write and
    events:PutEvents (the judges use the SAME model path as chat — no extra
    Bedrock grants); the Control API gains events:PutEvents; and the
    policy_rebuild EventBridge rule is created onto the incremental queue.
    Works in EVERY region the chat model works in. Costs a handful of small
    judge calls per opted-in analytical query / step batch.
  EOT
}

variable "enable_policy_build" {
  type        = bool
  default     = false
  description = <<-EOT
    Author/refresh policy documents from the wiki (requires
    var.enable_policy_checks). Turns on the HARVEST runtime's finalize hook
    (gather sources -> run the policy-author agent -> persist policies.yaml ->
    stamp ready, best-effort AFTER the commit marker) and the INCREMENTAL
    rebuild authority (policy_rebuild events + the nightly reconcile that
    reaps stalled authoring runs and hash-verifies every enrolled dataset).
    No Bedrock control-plane resources are involved.

    Separate from enable_policy_checks so a deployment can consume documents
    authored elsewhere, or turn checks on before the authoring is trusted.
  EOT
}


variable "chat_policy_check_model" {
  type        = string
  default     = "openai.gpt-5.6-luna"
  description = "The policy checks' model id, serving BOTH the curated-question rewrite (minimal effort — extraction) and the judge fleets (reasoning ON but shallow: code-level OKF_CHAT_POLICY_JUDGE_EFFORT, default low on GPT; an 8000-token thinking budget on a pre-adaptive Converse id like Haiku 4.5). An openai.* id is fully supported: chat_mantle_enabled derives the role's Mantle grants from this var too (gated on enable_policy_checks)."
}

variable "policy_preprocess_model" {
  type        = string
  default     = "openai.gpt-5.6-luna"
  description = "Model for the ar_rules.md authoring agent (harvest.ar_author), run with FULL reasoning — turning wiki prose into decidable rules is judgment work, and it runs only when policy sources actually change. Runs exclusively on the harvest runtime (mode=\"ar_rules\"); harvest_mantle_enabled derives the Mantle grants from this var (gated on enable_policy_build). Kept separate from chat_policy_check_model — different services, different env namespaces."
}

variable "tags" {
  type    = map(string)
  default = { project = "okf-on-aws", managed_by = "terraform" }
}

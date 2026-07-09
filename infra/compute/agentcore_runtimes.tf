# The two AgentCore runtimes. Created only when their container image URIs are
# provided (scripts/deploy pushes ARM64 images to ECR first, then passes the
# URIs), so `terraform validate`/`plan` works before images exist.

# --- Harvest runtime (HTTP protocol, port 8080) ------------------------------
# Long crawl runs on a background thread; /ping reports HealthyBusy. Mounts the
# shared okf/-rooted S3 Files access point at /mnt/data (runtime-scoped, VPC
# required) when configured; per-dataset containment is enforced in-process by
# the deepagents FilesystemBackend(virtual_mode=True).

resource "aws_bedrockagentcore_agent_runtime" "harvest" {
  count = var.harvest_image_uri != "" ? 1 : 0

  agent_runtime_name = "${var.name_prefix}_harvest"
  role_arn           = aws_iam_role.harvest.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.harvest_image_uri
    }
  }

  # VPC only when an S3 Files access point is mounted (S3 Files requires VPC
  # networking); otherwise PUBLIC so the runtime can still come up for testing.
  # Gate on harvest_has_fs (plan-known) — NOT the ARN, whose value is apply-time.
  dynamic "network_configuration" {
    for_each = local.harvest_has_fs ? [1] : []
    content {
      network_mode = "VPC"
      network_mode_config {
        subnets         = local.effective_subnet_ids
        security_groups = local.effective_sg_ids
      }
    }
  }
  dynamic "network_configuration" {
    for_each = local.harvest_has_fs ? [] : [1]
    content {
      network_mode = "PUBLIC"
    }
  }

  dynamic "filesystem_configuration" {
    for_each = local.harvest_has_fs ? [1] : []
    content {
      s3_files_access_point {
        access_point_arn = local.harvest_access_point_arn
        mount_path       = "/mnt/data"
      }
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = merge(local.common_env, local.otel_common_env, {
    # AgentCore containers (unlike Lambda) do NOT auto-inject AWS_REGION, and the
    # container code reads AWS_REGION — so set it explicitly here. common_env
    # carries AWS_REGION_NAME for the Lambdas (where AWS_REGION is reserved).
    AWS_REGION           = var.region
    OKF_MOUNT_PATH       = "/mnt/data"
    OKF_ATHENA_WORKGROUP = var.athena_workgroup
    OKF_ATHENA_OUTPUT    = local.athena_output
    # Per-invocation down-scope: the runtime assumes this DATA role with an inline
    # STS session policy pinned to the target database + workgroup, and builds its
    # Glue/Athena clients from those scoped creds (clients.build_scoped_session).
    # This is what collapses the harvest role's Glue/Athena reach from account-wide
    # to the single invoked dataset (threats #9/#60), enforced at IAM.
    OKF_HARVEST_DATA_ROLE_ARN = aws_iam_role.harvest_data.arn
    # Lake Formation-governed catalog? When true, the per-invocation session policy
    # includes lakeformation:GetDataAccess so LF can vend S3 creds for governed
    # table data (must match the data role's identity policy; see clients.py).
    OKF_ENABLE_LAKEFORMATION = var.enable_lakeformation ? "true" : ""
    # Network-isolated Code Interpreter for the run_code tool (extract text from
    # binary .context/ docs). Empty when the feature is disabled -> the agent runs
    # without run_code (build_sandbox returns None on an unset id).
    OKF_CODE_INTERPRETER_ID = var.enable_code_interpreter ? aws_bedrockagentcore_code_interpreter.harvest[0].code_interpreter_id : ""
    # Harvest model. Default: Claude Opus 4.8 (Converse) with adaptive thinking.
    # An openai.* id instead routes to Bedrock Mantle in OKF_HARVEST_MANTLE_REGION
    # (see agent._build_model / _build_mantle_openai). The Mantle region is set
    # unconditionally — it's inert for Converse models and only read for GPT.
    OKF_HARVEST_MODEL         = var.harvest_model
    OKF_HARVEST_MANTLE_REGION = var.harvest_mantle_region
    OKF_HARVEST_EFFORT        = var.harvest_effort
    OKF_HARVEST_MAX_TOKENS    = tostring(var.harvest_max_tokens)
    # Cap concurrent dynamic-subagent (task()) crawls; the rest queue.
    OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY = tostring(var.harvest_max_subagent_concurrency)

    # Observability trajectory identity. service.name is what the CloudWatch
    # GenAI Observability console keys the agent card on.
    OTEL_RESOURCE_ATTRIBUTES = "service.name=${var.name_prefix}_harvest"

    # LangChain/deepagents (LangGraph + ChatBedrockConverse) span capture via the
    # langsmith SDK's NATIVE OTEL bridge — this is what actually emits the LLM /
    # tool / sub-agent spans. Without LANGSMITH_TRACING, LangChain's tracer
    # callback never attaches, no runs are created, and NOTHING feeds the ADOT
    # provider (only ADOT's log instrumentation reaches CloudWatch — which is why
    # we saw gen_ai.* LOGS but aws/spans stayed empty).
    #   - LANGSMITH_TRACING=true    : master switch; attaches the LangChainTracer.
    #   - LANGSMITH_OTEL_ONLY=true  : OTEL-only mode. The SDK reuses the global
    #     TracerProvider that `opentelemetry-instrument` already installed (SigV4
    #     -> X-Ray -> aws/spans) and makes NO call to api.smith.langchain.com.
    # We deliberately do NOT set LANGSMITH_OTEL_ENABLED (that is "hybrid" mode,
    # which also tries the LangSmith cloud ingest), and NO LANGSMITH_API_KEY:
    # because a real global provider exists, the SDK never builds its own
    # LangSmith exporter, so there is zero egress to LangSmith cloud.
    LANGSMITH_TRACING   = "true"
    LANGSMITH_OTEL_ONLY = "true"
    LANGSMITH_PROJECT   = "${var.name_prefix}_harvest"

    # Content capture (reasoning/prompts/tool I/O). langsmith captures by default;
    # HIDE_* = "true" REDACTS. Driven by var.capture_trace_content so the operator
    # flips the whole trajectory on/off in one place (default ON).
    LANGSMITH_HIDE_INPUTS  = var.capture_trace_content ? "false" : "true"
    LANGSMITH_HIDE_OUTPUTS = var.capture_trace_content ? "false" : "true"
  })

  # Harvest sessions can run for hours; allow the 8h max lifetime.
  lifecycle_configuration {
    idle_runtime_session_timeout = 3600  # 60 min idle
    max_lifetime                 = 28800 # 8 h
  }

  tags = var.tags

  # CreateAgentRuntime validates the execution role's permissions, so the inline
  # policy (ECR/logs/xray + s3files) MUST exist AND have propagated first. Wait on
  # the propagation delay (which itself depends on the policy) rather than the
  # policy directly, to avoid the stale-snapshot ValidationException.
  depends_on = [time_sleep.iam_propagation]
}

# --- Consumption MCP runtime (MCP protocol, port 8000/mcp) -------------------
# Stateless streamable-HTTP MCP server, Cognito JWT inbound auth via the same
# OIDC discovery URL the UI + Control API use.

resource "aws_bedrockagentcore_agent_runtime" "consumption" {
  count = var.consumption_image_uri != "" ? 1 : 0

  agent_runtime_name = "${var.name_prefix}_consumption"
  role_arn           = aws_iam_role.consumption.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.consumption_image_uri
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  # Scopes-based inbound auth: trust ANY Cognito token bearing the shared MCP
  # scope, instead of an allowed_clients allowlist. This is what makes credential
  # vending self-serve — a newly created M2M client works the moment it's granted
  # `okf-mcp/invoke`, with NO change here (no per-client drift on terraform apply).
  # The SPA also carries this scope (see durable cognito.tf), so human sessions
  # pass the same check. (Cognito M2M tokens have no `aud`, so allowed_audience
  # is unusable; scope is the right discriminator — see CONVENTIONS.md.)
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url  = local.d.oidc_discovery_url
      allowed_scopes = [local.d.mcp_scope]
    }
  }

  # AgentCore containers don't auto-inject AWS_REGION (the container code reads
  # it); set it explicitly alongside the shared env + the ADOT observability env.
  # No OpenInference vars here — this server is FastMCP, not LangChain.
  environment_variables = merge(local.common_env, local.otel_common_env, {
    AWS_REGION               = var.region
    OTEL_RESOURCE_ATTRIBUTES = "service.name=${var.name_prefix}_consumption"
  })

  tags = var.tags

  # Wait for the exec-role policy to propagate before CreateAgentRuntime
  # validates it (IAM eventual consistency — see time_sleep.iam_propagation).
  depends_on = [time_sleep.iam_propagation]
}

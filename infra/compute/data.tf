data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Durable stack outputs (buckets, index, Cognito, DynamoDB). Root-level outputs
# only — the durable stack re-exports everything the compute stack needs.
data "terraform_remote_state" "durable" {
  backend = "s3"
  config = {
    bucket = var.durable_state_bucket
    key    = var.durable_state_key
    region = var.region
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  d          = data.terraform_remote_state.durable.outputs

  # Common env for the Lambdas + runtimes (names frozen in docs/CONVENTIONS.md).
  common_env = {
    AWS_REGION_NAME       = var.region
    OKF_ACCOUNT_ID        = local.account_id
    OKF_BUNDLE_BUCKET     = local.d.bundle_bucket
    OKF_VECTOR_BUCKET     = local.d.vector_bucket
    OKF_VECTOR_INDEX      = local.d.vector_index
    OKF_REGISTRY_TABLE    = local.d.registry_table
    OKF_FRESHNESS_TABLE   = local.d.freshness_table
    OKF_ANNOTATIONS_TABLE = local.d.annotations_table
  }

  # Source dirs for the Lambda deployment packages (populated by
  # scripts/build_lambdas.sh, which vendors okf_core + okf_aws in).
  build_root = "${path.root}/.build/packages"

  # The harvest runtime's CloudWatch log group, read back by the Control API for
  # the live step feed. AgentCore ships a runtime's stdout to
  # /aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>, where <runtime-id>
  # is the last path segment of the runtime ARN and the DEFAULT qualifier maps to
  # the "DEFAULT" endpoint. Derived here; overridable via var.harvest_log_group
  # if a given account's naming differs. Empty string (no runtime yet) disables
  # the feed gracefully — the events handler then returns an empty batch.
  _harvest_runtime_id = try(
    element(split("/", aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn), 1),
    "",
  )
  harvest_log_group = (
    var.harvest_log_group != "" ? var.harvest_log_group :
    local._harvest_runtime_id != "" ?
    "/aws/bedrock-agentcore/runtimes/${local._harvest_runtime_id}-DEFAULT" : ""
  )

  # Resources for scoping bedrock-agentcore:InvokeAgentRuntime on the control_api
  # and incremental roles down from "*" to the one harvest runtime they invoke.
  # The runtime ARN is apply-time (AWS-generated runtime id), so it can't be built
  # from name_prefix; before the image exists (no runtime) we fall back to the
  # account's own AgentCore runtime namespace, not "*". We grant the runtime ARN
  # AND its "/*" sub-resources because a DEFAULT-qualifier invoke authorizes
  # HIERARCHICALLY against both the runtime and its endpoint resource
  # (arn:...:runtime/<id>/endpoint/<id>) — the bare runtime ARN alone would
  # AccessDeny every harvest.
  harvest_runtime_arn = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
  harvest_invoke_resources = (
    local.harvest_runtime_arn != "" ?
    [local.harvest_runtime_arn, "${local.harvest_runtime_arn}/*"] :
    ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:runtime/*"]
  )

  ui_bucket = var.ui_bucket_name != "" ? var.ui_bucket_name : "${var.name_prefix}-ui-${local.account_id}"

  # OTEL/ADOT env shared by BOTH AgentCore runtime containers. These are
  # self-built images (not AgentCore-CLI builds); opentelemetry-instrument + the
  # installed aws-opentelemetry-distro run in agent-observability mode.
  # AGENT_OBSERVABILITY_ENABLED=true is the load-bearing switch that routes OTLP
  # telemetry to CloudWatch for the GenAI Observability console.
  #
  # CRITICAL — traces vs logs are INDEPENDENT export paths in ADOT. Logs resolve
  # their endpoint automatically (they flow fine), but the TRACES path silently
  # no-ops if OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is unresolved: the configurator
  # does `if not traces_endpoint: return`, dropping 100% of spans with NO error
  # (symptom: aws/spans empty + X-Ray 0 traces while logs still arrive). So we
  # PIN the X-Ray traces endpoint explicitly → forces the SigV4-signed
  # OTLPAwsSpanExporter. Do NOT set the generic OTEL_EXPORTER_OTLP_ENDPOINT (it
  # suppresses the auto per-signal AWS endpoints) and do NOT set
  # OTEL_TRACES_SAMPLER (agent mode exports 100% via BatchUnsampledSpanProcessor,
  # independent of the sampler — so `otelTraceSampled:false` is expected/benign).
  otel_common_env = {
    AGENT_OBSERVABILITY_ENABLED        = "true"
    OTEL_PYTHON_DISTRO                 = "aws_distro"
    OTEL_PYTHON_CONFIGURATOR           = "aws_configurator"
    OTEL_EXPORTER_OTLP_PROTOCOL        = "http/protobuf"
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = "https://xray.${var.region}.amazonaws.com/v1/traces"

    # Silence the OTEL LOGS signal (the voluminous gen_ai.* event records the SDK
    # ships to the runtime log group). TRACES are a separate pipeline and are
    # unaffected — spans still flow to aws/spans, and message content still rides
    # on the spans via the langsmith bridge (LANGSMITH_HIDE_* controls it). Also
    # disables the Python-log -> OTEL bridge so app INFO logs aren't re-emitted as
    # OTEL records. Plain container stdout still reaches the log group.
    # NOTE: if the console Traces view loses prompt/completion content after this,
    # flip OTEL_LOGS_EXPORTER back to the default (unset) — it's env-only, no
    # image rebuild (update-agent-runtime). Content-on-spans is expected to
    # survive but was not source-verified.
    OTEL_LOGS_EXPORTER                               = "none"
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED = "false"
  }
}

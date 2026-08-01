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

  # Secrets Manager resources the Redshift GetSecretValue grants are scoped to
  # (harvest data role, Control API role, chat role). Per-mapping secrets can't be
  # enumerated at deploy time, so the ceiling is a NAME-PREFIX pattern instead of
  # "*" — only secrets named "<prefix>*" are usable as Redshift connection
  # secrets. "" prefix -> any secret (arn:...:secret:*). The trailing "-??????"
  # random suffix Secrets Manager appends is covered by the "*".
  redshift_secret_resources = [
    "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.redshift_secret_name_prefix}*",
  ]

  # --- Automated Reasoning (AR) policy checks ---------------------------------
  # AR policies + per-dataset guardrails are RUNTIME-managed via boto3 (there is
  # no aws_bedrock_automated_reasoning_policy resource in hashicorp/aws ~> 6.0 —
  # only awscc has one; aws_bedrock_guardrail DOES exist, but the per-dataset
  # guardrails are created dynamically per registered dataset, so they stay
  # runtime-managed too — the S3 Vectors posture. Do not "fix" this later).
  # Terraform contributes IAM only, so these are ARN PATTERNS over the account's
  # own namespace, not references to TF-managed resources.
  ar_policy_resources = [
    "arn:aws:bedrock:${var.region}:${local.account_id}:automated-reasoning-policy/*",
  ]
  ar_guardrail_resources = [
    "arn:aws:bedrock:${var.region}:${local.account_id}:guardrail/*",
  ]

  # A guardrail carrying an AR policy REQUIRES a cross-region guardrail profile
  # (guardrailProfileIdentifier, e.g. us.guardrail.v1:0) — omitting it is a
  # ValidationException. ApplyGuardrail then authorizes against the guardrail in
  # the SOURCE region AND the profile object in EVERY DESTINATION region of that
  # profile (AWS "Permissions for using cross-Region inference with Amazon
  # Bedrock Guardrails"). A missing destination is an AccessDenied at check
  # time, so the destination set is enumerated per source region here. Keep
  # this map consistent with var.policy_guardrail_profile — an EU profile with
  # US destinations (or vice versa) fails at runtime, not at plan.
  ar_profile_destinations = {
    "us-east-1"    = ["us-east-1", "us-east-2", "us-west-2"]
    "us-east-2"    = ["us-east-1", "us-east-2", "us-west-2"]
    "us-west-2"    = ["us-east-1", "us-east-2", "us-west-2"]
    "eu-central-1" = ["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"]
    "eu-west-1"    = ["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"]
    "eu-west-3"    = ["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"]
  }
  # The profile FAMILY must match the deployment region (live-verified:
  # CreateGuardrail in eu-west-1 with us.guardrail.v1:0 is a
  # ValidationException). Empty var -> derive from the region.
  ar_guardrail_profile = (
    var.policy_guardrail_profile != "" ? var.policy_guardrail_profile :
    startswith(var.region, "eu-") ? "eu.guardrail.v1:0" : "us.guardrail.v1:0"
  )
  ar_guardrail_profile_resources = [
    for r in lookup(local.ar_profile_destinations, var.region, [var.region]) :
    "arn:aws:bedrock:${r}:${local.account_id}:guardrail-profile/${local.ar_guardrail_profile}"
  ]

  # AR checks are region-limited (six regions as of mid-2026 — notably NOT
  # ap-southeast-2). In an unsupported region the feature degrades to ABSENT
  # (the build hook no-ops with ar_build_status="unsupported_region" and
  # policy_check reports "no policy"), and the grants are withheld too.
  ar_supported_regions = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-central-1", "eu-west-1", "eu-west-3",
  ]
  ar_enabled       = var.enable_policy_checks && contains(local.ar_supported_regions, var.region)
  ar_build_enabled = local.ar_enabled && var.enable_policy_build

  # The default EventBridge bus — this stack creates no custom bus; every rule
  # (reindex, glue_table_change, policy_rebuild) lives on "default".
  event_bus_arn  = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default"
  event_bus_name = "default"

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

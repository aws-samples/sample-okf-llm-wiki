# Long-term chat memory — Bedrock AgentCore Memory (per-user preferences +
# question→computation bindings). DURABLE by lifecycle: memory records are user
# state, they must survive compute redeploys exactly like the chat threads.
#
# The design contract (Roadmap §10): memory stores facts about the USER, never
# facts about the data — tables/joins/metrics belong to the wiki, and a recalled
# binding is only a HINT until the chat runtime re-validates the computation it
# points at (VERIFIED + hash) at use time.
#
# awscc, not hashicorp/aws, ON PURPOSE (the one exception to the aws-native
# rule — see versions.tf): only the Cloud Control schema exposes the strategy's
# per-record METADATA SCHEMA, which is what makes `type`/`dataset`/`expires_at`
# real filterable record fields instead of a parsed header convention. The
# runtime + control API still parse a legacy content header as a FALLBACK
# (okf_core/memory_records.py) for records whose extraction drifted.
#
# One CUSTOM strategy on the user-preference base; one namespace per user
# (actorId = Cognito sub). Dataset scoping is server-side at retrieval
# (metadataFilters on `dataset`), TTL stays client-side lazy-delete (an
# "unset OR future" filter isn't expressible — filters AND together).

# The extraction/consolidation model. AgentCore Memory invokes it inside the
# managed pipeline (async, minutes after events land) — latency is free here,
# so quality wins: the acceptance judgment on bindings (did the user's
# follow-up accept the interpretation?) and the dataset-specificity call are
# real reading-comprehension work, not classification.
variable "chat_memory_model" {
  type        = string
  description = "Model id for AgentCore Memory extraction + consolidation (custom strategy overrides)."
  default     = "global.anthropic.claude-sonnet-5"
}

locals {
  # Everything the extractor is allowed to remember. This prompt IS the
  # design's wiki-conflict firewall — see Roadmap §10 before loosening it.
  # APPENDED to the built-in user-preference prompt; the metadata fields have
  # their own extraction instructions in the metadata_schema below.
  chat_memory_extraction_prompt = <<-EOT
    Additional rules for this deployment (they override anything more permissive):

    Extract ONLY these three kinds of memory, and nothing else:
    1. STATED user preferences — presentation, workflow, language, report style
       (e.g. "prefers tables over charts", "answers in German"), AND the
       MEANING the user assigns to terms they use: how THEY define a measure,
       term, or comparison (e.g. "most successful driver means most world
       championships won", "by group I mean the EMEA org", "compare means
       month over month"). A meaning stated in a clarification answer is a
       preference to keep.
    2. PERSONAL CONTEXT the user states about themselves: their name, role,
       team, or how they want to be addressed (e.g. "my name is Edvin",
       "I'm on the EMEA analytics team"). Only what the USER says about
       themselves — never inferred, never about other people.
    3. BINDINGS: how this user's recurring question maps to a governed artifact.
       Extract a binding ONLY when a [[okf-harness]] annotation in the events
       shows the turn was resolved by run_computation or query_metric AND the
       user's following turn accepted the answer (no correction, no rephrasing
       of the same question). A correction such as "no, I meant ..." means: do
       NOT extract the binding.

    Clarifying-question exchanges (the assistant's "(clarifying question) ..."
    messages and the user's replies) are the STRONGEST evidence: the user is
    explicitly stating what they mean. A preference or interpretation the user
    gave in a clarification answer counts as accepted.

    NEVER extract facts about the data itself — table locations, join keys,
    column meanings, metric definitions, data caveats. Those belong to the wiki
    and must never enter memory.

    For a BINDING, the "preference" field of your output must name the
    computation or metric slug EXACTLY as the annotation shows it, plus the
    parameter SHAPE in words (e.g. "latest full month compared to the month
    before") — never literal parameter values such as concrete months or dates.
  EOT

  chat_memory_consolidation_prompt = <<-EOT
    Additional consolidation rules for this deployment:
    - The user's latest ACCEPTED statement or interpretation supersedes older
      records that mean the same thing — update in place, do not duplicate.
    - A restated preference refreshes the existing record.
    - When only the validity window changes ("actually until year end"), keep
      the record and update its expires_at metadata.
  EOT
}

# A memory with CUSTOM strategies invokes our chosen Bedrock model inside the
# managed extraction/consolidation pipeline — CreateMemory REQUIRES an
# execution role for that (found live: "Please provide memoryExecutionRoleArn
# as memory contains one or more Custom strategies"). Trust follows the
# AgentCore service-role pattern (service principal + SourceAccount/SourceArn
# confinement); permissions are AWS's purpose-built managed policy for memory
# model inference.
resource "aws_iam_role" "chat_memory_execution" {
  name = "${var.name_prefix}-chat-memory-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["bedrock-agentcore.amazonaws.com"] }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        # The service VALIDATES this trust policy at CreateMemory and expects
        # the documented shape — a narrower `memory/*` SourceArn is REJECTED
        # ("Please provide a role with a valid trust policy", found live).
        # Account+region confinement still holds via the wildcard + account.
        ArnLike = {
          "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "chat_memory_inference" {
  role       = aws_iam_role.chat_memory_execution.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

resource "awscc_bedrockagentcore_memory" "chat" {
  name        = "${replace(var.name_prefix, "-", "_")}_chat_memory"
  description = "Per-user long-term chat memory: stated preferences + question-to-computation bindings"

  memory_execution_role_arn = aws_iam_role.chat_memory_execution.arn

  # IAM is eventually consistent — the policy must be attached before
  # CreateMemory validates the role, and a fresh role can still take a few
  # seconds to propagate (re-apply on a transient AccessDenied).
  depends_on = [aws_iam_role_policy_attachment.chat_memory_inference]

  # Raw conversational events (short-term memory) are extraction feedstock, not
  # the transcript of record — the chat checkpointer owns transcripts. 30 days
  # comfortably covers the async extraction pipeline + debugging windows.
  event_expiry_duration = 30

  # Indexed = usable in retrieval metadataFilters (the runtime's server-side
  # dataset scoping + the type split). expires_at is deliberately NOT indexed:
  # "unset OR future" isn't expressible in AND-composed filters, so TTL is a
  # client-side check either way (chat.memory lazy-deletes on recall).
  indexed_keys = [
    { key = "type", type = "STRING" },
    { key = "dataset", type = "STRING" },
  ]

  memory_strategies = [{
    custom_memory_strategy = {
      name        = "wiki_preferences"
      description = "Stated preferences + question-to-artifact bindings, per user"

      # One namespace per user (actorId = the Cognito sub).
      namespaces = ["wiki/{actorId}"]

      configuration = {
        user_preference_override = {
          extraction = {
            append_to_prompt = local.chat_memory_extraction_prompt
            model_id         = var.chat_memory_model
          }
          consolidation = {
            append_to_prompt = local.chat_memory_consolidation_prompt
            model_id         = var.chat_memory_model
          }
        }
      }

      # The structured record fields. All three are LLM_INFERRED on purpose:
      # a strictly-consistent event value would stamp EVERY record extracted
      # from that event (a generic "prefers tables" said in a pinned chat must
      # NOT inherit the pin) — the [[okf-harness]] annotation gives the model
      # the observed facts, the instructions below bound its choices.
      memory_record_schema = {
        metadata_schema = [
          {
            key  = "type"
            type = "STRING"
            extraction_config = {
              llm_extraction_config = {
                definition                 = "The memory kind: a stated user preference, personal context about the user, or a binding from a recurring question to a governed artifact."
                llm_extraction_instruction = "Use exactly 'binding' for question-to-artifact bindings, 'personal' for personal context the user states about themselves (name, role, team), and 'stated' for everything else."
                validation = {
                  string_validation = { allowed_values = ["stated", "binding", "personal"] }
                }
              }
            }
          },
          {
            key  = "dataset"
            type = "STRING"
            extraction_config = {
              llm_extraction_config = {
                definition                 = "The domain/dataset this memory is specific to, when it is specific to one."
                llm_extraction_instruction = "For bindings, copy the dataset value from the [[okf-harness]] annotation VERBATIM. For stated preferences, set it ONLY when the preference is specific to one dataset's semantics, and only to a value in the annotation's datasets-cited list (or its datasets-touched list when no cited line is present — the harness emits one or the other, cited being the answer's own validated attribution). Otherwise OMIT this field entirely. Never invent a dataset id from conversation wording."
              }
            }
          },
          {
            key  = "expires_at"
            type = "STRING"
            extraction_config = {
              llm_extraction_config = {
                definition                 = "The ABSOLUTE date (YYYY-MM-DD) after which this memory no longer applies, when the user stated or clearly implied a validity window."
                llm_extraction_instruction = "Resolve relative windows ('until end of September', 'this quarter') against the event timestamp into a YYYY-MM-DD date. OMIT this field when no validity window was expressed."
              }
            }
          },
        ]
      }
    }
  }]
}

# Custom AgentCore Code Interpreter for the harvest agent, in SANDBOX network
# mode — network-ISOLATED (no internet egress). The harvest agent uses it only to
# extract text from uploaded .context/ docs whose binary formats the built-in
# read_file can't decode (PDF/DOCX/PPTX/XLSX). Network isolation is the right
# posture here: .context/ content is UNTRUSTED (authored by upstream parties), so
# the code parsing it must have no path to exfiltrate or fetch anything. The
# managed default (aws.codeinterpreter.v1) has internet access, so we do NOT use
# it. Gated on var.enable_code_interpreter so validate/plan and CI-less regions
# still work — the harvest degrades to text-only .context reading when absent.

# SANDBOX mode REQUIRES an execution role. Deliberately MINIMAL: the sandbox only
# runs the agent's extraction Python against files we upload into the session. It
# gets NONE of the harvest role's Glue/Athena/bundle/Bedrock grants — credential
# isolation (the ticket's guardrail). Only the baseline logs the service needs.
data "aws_iam_policy_document" "code_interpreter_assume" {
  count = var.enable_code_interpreter ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*"]
    }
  }
}

data "aws_iam_policy_document" "code_interpreter_exec" {
  count = var.enable_code_interpreter ? 1 : 0
  # Just enough for the sandbox to emit its own logs. NO data-plane grants.
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/code-interpreter/*"]
  }
  statement {
    sid       = "LogGroup"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/code-interpreter/*"]
  }
}

resource "aws_iam_role" "code_interpreter" {
  count              = var.enable_code_interpreter ? 1 : 0
  name               = "${var.name_prefix}-code-interpreter"
  assume_role_policy = data.aws_iam_policy_document.code_interpreter_assume[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "code_interpreter" {
  count  = var.enable_code_interpreter ? 1 : 0
  name   = "code-interpreter-policy"
  role   = aws_iam_role.code_interpreter[0].id
  policy = data.aws_iam_policy_document.code_interpreter_exec[0].json
}

resource "aws_bedrockagentcore_code_interpreter" "harvest" {
  count              = var.enable_code_interpreter ? 1 : 0
  name               = "${var.name_prefix}_harvest_ci"
  description        = "Network-isolated sandbox for the harvest agent to extract text from binary .context/ docs."
  execution_role_arn = aws_iam_role.code_interpreter[0].arn

  network_configuration {
    network_mode = "SANDBOX"
  }

  tags = var.tags
}

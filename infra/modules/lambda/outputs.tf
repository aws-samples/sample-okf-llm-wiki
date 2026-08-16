output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_arn" {
  value = aws_lambda_function.this.arn
}

output "invoke_arn" {
  # Point integrations at the alias when provisioned concurrency is on, so
  # invocations hit the pre-warmed instances rather than $LATEST.
  value = var.provisioned_concurrency > 0 ? aws_lambda_alias.live[0].invoke_arn : aws_lambda_function.this.invoke_arn
}

# Alias name to use as the qualifier on lambda:InvokeFunction permissions when
# provisioned concurrency is on; null (unqualified) otherwise.
output "qualifier" {
  value = var.provisioned_concurrency > 0 ? aws_lambda_alias.live[0].name : null
}

output "role_arn" {
  value = aws_iam_role.this.arn
}

output "role_name" {
  value = aws_iam_role.this.name
}

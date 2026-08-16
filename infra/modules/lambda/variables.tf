variable "name" {
  type = string
}

variable "handler" {
  type    = string
  default = "handler.lambda_handler"
}

variable "runtime" {
  type    = string
  default = "python3.12"
}

variable "source_dir" {
  type        = string
  description = "Directory to zip as the deployment package (built by the packaging step)."
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "timeout" {
  type    = number
  default = 60
}

variable "memory_size" {
  type    = number
  default = 512
}

variable "policy_json" {
  type        = string
  description = "IAM policy document (JSON) for the function's least-privilege permissions."
}

variable "provisioned_concurrency" {
  type        = number
  default     = 0
  description = <<-EOT
    Number of provisioned (pre-warmed) execution environments to keep hot,
    to minimize cold starts. When > 0 the function is published as an
    immutable version fronted by a "live" alias, and provisioned concurrency
    is configured on that alias. Callers MUST invoke via `invoke_arn`
    (which points at the alias when this is set) for the warm instances to
    be used. 0 disables it (plain $LATEST, no version churn).
  EOT
}

variable "tags" {
  type    = map(string)
  default = {}
}

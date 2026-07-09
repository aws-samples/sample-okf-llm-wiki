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

variable "tags" {
  type    = map(string)
  default = {}
}

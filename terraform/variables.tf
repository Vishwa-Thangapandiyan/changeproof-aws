variable "offline" {
  description = "Phase 0 mode: configure the provider with mock credentials so plan works with no AWS account. Set false only to actually apply, from the experiment driver."
  type        = bool
  default     = true
}

variable "region" {
  description = "AWS region. Used for provider configuration only; Phase 0 makes no AWS calls."
  type        = string
  default     = "us-east-1"
}

variable "environment_suffix" {
  description = "Isolation suffix for every resource name. Each test environment gets its own."
  type        = string
  default     = "demo"
}

variable "reserved_concurrency" {
  description = "Reserved concurrency for the api function. The first demo change moves this from 10 to 100."
  type        = number
  default     = 10

  validation {
    condition     = var.reserved_concurrency > 0
    error_message = "reserved_concurrency must be positive; the prediction rules do not model unreserved or zero concurrency."
  }
}

variable "worker_concurrency" {
  description = "Reserved concurrency for the worker function. Deliberately left low so the demo change creates a producer/consumer imbalance."
  type        = number
  default     = 5
}

variable "lambda_package_path" {
  description = "Path to the deployment zip. A placeholder in Phase 0, which never applies this stack."
  type        = string
  default     = "./build/function.zip"
}

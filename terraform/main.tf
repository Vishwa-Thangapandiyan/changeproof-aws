# ChangeProof demo target stack.
#
# This is the application that the proposed change is applied TO. It is not the
# ChangeProof control plane; that arrives in Phase 1 under terraform/platform/.
#
# The stack exists primarily so `terraform plan` can produce genuine plan JSON for
# the parser. In Phase 0 it is NEVER applied. See scripts/generate_plan.sh.
#
# Dependency chain, mirrored by the graph fixture in
# backend/lambda/changeproof/fixtures/dependency_graph.json:
#
#   aws_lambda_function.api
#     -> aws_sqs_queue.work
#       -> aws_lambda_function.worker
#         -> aws_dynamodb_table.orders
#
# DynamoDB is the terminal datastore rather than RDS deliberately: it is on-demand
# billed and free-tier eligible, where RDS is neither. See CLAUDE.md section 4.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Provider configuration, in two modes.
#
# offline = true (the default) is the Phase 0 mode. The placeholder credentials and
# skip_* flags exist so that `terraform plan -refresh=false` runs with no AWS
# account, no credentials and no network calls to AWS. Without them the provider
# calls STS GetCallerIdentity and the EC2 instance metadata endpoint during
# configuration. In this mode an apply would fail, which is the intent.
#
# offline = false is Phase 1. Every override becomes null, meaning "unset", so the
# provider falls back to the normal credential chain and the stack can actually be
# applied. Only the experiment driver sets this, and only behind an explicit
# --authorize-aws-spend flag.
provider "aws" {
  region = var.region

  access_key = var.offline ? "mock_access_key" : null
  secret_key = var.offline ? "mock_secret_key" : null

  skip_credentials_validation = var.offline
  skip_requesting_account_id  = var.offline
  skip_metadata_api_check     = var.offline
  skip_region_validation      = var.offline

  default_tags {
    tags = {
      Project     = "changeproof"
      Environment = var.environment_suffix
      ManagedBy   = "terraform"
      Ephemeral   = "true"
      Experiment  = var.environment_suffix
    }
  }
}

locals {
  prefix = "changeproof-${var.environment_suffix}"
}

# --- the request handler: the resource the demo change modifies ----------------------

resource "aws_lambda_function" "api" {
  function_name = "${local.prefix}-api"
  role          = aws_iam_role.api.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = var.lambda_package_path
  memory_size   = 512
  timeout       = 15

  # The first demo change moves this from 10 to 100.
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = {
      QUEUE_URL = aws_sqs_queue.work.url
    }
  }
}

# --- the work buffer -----------------------------------------------------------------

resource "aws_sqs_queue" "work" {
  name                       = "${local.prefix}-work"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 3600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "work_dlq" {
  name                      = "${local.prefix}-work-dlq"
  message_retention_seconds = 86400
}

# --- the consumer: deliberately left at low concurrency ------------------------------
#
# The worker's reserved concurrency stays at var.worker_concurrency while the api
# scales. This asymmetry is the demo scenario: the producer scales, the consumer
# does not, and the queue absorbs the difference until it cannot.

resource "aws_lambda_function" "worker" {
  function_name = "${local.prefix}-worker"
  role          = aws_iam_role.worker.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = var.lambda_package_path
  memory_size   = 512
  timeout       = 30

  reserved_concurrent_executions = var.worker_concurrency

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.orders.name
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 10
}

# --- the terminal datastore ----------------------------------------------------------

resource "aws_dynamodb_table" "orders" {
  name         = "${local.prefix}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "orderId"

  attribute {
    name = "orderId"
    type = "S"
  }
}

# --- IAM: least privilege, scoped to this stack's resources --------------------------

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.prefix}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "api" {
  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.prefix}-api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.work.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "${local.prefix}-worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# --- logs ----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.prefix}-api"
  retention_in_days = 3
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.prefix}-worker"
  retention_in_days = 3
}

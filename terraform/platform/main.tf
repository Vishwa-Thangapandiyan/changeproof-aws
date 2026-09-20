terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  bucket_name = "changeproof-evidence-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"
  table_name  = "changeproof-experiments"
}

variable "aws_region" {
  description = "AWS region for ChangeProof resources."
  type        = string
  default     = "ap-south-2"
}

resource "aws_s3_bucket" "evidence" {
  bucket = local.bucket_name

  tags = {
    Project = "ChangeProof"
    Purpose = "Experiment evidence storage"
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-evidence-after-30-days"
    status = "Enabled"

    filter {
      prefix = "experiments/"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_dynamodb_table" "experiments" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  attribute {
    name = "GSI2PK"
    type = "S"
  }

  attribute {
    name = "GSI2SK"
    type = "S"
  }

  global_secondary_index {
    name = "GSI1"
    key_schema {
      attribute_name = "GSI1PK"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "GSI1SK"
      key_type       = "RANGE"
    }
    projection_type = "INCLUDE"

    # GSI1PK/GSI1SK are index keys and are therefore automatically projected.
    # status and createdAt are also represented by those keys and cannot be
    # repeated in NonKeyAttributes.
    non_key_attributes = [
      "verdict",
      "observedSeverity",
      "predictedSeverity",
      "predictionAccuracy",
      "changeSummary",
      "reason",
      "evidenceKey",
      "simulated"
    ]
  }

  global_secondary_index {
    name = "GSI2"
    key_schema {
      attribute_name = "GSI2PK"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "GSI2SK"
      key_type       = "RANGE"
    }
    projection_type = "INCLUDE"

    # GSI2PK/GSI2SK are automatically projected. resourceAddress, metric,
    # predictions and outcomes are explicitly projected below.
    non_key_attributes = [
      "resourceAddress",
      "metric",
      "predictedLow",
      "predictedHigh",
      "baselineValue",
      "observedValue",
      "actualChangePct",
      "outcome",
      "simulated"
    ]
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Project = "ChangeProof"
    Purpose = "Experiment metadata and measurements"
  }
}

output "evidence_bucket_name" {
  value = aws_s3_bucket.evidence.bucket
}

output "experiments_table_name" {
  value = aws_dynamodb_table.experiments.name
}

output "aws_region" {
  value = data.aws_region.current.region
}

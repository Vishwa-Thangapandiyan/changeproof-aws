# Outputs are the contract between the target stack and the telemetry collector.
# Phase 1's CloudWatch adapter reads these to know which dimensions to query.

output "api_function_name" {
  description = "Lambda function name for CloudWatch FunctionName dimension."
  value       = aws_lambda_function.api.function_name
}

output "worker_function_name" {
  description = "Lambda function name for CloudWatch FunctionName dimension."
  value       = aws_lambda_function.worker.function_name
}

output "work_queue_name" {
  description = "SQS queue name for CloudWatch QueueName dimension."
  value       = aws_sqs_queue.work.name
}

output "orders_table_name" {
  description = "DynamoDB table name for CloudWatch TableName dimension."
  value       = aws_dynamodb_table.orders.name
}

output "graph_addresses" {
  description = "Terraform addresses in this stack, matching the node addresses in the dependency graph fixture."
  value = [
    "aws_lambda_function.api",
    "aws_sqs_queue.work",
    "aws_lambda_function.worker",
    "aws_dynamodb_table.orders",
  ]
}

# --- identifiers the experiment driver hands to the pipeline -------------------------
#
# Names above are what CloudWatch dimensions need. ARNs, the queue URL and the log
# group names are what everything else needs: the driver to invoke and to poll queue
# depth, an operator to find what was created, and cleanup to prove it is gone.

output "region" {
  description = "Region the experiment was provisioned in."
  value       = var.region
}

output "api_function_arn" {
  value = aws_lambda_function.api.arn
}

output "worker_function_arn" {
  value = aws_lambda_function.worker.arn
}

output "work_queue_url" {
  description = "Queue URL. The driver polls this between runs to confirm the queue has drained."
  value       = aws_sqs_queue.work.url
}

output "work_queue_arn" {
  value = aws_sqs_queue.work.arn
}

output "work_dlq_url" {
  description = "Dead letter queue. A non-empty DLQ means messages failed processing and the run is suspect."
  value       = aws_sqs_queue.work_dlq.url
}

output "orders_table_arn" {
  value = aws_dynamodb_table.orders.arn
}

output "api_log_group" {
  value = aws_cloudwatch_log_group.api.name
}

output "worker_log_group" {
  value = aws_cloudwatch_log_group.worker.name
}

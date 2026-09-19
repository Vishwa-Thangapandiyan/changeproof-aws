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

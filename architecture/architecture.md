# ChangeProof Architecture

## Goal

ChangeProof verifies the impact of proposed AWS infrastructure
changes before they reach production.

## Core flow

1. Developer submits a Terraform change.
2. ChangeProof parses the proposed infrastructure change.
3. The system identifies affected AWS resources.
4. Neptune represents dependencies between resources.
5. A prediction is generated from the dependency graph and change.
6. ChangeProof creates an isolated test environment.
7. The proposed change is applied to the test environment.
8. Synthetic workload is generated.
9. CloudWatch and X-Ray/OpenTelemetry collect telemetry.
10. The observed behavior is compared with the prediction.
11. The result is stored.
12. Bedrock generates a human-readable explanation.
13. The developer receives the verification result.

## Core AWS services

- API Gateway — API entry point
- Lambda — event-driven backend functions
- Step Functions — workflow orchestration
- Neptune — infrastructure dependency graph
- IAM — permission boundaries
- CloudWatch — metrics/logs
- CloudTrail — AWS API activity
- X-Ray/OpenTelemetry — distributed tracing
- S3 — experiment artifacts
- DynamoDB — experiment state
- Bedrock — explanation layer

## Design principle

Bedrock should explain evidence, not invent evidence.

The verification decision should be based on measurable
observations and predefined thresholds/rules.
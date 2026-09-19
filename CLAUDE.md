# CLAUDE.md — ChangeProof

## 1. Project purpose

ChangeProof is an **AWS-native infrastructure change verification system**.

> Don't just review the change. **Prove it.**

It takes a proposed Terraform infrastructure change, understands the affected AWS
dependency graph, predicts the impact, creates an isolated test environment, applies
the change there, generates representative workload, observes actual AWS telemetry,
compares predicted vs actual, and produces a safe approve/reject decision.

It is **not** "an AI that reviews Terraform". The differentiator is experimental
verification against real AWS behaviour.

Source of truth for design: `ChangeProof.md`, `ChangeProof_AWS_Services_Explained.md`,
`architecture/architecture.md`. Do not diverge from these without an explicit decision
from the user.

## 2. Architecture

Core flow (each arrow is a discrete, inspectable stage):

```
Terraform change
  → parse change
  → dependency graph
  → impact prediction
  → isolated test environment
  → apply change
  → synthetic workload
  → CloudWatch / X-Ray / CloudTrail telemetry
  → predicted vs actual
  → approve / reject
  → Bedrock explanation
```

Service responsibilities:

| Service | Role |
|---|---|
| API Gateway | Entry point (`POST /changes`, `GET /changes/{id}`, `GET /results/{id}`) |
| Lambda | Individual workflow steps (short, event-driven, single-purpose) |
| Step Functions | Orchestration, retries, branching, timeouts, workflow state |
| Amazon Neptune | Dependency graph — what is connected to what |
| CloudWatch | Observed metrics — what actually happened |
| X-Ray / OTel | Where latency was spent |
| CloudTrail | What AWS API changes occurred, by whom, when |
| S3 | Evidence artifacts (plan, prediction, metrics, traces, report) |
| DynamoDB | Experiment state and metadata |
| Bedrock | Human-readable **explanation of evidence only** |
| IAM | Hard boundary between ChangeProof and production |
| Terraform | The input (proposed change) and the test-environment provisioner |

### Architectural rule (non-negotiable)

**Bedrock is an explanation layer, NOT the source of truth.**

The prediction and the approve/reject decision must come from:
- deterministic analysis of the parsed Terraform change,
- dependency graph information,
- measurable telemetry,
- explicit, documented thresholds and rules.

A model may never introduce a number, a metric, a dependency or a verdict that the
deterministic layer did not already produce. If an explanation cannot be grounded in
stored evidence, emit no explanation rather than an invented one.

## 3. MVP scope

One change type, end-to-end, is worth more than ten partial features.

**First demo change: Lambda reserved concurrency 10 → 100.**

In scope now:

1. Terraform change parser
2. Dependency representation
3. Basic impact prediction (rules + thresholds)
4. Step Functions workflow skeleton
5. Isolated test-environment concept
6. Telemetry collection concept
7. Predicted-vs-actual comparison
8. Evidence storage
9. Bedrock explanation

Repository layout:

```
architecture/architecture.md     design notes
backend/lambda/handler.py        Lambda entry point(s)
backend/state_machine/workflow.json  Step Functions ASL definition
terraform/                       demo target infra + test-env definitions
```

## 4. Cost constraints (hard limit: $100 AWS credits)

This is a binding design constraint, not a preference. Every decision is made
local-first; AWS is contacted only when a stage genuinely cannot be proven without it.

**Never provision Amazon Neptune.** It has no free tier, bills while idle at roughly
$60/month, and would consume the entire budget. The dependency graph is a local Python
module behind a Neptune-shaped interface (`query_dependents`, `query_path`). Swapping
the backend later is one module. Same rule for RDS: model database pressure through SQS
depth and Lambda throttles instead.

| Service | Budget status |
|---|---|
| Neptune, RDS | **Forbidden** — no free tier, bills while idle |
| Lambda, Step Functions, S3, DynamoDB, API Gateway | Safe — free tier covers hackathon volume |
| CloudWatch, Bedrock | Safe — cents at this scale |
| `terraform plan` | Free — creates nothing |

### Offline Terraform plan generation

Real plan JSON can be produced with zero AWS contact and zero cost:

- AWS provider configured with mock keys plus `skip_credentials_validation`,
  `skip_requesting_account_id`, `skip_metadata_api_check`, `skip_region_validation`
- a committed `terraform.tfstate` representing the already-deployed stack at
  `reserved_concurrent_executions = 10`
- `terraform plan -refresh=false -var reserved_concurrency=100 -out=tfplan`
- `terraform show -json tfplan`

`-refresh=false` performs no AWS reads. The result is a genuine `update` action with
real before/after values. The parser consumes real Terraform output, not a fixture.

## 5. Build phases

**Phase 0 (current) — local only, no AWS account involvement.**

The binding rule:

> Everything from proposed Terraform change to prediction to verification to verdict
> must run on a laptop with **zero AWS credentials and zero AWS spend**.

Phase 0 forbids, with no exceptions:

- any `boto3` call, and any `boto3` import on a reachable code path
- S3, DynamoDB, Bedrock, CloudWatch, CloudTrail, Neptune, X-Ray — any AWS API at all
- anything that requires AWS credentials, a profile, a region, or a role
- any network call to an AWS endpoint

The Phase 0 pipeline is end-to-end local:

```
Terraform plan JSON (offline)
  -> parse_change
  -> local dependency graph fixture
  -> deterministic impact prediction
  -> fixture telemetry
  -> predicted-vs-actual comparison
  -> local evidence JSON on disk
  -> local deterministic explanation
```

AWS integrations exist in Phase 0 only as **interfaces and inert stubs**:

- every externally-backed capability is a Protocol in `ports.py`
- `adapters/local.py` holds the Phase 0 implementations that actually run
- `adapters/aws.py` holds the Phase 1/2 placeholders; every method raises
  `PhaseNotAuthorizedError`. It imports no SDK and executes nothing.
- `store_evidence` writes JSON files to disk; S3/DynamoDB replace it behind the same
  interface later
- `explain` produces a deterministic, template-built explanation from stored evidence;
  Bedrock replaces it behind the same interface later

Terraform CLI is permitted because it is local tooling. `terraform plan` must run with
`-refresh=false` and provider credential checks skipped, so it performs no AWS API
calls. A Terraform state file used to produce the offline plan is a **test fixture**
and must be named and documented as one. It is never presented as real account state.

Tooling permitted in Phase 0: Python, pytest, `pyright-lsp`, Terraform CLI, git.
Not permitted yet: Terraform MCP, any AWS MCP server, any AWS plugin, AWS CLI.

**Phase 1 — free-tier AWS only**, and only on explicit authorization from the user.
Lambda, Step Functions, S3, DynamoDB, API Gateway.

**Phase 2 — the one paid call**, again only on explicit authorization.
Bedrock explanation, a handful of invocations.

Do not start a phase before the previous one is working and tested, and never
advance a phase without the user explicitly authorizing it.

## 6. Coding expectations

- **Python** for backend logic, standard library only in Phase 0. `boto3` is available
  in the Lambda runtime but must not appear on any Phase 0 code path.
- **Terraform (HCL)** for infrastructure. No CDK, no CloudFormation, no SAM.
- Pure functions for parsing, prediction and comparison — they must be unit-testable
  with no AWS calls. Keep AWS I/O at the edges.
- Every stage takes a JSON-serializable input and returns a JSON-serializable output,
  so Step Functions can pass state between Lambdas and so evidence is storable as-is.
- Thresholds live in one explicit, named place — never scattered as magic numbers.
- Prediction output must carry its own reasoning: which rule fired, on which
  dependency, with which threshold. The comparison stage and Bedrock both read this.
- Fail loudly. An unknown resource type or an unparseable plan is an explicit error,
  not a silent "low risk".
- Match the surrounding code's style, naming and comment density. No decorative
  comments, no defensive scaffolding that isn't exercised.
- Keep dependencies minimal and justified. Adding one is a decision, not a detail.

## 7. Security constraints

- **ChangeProof must never be able to touch production.** IAM is the boundary:
  least-privilege roles, test-environment scope only, no wildcard admin policies.
- **Never give a model AWS credentials or unrestricted execution.** Bedrock receives
  evidence text and returns prose. That is the entire surface.
- No hardcoded secrets, account IDs, ARNs or credentials in the repo. Use variables,
  SSM/Secrets Manager, or environment configuration.
- Every test environment must be tagged, scoped and destroyable. Provisioning without
  a corresponding teardown path is a defect.
- Terraform state must never be committed. No `.tfstate`, no `.terraform/`.
- Treat Terraform plan JSON as untrusted input: validate before acting on it.

## 8. Do NOT do this yet

- **Do not provision Neptune or RDS under any circumstances.** See section 4.
- **Do not deploy AWS resources.** No `terraform apply`, no `aws` CLI mutations, no
  provisioning of any kind without explicit instruction.
- **Do not redesign the architecture.** The documents above are settled.
- **Do not build the full production architecture.** Cognito, SNS, OpenSearch,
  SageMaker, the React dashboard and backward/incident mode are all out of scope.
- **Do not add unnecessary dependencies.**
- **Do not fake AWS functionality and present it as working integration.** Mocks,
  fixtures and stubs are welcome and expected at this stage — they must be *labelled*
  as such, in the code and in any summary. Never report a simulated result as a real
  AWS observation.
- Do not let Bedrock (or any model) originate a prediction, a metric or a decision.
- Do not commit or push unless asked.

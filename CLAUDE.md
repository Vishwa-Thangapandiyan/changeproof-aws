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

## 2. Current status — Phase 0 complete

The offline verification pipeline runs end to end with zero AWS credentials and zero
spend. The repository today is the **brain** of ChangeProof; AWS becomes the body in
Phase 1+.

```bash
python scripts/demo.py              # full offline pipeline, no credentials needed
python -m pytest backend/tests -q   # 118 tests at the Phase 0 handoff; keep it green
pyright                             # must stay clean
```

`scripts/demo.py` exits non-zero on a REJECT verdict, so it drops into CI/CD naturally.
Its expected shape today:

```text
4-resource blast radius
  -> HIGH predicted reliability risk
  -> simulated verification
  -> 3 HIGH safety breaches
  -> 25% of metrics inside the predicted band
  -> REJECTED
```

Where things live:

```text
architecture/architecture.md                  design notes
backend/lambda/changeproof/                   parser, graph, predict, compare, pipeline,
                                              models, thresholds
backend/lambda/changeproof/ports.py           Protocol per externally-backed capability
backend/lambda/changeproof/adapters/local.py  Phase 0 implementations that actually run
backend/lambda/changeproof/adapters/aws.py    Phase 1/2 placeholders; every method raises
backend/lambda/changeproof/fixtures/          labelled offline plan, graph, telemetry
backend/state_machine/workflow.json           Step Functions ASL definition
backend/tests/                                unit tests plus test_no_aws.py
terraform/                                    demo target infra
terraform/fixtures/deployed-at-10.tfstate     committed on purpose: offline fixture
scripts/demo.py                               end-to-end offline run
scripts/generate_plan.sh                      offline terraform plan generation
```

Built: change representation, plan parsing, blast-radius traversal, deterministic
prediction, test/reject routing, simulated observation, safety evaluation, prediction
accuracy, verdict, deterministic explanation, evidence bundle on disk.

Not built, and deliberately so: real test-environment provisioning, change application,
synthetic workload generation, and every AWS-backed adapter (Neptune, CloudWatch,
CloudTrail, X-Ray, S3, DynamoDB, Bedrock). Extend `adapters/aws.py` behind the existing
`ports.py` Protocols rather than threading AWS calls through the pipeline.

## 3. Decision rules (how a verdict is reached)

Three rules the code already enforces. They must not drift.

**Severity routing.** `CONCURRENCY_SEVERITY_BANDS` in `thresholds.py` maps a change to
LOW / MEDIUM / HIGH / CRITICAL. LOW through HIGH are verified experimentally. CRITICAL
is rejected *without* spending a test environment. The 10x demo change sits in HIGH on
purpose, because verifying it is the entire point of the demo.

**Prediction accuracy never decides the verdict.** These are two independent questions:

- *Was the prediction accurate?* — the `PREDICTION_*` constants. Being wrong here costs
  accuracy, which is recorded and feeds future calibration.
- *Did the system actually become unsafe?* — `SAFETY_LIMITS`, applied to measured values
  only.

Only the second produces APPROVE/REJECT. A run reporting "25% of metrics inside the
predicted band, 3 safety breaches, REJECTED" is coherent, not contradictory. Equally, a
badly wrong prediction whose measurements stay inside every limit can still approve,
with the miss recorded.

**Simulated data announces itself.** Every fixture JSON carries a `$fixture` key stating
in plain words that the numbers were hand-authored and were not measured by AWS, and the
evidence bundle carries the same warning in its provenance block.
`test_fixtures_are_labelled_as_fixtures` enforces it. Never strip a label to make output
read more convincingly.

The zero-AWS boundary is enforced by `backend/tests/test_no_aws.py`: no SDK import in the
source tree, no SDK loaded on import, sockets blocked during a run, the pipeline running
with AWS environment variables stripped, deferred stages raising, the declared
deferred-stage list matching the stages that actually refuse, and fixtures labelled. Do
not weaken these tests to make something pass.

## 4. Architecture

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

These are not interchangeable; each answers a different question. CloudWatch says
*what* changed, X-Ray says *where* it changed. CloudTrail records control-plane actions,
CloudWatch records system behaviour. S3 holds large artifacts, DynamoDB holds queryable
state. Lambda does one thing, Step Functions decides what happens next.

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

## 5. MVP scope

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


## 6. Cost constraints (hard limit: $100 AWS credits)

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

## 7. Build phases

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

## 8. Coding expectations

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

## 9. Security constraints

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

## 10. Do NOT do this yet

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

## 11. Team and ownership

The agreed end-to-end workflow, confirmed by the team:

> Submit a Terraform change -> predict impact -> test baseline and changed
> configurations in AWS -> collect actual metrics -> produce and store an
> approve/reject verdict.

| Area | Owner | Scope |
|---|---|---|
| Test infrastructure and workload | Praanesh | Make the Terraform demo deployable; producer and worker Lambda code; baseline and change runs plus cleanup; publishes resource names and measurement timestamps |
| Pipeline and function integration | Vishwa | Stage wiring, Step Functions integration, the pipeline contract between stages |
| CloudWatch telemetry | unassigned | Metric collection for both runs, against the published names and timestamps |
| Experiment records | Varun | S3 evidence artifacts and the DynamoDB experiment schema |

The interface between infrastructure and telemetry is deliberately narrow: the workload
run publishes **resource names and measurement timestamps**, and telemetry collection
reads only those. Anything else is coupling.

Neptune and Bedrock are explicitly post-core work, by team agreement and by the cost
rule in section 6. Do not start either until the baseline-vs-change loop is working.

Open item: the DynamoDB table and attribute list is owed to the experiment-records
owner. It should be derived from the evidence bundle the pipeline already produces
(experiment id, phase, change summary, prediction, observed metrics, breaches,
prediction accuracy, verdict) rather than designed independently.

# Experiment infrastructure and workload

The isolated AWS environment a proposed change is tested in, the application that
runs inside it, and the driver that measures both configurations.

This is the part of ChangeProof that spends money. Everything here is Phase 1: it is
built, it is tested offline, and **it has never been applied to an AWS account.** No
`terraform apply` has been run, so treat first deployment as an unverified step.

---

## The four calls the pipeline makes

```python
from workload.driver import ExperimentDriver, WorkloadSpec

driver = ExperimentDriver(experiment_id="EXP-142", region="us-east-1", spec=WorkloadSpec())

environment = driver.create_experiment()   # -> ExperimentEnvironment
baseline    = driver.run_baseline()        # -> RunRecord
changed     = driver.run_changed()         # -> RunRecord
teardown    = driver.cleanup_experiment()  # -> TeardownReport
```

`driver.run_both()` runs the two in the configured order and returns them by role,
`(baseline, changed)`, whichever went first. Use it unless you need the two runs
interleaved with something of your own.

Every one returns a dataclass with `.to_dict()`. Nothing is communicated through logs.
The CLI wraps the same four calls and writes the combined manifest to
`.changeproof/experiments/<id>/experiment-manifest.json`.

---

## 1. What Terraform creates

`terraform/main.tf`, one stack per experiment, named `changeproof-<experiment-id>-*`:

| Resource | Address | Purpose |
|---|---|---|
| Lambda function | `aws_lambda_function.api` | The producer. **The resource the change is applied to.** |
| SQS queue | `aws_sqs_queue.work` | The buffer that absorbs producer/consumer imbalance |
| SQS queue | `aws_sqs_queue.work_dlq` | Dead letter queue, redrive after 3 receives |
| Lambda function | `aws_lambda_function.worker` | The consumer, capped low on purpose |
| Event source mapping | `aws_lambda_event_source_mapping.worker` | Queue to consumer, batch size 10 |
| DynamoDB table | `aws_dynamodb_table.orders` | Terminal datastore, on-demand billed |
| IAM roles + inline policies | `aws_iam_role.api`, `aws_iam_role.worker` | Least privilege, scoped to these ARNs |
| Log groups | `aws_cloudwatch_log_group.{api,worker}` | 3 day retention |

DynamoDB rather than RDS, and no Neptune anywhere: see CLAUDE.md section 6. Every
resource is tagged `Project=changeproof`, `Ephemeral=true`, `Experiment=<suffix>`, so
an orphan can always be traced back to the run that created it.

The dependency chain mirrors `fixtures/dependency_graph.json` exactly:

```text
aws_lambda_function.api -> aws_sqs_queue.work -> aws_lambda_function.worker -> aws_dynamodb_table.orders
```

## 2. The baseline configuration

```hcl
reserved_concurrency = 10    # aws_lambda_function.api
worker_concurrency   = 5     # aws_lambda_function.worker
```

Plus everything else in the stack held fixed: memory 512 MB, api timeout 15s, worker
timeout 30s, batch size 10, visibility timeout 60s, payload 256 bytes, 20 ms of work
per message.

## 3. The changed configuration

```hcl
reserved_concurrency = 100   # 10 -> 100
```

**That is the entire diff.** `worker_concurrency` deliberately stays at 5: the
producer scales, the consumer does not, and the queue absorbs the difference until it
cannot. That asymmetry is the demo.

## 4. Switching between them

`terraform apply -var reserved_concurrency=100` against the same state, in the same
environment, on the same queue and table. Reserved concurrency is an in-place update,
so nothing is replaced and nothing goes cold.

Before applying, the driver runs `plan -json` and asserts:

- exactly one resource changes, and it is `aws_lambda_function.api`
- the action is `["update"]`, not a replacement
- exactly one attribute differs, and it is `reserved_concurrent_executions`

If any of those fails the run aborts rather than proceeding. A two-variable
experiment proves nothing, and a silently drifted stack is the likeliest way to get
one.

The same guardrail applies in either direction, so `--order changed-first`
provisions at 100, measures, then applies 10 and measures again. See *Run order*
below.

## 5. The producer (`workload/src/index.py`)

Invoked once per attempted order with `{"runId", "seq", "payloadBytes"}`. Sends one
SQS message and returns the order id. Invocation is **synchronous** on purpose: an
async invocation that hits the concurrency cap is retried inside Lambda for up to six
hours, so the rejection never reaches the caller and the attempt count stops meaning
anything. `RequestResponse` surfaces it immediately as a 429, counted by the driver
and published as the `Throttles` metric.

## 6. The consumer

SQS event source, batches of up to 10. Per message: sleep `WORK_MS` (20 ms default),
then `PutItem` into `orders` with `orderId`, `runId`, `seq`, `createdAt`,
`processedAt` and `queueLatencyMs`.

Sleep rather than a busy loop, because the bottleneck being demonstrated is a
concurrency cap, not CPU contention, and sleep reproduces far more precisely across
invocations. Reproducibility is what makes the runs comparable.

A failed write re-raises: the batch retries and lands in the DLQ after three
attempts. Swallowing it would drain the queue, satisfy the drain gate, and produce
evidence describing work that never happened.

## 7. The workload

A fixed schedule computed before the first request: `attempts` invocations paced
evenly across `duration_seconds`, each carrying a fixed-size deterministic payload.
Defaults: 3000 attempts over 60s (50/s), 256 byte payloads, 50 warm-up attempts,
64 client threads.

Held identical across both runs:

| Held constant | How |
|---|---|
| The attempt schedule | Pure function of the spec; both runs build the same list |
| The payload | Fixed content, not random |
| The spec itself | SHA-256 `workloadFingerprint` recorded on both runs; a mismatch invalidates the comparison |
| Cold starts | Warm-up attempts precede the measurement window and are excluded from it |
| Starting queue depth | Drain gate: the next run does not start until the queue is empty |
| Infrastructure | Same stack, same queue, same table, same region |
| Everything but one attribute | Plan guardrail, in whichever direction the change is applied |

**Attempts are the controlled quantity, not successes.** Under the baseline most
invocations are rejected by the cap; under the change most succeed. That difference
*is* the effect, so holding successes constant would erase what the experiment exists
to measure. The manifest reports `attempted`, `accepted`, `throttled` and `failed`
separately for each run.

## 8. How a run starts and finishes

A run starts when the first measured attempt is issued (warm-up already done).

A run finishes when both hold:

1. every attempt has returned, and
2. the queue reports 0 visible and 0 in flight on **two consecutive polls** five
   seconds apart — one clean poll is not enough, `ApproximateNumberOfMessages` is
   eventually consistent and dips to zero while messages are still being handed out

If the queue has not drained within `drain_timeout_seconds` (default 300), the run is
recorded with `"drained": false` and a note saying the window closed with work still
in flight. It is not silently treated as complete.

Between runs the driver idles for `settle_seconds` (default 60) so one run's tail
does not land in the other's first datapoint.

## 9. Identifiers handed to the pipeline

From `terraform output`, keyed by the **Terraform address**, which is also the graph
node id and the `resourceAddress` on every `ObservedMetric` — the join key across the
whole system:

```jsonc
// shape, not data: no experiment has been run
{
  "graphAddress": "aws_lambda_function.api",
  "resourceType": "aws_lambda_function",
  "physicalName": "changeproof-exp-142-api",
  "arn": "arn:aws:lambda:...",
  "logGroup": "/aws/lambda/changeproof-exp-142-api",
  "cloudwatch": {
    "namespace": "AWS/Lambda",
    "dimensions": [{ "Name": "FunctionName", "Value": "changeproof-exp-142-api" }]
  }
}
```

Namespace and dimension are included; the **metric list is not**. Which metrics
matter per resource type already lives in `thresholds.METRICS_BY_RESOURCE_TYPE` and
must not be duplicated here.

## 10. Timestamps for CloudWatch

Each `RunRecord` carries four wall-clock instants and one window:

```jsonc
"timestamps": {
  "startedAt": "...",          // run began, including any apply
  "attemptsStartedAt": "...",  // first measured attempt
  "attemptsEndedAt": "...",    // last attempt returned
  "drainedAt": "..."           // queue confirmed empty
},
"metricWindow": {
  "startIso": "...", "endIso": "...",
  "startEpoch": 0, "endEpoch": 0
}
```

**Query `metricWindow`, not the raw timestamps.** It is `attemptsStartedAt` floored to
the minute through `drainedAt` plus 60 seconds, ceilinged to the minute. Both edges
snap outward because CloudWatch publishes on whole-minute boundaries, and the tail
exists because queue depth and message age peak *after* the producer stops. A window
cut at `attemptsEndedAt` would miss the backlog, which is the entire signal.

The baseline window fills `ObservedMetric.baselineValue`; the changed window fills
`observedValue`.

## 11. Cleanup

`cleanup_experiment()` runs `terraform destroy` against that experiment's own state
file, then runs `terraform state list` and reports what is left. It reports rather
than assumes: an experiment that cannot be torn down is a recurring bill.

- the CLI runs teardown in a `finally`, so a run that raises halfway still cleans up
- `cleanup` is a standalone command and works after a crash, from the state on disk:
  `python -m workload.driver.cli cleanup EXP-142 --authorize-aws-spend`
- surviving resources are listed by address in `teardown.notes`, with the exact
  `-state=` path to destroy them by hand
- `--keep` leaves the stack up and says so in the manifest; it is for debugging a
  failed run and costs money until you destroy it
- everything is tagged `Ephemeral=true` for a sweep of last resort

---

## Run order

`--order baseline-first` (default) measures 10 then 100. `--order changed-first`
provisions at 100 and measures that first.

Order matters because the second run inherits whatever drifted during the first:
account-level warmth, a noisy neighbour on shared infrastructure, time of day. A
single experiment cannot separate that drift from the change itself. Running the
same experiment twice in opposite orders can:

```bash
python -m workload.driver.cli run EXP-142-a --order baseline-first --authorize-aws-spend
python -m workload.driver.cli run EXP-142-b --order changed-first  --authorize-aws-spend
```

If both reach the same verdict, drift is not driving it. If they disagree, the
evidence is weaker than a single run makes it look, and `comparability.order` in each
manifest records which way round that run went.

`run_both()` always returns `(baseline, changed)` by role, so nothing downstream has
to branch on the order.

## Running it

```bash
bash scripts/build_lambda.sh                              # one file, no deps, no AWS
python -m workload.driver.cli plan EXP-142                # offline; describes the run
python -m workload.driver.cli run EXP-142 --authorize-aws-spend
python -m workload.driver.cli run EXP-142 --order changed-first --authorize-aws-spend
python -m workload.driver.cli cleanup EXP-142 --authorize-aws-spend
```

`plan` needs no credentials and makes no AWS call. It deliberately emits no
resource names, metrics or timestamps: those are measurements, and a dry run that
fabricated them would be indistinguishable from a real result in the evidence bundle.

`run` and `cleanup` refuse to start without `--authorize-aws-spend`, every time. An
environment variable someone exported once and forgot is not consent.

Tests: `python -m pytest workload/tests -q` — no credentials, no Terraform CLI.

## Known confounds

Stated because they affect how much the evidence is worth:

- **A single experiment does not cancel drift.** Order is controllable, but one run
  in one order cannot separate the change from whatever drifted while it ran. Two
  experiments in opposite orders can; nothing forces you to do that.
- **The load generator runs on a laptop.** Client-side network jitter affects the
  achieved attempt rate, though not the Lambda-side metrics. `comparability.attemptDeltaPct`
  reports the difference between the two runs; treat anything above a couple of
  percent as a reason to discard the comparison.
- **The DynamoDB table is shared across both runs** and accumulates items. Deliberate:
  a fresh table per run would be a second variable (cold partitions). Items are keyed
  by `runId`.
- **`terraform apply` has never been run.** First deployment is unverified.

## Not implemented

- Deploying into a separate AWS account (currently same-account isolation by name and
  tag only; IAM boundary work is the security owner's)
- Running the generator inside AWS rather than from a laptop
- Repeating an experiment automatically in both orders and reconciling the verdicts
- Any change type other than Lambda reserved concurrency

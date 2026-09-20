# ChangeProof AWS Handoff

**For:** Sanjay, taking over AWS integration
**Branch:** `feat/test-pipeline`
**Status at handoff:** everything below runs offline with zero AWS credentials and zero AWS spend. No `terraform apply` has ever been run by anyone on this project, and no AWS API call has ever been made from this repository.

---

## 1. Purpose

### What ChangeProof does

> Don't just review the change. **Prove it.**

A developer proposes a Terraform infrastructure change. Instead of guessing whether it is safe, ChangeProof:

1. parses the change,
2. works out what depends on the changed resource,
3. predicts the impact deterministically,
4. builds an isolated AWS test environment,
5. applies the change there and drives a realistic workload,
6. measures what actually happened,
7. compares prediction against reality,
8. returns APPROVE or REJECT with the evidence attached.

The first demo change is **Lambda reserved concurrency 10 → 100**.

### What this handoff is for

The **reasoning** half is built and tested. The **measurement** half is built but currently reads from a fixture instead of CloudWatch.

Your job is to replace that fixture with real CloudWatch data, without changing anything else. The seam for doing so already exists and is documented in section 7.

This document describes the repository exactly as it stands. Where something is not implemented, it says so.

---

## 2. Current Architecture

### The intended pipeline

```
Terraform change
   → manifest (what was provisioned, and when it was measured)
   → prediction (what we expect to happen)
   → experiment (actually run it in AWS)
   → telemetry (what actually happened)
   → comparison (predicted vs actual, plus safety limits)
   → verdict (APPROVE / REJECT)
```

### What exists today, stage by stage

| Stage | Module | Status | Notes |
|---|---|---|---|
| Parse Terraform change | `changeproof/parser.py` | ✅ **Implemented** | Parses real `terraform show -json`. Input today is a committed fixture plan |
| Dependency graph | `changeproof/graph.py` | ✅ **Implemented** | Local JSON fixture, **not** Neptune |
| Predict impact | `changeproof/predict.py` | ✅ **Implemented** | Deterministic rules + thresholds |
| Build test environment | `workload/driver/` | ⚠️ **Built, never executed** | Praanesh's driver. Real Terraform + boto3 code, but no `terraform apply` has been run |
| Apply change + workload | `workload/driver/` | ⚠️ **Built, never executed** | Same |
| **Manifest boundary** | `changeproof/manifest.py` | ✅ **Implemented** | New in this branch |
| **Metric contract** | `changeproof/thresholds.py` | ✅ **Implemented** | New in this branch |
| **Telemetry collection** | `changeproof/telemetry.py` | 🟡 **Implemented, fixture-backed** | **This is your entry point** |
| Compare + verdict | `changeproof/compare.py` | ✅ **Implemented** | Deliberately untouched |
| Evidence storage | `adapters/local.py` | 🟡 **Local JSON files only** | S3/DynamoDB not implemented |
| Explanation | `adapters/local.py` | 🟡 **Deterministic templates** | Bedrock not implemented |

### The two halves and how they meet

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  workload/driver/           │        │  backend/lambda/changeproof/ │
│  (Praanesh)                 │        │  (the engine)                │
│                             │        │                              │
│  provisions AWS             │        │  parses, predicts, compares  │
│  drives workload            │        │  decides APPROVE / REJECT    │
│  may import boto3           │        │  MUST NOT import boto3       │
└──────────────┬──────────────┘        └───────────────▲──────────────┘
               │                                       │
               │  experiment-manifest.json             │
               └───────────────────────────────────────┘
                     the ONLY contract between them
```

The two never import each other. The manifest JSON is the entire interface.

---

## 3. Work Completed So Far

Everything in this section was added **after** Praanesh's `feat/test-environment` work, which is already merged into this branch.

### 3.1 Manifest boundary

#### What `ExperimentManifest` is

The experiment driver writes a JSON file at `.changeproof/experiments/<id>/experiment-manifest.json` after a run. `changeproof/manifest.py` parses it into typed Python objects.

**Critically: the manifest carries no measurements.** It tells you *what to ask CloudWatch about* and *over which time windows*. The numbers come from CloudWatch, not from the manifest.

#### What it carries

| Field | Type | What it is |
|---|---|---|
| `experiment_id` | `str` | e.g. `EXP-FIXTURE-001` |
| `region` | `str` | AWS region the experiment ran in |
| `resources` | `tuple[ManifestResource, ...]` | One per AWS resource in the test environment |
| `baseline_window` | `MetricWindow` | When to measure the *before* configuration |
| `changed_window` | `MetricWindow` | When to measure the *after* configuration |

`ManifestResource` carries `graph_address`, `resource_type`, `physical_name`, `cloudwatch` (namespace + dimensions), and optional `arn` / `queue_url`.

`MetricWindow` carries `start_epoch`, `end_epoch`, `start_iso`, `end_iso`, and a derived `duration_seconds`.

#### `graphAddress` is the join key — this matters more than anything else here

`graphAddress` is the **Terraform resource address**, e.g. `aws_lambda_function.api`. The same string is used in four places:

1. the Terraform plan (`parser.py`)
2. the dependency graph node id (`graph.py`)
3. `ResourceIdentity.graph_address` in the driver
4. `ObservedMetric.resource_address` in the engine

That is what lets a prediction about `aws_sqs_queue.work` be compared against a measurement of `aws_sqs_queue.work`.

**`physical_name` changes on every experiment** (`changeproof-exp-142-work`, `changeproof-exp-143-work`, …). It is used only as a CloudWatch dimension *value*. **Never key anything on it.**

#### Validation behaviour

The manifest is produced by a separate process, so it is treated as untrusted input — the same discipline `parser.py` applies to Terraform plan JSON. `parse_manifest` raises `ManifestParseError` on:

- a non-object document, or a missing/empty `experimentId`
- a missing `environment`, or a non-string `region`
- an empty or missing `resources` list
- a resource missing `graphAddress`, `resourceType`, `physicalName`, or `cloudwatch`
- empty `dimensions`, or a dimension missing `Name`/`Value`
- **a duplicate `graphAddress`** — it is the join key, so two rows under one key would silently drop a resource
- a missing `baseline` or `changed` run, or either missing `metricWindow`
- non-integer epochs (`bool` is rejected explicitly, since `True` would otherwise become epoch 1)
- **`endEpoch <= startEpoch`** — a window with no duration cannot be queried
- top-level `experimentId` disagreeing with `environment.experimentId`

Unknown keys are **ignored**, so the driver's full manifest parses without the engine knowing about `workload`, `result`, `timestamps`, `drained`, `dlqDepth`, `comparability` or `teardown`.

#### Driver compatibility

The fixture was built from the driver's own `to_dict()` output, and `ExperimentManifest.to_dict()` emits the manifest's key paths (`environment.region`, `baseline.metricWindow`), not a private flattened shape — so its output re-parses through `parse_manifest`. A test asserts exactly that.

#### Fixture status

`backend/lambda/changeproof/fixtures/experiment_manifest.json` is **hand-authored**. Its `$fixture` field says so in plain words, including "NO EXPERIMENT HAS BEEN RUN". ARNs use the placeholder account `000000000000`. A test asserts that label survives edits.

---

### 3.2 Metric contract

Before this branch, statistics and units existed only in a prose table in `architecture/telemetry-contract.md`. They are now **code**, in `backend/lambda/changeproof/thresholds.py`.

#### Where the definitions live

| Constant | What it answers |
|---|---|
| `METRICS_BY_RESOURCE_TYPE` | *Which* metrics matter for each resource type |
| `METRIC_DEFINITIONS` | *How* to read each metric: statistic, unit, extra dimensions |
| `definition_for(metric)` | Lookup. **Raises** on an unknown metric rather than guessing |
| `STATISTICS` / `UNITS` | Valid-value sets, so a typo fails a test instead of an API call |

Do not restate either list anywhere else. The collector reads both from here precisely so they cannot drift.

#### The 13 resource/metric combinations

Four resources × their type's metrics = 13 rows the collector must produce.

| # | graphAddress | Metric | Namespace | Dimensions | Statistic | Unit |
|---|---|---|---|---|---|---|
| 1 | `aws_lambda_function.api` | `ConcurrentExecutions` | `AWS/Lambda` | `FunctionName` | `Maximum` | Count |
| 2 | `aws_lambda_function.api` | `Duration` | `AWS/Lambda` | `FunctionName` | `Average` | Milliseconds |
| 3 | `aws_lambda_function.api` | `Throttles` | `AWS/Lambda` | `FunctionName` | `Sum` | Count |
| 4 | `aws_lambda_function.api` | `Errors` | `AWS/Lambda` | `FunctionName` | `Sum` | Count |
| 5 | `aws_sqs_queue.work` | `ApproximateNumberOfMessagesVisible` | `AWS/SQS` | `QueueName` | `Maximum` | Count |
| 6 | `aws_sqs_queue.work` | `ApproximateAgeOfOldestMessage` | `AWS/SQS` | `QueueName` | `Maximum` | Seconds |
| 7 | `aws_lambda_function.worker` | `ConcurrentExecutions` | `AWS/Lambda` | `FunctionName` | `Maximum` | Count |
| 8 | `aws_lambda_function.worker` | `Duration` | `AWS/Lambda` | `FunctionName` | `Average` | Milliseconds |
| 9 | `aws_lambda_function.worker` | `Throttles` | `AWS/Lambda` | `FunctionName` | `Sum` | Count |
| 10 | `aws_lambda_function.worker` | `Errors` | `AWS/Lambda` | `FunctionName` | `Sum` | Count |
| 11 | `aws_dynamodb_table.orders` | `ConsumedWriteCapacityUnits` | `AWS/DynamoDB` | `TableName` | `Sum` | Count |
| 12 | `aws_dynamodb_table.orders` | `ThrottledRequests` | `AWS/DynamoDB` | `TableName` | `Sum` | Count |
| 13 | `aws_dynamodb_table.orders` | `SuccessfulRequestLatency` | `AWS/DynamoDB` | **`TableName` + `Operation=PutItem`** | `Average` | Milliseconds |

`period` is **60 seconds**, defined as `PERIOD_SECONDS` in `changeproof/telemetry.py`.

#### `SuccessfulRequestLatency` needs `Operation=PutItem` — read this twice

DynamoDB publishes `SuccessfulRequestLatency` **per operation**. Queried with `TableName` alone it returns **no datapoints at all, silently** — no error, just an empty series. `PutItem` is the worker's only write (see `workload/src/index.py`).

This is the **only** metric in the system with an extra dimension. It is stored on the metric, not on the resource, because the manifest's dimension list belongs to a *resource* — putting `Operation` there would wrongly narrow rows 11 and 12 as well.

Three tests pin this from both directions: only `SuccessfulRequestLatency` has extras, it has exactly `("Operation", "PutItem")`, and the table's other metrics use `TableName` alone.

---

### 3.3 CloudWatch collector

Lives in `backend/lambda/changeproof/telemetry.py`. **It is fully implemented and fully offline.** It does not import boto3 and makes no network call.

#### The pieces

| Name | What it is |
|---|---|
| `MetricQuery` | One metric on one resource: `query_id`, `resource_address`, `metric`, `namespace`, `dimensions`, `statistic`, `unit`, `period_seconds`. Has `dimensions_as_cloudwatch()` returning `[{"Name":…,"Value":…}]` |
| `build_queries(manifest)` | Produces all 13 `MetricQuery` objects from the manifest + `thresholds` |
| `MetricDataSource` | **Protocol (the seam).** `fetch(queries, window) -> dict[query_id, list[float]]` |
| `StaticMetricDataSource` | Offline implementation in `adapters/local.py`, reads a fixture |
| `CloudWatchCollector` | Satisfies `ports.TelemetrySource`. Orchestrates: build queries → fetch both windows → fold → `Observation` |
| `TelemetryError` | Raised on a mismatched experiment id, no measurable resources, or an unfoldable statistic |

#### How the two windows are used

```python
baseline = source.fetch(queries, manifest.baseline_window)   # the "before" numbers
changed  = source.fetch(queries, manifest.changed_window)    # the "after" numbers
```

**One `ObservedMetric` carries both runs.** The baseline window fills `baseline_value`; the changed window fills `observed_value`. That pairing is the collector's job — nothing downstream re-associates them, and `compare.py` assumes it is already done.

#### How series are folded

At `period=60`, a 4-minute window returns ~4 datapoints. The collector must produce **one** number, so it applies the same statistic *across* periods as *within* them:

| Statistic | Fold |
|---|---|
| `Maximum` | `max(values)` |
| `Minimum` | `min(values)` |
| `Sum` | `sum(values)` |
| `Average` | `statistics.fmean(values)` |
| `SampleCount` | `sum(values)` |

A peak stays a peak, a total stays a total, a mean stays a mean. Any other choice would make the number mean something different from what `SAFETY_LIMITS` assumes.

#### How missing data is handled — do not change this

CloudWatch returns **no datapoints** for an idle resource, not zeros. AWS documents this explicitly for SQS, and a queue that was quiet during the baseline run is exactly that case.

**Rule: if either window is missing for a metric, the `ObservedMetric` is omitted entirely.** It is never zero-filled.

Why this matters: a fabricated baseline of `0` against an observed `2680` is an infinite increase and a **guaranteed false REJECT**. `compare.py` already handles an absent metric — it scores as `NOT_OBSERVED` against prediction accuracy and creates no breach.

A genuine measured zero on **both** sides *is* kept. `ObservedMetric.change_pct` returns `None` for a zero baseline, and the absolute safety limits handle it.

`CloudWatchCollector.missing(observation)` reports which queries produced nothing, so a half-empty run is visible rather than quietly thin.

#### How `ObservedMetric` objects are produced

```python
ObservedMetric(
    resource_address = query.resource_address,   # the graphAddress
    metric           = query.metric,
    unit             = query.unit,               # from METRIC_DEFINITIONS
    baseline_value   = fold(baseline_series),
    observed_value   = fold(changed_series),
)
```

Wrapped in `Observation(source="cloudwatch", simulated=False, metrics=(...))`.

#### 🟡 The current implementation is OFFLINE

`StaticMetricDataSource` reads `fixtures/cloudwatch_responses.json` — hand-authored per-period series labelled `$fixture`. They fold to the same headline values as the older `telemetry_observed.json`, so the demo narrative is unchanged.

**No CloudWatch call exists anywhere in this repository.** The `simulated=False` flag on the produced `Observation` describes the *pathway* (a real collector, not a fixture reader), not the data. Until a real `MetricDataSource` is written, the numbers are still invented.

---

### 3.4 The existing ChangeProof engine

`compare.py`, `predict.py`, `models.py`, `ports.py`, `parser.py`, `graph.py` were **deliberately not changed** by any of this work. Verified with `git diff`.

The collector feeds them unchanged. Confirmed end to end:

```
manifest → CloudWatchCollector → 13 ObservedMetric rows → compare()
  → verdict=REJECT  severity=HIGH  accuracy=25%
  → breaches: orders SuccessfulRequestLatency (HIGH)
              worker Throttles (HIGH)
              work ApproximateNumberOfMessagesVisible (HIGH)
              work ApproximateAgeOfOldestMessage (MEDIUM)
```

#### The rule that governs everything in `compare.py`

Two independent questions, deliberately kept apart:

- **Was the prediction accurate?** → `prediction_accuracy`. Recorded, feeds future calibration.
- **Did the system actually become unsafe?** → `SAFETY_LIMITS` applied to measured values.

**Only the second produces APPROVE/REJECT.** A run reporting "25% of metrics inside the predicted band, 3 safety breaches, REJECTED" is coherent, not contradictory.

---

## 4. Current Test / Verification State

Measured on this branch at the time of writing. Not estimates.

| Check | Result |
|---|---|
| `pytest backend/tests/test_telemetry.py` | **38 passed** |
| `pytest backend/tests/test_manifest.py` | **48 passed** |
| `pytest backend/tests/test_thresholds.py` | **48 passed** |
| `pytest backend/tests` | **252 passed** |
| `pytest workload/tests` | **33 passed** |
| `pytest` (full suite) | **285 passed** |
| `pyright` | **0 errors, 0 warnings** |
| `python scripts/demo.py` | **REJECT**, 25% prediction accuracy |
| manifest → collector → compare | works: 13 rows, REJECT / HIGH / 25% |

### What these numbers do and do not mean

✅ A passing suite proves the reasoning engine, the manifest boundary, the metric contract and the collector's logic are correct **against fixture data**.

❌ It proves **nothing** about AWS integration. No AWS call has been made. No resource has been provisioned.

⚠️ **`scripts/demo.py` does not use the new collector.** It still uses the older `FixtureTelemetrySource`. The collector was verified by its own tests and by a manual end-to-end script, not by the demo. Nothing in `pipeline.py` or `handler.py` calls `CloudWatchCollector` yet — wiring it in is a separate, not-yet-done step.

---

## 5. Files Added / Changed

From `git status` and `git diff --stat` on this branch.

### Added (7 files)

| File | Lines | Purpose |
|---|---|---|
| `backend/lambda/changeproof/manifest.py` | 318 | Parses the driver's experiment manifest as untrusted input |
| `backend/lambda/changeproof/telemetry.py` | 234 | Query building, folding, `CloudWatchCollector`, the `MetricDataSource` seam |
| `backend/lambda/changeproof/fixtures/experiment_manifest.json` | 195 | Offline sample manifest, `$fixture` labelled |
| `backend/lambda/changeproof/fixtures/cloudwatch_responses.json` | 54 | Offline per-period datapoint series, `$fixture` labelled |
| `backend/tests/test_manifest.py` | 313 | 48 tests: field access + 20 rejection cases |
| `backend/tests/test_thresholds.py` | 191 | 48 tests: statistic/unit/dimension coverage |
| `backend/tests/test_telemetry.py` | 350 | 38 tests: query building, folding, missing data |

### Modified (2 files, +137 / −1)

| File | Change |
|---|---|
| `backend/lambda/changeproof/thresholds.py` | +81. Added `MetricDefinition`, `METRIC_DEFINITIONS`, `STATISTICS`, `UNITS`, `definition_for()`. Module docstring updated from "two families" to "three". **No existing threshold or limit was altered.** |
| `backend/lambda/changeproof/adapters/local.py` | +57. Added `StaticMetricDataSource`. Existing classes untouched |

### Deliberately NOT changed

`compare.py` · `predict.py` · `models.py` · `ports.py` · `parser.py` · `graph.py` · `handler.py` · `backend/state_machine/workflow.json` · all of `workload/` · all of `terraform/`

---

## 6. What Is NOT Implemented Yet

Stated plainly. **Nothing in this list is partially done unless noted.**

| Capability | Status | Evidence |
|---|---|---|
| **Real CloudWatch API** | ❌ Not implemented | No boto3 import anywhere under `backend/lambda/`. `adapters/aws.CloudWatchTelemetrySource.collect()` raises `PhaseNotAuthorizedError` |
| **S3 integration** | ❌ Not implemented | `adapters/aws.S3DynamoEvidenceStore.store()` raises. Evidence goes to local JSON files |
| **DynamoDB integration** | ❌ Not implemented | Same class, same behaviour. A schema is *designed* in `architecture/experiment-records.md`, not built |
| **Real AWS deployment** | ❌ Never done | No `terraform apply` has been run. `terraform/` defaults to `offline = true` with mock credentials |
| **IAM permissions** | ❌ Not defined | No role or policy exists for ChangeProof itself. `terraform/main.tf` defines roles for the *demo app* only |
| **Step Functions wiring** | ⚠️ Definition only | `workflow.json` exists and is structurally validated by tests. **Never deployed.** It also has a known gap: it applies the change before the only workload run, so the baseline is never measured |
| **Neptune** | 🚫 **Forbidden for now** | `CLAUDE.md` §6: no free tier, ~$60/month idle, would consume the $100 budget. The dependency graph runs locally behind a Neptune-shaped interface |
| **Bedrock** | ❌ Not implemented | `adapters/aws.BedrockExplainer.explain()` raises. `DeterministicExplainer` produces template prose instead |
| **Test environment provisioning** | ⚠️ Built, never run | `workload/driver/` is complete and tested offline. No experiment has ever been executed |
| **API Gateway** | ❌ Not implemented | No code, no IaC |
| **CloudTrail / X-Ray** | ❌ Not implemented | Architectural targets only |
| **Cognito / SNS / OpenSearch / SageMaker / React dashboard** | ❌ Out of MVP scope | `CLAUDE.md` §10 |

**The collector is wired to nothing.** `pipeline.py` and `handler.py` still construct `FixtureTelemetrySource`. Wiring is a separate task.

---

## 7. AWS Integration Plan for Sanjay

### 7.1 CloudWatch — your main task

#### Where you plug in

```
OFFLINE (today)
  ExperimentManifest
    → CloudWatchCollector          ← KEEP
      → StaticMetricDataSource     ← replace
        → fixtures/cloudwatch_responses.json
          → ObservedMetric ×13     ← KEEP

TARGET
  ExperimentManifest
    → CloudWatchCollector          ← UNCHANGED
      → AwsMetricDataSource        ← YOU WRITE THIS
        → cloudwatch:GetMetricData
          → real datapoints
            → ObservedMetric ×13   ← UNCHANGED
```

#### The one interface you implement

```python
# changeproof/telemetry.py  — already exists, do not change
@runtime_checkable
class MetricDataSource(Protocol):
    def fetch(
        self, queries: tuple[MetricQuery, ...], window: MetricWindow
    ) -> dict[str, list[float]]:
        ...
```

Input: the 13 queries, and one window.
Output: `{query_id: [datapoint, datapoint, …]}`.

**Return an empty list, or omit the key entirely, when there are no datapoints. Both mean "missing". Never return `[0.0]` to fill a gap.**

That is the whole contract. Everything else — folding, pairing baseline with changed, omitting incomplete metrics, building `ObservedMetric` — is already written and tested.

#### What each `MetricQuery` gives you

| Field | Maps to `GetMetricData` |
|---|---|
| `query_id` | `MetricDataQueries[].Id` (already CloudWatch-safe: lowercase start, alphanumerics and underscores) |
| `namespace` | `MetricStat.Metric.Namespace` |
| `dimensions_as_cloudwatch()` | `MetricStat.Metric.Dimensions` — **already includes `Operation=PutItem` where needed** |
| `metric` | `MetricStat.Metric.MetricName` |
| `statistic` | `MetricStat.Stat` |
| `period_seconds` | `MetricStat.Period` (60) |

And from the window: `window.start_epoch` → `StartTime`, `window.end_epoch` → `EndTime`.

Region comes from `manifest.region`.

#### Where the file should live

`backend/lambda/changeproof/adapters/aws.py` already declares `CloudWatchTelemetrySource` as a placeholder that raises. **However** — that file is currently guaranteed SDK-free by `test_no_aws.py`, which asserts **no `boto3` import anywhere under `backend/lambda/`**.

So placing a boto3-importing class there **will fail the existing test suite**. This is a real, unresolved boundary question, not an oversight. See section 11, item 5. Agree the resolution with Vishwa before writing the file — do not weaken the test to make it pass.

### 7.2 The other AWS pieces, at a high level

Only what the repository and `ChangeProof_AWS_Services_Explained.md` establish. No invented detail.

| Service | Role | Where it connects | Status |
|---|---|---|---|
| **Terraform test env** | Provisions the isolated stack the change is applied to | `workload/driver/terraform.py` shells out to the Terraform CLI. Set `offline = false` and pass `--authorize-aws-spend` | Built, never run |
| **CloudWatch** | "What happened?" — measured evidence | `MetricDataSource` (section 7.1) | **Your task** |
| **S3** | Large artifacts: plan, prediction, metrics, report | Behind `ports.EvidenceStore`. Local layout already mirrors the intended S3 key structure (`experiments/<id>/evidence.json`) | Not implemented |
| **DynamoDB** | Queryable experiment state and metadata | Same port. Schema designed in `architecture/experiment-records.md` | Not implemented |
| **Step Functions** | "What happens next?" — orchestration, retries, timeouts | `backend/state_machine/workflow.json`. Needs restructuring for two runs before deployment | Definition only |
| **Lambda** | Runs each workflow stage | `backend/lambda/handler.py` dispatches on `event["stage"]`. No build script for this package exists yet | Not deployed |
| **IAM** | Hard boundary: ChangeProof must never reach production | Least-privilege role, test-environment scope only | Not defined |
| **API Gateway** | Front door (`POST /changes`, `GET /results/{id}`) | Not started | Not implemented |
| **Bedrock** | Explains evidence — **never originates it** | Behind `ports.Explainer`, same interface as `DeterministicExplainer` | Not implemented |
| **Neptune** | "What is connected to what?" | Behind `ports.GraphSource`. Local graph implements the same two methods | 🚫 Forbidden by cost |
| **CloudTrail / X-Ray** | Control-plane audit / where latency was spent | Architectural targets | Not implemented |

---

## 8. Recommended AWS Implementation Order

**Existing project decisions** (from `CLAUDE.md`, not negotiable without the team):

- Phase 0 = local only. Phase 1 = free-tier AWS, **only on explicit authorization**. Phase 2 = Bedrock.
- Neptune and RDS are **forbidden** — no free tier, they bill while idle, and the budget is $100 total.
- Never advance a phase without the user explicitly authorizing it.

**The ordering below is a recommendation**, derived from what is built and what blocks what. It is not a decision the team has already taken.

| # | Step | Why here | Cost |
|---|---|---|---|
| 1 | AWS sandbox account + IAM role for the collector | Nothing else can run. Scope the role to `cloudwatch:GetMetricData` only to start | $0 |
| 2 | **Real `MetricDataSource`** (section 10) | Testable against *any* existing metrics before an experiment is ever run | ~$0 |
| 3 | Run one real experiment via the driver CLI | First real `terraform apply`. Produces a genuine manifest | Free tier |
| 4 | Collect real telemetry for that experiment | The first honest `simulated=False` Observation | Cents |
| 5 | Wire the collector into `pipeline.py` / `handler.py` | Currently nothing calls it | $0 |
| 6 | S3 evidence | Evidence survives process exit | Free tier |
| 7 | DynamoDB state | Queryable experiment history | Free tier |
| 8 | Step Functions | Needs the two-run restructure first | Free tier |
| 9 | Bedrock | Phase 2, separate authorization | Cents |
| 10 | Neptune, API Gateway, CloudTrail, X-Ray | Post-core | Neptune blocked on budget |

**Why step 2 before step 3:** you can point a real `AwsMetricDataSource` at *any* Lambda function that already exists in a sandbox and verify the request shape, dimensions and response parsing — without provisioning a test environment or spending the experiment budget. Getting the `Operation=PutItem` dimension wrong is silent, so proving the plumbing before the experiment is worth a lot.

---

## 9. Boundaries / Do Not Break

These are enforced by tests. Breaking them breaks the build, and in most cases would also corrupt a verdict.

### 1. `graphAddress` is the resource identity

Terraform address = graph node id = `ObservedMetric.resource_address`. Never key metrics, storage or comparison on `physical_name` — it changes every experiment.

### 2. Do not modify `compare.py` or `predict.py` to make AWS work

If real telemetry does not fit, the collector is wrong, not the engine. The verdict logic is settled and covered by tests asserting that an accurate prediction of harm still rejects, and an inaccurate prediction of a safe change still approves.

### 3. Do not zero-fill missing telemetry

Covered by four tests in `test_telemetry.py`. A fabricated zero baseline manufactures an infinite increase and a false REJECT. Omit the metric; `compare.py` handles absence correctly.

### 4. Preserve baseline vs changed

One `ObservedMetric` carries both. Both windows must be queried separately. `test_baseline_and_changed_windows_are_read_separately` catches a collector that queries one window twice.

### 5. Preserve the metric definitions

`METRICS_BY_RESOURCE_TYPE` and `METRIC_DEFINITIONS` are the single source. Do not hardcode a second metric list in the collector. `test_no_definition_is_orphaned` and `test_every_collected_metric_has_a_definition` enforce both directions.

### 6. `backend/` must stay free of AWS SDK imports

`backend/tests/test_no_aws.py` enforces this with eight tests:

| Test | What it asserts |
|---|---|
| `test_source_tree_contains_no_sdk_import` | No `boto3`/`botocore` import anywhere under `backend/lambda/` |
| `test_importing_the_pipeline_loads_no_sdk` | Importing every module loads no SDK into `sys.modules` |
| `test_pipeline_makes_no_network_call` | Any socket connection during a run fails the test |
| `test_pipeline_runs_without_aws_credentials` | Works with every `AWS_*` env var stripped |
| `test_aws_stages_refuse_to_run` | The four deferred handler stages raise |
| `test_every_deferred_stage_is_declared` | The declared list matches the stages that actually refuse |
| `test_aws_adapters_all_refuse` | Every method on every `adapters/aws.py` class raises |
| `test_fixtures_are_labelled_as_fixtures` | Every fixture JSON carries a `$fixture` label |

The parallel boundary on the driver side is `workload/tests/test_boundary.py`, which confines the SDK to `src/index.py` and `driver/awsio.py` and asserts that spending requires `--authorize-aws-spend` **per invocation**.

**Do not weaken any of these to make AWS work.** If a real adapter needs an SDK, that is a boundary decision for Vishwa (section 11, item 5) — resolve it by agreeing where the adapter lives, not by deleting the test.

### 7. Simulated data announces itself

Every fixture carries `$fixture`. Every `Observation` from a fixture carries `simulated=True`. That flag reaches the evidence bundle and the printed explanation. Never strip a label to make output read more convincingly.

---

## 10. First Task for Sanjay

> **Implement the real AWS `MetricDataSource` for CloudWatch, preserving `CloudWatchCollector` and the existing offline implementation.**

### Files to inspect first

| File | Why |
|---|---|
| `backend/lambda/changeproof/telemetry.py` | The `MetricDataSource` Protocol and `MetricQuery` — your input contract |
| `backend/lambda/changeproof/adapters/local.py` | `StaticMetricDataSource` — the shape to mirror |
| `backend/lambda/changeproof/thresholds.py` | `METRIC_DEFINITIONS` — statistic, unit, extra dimensions |
| `backend/lambda/changeproof/manifest.py` | `MetricWindow`, and `manifest.region` |
| `backend/tests/test_telemetry.py` | How the collector is tested offline |
| `architecture/telemetry-contract.md` | The gotchas, especially §5 (missing data), §6 (timing), §7 |

### Expected input / output

```python
class AwsMetricDataSource:
    def __init__(self, region: str) -> None: ...

    def fetch(
        self, queries: tuple[MetricQuery, ...], window: MetricWindow
    ) -> dict[str, list[float]]:
        """One cloudwatch:GetMetricData call. Returns {query_id: [values]}.

        Missing data returns an empty list or an absent key. Never a zero.
        """
```

### What must remain unchanged

- `CloudWatchCollector` — all folding, pairing and omission logic
- `MetricDataSource` Protocol signature
- `StaticMetricDataSource` — the offline path must keep working
- `compare.py`, `predict.py`, `models.py`, `ports.py`
- `METRIC_DEFINITIONS` and `METRICS_BY_RESOURCE_TYPE`
- Every test in `test_no_aws.py`

### Tests to add

1. **Request shape, offline** — assert the `MetricDataQueries` built from the 13 `MetricQuery` objects have the right `Id`, `Namespace`, `Dimensions`, `Stat`, `Period`. Use a stubbed client; no AWS call.
2. **`Operation=PutItem` survives** into the DynamoDB latency query specifically.
3. **Response parsing** — a canned `GetMetricData` response maps to `{query_id: [values]}`.
4. **Empty `Values` stays empty** — not zero-filled. This is the one most likely to regress.
5. **Pagination** — `GetMetricData` returns `NextToken`; assert all pages are consumed.
6. **`test_no_aws.py` still passes** unchanged.

### How to verify in a sandbox

1. Pick **any existing Lambda function** in the sandbox. You do not need the ChangeProof test environment for this.
2. Hand-build one `MetricQuery` for it (`AWS/Lambda`, `FunctionName`, `Invocations`, `Sum`, 60).
3. Call `fetch()` over a window when you know there was traffic. Confirm datapoints come back.
4. Call it over a window when the function was idle. **Confirm you get an empty list, not zeros.** This is the behaviour the whole missing-data rule depends on.
5. Repeat for DynamoDB `SuccessfulRequestLatency` **with and without** `Operation`. Confirm that without it you get nothing, silently — so you have seen the failure mode yourself.

Cost: `GetMetricData` is billed per metric requested and is fractions of a cent at this volume.

---

## 11. Known Open Decisions / Risks

Genuinely unresolved in the repository. Items 1–4 are `architecture/telemetry-contract.md` §8, addressed to Vishwa.

| # | Open item | Current state |
|---|---|---|
| 1 | **`Duration`: `Average` or `p95`?** | Pinned to `Average` in code, with `test_duration_is_average_not_p95` asserting it deliberately. The contract flags `p95` as arguably better |
| 2 | **`GetMetricData` vs `GetMetricStatistics`** | Undecided. `GetMetricData` batches 13 metrics in one call and is the better fit |
| 3 | **Where the collector runs, and which IAM role needs `cloudwatch:GetMetricData`** | Undecided |
| 4 | **What the pipeline does when telemetry is materially incomplete** | Undecided — fail the experiment, or produce a verdict flagged low-evidence. The contract warns that leaving it open means it gets decided by accident |
| 5 | **Where a boto3-importing adapter may live** | Unresolved. `test_no_aws.py` forbids an SDK import anywhere under `backend/lambda/`, including `adapters/aws.py`. **This blocks section 10 as literally written** |
| 6 | **`TelemetrySource.collect(experiment_id)` cannot carry a manifest** | Worked around by constructor injection — `CloudWatchCollector(manifest, source)`. The Protocol was deliberately left unchanged. The decision is deferred, not made |
| 7 | **`Evidence` has no slot for run quality** | The driver produces `comparability`, `drained`, `dlqDepth`, `workloadFingerprint`. `Evidence` has only a free-form `notes` tuple. A fingerprint mismatch means the runs were not the same experiment — and would currently pass unnoticed |
| 8 | **`Observation.source`: `"aws-experiment"` vs `"cloudwatch"`** | The manifest says the former, the contract specifies the latter. They describe different things. The collector currently sets `"cloudwatch"` |
| 9 | **`PERIOD_SECONDS` lives in `telemetry.py`, not `thresholds.py`** | Placed with the query parameters rather than the thresholds. Move it if the team prefers the contract in one file |
| 10 | **`terraform/fixtures/deployed-at-10.tfstate` is unverified** | Documented as such. Terraform has never been run against it |
| 11 | **`workflow.json` never measures the baseline** | It applies the change before the only workload run. Known, documented, not yet fixed |
| 12 | **SQS activation delay** | A new queue is inactive; CloudWatch can delay metrics by **up to 15 minutes**. The driver mitigates with 5 activation messages and a 120s wait. **This reduces the risk; it does not remove it.** An empty baseline series is the signature — say so explicitly rather than reporting a zero baseline |

---

## 12. Handoff Summary

### What has already been built

A complete, deterministic reasoning engine (parse → graph → predict → compare → verdict), a complete but never-executed AWS experiment driver, a typed manifest boundary between them, a machine-readable metric contract covering all 13 resource/metric pairs, and a CloudWatch collector with all its query-building, folding and missing-data logic written and tested — **reading from a fixture**.

285 tests pass. pyright is clean. **Not one AWS call has been made.**

### What Sanjay now owns

Turning measurement from fixture-backed into real: the CloudWatch adapter first, then the AWS-backed evidence store, and eventually the telemetry parts of the deployed pipeline. Plus resolving open items 2, 3 and 4 with Vishwa, since they shape the adapter you write.

### The next concrete step

Implement `AwsMetricDataSource.fetch(queries, window) -> dict[str, list[float]]` against `cloudwatch:GetMetricData`.

Everything downstream of it already works. Do not change `CloudWatchCollector`, and do not weaken `test_no_aws.py` — if the SDK import has nowhere legal to live, that is open item 5 and needs Vishwa, not a deleted test.

Verify it against any existing sandbox Lambda before touching the experiment environment, and make sure you have personally seen an idle resource return **no datapoints rather than zeros**. The correctness of every verdict rests on that distinction.

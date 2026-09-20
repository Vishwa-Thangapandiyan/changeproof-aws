# Telemetry contract

What the experiment driver hands the CloudWatch collector, and what comes back.

Owners: experiment environment (Praanesh) produces it, `CloudWatchTelemetrySource`
(Sanjay) consumes it, the pipeline (Vishwa) carries it. The metric and statistic
table below is a **proposal to ratify**, not a decision already taken.

## 1. What you receive

The experiment manifest at `.changeproof/experiments/<id>/experiment-manifest.json`.
Two parts matter to telemetry: `environment.resources[]` and each run's
`metricWindow`.

```jsonc
// shape, not data: no experiment has been run
{
  "environment": {
    "region": "us-east-1",
    "resources": [{
      "graphAddress": "aws_lambda_function.api",     // the join key
      "resourceType": "aws_lambda_function",
      "physicalName": "changeproof-exp-142-api",
      "cloudwatch": {
        "namespace": "AWS/Lambda",
        "dimensions": [{ "Name": "FunctionName", "Value": "changeproof-exp-142-api" }]
      }
    }]
  },
  "baseline": { "metricWindow": { "startEpoch": 0, "endEpoch": 0 }, "drained": true, "dlqDepth": 0 },
  "changed":  { "metricWindow": { "startEpoch": 0, "endEpoch": 0 }, "drained": true, "dlqDepth": 0 }
}
```

**`graphAddress` is the join key** across the whole system: it is the Terraform
address, the node id in the dependency graph, and `ObservedMetric.resourceAddress`.
Never key anything on the physical name — it changes per experiment.

**Query `metricWindow`, never the raw timestamps.** It is already floored and
ceilinged to whole minutes with a tail past drain, because queue depth and message
age peak after the producer stops.

## 2. The mapping

| Graph address | Namespace | Dimension |
|---|---|---|
| `aws_lambda_function.api` | `AWS/Lambda` | `FunctionName` |
| `aws_lambda_function.worker` | `AWS/Lambda` | `FunctionName` |
| `aws_sqs_queue.work` | `AWS/SQS` | `QueueName` |
| `aws_dynamodb_table.orders` | `AWS/DynamoDB` | `TableName` |

## 3. Metrics, units and statistics — to ratify

Which metrics matter per resource type is already decided, in
`thresholds.METRICS_BY_RESOURCE_TYPE`. Read it from there rather than hardcoding a
second list. The **statistic** is not decided anywhere yet; this is the proposal:

| Resource | Metric | Statistic | Unit | Why this statistic |
|---|---|---|---|---|
| `aws_lambda_function` | `ConcurrentExecutions` | `Maximum` | Count | Peak concurrency is the thing the change moves; an average over a 60s window understates it badly |
| | `Duration` | `Average` | Milliseconds | Typical request cost. `p95` is arguably better and is worth discussing |
| | `Throttles` | `Sum` | Count | A count of rejections over the window, not a rate |
| | `Errors` | `Sum` | Count | Same |
| `aws_sqs_queue` | `ApproximateNumberOfMessagesVisible` | `Maximum` | Count | Peak backlog is the demo's headline signal |
| | `ApproximateAgeOfOldestMessage` | `Maximum` | Seconds | Worst wait experienced |
| `aws_dynamodb_table` | `ConsumedWriteCapacityUnits` | `Sum` | Count | Total work done |
| | `ThrottledRequests` | `Sum` | Count | |
| | `SuccessfulRequestLatency` | `Average` | Milliseconds | **Needs an `Operation` dimension** — see gotchas |

`period`: 60. Anything larger collapses a 60-second run to a single point.

## 4. What you return

An `Observation` per `ports.TelemetrySource`:

```python
Observation(
    source="cloudwatch",
    simulated=False,                 # the entire point of Phase 1
    metrics=(ObservedMetric(
        resource_address="aws_sqs_queue.work",
        metric="ApproximateNumberOfMessagesVisible",
        unit="Count",
        baseline_value=...,          # from the BASELINE run's window
        observed_value=...,          # from the CHANGED run's window
    ), ...),
)
```

One `ObservedMetric` carries **both** runs. Baseline window fills `baseline_value`,
changed window fills `observed_value`. That pairing is the collector's job; nothing
downstream re-associates them.

## 5. Missing data stays missing

Agreed rule, and AWS makes it load-bearing rather than theoretical:

> "Missing data, or data representing zero, can't be visualized in the CloudWatch
> metrics for Amazon SQS for the time period that your Amazon SQS queue was
> inactive."

So an idle queue returns **no datapoints, not zeros**. A baseline run where the
change had little effect is exactly the case that produces an empty series.

- **Never substitute 0 for an absent datapoint.** `baselineValue = 0` with
  `observedValue = 2680` is an infinite increase and a guaranteed breach; the
  engine's `change_pct` handles a genuine zero baseline with an absolute limit, and
  feeding it a fabricated one produces a fabricated verdict.
- Omit the `ObservedMetric` entirely when either side is missing. `compare.py`
  already handles an absent metric: it scores as a miss against prediction accuracy
  and does not create a breach.
- Record what was missing and why. A run whose metrics were half absent should be
  visible as such, not quietly thin.

## 6. Timing

- SQS pushes metrics at **one-minute intervals** for queues CloudWatch considers
  active; a queue counts as active for up to six hours after any message or API call.
- **A newly created queue is inactive, and activation can delay metrics by up to 15
  minutes.** Every experiment provisions a brand new queue, so this lands squarely on
  the baseline run. The driver mitigates it by sending a handful of messages
  immediately after provisioning and idling `--activation-wait` (default 120s) before
  the first measured run. That reduces the risk; it does not eliminate it.
- **Do not query the moment a window closes.** Datapoints land after the fact. Poll
  with a backoff until the expected number of points is present or a deadline passes,
  then report what you got.
- If the baseline series is empty and the changed series is not, say so explicitly.
  That is the signature of the activation delay, not of a zero baseline.

## 7. Gotchas

- **`SuccessfulRequestLatency` requires `TableName` *and* `Operation`.** Query with
  `Operation=PutItem` — the worker's only write. Without the second dimension the
  query returns nothing, silently.
- **`ConcurrentExecutions` per function** is published when the function has reserved
  concurrency, which both of ours do. Do not fall back to the account-level metric;
  it aggregates every function in the account.
- **`Throttles` on the worker** is the interesting one for the demo, not the api's.
- **`dlqDepth > 0` in a run record means messages failed permanently.** The queue
  still drained, so nothing else flags it. Treat that run's metrics as understating
  the work offered.
- **`drained: false`** means the window closed with work in flight. Metrics are still
  real, but the two runs are not cleanly comparable.

## 8. To agree with Vishwa

1. The statistics in section 3 — particularly `Duration`: `Average` or `p95`.
2. Whether `GetMetricData` or `GetMetricStatistics`. `GetMetricData` batches many
   metrics in one call and is the better fit for ~13 metrics across two windows.
3. Where the collector runs, and therefore which IAM role needs
   `cloudwatch:GetMetricData`.
4. What the pipeline does when telemetry comes back materially incomplete: fail the
   experiment, or produce a verdict flagged as low-evidence. Neither is obviously
   right; leaving it undecided means it gets decided by accident.

Sources: [SQS CloudWatch monitoring](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/monitoring-using-cloudwatch.html),
[available SQS metrics](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)

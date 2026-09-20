# Experiment records: DynamoDB tables and S3 layout

Draft for review. Derived from what the pipeline already emits — the evidence bundle
in `.changeproof/experiments/<id>/evidence.json` and the experiment manifest from
`workload/driver` — rather than designed independently. Nothing here is implemented.

## The split

**S3 holds artifacts. DynamoDB holds the state you query.**

The evidence bundle for the demo change is already a few hundred lines: 13 predicted
metrics, 3 dependency paths with hops, rule traces with threshold bands. It will grow
with the graph. DynamoDB items cap at 400 KB, and none of that nested detail is ever
filtered or sorted on.

So: the full bundle goes to S3 as one immutable object, and DynamoDB stores the
handful of facts a dashboard or a calibration query actually needs, plus the S3
pointer. Nothing is duplicated between them except the experiment id.

## Access patterns first

The schema falls out of these. If a pattern is missing, say so before building.

| # | Pattern | Frequency |
|---|---|---|
| 1 | Get one experiment's status and verdict by id | Very high — dashboard polls during a run |
| 2 | List experiments newest first, by status | High — dashboard home |
| 3 | Get the breaches that caused a rejection | Medium |
| 4 | Get predicted-vs-actual per metric for one experiment | Medium — the feedback loop |
| 5 | Get every (predicted, actual) pair for a given metric, across experiments | Low, but it is the point — this is what recalibrates `METRIC_SENSITIVITY` |
| 6 | Get the run windows and workload spec for one experiment | Low — re-collecting telemetry, debugging |
| 7 | List experiments for a repo / commit | Not yet — no GitHub integration |

## One table: `changeproof-experiments`

Single-table design. Four item types under one partition per experiment, so
"everything about experiment X" is one `Query` rather than four `GetItem`s.

```text
PK = EXP#<experimentId>
SK = META | RUN#<label> | BREACH#<resource>#<metric> | METRIC#<resource>#<metric>
```

Billing: `PAY_PER_REQUEST`. Free-tier eligible, bills nothing while idle, and the
demo writes a few dozen items per experiment.

### Item type 1 — `META`

One per experiment. The dashboard's row, and pattern 1.

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `PK` / `SK` | S | — | `EXP#<id>` / `META` |
| `experimentId` | S | `evidence.experimentId` | e.g. `EXP-DEMO-001` |
| `status` | S | pipeline | `CREATING_ENVIRONMENT` \| `RUNNING_BASELINE` \| `APPLYING_CHANGE` \| `RUNNING_CHANGED` \| `COLLECTING_METRICS` \| `COMPARING` \| `COMPLETED` \| `FAILED` \| `REJECTED_WITHOUT_TESTING` |
| `verdict` | S | `comparison.verdict` | `APPROVE` \| `REJECT`. **Absent until the comparison runs** — a status of `COMPLETED` with no verdict is a bug |
| `observedSeverity` | S | `comparison.observedSeverity` | `LOW`…`CRITICAL` |
| `predictedSeverity` | S | `prediction.severity` | Kept separate from observed, deliberately |
| `predictionAccuracy` | N | `comparison.predictionAccuracy` | 0–1. **Never an input to `verdict`.** Stored so it can be tracked, not so it can decide |
| `confidence` | N | `prediction.confidence` | |
| `blastRadiusCount` | N | `prediction.blastRadius.resourceCount` | 4 for the demo change |
| `changeSummary` | S | `changeSet.changes[]` | Human-readable: `aws_lambda_function.api reserved_concurrent_executions 10 -> 100` |
| `changedAddresses` | SS | `changeSet.changes[].address` | String set, for a cheap filter |
| `phase` | S | `evidence.phase` | `phase-0-local`, `phase-1-aws` |
| `telemetrySource` | S | `observation.source` | `fixture:telemetry_observed.json` or `cloudwatch` |
| `simulated` | BOOL | `observation.simulated` | **Mandatory. See "The one attribute nobody may drop".** |
| `reason` | S | `comparison.reason` | One line, already composed by the engine |
| `evidenceKey` | S | S3 | `experiments/<id>/evidence.json` |
| `createdAt` / `updatedAt` | S | — | ISO-8601 UTC, `2026-09-20T06:14:00Z` |
| `completedAt` | S | — | Absent while running |
| `ttl` | N | — | Epoch seconds. See "Expiry" |
| `GSI1PK` / `GSI1SK` | S | — | `STATUS#<status>` / `createdAt` |

### Item type 2 — `RUN#<label>`

Two per experiment: `RUN#baseline`, `RUN#changed`. Straight from the driver's
manifest (`workload/README.md` sections 9 and 10). Pattern 6.

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `SK` | S | — | `RUN#baseline` \| `RUN#changed` |
| `label` | S | `RunRecord.label` | |
| `configuration` | M | `RunRecord.configuration` | `{reserved_concurrency, worker_concurrency}` |
| `workloadFingerprint` | S | `RunRecord` | SHA-256 prefix. **Both runs must match or the comparison is void** |
| `attempted` / `accepted` / `throttled` / `failed` | N | `result` | Attempts are the controlled quantity, not successes |
| `achievedRatePerSecond` | N | `result` | |
| `windowStartEpoch` / `windowEndEpoch` | N | `metricWindow` | What CloudWatch was actually queried with |
| `windowStartIso` / `windowEndIso` | S | `metricWindow` | Same instants, for humans |
| `attemptsStartedAt` / `attemptsEndedAt` / `drainedAt` | S | `timestamps` | |
| `drained` | BOOL | `RunRecord.drained` | False means the window closed with work in flight — the run is suspect |
| `notes` | L of S | `RunRecord.notes` | |

### Item type 3 — `BREACH#<resourceAddress>#<metric>`

Zero or more. These are what produced the verdict. Pattern 3.

| Attribute | Type | Source |
|---|---|---|
| `resourceAddress` | S | `breach.resourceAddress` |
| `metric` | S | `breach.metric` |
| `actualValue` | N | `breach.actualValue` |
| `limit` | N | `breach.limit` |
| `limitKind` | S | `change_pct` \| `absolute` |
| `severity` | S | `breach.severity` |
| `description` | S | `breach.description` |

### Item type 4 — `METRIC#<resourceAddress>#<metric>`

One per compared metric, 13 for the demo change. This is the feedback-loop table:
patterns 4 and 5, and the only reason to store per-metric rows at all.

| Attribute | Type | Source | Notes |
|---|---|---|---|
| `resourceAddress` / `metric` | S | `metricComparison` | |
| `resourceType` | S | derived | `aws_lambda_function` etc. — needed to group across experiments |
| `unit` | S | `observedMetric.unit` | |
| `predictedLow` / `predictedHigh` | N | `metricComparison` | The band, nullable |
| `baselineValue` / `observedValue` | N | `observedMetric` | |
| `actualChangePct` | N | `observedMetric.changePct` | |
| `outcome` | S | `metricComparison.outcome` | `WITHIN_PREDICTION` \| `UNDERESTIMATED` \| `OVERESTIMATED` \| `UNPREDICTED` |
| `ruleId` | S | `prediction.rulesFired[].ruleId` | Which rule produced the band, so a bad rule is traceable |
| `simulated` | BOOL | `observation.simulated` | Repeated on purpose — see below |
| `GSI2PK` / `GSI2SK` | S | — | `METRIC#<resourceType>#<metric>` / `createdAt` |

## Indexes

**GSI1 — `by-status`** · PK `STATUS#<status>`, SK `createdAt` · pattern 2.
Query `STATUS#COMPLETED` descending for the dashboard. "Everything, newest first"
means querying the few live statuses, not scanning. Only `META` items carry GSI1
keys, so the index stays small.

**GSI2 — `by-metric`** · PK `METRIC#<resourceType>#<metric>`, SK `createdAt` ·
pattern 5. `METRIC#aws_sqs_queue#ApproximateNumberOfMessagesVisible` returns every
prediction ever made about queue depth alongside what actually happened. That query
is what recalibrates `METRIC_SENSITIVITY` in `thresholds.py`, which today is a set of
modelling assumptions with a comment admitting they are guesses.

Projection: `INCLUDE` the attributes above, not `ALL`. Index storage is billed.

## S3 layout

Bucket `changeproof-evidence-<account>-<region>`, keys mirroring the Phase 0 local
layout exactly so the swap is a swap and not a redesign:

```text
experiments/<experimentId>/
    evidence.json            the full bundle: changeSet, prediction, observation, comparison
    experiment-manifest.json the driver's output: environment, runs, comparability
    terraform-plan.json      the plan the change was parsed from
    explanation.txt          deterministic now, Bedrock later
    telemetry/baseline.json  raw CloudWatch response
    telemetry/changed.json   raw CloudWatch response
```

Settings: block all public access, SSE-S3, versioning on (evidence is a record — an
overwrite should be recoverable), lifecycle expiry to match the DynamoDB TTL.

Keep the raw CloudWatch responses. When a verdict is disputed, "here is exactly what
the API returned" ends the argument; a re-derived number does not.

## The one attribute nobody may drop

`simulated` must be written on every item that carries a measurement, and it must
come from `observation.simulated` rather than being assumed.

Phase 0 telemetry is a hand-authored fixture. Those numbers are already realistic
enough to be mistaken for real ones, and once they are sitting in a DynamoDB table
next to real ones with no flag, nothing distinguishes them. `test_fixtures_are_labelled_as_fixtures`
enforces this at the file level; the table needs the same discipline.

Any query that feeds calibration must filter `simulated = false`.

## Gotchas

- **`float` does not serialise.** boto3 rejects Python floats; use
  `decimal.Decimal(str(value))`. Everything numeric here comes from the engine as a
  float, so this will bite on the first write.
- **Empty string is not a valid attribute value** in older SDK behaviour and is still
  a trap. Omit the attribute instead.
- **`verdict` is absent, not `PENDING`**, until the comparison runs. A sentinel
  invites someone to treat "not decided" as a decision.
- **Write `META` before the run starts**, with `status=CREATING_ENVIRONMENT`, then
  `UpdateItem` as the pipeline advances. If the pipeline crashes, a partial record
  with a stuck status is far more useful than no record.
- **Do not store `predictionAccuracy` and `verdict` in a combined field**, and do not
  add a computed "score" that mixes them. They answer different questions, and the
  engine keeps them apart deliberately (CLAUDE.md section 3).

## Expiry

Set `ttl` on `META` to roughly 30 days out, and a matching S3 lifecycle rule. Demo
data should not accumulate indefinitely, and a TTL that exists from the start is
easier than a cleanup script later. Raise it when there is real prediction history
worth keeping.

## Open questions for whoever builds this

1. Is one table the right call, or would three simple tables be easier for the team
   to work with? Single-table is the idiomatic answer and makes pattern 4 one query;
   it is also the harder one to read.
2. Should `METRIC#` rows exist in Phase 1 at all, given the only consumer
   (calibration) needs many experiments before it says anything? They are cheap, and
   backfilling them later means reprocessing S3.
3. Who writes the record — the Step Functions workflow at each stage, or one Lambda
   at the end? Per-stage writes give a live dashboard; one write at the end is
   simpler and atomic.

# ChangeProof

**Don't just review the change. Prove it.**

ChangeProof is an AWS-native infrastructure change verification system. It takes a
proposed Terraform change, works out what depends on the resource being changed,
predicts the impact deterministically, applies the change in an isolated test
environment, generates workload, observes what actually happens, compares prediction
against reality, and produces an approve/reject decision backed by evidence.

The decision comes from measured telemetry and explicit thresholds. Bedrock explains
that evidence; it never originates it.

## Status: Phase 0 — local, complete, tested

Phase 0 runs the full reasoning chain on a laptop with **zero AWS credentials and
zero AWS spend**:

```
Terraform plan JSON  ->  parse_change
                     ->  local dependency graph
                     ->  deterministic impact prediction
                     ->  fixture telemetry
                     ->  predicted-vs-actual comparison
                     ->  local evidence JSON
                     ->  local deterministic explanation
```

What is real: the parser, the graph traversal, the prediction rules, the comparison
engine, the verdict, the evidence bundle and the explanation. All of it is
deterministic and unit-tested.

What is not yet built: test environment provisioning, applying the change to it, and
synthetic workload generation. Those stages raise `PhaseNotAuthorizedError` rather
than returning a plausible-looking result. Telemetry currently comes from a fixture
that is labelled as simulated everywhere it surfaces.

## Try it

```bash
python scripts/demo.py
```

No credentials needed, nothing is installed, nothing is deployed. Exit code is 0 on
approval and 1 on rejection, so it drops into CI unchanged.

The demo change is `aws_lambda_function.api` reserved concurrency `10 -> 100`. The
engine predicts HIGH reliability risk across a four-resource blast radius, the
fixture telemetry shows the worker cannot keep up, three safety limits are breached,
and the change is rejected. Prediction accuracy lands at 25% — the engine
under-predicted the queue backlog, and says so.

## Tests

```bash
python -m pytest backend/tests -q     # 118 tests
pyright                               # 0 errors
```

`backend/tests/test_no_aws.py` enforces the Phase 0 boundary directly: no AWS SDK may
be imported anywhere under `backend/lambda/`, importing the pipeline must not load one,
any socket connection during a run fails the test, the pipeline must work with every
`AWS_*` environment variable stripped, and every deferred stage must refuse to run.

## Layout

```
backend/lambda/handler.py            Lambda entry point; dispatches on `stage`
backend/lambda/changeproof/
  models.py                          frozen, JSON-serializable types
  ports.py                           Protocols for every AWS-backed capability
  parser.py                          terraform show -json  ->  ChangeSet
  graph.py                           dependency traversal and blast radius
  thresholds.py                      every threshold, in one place
  predict.py                         deterministic prediction rules
  compare.py                         safety limits, verdict, prediction accuracy
  pipeline.py                        the whole chain, wired locally
  adapters/local.py                  Phase 0 implementations (these run)
  adapters/aws.py                    Phase 1/2 placeholders (these refuse)
  fixtures/                          labelled test data
backend/state_machine/workflow.json  Step Functions ASL, validated not deployed
terraform/                           demo target stack, planned offline never applied
scripts/demo.py                      run the pipeline
scripts/generate_plan.sh             regenerate plan JSON offline via Terraform CLI
```

## Design rules

**The verdict is deterministic.** It comes from applying `SAFETY_LIMITS` to measured
values. An accurate prediction does not excuse an unsafe outcome, and an inaccurate
prediction does not condemn a safe one. Both cases are covered by tests.

**The explainer may only restate the evidence.** `DeterministicExplainer` builds prose
from templates over the evidence bundle. `BedrockExplainer` will inherit exactly the
same contract. If the explanation layer ever influences the verdict, the architecture
has been violated.

**Simulated data is always labelled.** Every fixture carries a `$fixture` marker, every
`Observation` from a fixture carries `simulated=True`, and that flag reaches the
evidence JSON and the printed explanation. A test asserts the labels exist.

**Budget is a design constraint.** Neptune and RDS are forbidden — no free tier, and
they bill while idle. The dependency graph runs locally behind a Neptune-shaped
interface; DynamoDB is the terminal datastore in the demo stack rather than RDS.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Local engine: parse, graph, predict, compare, evidence, explain | Complete |
| 1 | Free-tier AWS: Lambda, Step Functions, S3, DynamoDB, API Gateway, real CloudWatch telemetry, test environment provisioning | Not started, needs authorization |
| 2 | Bedrock explanation | Not started, needs authorization |

Phases do not advance without explicit authorization. See `CLAUDE.md` for the full
constraint set.

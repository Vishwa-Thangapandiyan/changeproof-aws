"""The structured result of every experiment operation.

The driver's contract with the rest of ChangeProof is these dataclasses, serialised
to JSON. Nothing downstream should ever have to parse a log line: `create_experiment`,
`run_baseline`, `run_changed` and `cleanup_experiment` each return one of these, and
the CLI prints them.

Two audiences:

- the pipeline, which needs resource identifiers to hand to the telemetry collector
- the telemetry collector, which needs a measurement window per run

JSON keys are camelCase, matching the evidence bundle the engine already emits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: CloudWatch namespaces, by Terraform resource type. The telemetry collector needs
#: the namespace and dimension to query; which *metrics* to query is not the driver's
#: business and stays in `thresholds.METRICS_BY_RESOURCE_TYPE`.
NAMESPACES: dict[str, str] = {
    "aws_lambda_function": "AWS/Lambda",
    "aws_sqs_queue": "AWS/SQS",
    "aws_dynamodb_table": "AWS/DynamoDB",
}

DIMENSION_NAMES: dict[str, str] = {
    "aws_lambda_function": "FunctionName",
    "aws_sqs_queue": "QueueName",
    "aws_dynamodb_table": "TableName",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


@dataclass(frozen=True)
class ResourceIdentity:
    """One resource, addressed the way each consumer needs to address it.

    `graph_address` is the Terraform address, which is also the node id in the
    dependency graph and the `resourceAddress` on every ObservedMetric. It is the
    join key across the whole system.
    """

    graph_address: str
    resource_type: str
    physical_name: str
    arn: str | None = None
    queue_url: str | None = None
    log_group: str | None = None

    @property
    def namespace(self) -> str:
        return NAMESPACES[self.resource_type]

    @property
    def dimension_name(self) -> str:
        return DIMENSION_NAMES[self.resource_type]

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "graphAddress": self.graph_address,
            "resourceType": self.resource_type,
            "physicalName": self.physical_name,
            "cloudwatch": {
                "namespace": self.namespace,
                "dimensions": [{"Name": self.dimension_name, "Value": self.physical_name}],
            },
        }
        if self.arn:
            document["arn"] = self.arn
        if self.queue_url:
            document["queueUrl"] = self.queue_url
        if self.log_group:
            document["logGroup"] = self.log_group
        return document


@dataclass(frozen=True)
class ExperimentEnvironment:
    """What `create_experiment` produced, and where it lives."""

    experiment_id: str
    region: str
    environment_suffix: str
    terraform_dir: str
    state_path: str
    resources: tuple[ResourceIdentity, ...]
    configuration: dict[str, int]
    created_at: datetime = field(default_factory=utc_now)

    def resource(self, graph_address: str) -> ResourceIdentity:
        for candidate in self.resources:
            if candidate.graph_address == graph_address:
                return candidate
        raise KeyError(f"no resource in this environment at address {graph_address}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "region": self.region,
            "environmentSuffix": self.environment_suffix,
            "terraformDir": self.terraform_dir,
            "statePath": self.state_path,
            "configuration": dict(self.configuration),
            "createdAt": iso(self.created_at),
            "resources": [resource.to_dict() for resource in self.resources],
        }


@dataclass(frozen=True)
class WorkloadSpec:
    """The workload definition, identical for every run in an experiment.

    `attempts` rather than successes is the controlled quantity. Under the baseline
    configuration most invocations are rejected by the concurrency cap; under the
    changed configuration most succeed. That difference is the effect being measured,
    so holding *successes* constant would erase the very thing the experiment exists
    to observe.
    """

    attempts: int = 3000
    duration_seconds: int = 60
    payload_bytes: int = 256
    warmup_attempts: int = 50
    client_concurrency: int = 64

    @property
    def target_rate(self) -> float | None:
        """Attempts per second, or None when the run is an unpaced burst.

        `duration_seconds = 0` means "issue every attempt as fast as the client
        can", which has no target rate. It is a legitimate spec, so it returns None
        rather than raising or reporting a rate it is not pacing to.
        """
        if self.duration_seconds <= 0:
            return None
        return self.attempts / self.duration_seconds

    def fingerprint(self) -> str:
        """Stable hash of the spec.

        Recorded on both runs. If the two fingerprints differ, the runs were not
        driven by the same workload and are not comparable, whatever the metrics say.
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "durationSeconds": self.duration_seconds,
            "payloadBytes": self.payload_bytes,
            "warmupAttempts": self.warmup_attempts,
            "clientConcurrency": self.client_concurrency,
            "targetRatePerSecond": round(self.target_rate, 3) if self.target_rate is not None else None,
        }


@dataclass(frozen=True)
class WorkloadResult:
    """What the generator actually achieved, as opposed to what it intended."""

    attempted: int
    accepted: int
    throttled: int
    failed: int
    achieved_rate: float
    slowest_attempt_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "accepted": self.accepted,
            "throttled": self.throttled,
            "failed": self.failed,
            "achievedRatePerSecond": round(self.achieved_rate, 3),
            "slowestAttemptMs": round(self.slowest_attempt_ms, 1),
        }


@dataclass(frozen=True)
class RunRecord:
    """One measured run: a configuration, a workload, and a window to measure it in."""

    run_id: str
    label: str
    configuration: dict[str, int]
    spec: WorkloadSpec
    result: WorkloadResult
    started_at: datetime
    attempts_started_at: datetime
    attempts_ended_at: datetime
    drained_at: datetime
    window_start: datetime
    window_end: datetime
    drained: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "label": self.label,
            "configuration": dict(self.configuration),
            "workload": self.spec.to_dict(),
            "workloadFingerprint": self.spec.fingerprint(),
            "result": self.result.to_dict(),
            "timestamps": {
                "startedAt": iso(self.started_at),
                "attemptsStartedAt": iso(self.attempts_started_at),
                "attemptsEndedAt": iso(self.attempts_ended_at),
                "drainedAt": iso(self.drained_at),
            },
            "metricWindow": {
                "startIso": iso(self.window_start),
                "endIso": iso(self.window_end),
                "startEpoch": epoch(self.window_start),
                "endEpoch": epoch(self.window_end),
            },
            "drained": self.drained,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TeardownReport:
    destroyed_at: datetime
    resources_destroyed: int
    state_empty: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "destroyedAt": iso(self.destroyed_at),
            "resourcesDestroyed": self.resources_destroyed,
            "stateEmpty": self.state_empty,
            "notes": list(self.notes),
        }


def comparability(baseline: RunRecord, changed: RunRecord) -> dict[str, Any]:
    """Whether these two runs may honestly be compared.

    This is a report, not a verdict. It belongs to the evidence bundle so that a
    reviewer can see what was held constant, and so that a comparison drawn across
    two runs that were not actually comparable is visibly so rather than silently
    wrong.
    """
    attempt_delta = 0.0
    if baseline.result.attempted:
        attempt_delta = abs(changed.result.attempted - baseline.result.attempted) / baseline.result.attempted * 100

    differing = sorted(
        key
        for key in set(baseline.configuration) | set(changed.configuration)
        if baseline.configuration.get(key) != changed.configuration.get(key)
    )

    return {
        "workloadFingerprintMatch": baseline.spec.fingerprint() == changed.spec.fingerprint(),
        "attemptDeltaPct": round(attempt_delta, 3),
        "bothRunsDrained": baseline.drained and changed.drained,
        "configurationDeltaKeys": differing,
        "singleVariable": len(differing) == 1,
        "order": [record.label for record in sorted((baseline, changed), key=lambda run: run.started_at)],
        "windowSecondsBaseline": epoch(baseline.window_end) - epoch(baseline.window_start),
        "windowSecondsChanged": epoch(changed.window_end) - epoch(changed.window_start),
    }

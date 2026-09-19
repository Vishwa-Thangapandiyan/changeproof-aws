"""Every threshold and modelling constant in the system, in one auditable place.

Two distinct families live here and must not be confused:

`PREDICTION_*` shape what ChangeProof expects to happen. Being wrong about them
costs accuracy, not safety.

`SAFETY_LIMITS` decide whether an observed outcome is acceptable. They are the
verdict. They are applied to measured values only, never to predictions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Severity

# --- prediction model ----------------------------------------------------------------

#: How far downstream pressure is traced from a changed resource.
PREDICTION_MAX_DEPTH = 4

#: Half-width of the predicted band, as a fraction of the point estimate. A point
#: estimate of +600% with a 0.35 band predicts +390% to +810%.
PREDICTION_BAND = 0.35

#: How strongly a given metric responds to upstream concurrency pressure.
#: 1.0 means the metric tracks the pressure multiplier directly; lower values mean
#: the metric absorbs some of it. These are modelling assumptions, not measurements,
#: and are the first thing to recalibrate once real predicted-vs-actual data exists.
METRIC_SENSITIVITY: dict[str, float] = {
    "ConcurrentExecutions": 1.0,
    "Invocations": 1.0,
    "ApproximateNumberOfMessagesVisible": 1.0,
    "ConsumedWriteCapacityUnits": 1.0,
    "Throttles": 1.0,
    "ThrottledRequests": 1.0,
    "ApproximateAgeOfOldestMessage": 0.6,
    "Duration": 0.35,
    "SuccessfulRequestLatency": 0.35,
    "Errors": 0.5,
}

#: Metrics worth predicting for each resource type.
METRICS_BY_RESOURCE_TYPE: dict[str, tuple[str, ...]] = {
    "aws_lambda_function": ("ConcurrentExecutions", "Duration", "Throttles", "Errors"),
    "aws_sqs_queue": ("ApproximateNumberOfMessagesVisible", "ApproximateAgeOfOldestMessage"),
    "aws_dynamodb_table": ("ConsumedWriteCapacityUnits", "ThrottledRequests", "SuccessfulRequestLatency"),
}

#: Concurrency increase multiplier, mapped to a predicted reliability severity.
#: CRITICAL is reserved for changes so extreme that spending a test environment on
#: them is not worth it; everything below it must still be verified experimentally.
#: The 10x demo change sits in HIGH deliberately, because verifying it is the point.
CONCURRENCY_SEVERITY_BANDS: tuple[tuple[float, Severity], ...] = (
    (1.5, Severity.LOW),
    (3.0, Severity.MEDIUM),
    (20.0, Severity.HIGH),
    (float("inf"), Severity.CRITICAL),
)

#: Confidence starts here and decays with the depth of the blast radius, because a
#: prediction four hops from the change is a weaker claim than one hop from it.
PREDICTION_BASE_CONFIDENCE = 0.85
PREDICTION_CONFIDENCE_DECAY_PER_HOP = 0.06
PREDICTION_MIN_CONFIDENCE = 0.4


# --- safety limits: these decide the verdict -----------------------------------------


@dataclass(frozen=True)
class SafetyLimit:
    """A limit applied to a measured value.

    `kind` is either "change_pct" (relative to the baseline) or "absolute"
    (the observed value itself). Absolute limits exist because some metrics are
    zero at baseline, where a percentage is undefined and a single non-zero
    reading is already a failure.
    """

    metric: str
    kind: str
    limit: float
    severity: Severity
    description: str


SAFETY_LIMITS: tuple[SafetyLimit, ...] = (
    SafetyLimit(
        metric="Throttles",
        kind="absolute",
        limit=0.0,
        severity=Severity.HIGH,
        description="Lambda throttling means requests were rejected outright.",
    ),
    SafetyLimit(
        metric="ThrottledRequests",
        kind="absolute",
        limit=0.0,
        severity=Severity.HIGH,
        description="DynamoDB throttled requests mean writes were rejected.",
    ),
    SafetyLimit(
        metric="Errors",
        kind="absolute",
        limit=0.0,
        severity=Severity.HIGH,
        description="Function errors appeared under the test workload.",
    ),
    SafetyLimit(
        metric="Duration",
        kind="change_pct",
        limit=100.0,
        severity=Severity.HIGH,
        description="Function duration more than doubled under the test workload.",
    ),
    SafetyLimit(
        metric="SuccessfulRequestLatency",
        kind="change_pct",
        limit=100.0,
        severity=Severity.HIGH,
        description="Datastore latency more than doubled under the test workload.",
    ),
    SafetyLimit(
        metric="ApproximateNumberOfMessagesVisible",
        kind="change_pct",
        limit=300.0,
        severity=Severity.HIGH,
        description="Queue depth grew more than fourfold, indicating the consumer cannot keep up.",
    ),
    SafetyLimit(
        metric="ApproximateAgeOfOldestMessage",
        kind="change_pct",
        limit=200.0,
        severity=Severity.MEDIUM,
        description="Messages are waiting substantially longer before being processed.",
    ),
    SafetyLimit(
        metric="ConsumedWriteCapacityUnits",
        kind="change_pct",
        limit=400.0,
        severity=Severity.MEDIUM,
        description="Write capacity consumption rose sharply, with cost implications.",
    ),
)

#: An observed severity at or above this level rejects the change.
REJECT_AT_OR_ABOVE = Severity.HIGH


def limits_for(metric: str) -> tuple[SafetyLimit, ...]:
    return tuple(limit for limit in SAFETY_LIMITS if limit.metric == metric)

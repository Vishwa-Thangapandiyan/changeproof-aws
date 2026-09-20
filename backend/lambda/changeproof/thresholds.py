"""Every threshold and modelling constant in the system, in one auditable place.

Three distinct families live here and must not be confused:

`PREDICTION_*` shape what ChangeProof expects to happen. Being wrong about them
costs accuracy, not safety.

`SAFETY_LIMITS` decide whether an observed outcome is acceptable. They are the
verdict. They are applied to measured values only, never to predictions.

`METRIC_DEFINITIONS` say how each metric is read out of CloudWatch: which
statistic collapses the window, what unit the number is in, and any dimension the
metric needs beyond the ones that identify the resource. They describe collection,
not risk, and nothing here decides anything.
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


# --- how each metric is collected ----------------------------------------------------


@dataclass(frozen=True)
class MetricDefinition:
    """How one metric is read out of CloudWatch.

    `statistic` collapses a window of datapoints into the single number the engine
    compares. It is a property of what the metric means, not of any one experiment:
    a peak is the right reading for concurrency and queue depth, a total for counts
    of rejections, a mean for per-request cost.

    `extra_dimensions` are dimensions this metric needs *in addition to* the one
    that identifies the resource. They are (Name, Value) pairs in CloudWatch's own
    casing. They live here rather than in the experiment manifest because they are a
    property of the metric, not of the environment: the manifest's dimension list is
    attached to a resource, and adding one there would wrongly narrow every other
    metric on that same resource.
    """

    statistic: str
    unit: str
    extra_dimensions: tuple[tuple[str, str], ...] = ()


#: Valid CloudWatch statistics, so a typo fails a test rather than an API call.
STATISTICS = frozenset({"Average", "Maximum", "Minimum", "Sum", "SampleCount"})

#: Units used by the metrics in scope. CloudWatch defines more; these are the ones
#: our metrics report, and `ObservedMetric.unit` carries the value through.
UNITS = frozenset({"Count", "Milliseconds", "Seconds"})

#: One entry per distinct metric in METRICS_BY_RESOURCE_TYPE. The collector reads
#: which metrics to fetch from that mapping and how to fetch each one from here;
#: neither list may grow a metric the other does not have.
METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    # Lambda
    "ConcurrentExecutions": MetricDefinition(statistic="Maximum", unit="Count"),
    "Duration": MetricDefinition(statistic="Average", unit="Milliseconds"),
    "Throttles": MetricDefinition(statistic="Sum", unit="Count"),
    "Errors": MetricDefinition(statistic="Sum", unit="Count"),
    # SQS
    "ApproximateNumberOfMessagesVisible": MetricDefinition(statistic="Maximum", unit="Count"),
    "ApproximateAgeOfOldestMessage": MetricDefinition(statistic="Maximum", unit="Seconds"),
    # DynamoDB
    "ConsumedWriteCapacityUnits": MetricDefinition(statistic="Sum", unit="Count"),
    "ThrottledRequests": MetricDefinition(statistic="Sum", unit="Count"),
    # SuccessfulRequestLatency is published per operation. Queried with TableName
    # alone it returns no datapoints at all, silently, which is indistinguishable
    # from a genuinely idle table. PutItem is the worker's only write.
    "SuccessfulRequestLatency": MetricDefinition(
        statistic="Average",
        unit="Milliseconds",
        extra_dimensions=(("Operation", "PutItem"),),
    ),
}


def definition_for(metric: str) -> MetricDefinition:
    """How to collect `metric`.

    Raises rather than returning a default: a metric the engine asks for but cannot
    describe is a gap in this table, and guessing a statistic would silently produce
    a number that means something other than what the safety limits assume.
    """
    try:
        return METRIC_DEFINITIONS[metric]
    except KeyError:
        known = ", ".join(sorted(METRIC_DEFINITIONS))
        raise ValueError(
            f"no collection definition for metric {metric!r}; defined metrics are: {known}"
        ) from None

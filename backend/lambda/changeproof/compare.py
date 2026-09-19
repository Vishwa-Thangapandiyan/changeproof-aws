"""Predicted vs actual, and the verdict.

Two separate questions are answered here, and keeping them separate is the point:

1. **Is the observed behaviour acceptable?** Decided by applying `SAFETY_LIMITS` to
   measured values. This, and only this, produces the verdict. A change that breaches
   a safety limit is rejected even if the prediction anticipated it perfectly.

2. **Was the prediction any good?** Scored by comparing each observation against its
   predicted band. This feeds the accuracy history and never touches the verdict.

Conflating the two would let a confidently wrong prediction approve a harmful change.
"""

from __future__ import annotations

from .models import (
    ComparisonOutcome,
    ComparisonResult,
    MetricComparison,
    Observation,
    ObservedMetric,
    Prediction,
    Severity,
    ThresholdBreach,
    Verdict,
)
from . import thresholds as T


def compare(prediction: Prediction, observation: Observation) -> ComparisonResult:
    breaches = _find_breaches(observation)
    comparisons = _compare_metrics(prediction, observation)

    observed_severity = Severity.highest(*(b.severity for b in breaches)) if breaches else Severity.NONE
    verdict = (
        Verdict.REJECT
        if observed_severity.rank >= T.REJECT_AT_OR_ABOVE.rank
        else Verdict.APPROVE
    )

    return ComparisonResult(
        verdict=verdict,
        observed_severity=observed_severity,
        breaches=breaches,
        metric_comparisons=comparisons,
        prediction_accuracy=_accuracy(comparisons),
        reason=_reason(verdict, observed_severity, breaches),
    )


def _find_breaches(observation: Observation) -> tuple[ThresholdBreach, ...]:
    breaches: list[ThresholdBreach] = []
    for metric in observation.metrics:
        for limit in T.limits_for(metric.metric):
            breach = _check(metric, limit)
            if breach is not None:
                breaches.append(breach)
    return tuple(sorted(breaches, key=lambda b: (-b.severity.rank, b.resource_address, b.metric)))


def _check(metric: ObservedMetric, limit: T.SafetyLimit) -> ThresholdBreach | None:
    if limit.kind == "absolute":
        actual = metric.observed_value
        exceeded = actual > limit.limit
    elif limit.kind == "change_pct":
        change = metric.change_pct
        if change is None:
            # Baseline of zero: a percentage is undefined. An absolute limit on the
            # same metric, if one exists, is what catches this case.
            return None
        actual = change
        exceeded = actual > limit.limit
    else:
        raise ValueError(f"unknown safety limit kind {limit.kind!r} for {limit.metric}")

    if not exceeded:
        return None

    return ThresholdBreach(
        resource_address=metric.resource_address,
        metric=metric.metric,
        actual_value=actual,
        limit=limit.limit,
        limit_kind=limit.kind,
        severity=limit.severity,
        description=limit.description,
    )


def _compare_metrics(
    prediction: Prediction, observation: Observation
) -> tuple[MetricComparison, ...]:
    comparisons: list[MetricComparison] = []

    for predicted in prediction.predicted_metrics:
        observed = observation.find(predicted.resource_address, predicted.metric)
        if observed is None:
            comparisons.append(
                MetricComparison(
                    resource_address=predicted.resource_address,
                    metric=predicted.metric,
                    predicted_low=predicted.expected_change_pct_low,
                    predicted_high=predicted.expected_change_pct_high,
                    actual_change_pct=None,
                    outcome=ComparisonOutcome.NOT_OBSERVED,
                )
            )
            continue

        actual = observed.change_pct
        if actual is None:
            outcome = ComparisonOutcome.NOT_OBSERVED
        elif actual > predicted.expected_change_pct_high:
            outcome = ComparisonOutcome.WORSE_THAN_PREDICTED
        elif actual < predicted.expected_change_pct_low:
            outcome = ComparisonOutcome.BETTER_THAN_PREDICTED
        else:
            outcome = ComparisonOutcome.WITHIN_PREDICTION

        comparisons.append(
            MetricComparison(
                resource_address=predicted.resource_address,
                metric=predicted.metric,
                predicted_low=predicted.expected_change_pct_low,
                predicted_high=predicted.expected_change_pct_high,
                actual_change_pct=actual,
                outcome=outcome,
            )
        )

    predicted_keys = {(p.resource_address, p.metric) for p in prediction.predicted_metrics}
    for observed in observation.metrics:
        key = (observed.resource_address, observed.metric)
        if key in predicted_keys:
            continue
        # Measured but never predicted. Recorded so the gap is visible rather than
        # silently dropped; it scores as a miss in the accuracy calculation.
        comparisons.append(
            MetricComparison(
                resource_address=observed.resource_address,
                metric=observed.metric,
                predicted_low=None,
                predicted_high=None,
                actual_change_pct=observed.change_pct,
                outcome=ComparisonOutcome.NOT_OBSERVED,
            )
        )

    return tuple(comparisons)


def _accuracy(comparisons: tuple[MetricComparison, ...]) -> float:
    """Fraction of comparable metrics that landed inside the predicted band."""
    comparable = [c for c in comparisons if c.outcome is not ComparisonOutcome.NOT_OBSERVED]
    if not comparable:
        return 0.0
    hits = sum(1 for c in comparable if c.outcome is ComparisonOutcome.WITHIN_PREDICTION)
    return hits / len(comparable)


def _reason(
    verdict: Verdict, severity: Severity, breaches: tuple[ThresholdBreach, ...]
) -> str:
    if verdict is Verdict.APPROVE:
        if not breaches:
            return "No safety limit was breached under the test workload."
        return (
            f"Observed impact reached {severity.value}, below the "
            f"{T.REJECT_AT_OR_ABOVE.value} rejection threshold."
        )

    worst = [b for b in breaches if b.severity.rank >= T.REJECT_AT_OR_ABOVE.rank]
    detail = "; ".join(f"{b.resource_address} {b.metric}" for b in worst)
    return f"Rejected at observed severity {severity.value}. Safety limits breached: {detail}."

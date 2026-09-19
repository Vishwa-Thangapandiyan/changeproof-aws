from __future__ import annotations

import pytest

from changeproof.models import (
    ComparisonOutcome,
    Observation,
    ObservedMetric,
    PredictedMetric,
    Prediction,
    Severity,
    Verdict,
    BlastRadius,
)


def _observation(*metrics):
    return Observation(source="test", simulated=True, metrics=tuple(metrics))


def _metric(address, metric, baseline, observed, unit="Count"):
    return ObservedMetric(
        resource_address=address,
        metric=metric,
        unit=unit,
        baseline_value=baseline,
        observed_value=observed,
    )


def _prediction(*predicted):
    return Prediction(
        severity=Severity.MEDIUM,
        dimensions={"reliability": Severity.MEDIUM},
        blast_radius=BlastRadius(origin_addresses=("a",), paths=()),
        predicted_metrics=tuple(predicted),
        rules_fired=(),
        confidence=0.8,
    )


def _predicted(address, metric, low, high):
    return PredictedMetric(
        resource_address=address,
        metric=metric,
        expected_change_pct_low=low,
        expected_change_pct_high=high,
        rationale="test",
    )


# --- the verdict comes from safety limits, not from the prediction --------------------


def test_demo_scenario_is_rejected(prediction, observation):
    from changeproof.compare import compare

    result = compare(prediction, observation)

    assert result.verdict is Verdict.REJECT
    assert result.observed_severity is Severity.HIGH


def test_queue_depth_breach_is_reported(prediction, observation):
    from changeproof.compare import compare

    result = compare(prediction, observation)
    breached = {(b.resource_address, b.metric) for b in result.breaches}

    assert ("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible") in breached
    assert ("aws_lambda_function.worker", "Throttles") in breached
    assert ("aws_dynamodb_table.orders", "SuccessfulRequestLatency") in breached


def test_clean_telemetry_is_approved():
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Duration", 10, 50)),
        _observation(_metric("a", "Duration", 100, 120, unit="Milliseconds")),
    )

    assert result.verdict is Verdict.APPROVE
    assert result.breaches == ()


def test_an_accurate_prediction_does_not_rescue_an_unsafe_change():
    """The whole point of the split: predicting harm is not permission to cause it."""
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Duration", 400, 600)),
        _observation(_metric("a", "Duration", 100, 600, unit="Milliseconds")),
    )

    comparison = result.metric_comparisons[0]
    assert comparison.outcome is ComparisonOutcome.WITHIN_PREDICTION
    assert result.prediction_accuracy == 1.0
    assert result.verdict is Verdict.REJECT


def test_an_inaccurate_prediction_does_not_reject_a_safe_change():
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Duration", 400, 600)),
        _observation(_metric("a", "Duration", 100, 105, unit="Milliseconds")),
    )

    assert result.metric_comparisons[0].outcome is ComparisonOutcome.BETTER_THAN_PREDICTED
    assert result.prediction_accuracy == 0.0
    assert result.verdict is Verdict.APPROVE


# --- absolute vs relative limits -----------------------------------------------------


def test_zero_baseline_is_caught_by_an_absolute_limit():
    """Throttles going 0 -> 1840 has no percentage, but is still a failure."""
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Throttles", 100, 200)),
        _observation(_metric("a", "Throttles", 0, 1840)),
    )

    assert result.verdict is Verdict.REJECT
    assert result.breaches[0].limit_kind == "absolute"
    assert result.breaches[0].actual_value == 1840


def test_zero_baseline_and_zero_observed_is_not_a_breach():
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Throttles", 100, 200)),
        _observation(_metric("a", "Throttles", 0, 0)),
    )

    assert result.verdict is Verdict.APPROVE
    assert result.metric_comparisons[0].outcome is ComparisonOutcome.NOT_OBSERVED


def test_a_metric_at_exactly_the_limit_does_not_breach():
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Duration", 90, 110)),
        _observation(_metric("a", "Duration", 100, 200, unit="Milliseconds")),
    )

    assert result.verdict is Verdict.APPROVE


# --- predicted vs actual classification ----------------------------------------------


@pytest.mark.parametrize(
    "observed_value, expected",
    [
        (700, ComparisonOutcome.WITHIN_PREDICTION),
        (2000, ComparisonOutcome.WORSE_THAN_PREDICTED),
        (110, ComparisonOutcome.BETTER_THAN_PREDICTED),
    ],
)
def test_outcome_classification(observed_value, expected):
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "ApproximateNumberOfMessagesVisible", 451, 937)),
        _observation(_metric("a", "ApproximateNumberOfMessagesVisible", 100, observed_value)),
    )

    assert result.metric_comparisons[0].outcome is expected


def test_predicted_but_unobserved_metric_is_recorded():
    from changeproof.compare import compare

    result = compare(
        _prediction(_predicted("a", "Duration", 10, 50)),
        _observation(),
    )

    assert result.metric_comparisons[0].outcome is ComparisonOutcome.NOT_OBSERVED
    assert result.metric_comparisons[0].actual_change_pct is None


def test_observed_but_unpredicted_metric_is_recorded():
    """A measurement nobody predicted must surface, not vanish."""
    from changeproof.compare import compare

    result = compare(
        _prediction(),
        _observation(_metric("surprise", "Errors", 1, 99)),
    )

    assert len(result.metric_comparisons) == 1
    assert result.metric_comparisons[0].resource_address == "surprise"
    assert result.metric_comparisons[0].predicted_low is None


def test_accuracy_is_zero_when_nothing_is_comparable():
    from changeproof.compare import compare

    result = compare(_prediction(), _observation())

    assert result.prediction_accuracy == 0.0


def test_demo_accuracy_is_partial(prediction, observation):
    """The demo deliberately under-predicts queue growth, so accuracy is not 1.0."""
    from changeproof.compare import compare

    result = compare(prediction, observation)

    assert 0.0 < result.prediction_accuracy < 1.0
    worse = [
        c
        for c in result.metric_comparisons
        if c.outcome is ComparisonOutcome.WORSE_THAN_PREDICTED
    ]
    assert any(c.metric == "ApproximateNumberOfMessagesVisible" for c in worse)


def test_breaches_are_ordered_by_severity(prediction, observation):
    from changeproof.compare import compare

    result = compare(prediction, observation)
    ranks = [b.severity.rank for b in result.breaches]

    assert ranks == sorted(ranks, reverse=True)


def test_reason_names_the_breached_resources(prediction, observation):
    from changeproof.compare import compare

    result = compare(prediction, observation)

    assert "aws_sqs_queue.work" in result.reason
    assert result.reason.startswith("Rejected at observed severity HIGH")


def test_medium_severity_alone_does_not_reject():
    from changeproof.compare import compare

    result = compare(
        _prediction(),
        _observation(_metric("a", "ApproximateAgeOfOldestMessage", 10, 40, unit="Seconds")),
    )

    assert result.observed_severity is Severity.MEDIUM
    assert result.verdict is Verdict.APPROVE
    assert "below the HIGH rejection threshold" in result.reason


def test_comparison_serializes(prediction, observation):
    import json

    from changeproof.compare import compare

    payload = compare(prediction, observation).to_dict()

    assert json.loads(json.dumps(payload)) == payload

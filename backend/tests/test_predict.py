from __future__ import annotations

import pytest

from changeproof.models import Severity


def _change_set(before, after, resource_type="aws_lambda_function", address="aws_lambda_function.api"):
    from changeproof.parser import parse_plan

    return parse_plan(
        {
            "format_version": "1.2",
            "terraform_version": "1.9.8",
            "resource_changes": [
                {
                    "address": address,
                    "mode": "managed",
                    "type": resource_type,
                    "name": address.split(".", 1)[1],
                    "change": {
                        "actions": ["update"],
                        "before": {"reserved_concurrent_executions": before},
                        "after": {"reserved_concurrent_executions": after},
                    },
                }
            ],
        }
    )


def test_demo_change_predicts_high_reliability_risk(prediction):
    assert prediction.dimensions["reliability"] is Severity.HIGH
    assert prediction.severity is Severity.HIGH


def test_prediction_is_deterministic(change_set, graph):
    from changeproof.predict import predict

    first = predict(change_set, graph).to_dict()
    second = predict(change_set, graph).to_dict()

    assert first == second


def test_rule_trace_records_its_inputs_and_thresholds(prediction):
    assert len(prediction.rules_fired) == 1
    rule = prediction.rules_fired[0]

    assert rule.rule_id == "lambda.reserved_concurrency.increase"
    assert rule.inputs["before"] == 10
    assert rule.inputs["after"] == 100
    assert rule.inputs["multiplier"] == 10.0
    assert rule.threshold["bands"]


def test_blast_radius_covers_the_whole_chain(prediction):
    assert set(prediction.blast_radius.affected_addresses) == {
        "aws_lambda_function.api",
        "aws_sqs_queue.work",
        "aws_lambda_function.worker",
        "aws_dynamodb_table.orders",
    }


def test_origin_feels_the_full_multiplier(prediction):
    concurrency = next(
        m
        for m in prediction.predicted_metrics
        if m.resource_address == "aws_lambda_function.api"
        and m.metric == "ConcurrentExecutions"
    )

    # multiplier 10, propagation 1.0, sensitivity 1.0 -> +900% point estimate
    midpoint = (concurrency.expected_change_pct_low + concurrency.expected_change_pct_high) / 2
    assert midpoint == pytest.approx(900.0)


def test_pressure_attenuates_with_depth(prediction):
    def point(address, metric):
        m = next(
            p
            for p in prediction.predicted_metrics
            if p.resource_address == address and p.metric == metric
        )
        return (m.expected_change_pct_low + m.expected_change_pct_high) / 2

    api = point("aws_lambda_function.api", "ConcurrentExecutions")
    queue = point("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")
    table = point("aws_dynamodb_table.orders", "ConsumedWriteCapacityUnits")

    assert api > queue > table


def test_downstream_pressure_never_falls_below_baseline(prediction):
    """Attenuation must never predict that an increase upstream reduces load."""
    for metric in prediction.predicted_metrics:
        assert metric.expected_change_pct_high > 0
        assert metric.expected_change_pct_low > 0


def test_band_widens_around_the_point_estimate(prediction):
    from changeproof import thresholds

    metric = prediction.predicted_metrics[0]
    midpoint = (metric.expected_change_pct_low + metric.expected_change_pct_high) / 2

    assert metric.expected_change_pct_low == pytest.approx(midpoint * (1 - thresholds.PREDICTION_BAND))
    assert metric.expected_change_pct_high == pytest.approx(midpoint * (1 + thresholds.PREDICTION_BAND))


@pytest.mark.parametrize(
    "before, after, expected",
    [
        (10, 12, Severity.LOW),
        (10, 25, Severity.MEDIUM),
        (10, 60, Severity.HIGH),
        (10, 100, Severity.HIGH),
        (10, 900, Severity.CRITICAL),
    ],
)
def test_severity_scales_with_the_multiplier(before, after, expected, graph):
    from changeproof.predict import predict

    prediction = predict(_change_set(before, after), graph)

    assert prediction.dimensions["reliability"] is expected


def test_confidence_decays_with_blast_radius_depth(prediction):
    from changeproof import thresholds

    deepest = max(p.depth for p in prediction.blast_radius.paths)
    expected = (
        thresholds.PREDICTION_BASE_CONFIDENCE
        - deepest * thresholds.PREDICTION_CONFIDENCE_DECAY_PER_HOP
    )

    assert prediction.confidence == pytest.approx(expected)


def test_a_decrease_does_not_fire_the_rule(graph):
    from changeproof.predict import UnsupportedChangeError, predict

    with pytest.raises(UnsupportedChangeError, match="no prediction rule matched"):
        predict(_change_set(100, 10), graph)


def test_unmodelled_resource_type_raises_rather_than_returning_low_risk(graph):
    from changeproof.predict import UnsupportedChangeError, predict

    change_set = _change_set(
        10, 100, resource_type="aws_security_group", address="aws_security_group.web"
    )

    with pytest.raises(UnsupportedChangeError, match="no prediction rule matched"):
        predict(change_set, graph)


def test_concurrency_from_unset_raises(graph):
    from changeproof.predict import UnsupportedChangeError, predict

    with pytest.raises(UnsupportedChangeError, match="does not exist yet"):
        predict(_change_set(None, 100), graph)


def test_changed_resource_absent_from_graph_raises(graph):
    from changeproof.predict import UnsupportedChangeError, predict

    change_set = _change_set(10, 100, address="aws_lambda_function.unknown")

    with pytest.raises(UnsupportedChangeError, match="absent from the dependency graph"):
        predict(change_set, graph)


def test_empty_plan_raises(graph):
    from changeproof.parser import parse_plan
    from changeproof.predict import UnsupportedChangeError, predict

    empty = parse_plan(
        {"format_version": "1.2", "terraform_version": "1.9.8", "resource_changes": []}
    )

    with pytest.raises(UnsupportedChangeError, match="no effective changes"):
        predict(empty, graph)


def test_prediction_serializes(prediction):
    import json

    payload = prediction.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["severity"] == "HIGH"

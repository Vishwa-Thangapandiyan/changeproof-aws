"""The metric collection contract, pinned.

These tests exist so the CloudWatch collector can be written against code rather
than against a prose table, and so a change to a statistic or a dimension is a
visible, deliberate edit rather than a silent one.

Nothing here touches AWS. The definitions are data.
"""

from __future__ import annotations

import json

import pytest

from changeproof import thresholds as T

#: The statistic and unit every metric must report, stated independently of the
#: table under test so that a typo in `thresholds.py` cannot agree with itself.
EXPECTED = {
    "ConcurrentExecutions": ("Maximum", "Count"),
    "Duration": ("Average", "Milliseconds"),
    "Throttles": ("Sum", "Count"),
    "Errors": ("Sum", "Count"),
    "ApproximateNumberOfMessagesVisible": ("Maximum", "Count"),
    "ApproximateAgeOfOldestMessage": ("Maximum", "Seconds"),
    "ConsumedWriteCapacityUnits": ("Sum", "Count"),
    "ThrottledRequests": ("Sum", "Count"),
    "SuccessfulRequestLatency": ("Average", "Milliseconds"),
}


def _collected_metrics() -> set[str]:
    """Every distinct metric the engine asks for, across all resource types."""
    return {m for metrics in T.METRICS_BY_RESOURCE_TYPE.values() for m in metrics}


def _resource_metric_pairs() -> list[tuple[str, str]]:
    """Every (graph address, metric) the collector must fetch, from the real graph."""
    from changeproof.adapters.local import load_graph

    graph = load_graph()
    pairs = []
    for address in graph.known_addresses():
        node = graph.node(address)
        for metric in T.METRICS_BY_RESOURCE_TYPE.get(node.resource_type, ()):
            pairs.append((address, metric))
    return pairs


# --- coverage: no metric without a definition, no definition without a metric --------


def test_every_collected_metric_has_a_definition():
    missing = sorted(_collected_metrics() - set(T.METRIC_DEFINITIONS))

    assert missing == [], f"metrics the collector would not know how to fetch: {missing}"


def test_no_definition_is_orphaned():
    """A definition for a metric nobody collects is dead weight that will drift."""
    orphans = sorted(set(T.METRIC_DEFINITIONS) - _collected_metrics())

    assert orphans == [], f"defined but never collected: {orphans}"


def test_all_thirteen_resource_metric_pairs_resolve():
    pairs = _resource_metric_pairs()

    assert len(pairs) == 13
    for address, metric in pairs:
        definition = T.definition_for(metric)
        assert definition.statistic, f"{address} {metric} has no statistic"
        assert definition.unit, f"{address} {metric} has no unit"


def test_the_four_demo_resources_are_covered():
    addresses = {address for address, _ in _resource_metric_pairs()}

    assert addresses == {
        "aws_lambda_function.api",
        "aws_sqs_queue.work",
        "aws_lambda_function.worker",
        "aws_dynamodb_table.orders",
    }


# --- the values themselves -----------------------------------------------------------


@pytest.mark.parametrize("metric, expected", sorted(EXPECTED.items()))
def test_statistic_and_unit(metric, expected):
    statistic, unit = expected
    definition = T.definition_for(metric)

    assert definition.statistic == statistic
    assert definition.unit == unit


def test_every_definition_is_covered_by_this_test():
    """Adding a metric must force a decision here, not inherit one by omission."""
    assert set(EXPECTED) == set(T.METRIC_DEFINITIONS)


@pytest.mark.parametrize("metric", sorted(T.METRIC_DEFINITIONS))
def test_statistic_is_a_real_cloudwatch_statistic(metric):
    assert T.definition_for(metric).statistic in T.STATISTICS


@pytest.mark.parametrize("metric", sorted(T.METRIC_DEFINITIONS))
def test_unit_is_known(metric):
    assert T.definition_for(metric).unit in T.UNITS


def test_duration_is_average_not_p95():
    """Pinned deliberately. p95 is arguably better and is an open question; changing
    it must be a decision someone makes, not a drift someone notices later."""
    assert T.definition_for("Duration").statistic == "Average"


# --- extra dimensions: exactly one metric has them -----------------------------------


def test_only_successful_request_latency_has_extra_dimensions():
    with_extras = sorted(
        metric
        for metric, definition in T.METRIC_DEFINITIONS.items()
        if definition.extra_dimensions
    )

    assert with_extras == ["SuccessfulRequestLatency"]


def test_successful_request_latency_needs_operation_putitem():
    """Without it CloudWatch returns no datapoints at all, and says nothing."""
    assert T.definition_for("SuccessfulRequestLatency").extra_dimensions == (
        ("Operation", "PutItem"),
    )


@pytest.mark.parametrize(
    "metric", sorted(set(T.METRIC_DEFINITIONS) - {"SuccessfulRequestLatency"})
)
def test_every_other_metric_uses_only_the_resource_dimensions(metric):
    assert T.definition_for(metric).extra_dimensions == ()


def test_extra_dimensions_are_name_value_pairs():
    for metric, definition in T.METRIC_DEFINITIONS.items():
        for pair in definition.extra_dimensions:
            assert len(pair) == 2, f"{metric} has a malformed dimension pair"
            assert all(isinstance(part, str) and part for part in pair)


# --- lookup --------------------------------------------------------------------------


def test_unknown_metric_raises_and_lists_what_is_defined():
    with pytest.raises(ValueError, match="no collection definition for metric"):
        T.definition_for("CPUUtilization")


def test_definitions_are_immutable():
    definition = T.definition_for("Throttles")

    with pytest.raises(Exception):
        definition.statistic = "Average"  # type: ignore[misc]


# --- consistency with the rest of the engine -----------------------------------------


def test_units_agree_with_the_telemetry_fixture(fixtures_dir):
    """The fixture was written before this table existed; they must not disagree."""
    document = json.loads(
        (fixtures_dir / "telemetry_observed.json").read_text(encoding="utf-8")
    )

    for entry in document["metrics"]:
        expected = T.definition_for(entry["metric"]).unit
        assert entry["unit"] == expected, (
            f"{entry['resourceAddress']} {entry['metric']}: fixture says "
            f"{entry['unit']!r}, definition says {expected!r}"
        )


def test_every_metric_with_a_safety_limit_is_collectable():
    """A limit on a metric nobody fetches could never fire."""
    limited = {limit.metric for limit in T.SAFETY_LIMITS}

    assert limited <= set(T.METRIC_DEFINITIONS)

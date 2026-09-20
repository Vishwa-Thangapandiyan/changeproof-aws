"""The CloudWatch collector, exercised entirely offline.

Datapoints come from a local fixture or from dicts built in the test. No AWS call is
made, no SDK is imported, and `test_no_aws.py` already asserts the second of those
across the whole source tree.
"""

from __future__ import annotations

import pytest

from changeproof import thresholds as T
from changeproof.adapters.local import StaticMetricDataSource
from changeproof.manifest import MetricWindow, parse_manifest_file
from changeproof.models import Observation
from changeproof.telemetry import (
    CloudWatchCollector,
    MetricDataSource,
    MetricQuery,
    TelemetryError,
    build_queries,
)

EXPERIMENT_ID = "EXP-FIXTURE-001"


@pytest.fixture(scope="module")
def manifest(fixtures_dir):
    return parse_manifest_file(fixtures_dir / "experiment_manifest.json")


@pytest.fixture(scope="module")
def queries(manifest):
    return build_queries(manifest)


@pytest.fixture
def source(manifest):
    return StaticMetricDataSource(manifest)


@pytest.fixture
def observation(manifest, source):
    return CloudWatchCollector(manifest, source).collect(EXPERIMENT_ID)


class DictSource:
    """A MetricDataSource backed by literal values, for the edge cases."""

    def __init__(self, baseline, changed, baseline_window, changed_window):
        self._by_window = {
            baseline_window: baseline,
            changed_window: changed,
        }

    def fetch(self, queries, window):
        series = self._by_window[window]
        return {
            q.query_id: series[(q.resource_address, q.metric)]
            for q in queries
            if (q.resource_address, q.metric) in series
        }


def _dict_collector(manifest, baseline, changed):
    return CloudWatchCollector(
        manifest,
        DictSource(baseline, changed, manifest.baseline_window, manifest.changed_window),
    )


# --- query building ------------------------------------------------------------------


def test_thirteen_queries_are_built(queries):
    assert len(queries) == 13


def test_queries_cover_every_resource_and_metric(queries, manifest):
    expected = {
        (resource.graph_address, metric)
        for resource in manifest.resources
        for metric in T.METRICS_BY_RESOURCE_TYPE[resource.resource_type]
    }

    assert {(q.resource_address, q.metric) for q in queries} == expected


def test_resource_address_is_the_graph_address(queries):
    assert {q.resource_address for q in queries} == {
        "aws_lambda_function.api",
        "aws_sqs_queue.work",
        "aws_lambda_function.worker",
        "aws_dynamodb_table.orders",
    }


@pytest.mark.parametrize(
    "address, namespace",
    [
        ("aws_lambda_function.api", "AWS/Lambda"),
        ("aws_sqs_queue.work", "AWS/SQS"),
        ("aws_dynamodb_table.orders", "AWS/DynamoDB"),
    ],
)
def test_namespace_comes_from_the_manifest(queries, address, namespace):
    for query in queries:
        if query.resource_address == address:
            assert query.namespace == namespace


def test_statistic_and_unit_come_from_the_definitions(queries):
    for query in queries:
        definition = T.definition_for(query.metric)
        assert query.statistic == definition.statistic
        assert query.unit == definition.unit


def test_period_is_sixty_seconds(queries):
    assert {q.period_seconds for q in queries} == {60}


def test_query_ids_are_unique_and_cloudwatch_safe(queries):
    ids = [q.query_id for q in queries]

    assert len(set(ids)) == len(ids)
    for query_id in ids:
        assert query_id[0].islower()
        assert all(char.isalnum() or char == "_" for char in query_id)


# --- the Operation dimension ---------------------------------------------------------


def test_successful_request_latency_carries_operation_putitem(queries):
    query = next(q for q in queries if q.metric == "SuccessfulRequestLatency")

    assert query.dimensions == (
        ("TableName", "changeproof-exp-fixture-001-orders"),
        ("Operation", "PutItem"),
    )


def test_operation_is_appended_after_the_resource_dimension(queries):
    """The resource's own dimension must survive, not be replaced."""
    query = next(q for q in queries if q.metric == "SuccessfulRequestLatency")

    assert query.dimensions_as_cloudwatch() == [
        {"Name": "TableName", "Value": "changeproof-exp-fixture-001-orders"},
        {"Name": "Operation", "Value": "PutItem"},
    ]


def test_no_other_metric_gets_an_operation_dimension(queries):
    for query in queries:
        if query.metric == "SuccessfulRequestLatency":
            continue
        assert all(name != "Operation" for name, _ in query.dimensions)


def test_the_tables_other_metrics_use_tablename_alone(queries):
    for query in queries:
        if query.resource_address != "aws_dynamodb_table.orders":
            continue
        if query.metric == "SuccessfulRequestLatency":
            continue
        assert query.dimensions == (("TableName", "changeproof-exp-fixture-001-orders"),)


# --- collection ----------------------------------------------------------------------


def test_collector_satisfies_the_telemetry_port(manifest, source):
    collector = CloudWatchCollector(manifest, source)

    assert callable(collector.collect)
    assert isinstance(collector.collect(EXPERIMENT_ID), Observation)


def test_static_source_satisfies_the_data_source_protocol(source):
    assert isinstance(source, MetricDataSource)


def test_all_thirteen_rows_are_produced(observation):
    assert len(observation.metrics) == 13


def test_observation_is_not_simulated(observation):
    """The whole point of Phase 1. The datapoints are fake; the pathway is not."""
    assert observation.simulated is False
    assert observation.source == "cloudwatch"


def test_rows_are_keyed_by_graph_address(observation):
    assert observation.find("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")
    assert observation.find("aws_lambda_function.worker", "Throttles")


def test_units_come_from_the_definitions(observation):
    for metric in observation.metrics:
        assert metric.unit == T.definition_for(metric.metric).unit


# --- folding: the statistic is applied across periods as well as within them ---------


@pytest.mark.parametrize(
    "address, metric, baseline, observed",
    [
        # Maximum -> peak of the series
        ("aws_lambda_function.api", "ConcurrentExecutions", 9.0, 94.0),
        ("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible", 200.0, 2680.0),
        ("aws_sqs_queue.work", "ApproximateAgeOfOldestMessage", 4.0, 46.0),
        # Sum -> total over the window
        ("aws_lambda_function.worker", "Throttles", 0.0, 1840.0),
        ("aws_dynamodb_table.orders", "ConsumedWriteCapacityUnits", 45.0, 196.0),
        # Average -> mean of the periods
        ("aws_lambda_function.api", "Duration", 120.0, 180.0),
        ("aws_dynamodb_table.orders", "SuccessfulRequestLatency", 12.0, 58.0),
    ],
)
def test_series_fold_to_the_expected_values(observation, address, metric, baseline, observed):
    row = observation.find(address, metric)

    assert row is not None
    assert row.baseline_value == pytest.approx(baseline)
    assert row.observed_value == pytest.approx(observed)


def test_baseline_and_changed_windows_are_read_separately(manifest, source):
    """A collector that queried one window twice would produce equal values."""
    observation = CloudWatchCollector(manifest, source).collect(EXPERIMENT_ID)
    row = observation.find("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")

    assert row is not None
    assert row.baseline_value != row.observed_value


def test_an_unknown_statistic_is_rejected():
    """A statistic with no folding rule must raise, not silently pick one."""
    from changeproof.telemetry import _fold

    query = MetricQuery(
        query_id="m_x",
        resource_address="aws_sqs_queue.work",
        metric="ApproximateNumberOfMessagesVisible",
        namespace="AWS/SQS",
        dimensions=(("QueueName", "q"),),
        statistic="Percentile99",
        unit="Count",
    )

    with pytest.raises(TelemetryError, match="no rule for folding"):
        _fold(query, [1.0, 2.0])


def test_every_defined_statistic_can_be_folded():
    """thresholds may not name a statistic this module cannot collapse."""
    from changeproof.telemetry import _FOLD

    assert {d.statistic for d in T.METRIC_DEFINITIONS.values()} <= set(_FOLD)


# --- missing data stays missing ------------------------------------------------------


def test_a_metric_missing_from_one_window_is_omitted(manifest):
    """One-sided data cannot express a change, so the row must not appear at all."""
    key = ("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")
    collector = _dict_collector(manifest, baseline={key: [200.0]}, changed={})

    observation = collector.collect(EXPERIMENT_ID)

    assert observation.find(*key) is None
    assert observation.metrics == ()


def test_an_empty_series_counts_as_missing(manifest):
    """CloudWatch returns no datapoints for an idle resource, not a list of zeros."""
    key = ("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")
    collector = _dict_collector(manifest, baseline={key: []}, changed={key: [2680.0]})

    assert collector.collect(EXPERIMENT_ID).find(*key) is None


def test_a_missing_baseline_is_never_zero_filled(manifest):
    """A fabricated zero baseline against a real observation is an infinite increase
    and a guaranteed false breach. This is the failure mode the rule exists for."""
    key = ("aws_sqs_queue.work", "ApproximateNumberOfMessagesVisible")
    collector = _dict_collector(manifest, baseline={}, changed={key: [2680.0]})

    observation = collector.collect(EXPERIMENT_ID)

    assert observation.metrics == ()
    assert all(m.baseline_value != 0 for m in observation.metrics)


def test_a_genuine_zero_is_kept(manifest):
    """Zero measured on both sides is data, not absence."""
    key = ("aws_lambda_function.api", "Throttles")
    collector = _dict_collector(
        manifest, baseline={key: [0.0, 0.0]}, changed={key: [0.0, 0.0]}
    )

    row = collector.collect(EXPERIMENT_ID).find(*key)

    assert row is not None
    assert row.baseline_value == 0.0
    assert row.change_pct is None


def test_partial_collection_is_reportable(manifest):
    key = ("aws_lambda_function.api", "Throttles")
    collector = _dict_collector(manifest, baseline={key: [0.0]}, changed={key: [3.0]})

    observation = collector.collect(EXPERIMENT_ID)
    missing = collector.missing(observation)

    assert len(observation.metrics) == 1
    assert len(missing) == 12
    assert "aws_sqs_queue.work ApproximateNumberOfMessagesVisible" in missing


def test_nothing_missing_when_collection_is_complete(manifest, source, observation):
    assert CloudWatchCollector(manifest, source).missing(observation) == ()


# --- the manifest pairing ------------------------------------------------------------


def test_collecting_for_the_wrong_experiment_raises(manifest, source):
    collector = CloudWatchCollector(manifest, source)

    with pytest.raises(TelemetryError, match="but this collector was built for"):
        collector.collect("EXP-SOMETHING-ELSE")


def test_a_window_from_another_experiment_is_rejected(manifest, source):
    stray = MetricWindow(start_epoch=1, end_epoch=2, start_iso="a", end_iso="b")

    with pytest.raises(ValueError, match="belongs to neither run"):
        source.fetch(build_queries(manifest), stray)


def test_source_label_is_overridable(manifest, source):
    observation = CloudWatchCollector(manifest, source, source_label="cloudwatch:test").collect(
        EXPERIMENT_ID
    )

    assert observation.source == "cloudwatch:test"

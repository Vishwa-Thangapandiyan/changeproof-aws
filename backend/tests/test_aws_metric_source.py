"""`AwsMetricDataSource`, exercised against a stubbed client.

No AWS call is made and no credentials are needed. The stub records each request and
replays canned `GetMetricData` responses, so what is asserted is the request shape and
the response handling, which are the parts that fail silently against real CloudWatch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from changeproof.manifest import parse_manifest_file
from changeproof.telemetry import CloudWatchCollector, MetricDataSource, TelemetryError, build_queries
from telemetry_aws.cloudwatch import MAX_QUERIES_PER_CALL, AwsMetricDataSource, metric_data_query


class StubClient:
    def __init__(self, *pages):
        self._pages = list(pages)
        self.requests = []

    def get_metric_data(self, **kwargs):
        self.requests.append(kwargs)
        return self._pages.pop(0)


def result(query_id, values, status="Complete"):
    return {"Id": query_id, "Values": values, "StatusCode": status}


@pytest.fixture(scope="module")
def manifest(fixtures_dir):
    return parse_manifest_file(fixtures_dir / "experiment_manifest.json")


@pytest.fixture(scope="module")
def queries(manifest):
    return build_queries(manifest)


def source_for(*pages):
    client = StubClient(*pages)
    return AwsMetricDataSource("us-east-1", client=client), client


def test_satisfies_the_seam():
    assert isinstance(AwsMetricDataSource("us-east-1", client=StubClient()), MetricDataSource)


def test_request_shape(queries, manifest):
    source, client = source_for({"MetricDataResults": []})
    source.fetch(queries, manifest.baseline_window)

    request = client.requests[0]
    assert len(request["MetricDataQueries"]) == 13
    assert request["StartTime"] == datetime.fromtimestamp(
        manifest.baseline_window.start_epoch, tz=timezone.utc
    )
    assert request["EndTime"] == datetime.fromtimestamp(
        manifest.baseline_window.end_epoch, tz=timezone.utc
    )

    for query, entry in zip(queries, request["MetricDataQueries"]):
        stat = entry["MetricStat"]
        assert entry["Id"] == query.query_id
        assert stat["Metric"]["Namespace"] == query.namespace
        assert stat["Metric"]["MetricName"] == query.metric
        assert stat["Metric"]["Dimensions"] == query.dimensions_as_cloudwatch()
        assert stat["Stat"] == query.statistic
        assert stat["Period"] == 60


def test_unit_is_not_sent(queries):
    for query in queries:
        assert "Unit" not in metric_data_query(query)["MetricStat"]


def test_dynamodb_latency_keeps_the_operation_dimension(queries):
    latency = next(q for q in queries if q.metric == "SuccessfulRequestLatency")
    dimensions = metric_data_query(latency)["MetricStat"]["Metric"]["Dimensions"]
    assert {"Name": "Operation", "Value": "PutItem"} in dimensions

    others = [q for q in queries if q.namespace == "AWS/DynamoDB" and q is not latency]
    assert others
    for query in others:
        names = [d["Name"] for d in metric_data_query(query)["MetricStat"]["Metric"]["Dimensions"]]
        assert "Operation" not in names


def test_response_is_mapped_by_query_id(queries, manifest):
    first, second = queries[0], queries[1]
    source, _ = source_for(
        {"MetricDataResults": [result(first.query_id, [1.0, 4.0]), result(second.query_id, [2.5])]}
    )
    fetched = source.fetch(queries, manifest.baseline_window)
    assert fetched[first.query_id] == [1.0, 4.0]
    assert fetched[second.query_id] == [2.5]


def test_empty_values_stay_empty_and_are_never_zero_filled(queries, manifest):
    idle = queries[0]
    source, _ = source_for({"MetricDataResults": [result(idle.query_id, [])]})
    fetched = source.fetch(queries, manifest.baseline_window)
    assert fetched[idle.query_id] == []
    assert all(values == [] for values in fetched.values())


def test_a_query_absent_from_the_response_is_empty_not_zero(queries, manifest):
    source, _ = source_for({"MetricDataResults": []})
    fetched = source.fetch(queries, manifest.baseline_window)
    assert set(fetched) == {q.query_id for q in queries}
    assert not any(fetched.values())


def test_pagination_consumes_every_page_in_order(queries, manifest):
    query = queries[0]
    source, client = source_for(
        {"MetricDataResults": [result(query.query_id, [1.0, 2.0], "PartialData")], "NextToken": "t1"},
        {"MetricDataResults": [result(query.query_id, [3.0], "PartialData")], "NextToken": "t2"},
        {"MetricDataResults": [result(query.query_id, [4.0])]},
    )
    fetched = source.fetch(queries, manifest.baseline_window)
    assert fetched[query.query_id] == [1.0, 2.0, 3.0, 4.0]
    assert [r.get("NextToken") for r in client.requests] == [None, "t1", "t2"]


def test_unrequested_ids_are_ignored(queries, manifest):
    source, _ = source_for({"MetricDataResults": [result("someone_else", [9.0])]})
    assert "someone_else" not in source.fetch(queries, manifest.baseline_window)


def test_internal_error_raises_rather_than_reporting_empty(queries, manifest):
    source, _ = source_for({"MetricDataResults": [result(queries[0].query_id, [], "InternalError")]})
    with pytest.raises(TelemetryError):
        source.fetch(queries, manifest.baseline_window)


def test_large_query_sets_are_split_into_batches(queries, manifest):
    many = tuple(queries[0].__class__(**{**queries[0].__dict__, "query_id": f"q{i}"}) for i in range(MAX_QUERIES_PER_CALL + 1))
    source, client = source_for({"MetricDataResults": []}, {"MetricDataResults": []})
    source.fetch(many, manifest.baseline_window)
    assert [len(r["MetricDataQueries"]) for r in client.requests] == [MAX_QUERIES_PER_CALL, 1]


def test_collector_end_to_end_keeps_baseline_and_changed_separate(manifest, queries):
    target = next(q for q in queries if q.metric == "Throttles" and q.resource_address.endswith("worker"))
    baseline = {"MetricDataResults": [result(target.query_id, [0.0, 1.0])]}
    changed = {"MetricDataResults": [result(target.query_id, [10.0, 20.0])]}
    source, client = source_for(baseline, changed)

    observation = CloudWatchCollector(manifest, source).collect(manifest.experiment_id)

    metric = next(m for m in observation.metrics if m.resource_address == target.resource_address and m.metric == "Throttles")
    assert (metric.baseline_value, metric.observed_value) == (1.0, 30.0)
    assert client.requests[0]["StartTime"] != client.requests[1]["StartTime"]
    # Only the one metric had data on both sides; nothing else was invented.
    assert len(observation.metrics) == 1

"""`cloudwatch:GetMetricData` behind the `telemetry.MetricDataSource` seam.

This is the Phase 1 counterpart of `adapters.local.StaticMetricDataSource`. It lives
outside `backend/lambda/` on purpose: that tree is guaranteed SDK-free by
`test_no_aws.py`, and this is the one place the collection path is allowed to import
boto3. `CloudWatchCollector` does not change.

What this module must never do
------------------------------

Turn "no datapoints" into a number. CloudWatch returns nothing for an idle resource,
so a query with no values comes back as an empty list. The collector then omits the
metric, and `compare.py` scores it as unobserved instead of breaching on a fabricated
zero baseline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from changeproof.manifest import MetricWindow
from changeproof.telemetry import MetricQuery, TelemetryError

#: GetMetricData accepts at most 500 queries per call.
MAX_QUERIES_PER_CALL = 500


class AwsMetricDataSource:
    """Satisfies `telemetry.MetricDataSource`. Read-only: one permission, `GetMetricData`.

    `client` exists so tests can inject a stub. When it is omitted a boto3 CloudWatch
    client is built for `region`, and boto3 is imported only then.
    """

    def __init__(self, region: str, client: Any | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("cloudwatch", region_name=region)
        self._client = client

    def fetch(
        self, queries: tuple[MetricQuery, ...], window: MetricWindow
    ) -> dict[str, list[float]]:
        results: dict[str, list[float]] = {}
        for start in range(0, len(queries), MAX_QUERIES_PER_CALL):
            batch = queries[start : start + MAX_QUERIES_PER_CALL]
            results.update(self._fetch_batch(batch, window))
        return results

    def _fetch_batch(
        self, queries: tuple[MetricQuery, ...], window: MetricWindow
    ) -> dict[str, list[float]]:
        wanted = {query.query_id for query in queries}
        series: dict[str, list[float]] = {query.query_id: [] for query in queries}

        request: dict[str, Any] = {
            "MetricDataQueries": [metric_data_query(query) for query in queries],
            "StartTime": _utc(window.start_epoch),
            "EndTime": _utc(window.end_epoch),
            "ScanBy": "TimestampAscending",
        }

        while True:
            page = self._client.get_metric_data(**request)

            for result in page.get("MetricDataResults", []):
                query_id = result.get("Id")
                if query_id not in wanted:
                    continue
                if result.get("StatusCode") == "InternalError":
                    raise TelemetryError(
                        f"CloudWatch returned InternalError for {query_id}: "
                        f"{result.get('Messages')}"
                    )
                # A series can be split across pages; ascending order keeps it in
                # time order when the pieces are joined.
                series[query_id].extend(float(value) for value in result.get("Values", []))

            token = page.get("NextToken")
            if not token:
                break
            request["NextToken"] = token

        return series


def metric_data_query(query: MetricQuery) -> dict[str, Any]:
    """One `MetricDataQueries` entry.

    `Unit` is deliberately not sent. CloudWatch filters datapoints by unit when one
    is given, so a wrong unit yields an empty series with no error, which is the same
    silent failure as a missing dimension. The unit stays on the `ObservedMetric`
    from `METRIC_DEFINITIONS`.
    """
    return {
        "Id": query.query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": query.namespace,
                "MetricName": query.metric,
                "Dimensions": query.dimensions_as_cloudwatch(),
            },
            "Period": query.period_seconds,
            "Stat": query.statistic,
        },
        "ReturnData": True,
    }


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)

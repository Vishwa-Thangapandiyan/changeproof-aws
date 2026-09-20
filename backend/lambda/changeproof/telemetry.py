"""Experiment manifest + metric data -> Observation.

This is the collection half of the telemetry boundary. It decides *what to ask*
(from the manifest and the metric definitions) and *how to read the answer* (folding
a series of datapoints into the single number the engine compares). It does not
decide where the answer comes from: that is the `MetricDataSource` seam.

Phase 0 fills that seam from a local fixture. Phase 1 fills it with a boto3-backed
implementation and nothing in this module changes. Nothing here imports an SDK.

What this module must never do
------------------------------

Substitute a value for one that is missing. CloudWatch returns *no datapoints* for
an idle resource rather than zeros, and a queue that was quiet during the baseline
run is exactly that case. A fabricated baseline of 0 against an observed 2680 is an
infinite increase and a guaranteed breach, so a missing reading omits the metric
entirely and lets `compare.py` score it as unobserved.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from . import thresholds as T
from .manifest import ExperimentManifest, MetricWindow
from .models import Observation, ObservedMetric

#: Seconds per datapoint. 60 is the finest granularity SQS publishes at, and anything
#: coarser collapses a 60-second run to a single point.
PERIOD_SECONDS = 60

#: How a series of per-period datapoints becomes one number for the window.
#:
#: The same statistic is applied across periods as within them. A peak stays a peak,
#: a total stays a total, a mean stays a mean. Any other choice would make the number
#: mean something different from what `SAFETY_LIMITS` assumes it means.
_FOLD = {
    "Maximum": max,
    "Minimum": min,
    "Sum": sum,
    "Average": statistics.fmean,
    "SampleCount": sum,
}


class TelemetryError(ValueError):
    """Collection could not proceed. Never downgraded to an empty Observation."""


@dataclass(frozen=True)
class MetricQuery:
    """One metric on one resource, ready to be asked of CloudWatch.

    `query_id` is how a response is matched back to the metric that asked for it.
    It is derived from the graph address and metric name, so a response cannot be
    silently attributed to the wrong resource.
    """

    query_id: str
    resource_address: str
    metric: str
    namespace: str
    dimensions: tuple[tuple[str, str], ...]
    statistic: str
    unit: str
    period_seconds: int = PERIOD_SECONDS

    def dimensions_as_cloudwatch(self) -> list[dict[str, str]]:
        return [{"Name": name, "Value": value} for name, value in self.dimensions]


@runtime_checkable
class MetricDataSource(Protocol):
    """Where datapoints come from.

    Phase 0: a local fixture. Phase 1: `cloudwatch:GetMetricData` over the window.

    Returning an empty list for a query means "no datapoints", which is a real and
    expected answer. Omitting the key entirely means the same thing. Neither is an
    error, and neither may be turned into a zero.
    """

    def fetch(
        self, queries: tuple[MetricQuery, ...], window: MetricWindow
    ) -> dict[str, list[float]]:
        ...


def build_queries(manifest: ExperimentManifest) -> tuple[MetricQuery, ...]:
    """Every metric the engine needs, for every resource in the experiment.

    Which metrics matter per resource type comes from `METRICS_BY_RESOURCE_TYPE`;
    how to read each one comes from `METRIC_DEFINITIONS`. Neither list is restated
    here, so the two cannot drift out of step with the collector.
    """
    queries: list[MetricQuery] = []

    for resource in manifest.resources:
        metrics = T.METRICS_BY_RESOURCE_TYPE.get(resource.resource_type, ())
        if not metrics:
            # A resource type nobody has decided how to measure. Skipping it is
            # correct -- the graph may contain resources the engine does not model.
            continue

        base = tuple(
            (dimension.name, dimension.value)
            for dimension in resource.cloudwatch.dimensions
        )

        for metric in metrics:
            definition = T.definition_for(metric)
            queries.append(
                MetricQuery(
                    query_id=_query_id(resource.graph_address, metric),
                    resource_address=resource.graph_address,
                    metric=metric,
                    namespace=resource.cloudwatch.namespace,
                    # The resource's own dimensions first, then anything the metric
                    # needs on top. Order matters only for readability; CloudWatch
                    # treats the set as unordered.
                    dimensions=base + definition.extra_dimensions,
                    statistic=definition.statistic,
                    unit=definition.unit,
                )
            )

    return tuple(queries)


class CloudWatchCollector:
    """Satisfies `ports.TelemetrySource`.

    The manifest arrives at construction rather than through `collect`, because the
    port's signature carries only an experiment id. `collect` verifies the id it is
    given is the one this collector was built for, so a mismatched pairing fails
    loudly instead of returning another experiment's numbers.
    """

    def __init__(
        self,
        manifest: ExperimentManifest,
        source: MetricDataSource,
        source_label: str = "cloudwatch",
    ) -> None:
        self._manifest = manifest
        self._source = source
        self._source_label = source_label

    def collect(self, experiment_id: str) -> Observation:
        if experiment_id != self._manifest.experiment_id:
            raise TelemetryError(
                f"asked for experiment {experiment_id!r} but this collector was built "
                f"for {self._manifest.experiment_id!r}"
            )

        queries = build_queries(self._manifest)
        if not queries:
            raise TelemetryError(
                f"experiment {experiment_id} has no measurable resources; "
                "nothing in its manifest maps to a metric the engine collects"
            )

        baseline = self._source.fetch(queries, self._manifest.baseline_window)
        changed = self._source.fetch(queries, self._manifest.changed_window)

        metrics = []
        for query in queries:
            baseline_value = _fold(query, baseline.get(query.query_id))
            observed_value = _fold(query, changed.get(query.query_id))

            # Both sides or neither. One-sided data cannot express a change, and
            # inventing the other side would invent the verdict.
            if baseline_value is None or observed_value is None:
                continue

            metrics.append(
                ObservedMetric(
                    resource_address=query.resource_address,
                    metric=query.metric,
                    unit=query.unit,
                    baseline_value=baseline_value,
                    observed_value=observed_value,
                )
            )

        return Observation(
            source=self._source_label,
            simulated=False,
            metrics=tuple(metrics),
        )

    def missing(self, observation: Observation) -> tuple[str, ...]:
        """Which queries produced no metric, as 'address metric' strings.

        The telemetry contract asks that a run whose metrics were half absent be
        visible as such rather than quietly thin. This reports it; it does not
        decide what to do about it.
        """
        present = {(m.resource_address, m.metric) for m in observation.metrics}
        return tuple(
            f"{q.resource_address} {q.metric}"
            for q in build_queries(self._manifest)
            if (q.resource_address, q.metric) not in present
        )


def _fold(query: MetricQuery, values: list[float] | None) -> float | None:
    """Collapse a window's datapoints into one number, or None when there are none."""
    if not values:
        return None

    try:
        fold = _FOLD[query.statistic]
    except KeyError:
        raise TelemetryError(
            f"{query.resource_address} {query.metric}: no rule for folding a "
            f"{query.statistic!r} series"
        ) from None

    return float(fold(values))


def _query_id(resource_address: str, metric: str) -> str:
    """A CloudWatch-safe id that still says which metric it belongs to.

    GetMetricData ids must start with a lowercase letter and contain only
    alphanumerics and underscores, so the address is transliterated rather than used
    directly.
    """
    safe = "".join(char if char.isalnum() else "_" for char in resource_address)
    return f"m_{safe}__{metric}"

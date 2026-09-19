"""Deterministic impact prediction.

No model, no randomness, no I/O. Given the same ChangeSet and the same graph, this
module always produces the same Prediction, and every number it emits is traceable
to a rule in `rules_fired` and a constant in `thresholds`.

The pressure model
------------------

A concurrency increase of factor `m` at the origin propagates downstream, attenuated
by the graph's per-edge propagation factors. For a path with combined propagation
`p`, the downstream pressure multiplier is `m ** p`:

    p = 1.0  ->  the target feels the full multiplier
    p = 0.0  ->  the target feels nothing (multiplier 1.0)

Exponential attenuation is used rather than linear so that the multiplier stays
above 1.0 at every depth. A downstream resource never sees pressure *reduced* by an
upstream increase, which linear attenuation would eventually imply.

Each metric then scales that pressure by its own sensitivity, and the point estimate
is widened into a band by `PREDICTION_BAND`.
"""

from __future__ import annotations

from .graph import DependencyGraph
from .models import (
    BlastRadius,
    ChangeSet,
    DependencyPath,
    Prediction,
    PredictedMetric,
    ResourceChange,
    RuleTrace,
    Severity,
)
from . import thresholds as T

#: The Terraform attribute the first demo change modifies.
RESERVED_CONCURRENCY = "reserved_concurrent_executions"


class UnsupportedChangeError(ValueError):
    """No rule understands this change.

    Raised rather than returning a LOW-risk prediction, because "no rule matched"
    and "this change is safe" are different statements and must not be conflated.
    """


def predict(change_set: ChangeSet, graph: DependencyGraph) -> Prediction:
    effective = change_set.effective_changes
    if not effective:
        raise UnsupportedChangeError("plan contains no effective changes to analyse")

    rules: list[RuleTrace] = []
    origins: list[str] = []
    severities: list[Severity] = []
    pressure_by_origin: dict[str, float] = {}

    for change in effective:
        result = _lambda_reserved_concurrency_rule(change)
        if result is None:
            continue
        trace, multiplier, severity = result
        rules.append(trace)
        origins.append(change.address)
        severities.append(severity)
        pressure_by_origin[change.address] = multiplier

    if not rules:
        addresses = ", ".join(c.address for c in effective)
        raise UnsupportedChangeError(
            f"no prediction rule matched any of: {addresses}. "
            "Add a rule before analysing this change type."
        )

    for origin in origins:
        if origin not in graph.known_addresses():
            raise UnsupportedChangeError(
                f"{origin} was changed but is absent from the dependency graph; "
                "its blast radius cannot be determined"
            )

    blast_radius = graph.blast_radius(tuple(origins), T.PREDICTION_MAX_DEPTH)
    predicted_metrics = _predict_metrics(graph, blast_radius, pressure_by_origin)

    reliability = Severity.highest(*severities)
    dimensions = {
        "security": Severity.NONE,
        "reliability": reliability,
        "cost": _cost_severity(max(pressure_by_origin.values())),
    }

    return Prediction(
        severity=Severity.highest(*dimensions.values()),
        dimensions=dimensions,
        blast_radius=blast_radius,
        predicted_metrics=predicted_metrics,
        rules_fired=tuple(rules),
        confidence=_confidence(blast_radius),
    )


def _lambda_reserved_concurrency_rule(
    change: ResourceChange,
) -> tuple[RuleTrace, float, Severity] | None:
    """Fires on an increase to a Lambda function's reserved concurrency."""
    if change.resource_type != "aws_lambda_function":
        return None

    attribute = change.attribute(RESERVED_CONCURRENCY)
    if attribute is None:
        return None

    before = _as_positive_number(attribute.before)
    after = _as_positive_number(attribute.after)
    if before is None or after is None:
        raise UnsupportedChangeError(
            f"{change.address}: {RESERVED_CONCURRENCY} changed "
            f"{attribute.before!r} -> {attribute.after!r}; a rule for unset or "
            "non-positive concurrency does not exist yet"
        )
    if after <= before:
        return None

    multiplier = after / before
    severity = _severity_for_multiplier(multiplier)

    trace = RuleTrace(
        rule_id="lambda.reserved_concurrency.increase",
        description=(
            "Raising reserved concurrency raises the number of simultaneous "
            "invocations, and therefore the simultaneous load placed on every "
            "downstream dependency."
        ),
        inputs={
            "address": change.address,
            "attribute": RESERVED_CONCURRENCY,
            "before": before,
            "after": after,
            "multiplier": round(multiplier, 3),
        },
        threshold={
            "bands": [
                {"upToMultiplier": None if limit == float("inf") else limit, "severity": sev.value}
                for limit, sev in T.CONCURRENCY_SEVERITY_BANDS
            ]
        },
        outcome=f"reliability severity {severity.value} at multiplier {multiplier:.2f}x",
    )
    return trace, multiplier, severity


def _predict_metrics(
    graph: DependencyGraph,
    blast_radius: BlastRadius,
    pressure_by_origin: dict[str, float],
) -> tuple[PredictedMetric, ...]:
    predicted: list[PredictedMetric] = []

    for origin, multiplier in pressure_by_origin.items():
        predicted.extend(_metrics_for_node(graph, origin, multiplier, propagation=1.0, path=None))

    for path in blast_radius.paths:
        multiplier = pressure_by_origin[path.origin]
        predicted.extend(
            _metrics_for_node(graph, path.target, multiplier, path.propagation, path)
        )

    return tuple(predicted)


def _metrics_for_node(
    graph: DependencyGraph,
    address: str,
    origin_multiplier: float,
    propagation: float,
    path: DependencyPath | None,
) -> list[PredictedMetric]:
    node = graph.node(address)
    metrics = T.METRICS_BY_RESOURCE_TYPE.get(node.resource_type, ())
    if not metrics:
        return []

    pressure = origin_multiplier**propagation
    pressure_pct = (pressure - 1.0) * 100.0

    if path is None:
        rationale_prefix = f"directly changed; pressure multiplier {pressure:.2f}x"
    else:
        chain = " -> ".join([path.origin] + [hop.target for hop in path.hops])
        rationale_prefix = (
            f"{chain} (depth {path.depth}, propagation {path.propagation:.3f}); "
            f"pressure multiplier {pressure:.2f}x"
        )

    out: list[PredictedMetric] = []
    for metric in metrics:
        sensitivity = T.METRIC_SENSITIVITY.get(metric)
        if sensitivity is None:
            continue
        point = pressure_pct * sensitivity
        out.append(
            PredictedMetric(
                resource_address=address,
                metric=metric,
                expected_change_pct_low=point * (1.0 - T.PREDICTION_BAND),
                expected_change_pct_high=point * (1.0 + T.PREDICTION_BAND),
                rationale=f"{rationale_prefix}, metric sensitivity {sensitivity}",
            )
        )
    return out


def _severity_for_multiplier(multiplier: float) -> Severity:
    for limit, severity in T.CONCURRENCY_SEVERITY_BANDS:
        if multiplier <= limit:
            return severity
    return Severity.CRITICAL


def _cost_severity(multiplier: float) -> Severity:
    """More concurrency means more billable compute, but bounded by actual traffic."""
    if multiplier <= 2.0:
        return Severity.LOW
    if multiplier <= 10.0:
        return Severity.MEDIUM
    return Severity.HIGH


def _confidence(blast_radius: BlastRadius) -> float:
    deepest = max((path.depth for path in blast_radius.paths), default=0)
    confidence = T.PREDICTION_BASE_CONFIDENCE - deepest * T.PREDICTION_CONFIDENCE_DECAY_PER_HOP
    return max(T.PREDICTION_MIN_CONFIDENCE, confidence)


def _as_positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None

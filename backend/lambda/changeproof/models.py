"""Data model shared by every pipeline stage.

Every type here is frozen and JSON-serializable via `to_dict`, so Step Functions can
pass state between stages and the accumulated object is itself the evidence bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def highest(cls, *values: "Severity") -> "Severity":
        return max(values, key=lambda s: s.rank) if values else cls.NONE


_SEVERITY_RANK = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ComparisonOutcome(str, Enum):
    WITHIN_PREDICTION = "WITHIN_PREDICTION"
    WORSE_THAN_PREDICTED = "WORSE_THAN_PREDICTED"
    BETTER_THAN_PREDICTED = "BETTER_THAN_PREDICTED"
    NOT_OBSERVED = "NOT_OBSERVED"


# --- stage 1: parsed Terraform change ------------------------------------------------


@dataclass(frozen=True)
class AttributeChange:
    attribute: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"attribute": self.attribute, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class ResourceChange:
    address: str
    resource_type: str
    name: str
    actions: tuple[str, ...]
    attribute_changes: tuple[AttributeChange, ...]

    def attribute(self, name: str) -> AttributeChange | None:
        for change in self.attribute_changes:
            if change.attribute == name:
                return change
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "resourceType": self.resource_type,
            "name": self.name,
            "actions": list(self.actions),
            "attributeChanges": [c.to_dict() for c in self.attribute_changes],
        }


@dataclass(frozen=True)
class ChangeSet:
    terraform_version: str
    format_version: str
    changes: tuple[ResourceChange, ...]

    @property
    def effective_changes(self) -> tuple[ResourceChange, ...]:
        """Changes that actually alter infrastructure; no-ops excluded."""
        return tuple(c for c in self.changes if c.actions != ("no-op",))

    def to_dict(self) -> dict[str, Any]:
        return {
            "terraformVersion": self.terraform_version,
            "formatVersion": self.format_version,
            "changes": [c.to_dict() for c in self.changes],
        }


# --- stage 2: dependency graph -------------------------------------------------------


@dataclass(frozen=True)
class DependencyHop:
    source: str
    target: str
    relation: str
    propagation: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "propagation": self.propagation,
        }


@dataclass(frozen=True)
class DependencyPath:
    origin: str
    target: str
    hops: tuple[DependencyHop, ...]

    @property
    def depth(self) -> int:
        return len(self.hops)

    @property
    def propagation(self) -> float:
        """Product of the per-hop propagation factors along the path."""
        total = 1.0
        for hop in self.hops:
            total *= hop.propagation
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "target": self.target,
            "depth": self.depth,
            "propagation": round(self.propagation, 6),
            "hops": [h.to_dict() for h in self.hops],
        }


@dataclass(frozen=True)
class BlastRadius:
    origin_addresses: tuple[str, ...]
    paths: tuple[DependencyPath, ...]

    @property
    def affected_addresses(self) -> tuple[str, ...]:
        seen: list[str] = list(self.origin_addresses)
        for path in self.paths:
            if path.target not in seen:
                seen.append(path.target)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "originAddresses": list(self.origin_addresses),
            "affectedAddresses": list(self.affected_addresses),
            "resourceCount": len(self.affected_addresses),
            "paths": [p.to_dict() for p in self.paths],
        }


# --- stage 3: prediction -------------------------------------------------------------


@dataclass(frozen=True)
class RuleTrace:
    """Why a rule fired, in enough detail to audit the verdict without reading code."""

    rule_id: str
    description: str
    inputs: dict[str, Any]
    threshold: dict[str, Any]
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "description": self.description,
            "inputs": self.inputs,
            "threshold": self.threshold,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class PredictedMetric:
    resource_address: str
    metric: str
    expected_change_pct_low: float
    expected_change_pct_high: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "resourceAddress": self.resource_address,
            "metric": self.metric,
            "expectedChangePctLow": round(self.expected_change_pct_low, 2),
            "expectedChangePctHigh": round(self.expected_change_pct_high, 2),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class Prediction:
    severity: Severity
    dimensions: dict[str, Severity]
    blast_radius: BlastRadius
    predicted_metrics: tuple[PredictedMetric, ...]
    rules_fired: tuple[RuleTrace, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "dimensions": {k: v.value for k, v in self.dimensions.items()},
            "blastRadius": self.blast_radius.to_dict(),
            "predictedMetrics": [m.to_dict() for m in self.predicted_metrics],
            "rulesFired": [r.to_dict() for r in self.rules_fired],
            "confidence": round(self.confidence, 3),
        }


# --- stage 4: observation ------------------------------------------------------------


@dataclass(frozen=True)
class ObservedMetric:
    resource_address: str
    metric: str
    unit: str
    baseline_value: float
    observed_value: float

    @property
    def change_pct(self) -> float | None:
        """Percentage change from baseline, or None when the baseline is zero.

        A zero baseline cannot express a ratio. Callers must handle it as an absolute
        threshold question instead of silently treating it as no change.
        """
        if self.baseline_value == 0:
            return None
        return (self.observed_value - self.baseline_value) / self.baseline_value * 100.0

    def to_dict(self) -> dict[str, Any]:
        change = self.change_pct
        return {
            "resourceAddress": self.resource_address,
            "metric": self.metric,
            "unit": self.unit,
            "baselineValue": self.baseline_value,
            "observedValue": self.observed_value,
            "changePct": None if change is None else round(change, 2),
        }


@dataclass(frozen=True)
class Observation:
    source: str
    simulated: bool
    metrics: tuple[ObservedMetric, ...]

    def find(self, resource_address: str, metric: str) -> ObservedMetric | None:
        for m in self.metrics:
            if m.resource_address == resource_address and m.metric == metric:
                return m
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "simulated": self.simulated,
            "metrics": [m.to_dict() for m in self.metrics],
        }


# --- stage 5: comparison and decision ------------------------------------------------


@dataclass(frozen=True)
class MetricComparison:
    resource_address: str
    metric: str
    predicted_low: float | None
    predicted_high: float | None
    actual_change_pct: float | None
    outcome: ComparisonOutcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "resourceAddress": self.resource_address,
            "metric": self.metric,
            "predictedLow": _round_or_none(self.predicted_low),
            "predictedHigh": _round_or_none(self.predicted_high),
            "actualChangePct": _round_or_none(self.actual_change_pct),
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True)
class ThresholdBreach:
    resource_address: str
    metric: str
    actual_value: float
    limit: float
    limit_kind: str
    severity: Severity
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "resourceAddress": self.resource_address,
            "metric": self.metric,
            "actualValue": round(self.actual_value, 2),
            "limit": self.limit,
            "limitKind": self.limit_kind,
            "severity": self.severity.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class ComparisonResult:
    verdict: Verdict
    observed_severity: Severity
    breaches: tuple[ThresholdBreach, ...]
    metric_comparisons: tuple[MetricComparison, ...]
    prediction_accuracy: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "observedSeverity": self.observed_severity.value,
            "breaches": [b.to_dict() for b in self.breaches],
            "metricComparisons": [c.to_dict() for c in self.metric_comparisons],
            "predictionAccuracy": round(self.prediction_accuracy, 3),
            "reason": self.reason,
        }


# --- the accumulated evidence bundle -------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    experiment_id: str
    phase: str
    change_set: ChangeSet
    prediction: Prediction
    observation: Observation
    comparison: ComparisonResult
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "phase": self.phase,
            "changeSet": self.change_set.to_dict(),
            "prediction": self.prediction.to_dict(),
            "observation": self.observation.to_dict(),
            "comparison": self.comparison.to_dict(),
            "notes": list(self.notes),
        }


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 2)

"""Phase 0 implementations: local filesystem only, no network, no credentials.

Everything produced here that did not come from a real measurement is flagged as
simulated, and that flag travels into the evidence bundle and the explanation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..graph import DependencyGraph
from ..models import (
    ComparisonOutcome,
    Evidence,
    Observation,
    ObservedMetric,
    Severity,
    Verdict,
)

if TYPE_CHECKING:  # imported for typing only, so this module stays import-light
    from ..manifest import ExperimentManifest, MetricWindow
    from ..telemetry import MetricQuery

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

#: Stamped into every evidence bundle so a Phase 0 artifact can never be mistaken
#: for one produced against real infrastructure.
PHASE = "phase-0-local"


def load_graph(path: str | Path | None = None) -> DependencyGraph:
    """Load the dependency graph from a local JSON fixture.

    Phase 1 replaces this with a Neptune-backed `GraphSource`. The engine calls
    `known_addresses` and `downstream_paths` either way.
    """
    source = Path(path) if path is not None else FIXTURES / "dependency_graph.json"
    return DependencyGraph.from_dict(json.loads(source.read_text(encoding="utf-8")))


class FixtureTelemetrySource:
    """Telemetry read from a local JSON fixture. Satisfies the `TelemetrySource` port.

    These are *not* AWS measurements. They are hand-authored values that describe
    what the demo scenario is asserting happened, and every Observation this class
    returns carries `simulated=True`.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else FIXTURES / "telemetry_observed.json"

    def collect(self, experiment_id: str) -> Observation:
        document = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{self._path} must contain a JSON object")

        raw_metrics = document.get("metrics")
        if not isinstance(raw_metrics, list):
            raise ValueError(f"{self._path} is missing a 'metrics' list")

        metrics = tuple(
            ObservedMetric(
                resource_address=entry["resourceAddress"],
                metric=entry["metric"],
                unit=entry["unit"],
                baseline_value=float(entry["baselineValue"]),
                observed_value=float(entry["observedValue"]),
            )
            for entry in raw_metrics
        )
        return Observation(
            source=f"fixture:{self._path.name}",
            simulated=True,
            metrics=metrics,
        )


class StaticMetricDataSource:
    """Datapoints from a local JSON fixture. Satisfies `telemetry.MetricDataSource`.

    These are *not* CloudWatch measurements. They are hand-authored series standing
    in for what GetMetricData would return, so the collector's query building and
    folding can be exercised with no account and no credentials.

    A metric absent from the fixture yields no datapoints, which is what CloudWatch
    does for an idle resource. That is a real answer, not an error, and the collector
    omits the metric rather than inventing a zero.

    Phase 1 replaces this with a boto3-backed source. The collector does not change.
    """

    def __init__(self, manifest: "ExperimentManifest", path: str | Path | None = None) -> None:
        self._manifest = manifest
        self._path = Path(path) if path is not None else FIXTURES / "cloudwatch_responses.json"

    def fetch(
        self, queries: "tuple[MetricQuery, ...]", window: "MetricWindow"
    ) -> dict[str, list[float]]:
        label = self._label_for(window)
        document = json.loads(self._path.read_text(encoding="utf-8"))

        series = document.get("windows", {}).get(label)
        if not isinstance(series, dict):
            raise ValueError(f"{self._path} has no '{label}' window")

        results: dict[str, list[float]] = {}
        for query in queries:
            values = series.get(query.resource_address, {}).get(query.metric)
            if values is None:
                continue
            results[query.query_id] = [float(value) for value in values]
        return results

    def _label_for(self, window: "MetricWindow") -> str:
        """Which run this window belongs to, by matching the manifest's own windows.

        Matching on the window rather than trusting call order means a caller that
        asks for the two windows out of order still gets the right series.
        """
        if window == self._manifest.baseline_window:
            return "baseline"
        if window == self._manifest.changed_window:
            return "changed"
        raise ValueError(
            f"window {window.start_epoch}-{window.end_epoch} belongs to neither run "
            f"of experiment {self._manifest.experiment_id}"
        )


class LocalEvidenceStore:
    """Evidence written to disk as JSON. Satisfies the `EvidenceStore` port.

    Phase 1 replaces this with S3 for artifacts and DynamoDB for experiment state.
    The layout below mirrors the intended S3 key structure so the move is a swap
    rather than a redesign.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def path_for(self, experiment_id: str) -> Path:
        return self._root / "experiments" / experiment_id / "evidence.json"

    def store(self, evidence: Evidence) -> str:
        target = self.path_for(evidence.experiment_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(evidence.to_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return str(target)

    def load(self, experiment_id: str) -> Evidence:
        raise NotImplementedError(
            "Rehydrating a full Evidence object from JSON is not needed in Phase 0; "
            "the explainer receives the in-memory bundle directly."
        )


class DeterministicExplainer:
    """Builds the explanation from templates over the evidence. No model involved.

    This is the Phase 0 stand-in for Bedrock, and it enforces the contract Bedrock
    will inherit: every figure in the output is read from the evidence bundle. If a
    value is not in the evidence, it does not appear in the prose.
    """

    def explain(self, evidence: Evidence) -> str:
        sections = [
            self._change(evidence),
            self._prediction(evidence),
            self._observation(evidence),
            self._decision(evidence),
            self._accuracy(evidence),
        ]
        body = "\n\n".join(section for section in sections if section)
        return f"{body}\n\n{self._provenance(evidence)}"

    def _change(self, evidence: Evidence) -> str:
        lines = ["PROPOSED CHANGE"]
        for change in evidence.change_set.effective_changes:
            action = "/".join(change.actions)
            lines.append(f"  {change.address} ({action})")
            for attribute in change.attribute_changes:
                lines.append(f"    {attribute.attribute}: {attribute.before} -> {attribute.after}")
        return "\n".join(lines)

    def _prediction(self, evidence: Evidence) -> str:
        prediction = evidence.prediction
        radius = prediction.blast_radius
        lines = [
            "PREDICTION",
            f"  Overall severity: {prediction.severity.value} "
            f"(confidence {prediction.confidence:.0%})",
        ]
        for dimension, severity in prediction.dimensions.items():
            lines.append(f"    {dimension}: {severity.value}")
        lines.append(
            f"  Blast radius: {len(radius.affected_addresses)} resources - "
            + ", ".join(radius.affected_addresses)
        )
        for rule in prediction.rules_fired:
            lines.append(f"  Rule {rule.rule_id}: {rule.outcome}")
            lines.append(f"    {rule.description}")
        return "\n".join(lines)

    def _observation(self, evidence: Evidence) -> str:
        observation = evidence.observation
        lines = [f"OBSERVED ({observation.source})"]
        for metric in observation.metrics:
            change = metric.change_pct
            movement = "no baseline ratio" if change is None else f"{change:+.1f}%"
            lines.append(
                f"  {metric.resource_address} {metric.metric}: "
                f"{metric.baseline_value:g} -> {metric.observed_value:g} "
                f"{metric.unit} ({movement})"
            )
        return "\n".join(lines)

    def _decision(self, evidence: Evidence) -> str:
        comparison = evidence.comparison
        marker = "APPROVED" if comparison.verdict is Verdict.APPROVE else "REJECTED"
        lines = [f"DECISION: {marker}", f"  {comparison.reason}"]
        if comparison.breaches:
            lines.append("  Safety limits breached:")
            for breach in comparison.breaches:
                limit = (
                    f"{breach.limit:g}%" if breach.limit_kind == "change_pct" else f"{breach.limit:g}"
                )
                actual = (
                    f"{breach.actual_value:+.1f}%"
                    if breach.limit_kind == "change_pct"
                    else f"{breach.actual_value:g}"
                )
                lines.append(
                    f"    [{breach.severity.value}] {breach.resource_address} "
                    f"{breach.metric}: {actual} exceeds limit {limit}. {breach.description}"
                )
        return "\n".join(lines)

    def _accuracy(self, evidence: Evidence) -> str:
        comparison = evidence.comparison
        worse = [
            c
            for c in comparison.metric_comparisons
            if c.outcome is ComparisonOutcome.WORSE_THAN_PREDICTED
        ]
        lines = [
            "PREDICTED vs ACTUAL",
            f"  Metrics inside the predicted band: {comparison.prediction_accuracy:.0%}",
        ]
        for c in worse:
            if c.actual_change_pct is None or c.predicted_high is None:
                continue
            lines.append(
                f"  Underestimated: {c.resource_address} {c.metric} "
                f"predicted up to {c.predicted_high:+.1f}%, observed {c.actual_change_pct:+.1f}%"
            )
        if not worse:
            lines.append("  No metric exceeded its predicted band.")
        return "\n".join(lines)

    def _provenance(self, evidence: Evidence) -> str:
        lines = [
            "PROVENANCE",
            f"  Experiment: {evidence.experiment_id}",
            f"  Phase: {evidence.phase}",
            "  Explanation generated by DeterministicExplainer (templates over the "
            "evidence bundle). No language model was involved.",
        ]
        if evidence.observation.simulated:
            lines.append(
                "  WARNING: telemetry is SIMULATED fixture data, not measured from AWS. "
                "This verdict demonstrates the engine, it does not describe real "
                "infrastructure behaviour."
            )
        for note in evidence.notes:
            lines.append(f"  Note: {note}")
        return "\n".join(lines)


def severity_marker(severity: Severity) -> str:
    return {
        Severity.NONE: "none",
        Severity.LOW: "low",
        Severity.MEDIUM: "medium",
        Severity.HIGH: "high",
        Severity.CRITICAL: "critical",
    }[severity]

"""The Phase 0 pipeline, wired from local adapters.

This is what makes the promise testable: given a Terraform plan on disk, it runs
parse -> graph -> predict -> telemetry -> compare -> store -> explain end to end,
with no credentials, no network and no AWS API call of any kind.

Step Functions drives the same stages individually in Phase 1; this module exists so
the whole chain can be exercised in one process, locally and in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .adapters.local import (
    PHASE,
    DeterministicExplainer,
    FixtureTelemetrySource,
    LocalEvidenceStore,
    load_graph,
)
from .compare import compare
from .models import Evidence
from .parser import parse_plan_file
from .predict import predict

PHASE_0_NOTES = (
    "Phase 0: no AWS API call was made at any point in this run.",
    "Test environment provisioning, change application and synthetic workload "
    "generation are not implemented; telemetry came from a fixture.",
)


@dataclass(frozen=True)
class PipelineResult:
    evidence: Evidence
    explanation: str
    evidence_path: str


def run(
    plan_path: str | Path,
    experiment_id: str,
    output_root: str | Path,
    graph_path: str | Path | None = None,
    telemetry_path: str | Path | None = None,
) -> PipelineResult:
    change_set = parse_plan_file(plan_path)
    graph = load_graph(graph_path)
    prediction = predict(change_set, graph)

    observation = FixtureTelemetrySource(telemetry_path).collect(experiment_id)
    comparison = compare(prediction, observation)

    evidence = Evidence(
        experiment_id=experiment_id,
        phase=PHASE,
        change_set=change_set,
        prediction=prediction,
        observation=observation,
        comparison=comparison,
        notes=PHASE_0_NOTES,
    )

    evidence_path = LocalEvidenceStore(output_root).store(evidence)
    explanation = DeterministicExplainer().explain(evidence)

    return PipelineResult(
        evidence=evidence,
        explanation=explanation,
        evidence_path=evidence_path,
    )

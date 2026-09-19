"""Lambda entry point.

One function, one deployment package. Step Functions invokes the same ARN for every
state and selects the stage with the `stage` field, so workflow structure lives in
the state machine definition rather than in branching here.

Each stage takes the accumulated state object and returns its own contribution.
Step Functions merges the result back under a `ResultPath`, so the state object
grows into the complete evidence bundle as the workflow proceeds.

Phase 0: the stages that need AWS raise PhaseNotAuthorizedError. They are not
stubbed with plausible return values, because a workflow that appears to succeed
without touching AWS would be reporting a result that never happened.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from changeproof import PhaseNotAuthorizedError
from changeproof.adapters import aws as aws_adapters
from changeproof.adapters.local import (
    PHASE,
    DeterministicExplainer,
    FixtureTelemetrySource,
    LocalEvidenceStore,
    load_graph,
)
from changeproof.compare import compare
from changeproof.models import Evidence
from changeproof.parser import parse_plan, parse_plan_file
from changeproof.pipeline import PHASE_0_NOTES
from changeproof.predict import predict

#: Where Phase 0 writes evidence. Overridable so tests do not write into the repo.
EVIDENCE_ROOT = os.environ.get("CHANGEPROOF_EVIDENCE_ROOT", ".changeproof")


class StageError(ValueError):
    """The event did not name a stage this handler knows how to run."""


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise StageError(f"event must be an object, got {type(event).__name__}")

    stage = event.get("stage")
    if not isinstance(stage, str):
        raise StageError("event is missing a string 'stage' field")

    runner = _STAGES.get(stage)
    if runner is None:
        known = ", ".join(sorted(_STAGES))
        raise StageError(f"unknown stage {stage!r}; known stages: {known}")

    return runner(event)


# --- Phase 0 stages: these run locally ------------------------------------------------


def _parse_change(event: dict[str, Any]) -> dict[str, Any]:
    """Terraform plan JSON -> ChangeSet.

    Accepts the plan inline as `plan` or as a local path in `planPath`.
    """
    if "plan" in event:
        change_set = parse_plan(event["plan"])
    elif "planPath" in event:
        change_set = parse_plan_file(event["planPath"])
    else:
        raise StageError("parse_change needs either 'plan' or 'planPath'")
    return {"changeSet": change_set.to_dict()}


def _resolve_dependencies(event: dict[str, Any]) -> dict[str, Any]:
    """Blast radius of the changed resources, from the local graph."""
    change_set = _require_change_set(event)
    graph = load_graph(event.get("graphPath"))
    origins = tuple(c.address for c in change_set.effective_changes)
    radius = graph.blast_radius(origins, _max_depth(event))
    return {"blastRadius": radius.to_dict()}


def _predict_impact(event: dict[str, Any]) -> dict[str, Any]:
    change_set = _require_change_set(event)
    graph = load_graph(event.get("graphPath"))
    return {"prediction": predict(change_set, graph).to_dict()}


def _collect_telemetry(event: dict[str, Any]) -> dict[str, Any]:
    """Phase 0 telemetry, from a fixture and flagged as simulated."""
    experiment_id = _require_experiment_id(event)
    observation = FixtureTelemetrySource(event.get("telemetryPath")).collect(experiment_id)
    return {"observation": observation.to_dict()}


def _compare(event: dict[str, Any]) -> dict[str, Any]:
    """Predicted vs actual, and the verdict.

    Recomputes prediction and observation from their sources rather than trusting
    the dicts already on the state object, so the verdict is always derived from
    typed values that this process produced.
    """
    prediction, observation = _rebuild_prediction_and_observation(event)
    return {"comparison": compare(prediction, observation).to_dict()}


def _store_evidence(event: dict[str, Any]) -> dict[str, Any]:
    experiment_id = _require_experiment_id(event)
    evidence = _build_evidence(event, experiment_id)
    location = LocalEvidenceStore(event.get("evidenceRoot", EVIDENCE_ROOT)).store(evidence)
    return {"evidenceLocation": location, "phase": PHASE}


def _explain(event: dict[str, Any]) -> dict[str, Any]:
    """Deterministic explanation built from the evidence. No model involved."""
    experiment_id = _require_experiment_id(event)
    evidence = _build_evidence(event, experiment_id)
    return {
        "explanation": DeterministicExplainer().explain(evidence),
        "explainer": "DeterministicExplainer",
    }


# --- stages deferred to a later phase -------------------------------------------------


def _provision_test_env(event: dict[str, Any]) -> dict[str, Any]:
    return aws_adapters.TerraformTestEnvironment().provision(_require_experiment_id(event))


def _apply_change(event: dict[str, Any]) -> dict[str, Any]:
    return aws_adapters.TerraformTestEnvironment().apply_change(
        _require_experiment_id(event), event.get("planPath", "")
    )


def _run_workload(event: dict[str, Any]) -> dict[str, Any]:
    return aws_adapters.TerraformTestEnvironment().run_workload(
        _require_experiment_id(event),
        int(event.get("requestsPerSecond", 100)),
        int(event.get("seconds", 60)),
    )


def _teardown_test_env(event: dict[str, Any]) -> dict[str, Any]:
    return aws_adapters.TerraformTestEnvironment().teardown(_require_experiment_id(event))


_STAGES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "parse_change": _parse_change,
    "resolve_dependencies": _resolve_dependencies,
    "predict_impact": _predict_impact,
    "provision_test_env": _provision_test_env,
    "apply_change": _apply_change,
    "run_workload": _run_workload,
    "collect_telemetry": _collect_telemetry,
    "teardown_test_env": _teardown_test_env,
    "compare": _compare,
    "store_evidence": _store_evidence,
    "explain": _explain,
}

#: Stages that cannot run until a later phase is authorized. Kept explicit so tests
#: can assert the boundary rather than infer it.
DEFERRED_STAGES = frozenset(
    {"provision_test_env", "apply_change", "run_workload", "teardown_test_env"}
)


# --- helpers --------------------------------------------------------------------------


def _require_experiment_id(event: dict[str, Any]) -> str:
    experiment_id = event.get("experimentId")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise StageError("event is missing a non-empty 'experimentId'")
    return experiment_id


def _max_depth(event: dict[str, Any]) -> int:
    from changeproof import thresholds

    value = event.get("maxDepth", thresholds.PREDICTION_MAX_DEPTH)
    return int(value)


def _require_change_set(event: dict[str, Any]):
    """Re-derive the ChangeSet from its source rather than from the state dict.

    The state object carries `changeSet` for evidence and for the workflow to
    inspect, but stages rebuild from the plan so that no stage depends on another
    stage's serialization being reversible.
    """
    if "plan" in event:
        return parse_plan(event["plan"])
    if "planPath" in event:
        return parse_plan_file(event["planPath"])
    raise StageError("stage needs either 'plan' or 'planPath' to rebuild the change set")


def _rebuild_prediction_and_observation(event: dict[str, Any]):
    change_set = _require_change_set(event)
    graph = load_graph(event.get("graphPath"))
    prediction = predict(change_set, graph)
    observation = FixtureTelemetrySource(event.get("telemetryPath")).collect(
        _require_experiment_id(event)
    )
    return prediction, observation


def _build_evidence(event: dict[str, Any], experiment_id: str) -> Evidence:
    change_set = _require_change_set(event)
    prediction, observation = _rebuild_prediction_and_observation(event)
    return Evidence(
        experiment_id=experiment_id,
        phase=PHASE,
        change_set=change_set,
        prediction=prediction,
        observation=observation,
        comparison=compare(prediction, observation),
        notes=PHASE_0_NOTES,
    )


__all__ = ["handler", "StageError", "DEFERRED_STAGES", "PhaseNotAuthorizedError"]

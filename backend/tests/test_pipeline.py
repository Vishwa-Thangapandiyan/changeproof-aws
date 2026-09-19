"""End-to-end Phase 0: Terraform plan on disk -> verdict, evidence and explanation."""

from __future__ import annotations

import json

import pytest

from changeproof.models import Verdict


@pytest.fixture
def result(tmp_path, plan_path):
    from changeproof.pipeline import run

    return run(plan_path, experiment_id="EXP-001", output_root=tmp_path)


def test_pipeline_produces_a_verdict(result):
    assert result.evidence.comparison.verdict is Verdict.REJECT


def test_evidence_is_written_to_disk(result, tmp_path):
    from pathlib import Path

    path = Path(result.evidence_path)

    assert path.exists()
    assert path == tmp_path / "experiments" / "EXP-001" / "evidence.json"


def test_written_evidence_is_valid_json_and_complete(result):
    from pathlib import Path

    document = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))

    assert document["experimentId"] == "EXP-001"
    assert document["phase"] == "phase-0-local"
    assert document["comparison"]["verdict"] == "REJECT"
    assert document["prediction"]["rulesFired"]
    assert document["changeSet"]["changes"]
    assert document["observation"]["metrics"]


def test_evidence_records_that_telemetry_was_simulated(result):
    assert result.evidence.observation.simulated is True
    assert "fixture" in result.evidence.observation.source


def test_evidence_notes_that_no_aws_call_was_made(result):
    assert any("no AWS API call" in note for note in result.evidence.notes)


def test_pipeline_is_deterministic(tmp_path, plan_path):
    from changeproof.pipeline import run

    first = run(plan_path, "EXP-A", tmp_path / "a").evidence.to_dict()
    second = run(plan_path, "EXP-B", tmp_path / "b").evidence.to_dict()

    first.pop("experimentId")
    second.pop("experimentId")
    assert first == second


# --- the explanation may only restate the evidence ------------------------------------


def test_explanation_reports_the_verdict(result):
    assert "DECISION: REJECTED" in result.explanation


def test_explanation_states_it_used_no_model(result):
    assert "No language model was involved" in result.explanation


def test_explanation_warns_that_telemetry_is_simulated(result):
    assert "SIMULATED" in result.explanation
    assert "does not describe real" in result.explanation


def test_explanation_shows_the_change(result):
    assert "reserved_concurrent_executions: 10 -> 100" in result.explanation


def test_explanation_quotes_only_values_present_in_the_evidence(result):
    """Every observed value printed must exist in the observation."""
    observed_values = set()
    for metric in result.evidence.observation.metrics:
        observed_values.add(f"{metric.baseline_value:g}")
        observed_values.add(f"{metric.observed_value:g}")

    for metric in result.evidence.observation.metrics:
        line = (
            f"{metric.resource_address} {metric.metric}: "
            f"{metric.baseline_value:g} -> {metric.observed_value:g}"
        )
        assert line in result.explanation


def test_explanation_names_the_underestimated_metric(result):
    assert "Underestimated" in result.explanation
    assert "ApproximateNumberOfMessagesVisible" in result.explanation


def test_explanation_reports_prediction_accuracy(result):
    assert "Metrics inside the predicted band" in result.explanation


# --- approval path --------------------------------------------------------------------


def test_benign_telemetry_yields_approval(tmp_path, plan_path, fixtures_dir):
    """Same change, telemetry that stays within limits: the verdict flips."""
    from changeproof.pipeline import run

    source = json.loads((fixtures_dir / "telemetry_observed.json").read_text(encoding="utf-8"))
    for metric in source["metrics"]:
        metric["observedValue"] = metric["baselineValue"]

    benign = tmp_path / "benign.json"
    benign.write_text(json.dumps(source), encoding="utf-8")

    result = run(plan_path, "EXP-OK", tmp_path / "out", telemetry_path=benign)

    assert result.evidence.comparison.verdict is Verdict.APPROVE
    assert "DECISION: APPROVED" in result.explanation

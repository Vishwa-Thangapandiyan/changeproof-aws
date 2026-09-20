"""The live prediction behind the demo page. Local engine only."""

from __future__ import annotations

import pytest

from demo_api import DemoInputError, predict_for


def test_the_recorded_change_matches_the_evidence_run():
    result = predict_for(100)
    assert result["recordedRun"] is True
    assert result["prediction"]["severity"] == "HIGH"
    assert len(result["prediction"]["predictedMetrics"]) == 13
    assert result["prediction"]["blastRadius"]["resourceCount"] == 4


def test_a_small_increase_is_low_risk_and_not_marked_as_the_recorded_run():
    result = predict_for(12)
    assert result["recordedRun"] is False
    assert result["prediction"]["severity"] == "LOW"


def test_risk_grows_with_the_change():
    order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    levels = [order[predict_for(n)["prediction"]["severity"]] for n in (12, 25, 100, 400)]
    assert levels == sorted(levels) and levels[0] < levels[-1]


@pytest.mark.parametrize("bad", [0, -5, 1001, "10", 10.5, None, True])
def test_invalid_input_is_refused(bad):
    with pytest.raises(DemoInputError):
        predict_for(bad)


@pytest.mark.parametrize("unsupported", [5, 10])
def test_changes_the_engine_does_not_model_fail_loudly(unsupported):
    with pytest.raises(DemoInputError):
        predict_for(unsupported)

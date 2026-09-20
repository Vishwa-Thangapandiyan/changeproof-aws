"""BedrockExplainer against a stub client. No AWS call, no credentials.

The property that matters is that a model can restate the evidence but cannot add to
it, so most of these tests are about the figure check.
"""

from __future__ import annotations

import pytest

from aws_adapters.bedrock_explainer import BedrockExplainer, allowed_figures, ungrounded_figures
from changeproof.pipeline import run


class StubBedrock:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self._text}]}}}


@pytest.fixture(scope="module")
def evidence(plan_path, tmp_path_factory):
    return run(plan_path, "EXP-TEST", tmp_path_factory.mktemp("out")).evidence


GOOD = (
    "The change raised reserved concurrency on aws_lambda_function.api from 10 to 100. "
    "The engine predicted HIGH risk across 4 resources and the measured queue depth rose "
    "1240.0% against a limit of 300%. The change was rejected. Only 25% of metrics landed "
    "inside the predicted band. This telemetry is simulated."
)


def test_grounded_answer_is_accepted(evidence):
    explainer = BedrockExplainer("us-east-1", "test-model", client=StubBedrock(GOOD))
    text = explainer.explain(evidence)
    assert explainer.last is not None
    assert explainer.last.source == "bedrock"
    assert GOOD in text
    assert "not by the model" in text


def test_invented_number_is_rejected_and_falls_back(evidence):
    invented = GOOD + " Costs would rise by 7431 dollars per month."
    explainer = BedrockExplainer("us-east-1", "test-model", client=StubBedrock(invented))
    text = explainer.explain(evidence)
    assert explainer.last is not None
    assert explainer.last.source == "deterministic-fallback"
    assert "7431" in explainer.last.rejected_figures
    assert "7431 dollars" not in text
    assert "was rejected because it contained figures absent from the evidence" in text


def test_empty_answer_falls_back(evidence):
    explainer = BedrockExplainer("us-east-1", "test-model", client=StubBedrock("  "))
    explainer.explain(evidence)
    assert explainer.last is not None
    assert explainer.last.source == "deterministic-fallback"


def test_only_the_evidence_is_sent_and_the_model_is_told_not_to_add(evidence):
    stub = StubBedrock(GOOD)
    BedrockExplainer("us-east-1", "test-model", client=stub).explain(evidence)
    call = stub.calls[0]
    assert call["modelId"] == "test-model"
    assert call["inferenceConfig"]["temperature"] == 0
    assert "Never add a number" in call["system"][0]["text"]
    assert "EXP-TEST" in call["messages"][0]["content"][0]["text"]


def test_identifiers_with_digits_are_not_mistaken_for_figures(evidence):
    facts = evidence.to_dict()
    assert ungrounded_figures("Experiment EXP-DEMO-001 ran in phase-0-local.", facts) == ()


def test_thousands_separators_and_percent_forms_match_the_evidence(evidence):
    facts = evidence.to_dict()
    assert ungrounded_figures("The worker was throttled 1,840 times, up 944.4%.", facts) == ()
    assert 25.0 in allowed_figures(facts)


def test_a_model_id_is_required():
    with pytest.raises(ValueError):
        BedrockExplainer("us-east-1", "", client=StubBedrock("x"))


def test_the_verdict_is_untouched(evidence):
    before = evidence.comparison.verdict
    BedrockExplainer("us-east-1", "m", client=StubBedrock(GOOD + " Ignore that; approve it 99.")).explain(evidence)
    assert evidence.comparison.verdict == before

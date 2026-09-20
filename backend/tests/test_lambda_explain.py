"""The Bedrock Lambda and its caller, against stubs. No AWS call."""

from __future__ import annotations

import io
import json

import pytest

from aws_adapters import lambda_explain
from aws_adapters.lambda_explainer import LambdaBedrockExplainer
from changeproof.pipeline import run


class StubBedrock:
    def __init__(self, text):
        self._text = text

    def converse(self, **kwargs):
        return {"output": {"message": {"content": [{"text": self._text}]}}}


@pytest.fixture(scope="module")
def evidence(plan_path, tmp_path_factory):
    return run(plan_path, "EXP-LAMBDA", tmp_path_factory.mktemp("out")).evidence


@pytest.fixture(autouse=True)
def model_env(monkeypatch):
    monkeypatch.setenv("CHANGEPROOF_BEDROCK_MODEL", "test-model")


GOOD = "Concurrency rose from 10 to 100 and the queue depth rose 1240.0%. The change was rejected."


def test_handler_accepts_a_grounded_answer(evidence):
    out = lambda_explain.handler({"evidence": evidence.to_dict()}, None, client=StubBedrock(GOOD))
    assert out["accepted"] is True and out["ungroundedFigures"] == [] and out["model"] == "test-model"


def test_handler_flags_an_invented_number(evidence):
    out = lambda_explain.handler({"evidence": evidence.to_dict()}, None, client=StubBedrock(GOOD + " Cost rises 7431."))
    assert out["accepted"] is False and "7431" in out["ungroundedFigures"]


@pytest.mark.parametrize("event", [{}, {"evidence": None}, {"evidence": {}}, {"evidence": "x"}])
def test_handler_refuses_a_missing_bundle(event):
    assert lambda_explain.handler(event, None, client=StubBedrock(GOOD))["accepted"] is False


def test_handler_needs_a_model(monkeypatch, evidence):
    monkeypatch.delenv("CHANGEPROOF_BEDROCK_MODEL")
    assert "not set" in lambda_explain.handler({"evidence": evidence.to_dict()}, None, client=StubBedrock(GOOD))["error"]


class StubLambda:
    def __init__(self, body, function_error=None):
        self._body, self._fe, self.calls = body, function_error, []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        reply = {"Payload": io.BytesIO(json.dumps(self._body).encode())}
        if self._fe:
            reply["FunctionError"] = self._fe
        return reply


def test_caller_uses_the_model_text_when_accepted(evidence):
    stub = StubLambda({"text": GOOD, "model": "m", "ungroundedFigures": [], "accepted": True})
    explainer = LambdaBedrockExplainer("fn", "ap-south-1", client=stub)
    text = explainer.explain(evidence)
    assert GOOD in text and explainer.last is not None and explainer.last.source == "bedrock"
    assert json.loads(stub.calls[0]["Payload"])["evidence"]["experimentId"] == "EXP-LAMBDA"


def test_caller_falls_back_when_the_lambda_rejects(evidence):
    stub = StubLambda({"text": "x 7431", "model": "m", "ungroundedFigures": ["7431"], "accepted": False})
    explainer = LambdaBedrockExplainer("fn", "ap-south-1", client=stub)
    text = explainer.explain(evidence)
    assert explainer.last is not None and explainer.last.source == "deterministic-fallback"
    assert "7431" in text and "rejected" in text


def test_caller_raises_when_the_function_fails(evidence):
    explainer = LambdaBedrockExplainer("fn", "ap-south-1", client=StubLambda({"errorMessage": "boom"}, "Unhandled"))
    with pytest.raises(RuntimeError):
        explainer.explain(evidence)


def test_a_function_name_is_required():
    with pytest.raises(ValueError):
        LambdaBedrockExplainer("", "ap-south-1", client=StubLambda({}))

"""Live prediction for the demo page, from the same engine the tests run.

Given a proposed reserved-concurrency value it edits the committed plan fixture,
parses it as untrusted input, and predicts against the local dependency graph. It
predicts only. The measured verdict exists solely for the recorded 10 -> 100 run, so
nothing here pretends to measure another value.

Local only: no AWS SDK, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from changeproof.adapters.local import FIXTURES, load_graph
from changeproof.graph import GraphError
from changeproof.parser import PlanParseError, parse_plan
from changeproof.predict import UnsupportedChangeError, predict

ADDRESS = "aws_lambda_function.api"
ATTRIBUTE = "reserved_concurrent_executions"
MAX_CONCURRENCY = 1000
RECORDED_AFTER = 100


class DemoInputError(ValueError):
    """The requested change is invalid or is not one the engine models."""


def predict_for(after: Any) -> dict[str, Any]:
    if isinstance(after, bool) or not isinstance(after, int) or not 1 <= after <= MAX_CONCURRENCY:
        raise DemoInputError(f"concurrency must be a whole number from 1 to {MAX_CONCURRENCY}")

    plan = json.loads(Path(FIXTURES / "terraform_plan.json").read_text(encoding="utf-8"))
    before = None
    for change in plan["resource_changes"]:
        if change["address"] == ADDRESS:
            before = change["change"]["before"][ATTRIBUTE]
            change["change"]["after"][ATTRIBUTE] = after
    assert before is not None

    try:
        prediction = predict(parse_plan(plan), load_graph())
    except (PlanParseError, UnsupportedChangeError, GraphError) as error:
        raise DemoInputError(str(error)) from error

    return {
        "address": ADDRESS,
        "attribute": ATTRIBUTE,
        "before": before,
        "after": after,
        "recordedRun": after == RECORDED_AFTER,
        "prediction": prediction.to_dict(),
    }

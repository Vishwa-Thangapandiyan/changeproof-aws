"""Explain evidence by invoking the deployed Bedrock Lambda. Satisfies `ports.Explainer`.

Use this when Bedrock lives in a different AWS account from the one running the
engine: the caller needs only `lambda:InvokeFunction` on one function, and never holds
Bedrock permission itself.
"""

from __future__ import annotations

import json
from typing import Any

from changeproof.models import Evidence

from .bedrock_explainer import Explanation, judge


class LambdaBedrockExplainer:
    def __init__(self, function_name: str, region: str, client: Any | None = None) -> None:
        if not function_name:
            raise ValueError("a function name is required (set CHANGEPROOF_EXPLAIN_FUNCTION)")
        if client is None:
            import boto3

            client = boto3.client("lambda", region_name=region)
        self._client = client
        self._function = function_name
        self.last: Explanation | None = None

    def explain(self, evidence: Evidence) -> str:
        response = self._client.invoke(
            FunctionName=self._function,
            Payload=json.dumps({"evidence": evidence.to_dict()}).encode(),
        )
        body = json.loads(response["Payload"].read())
        if response.get("FunctionError") or "error" in body:
            raise RuntimeError(f"explain function failed: {body.get('error') or body}")
        model = body.get("model", "unknown")
        bad = tuple(body.get("ungroundedFigures", ()))
        final, self.last = judge(evidence, model, body.get("text", ""), bad)
        return final

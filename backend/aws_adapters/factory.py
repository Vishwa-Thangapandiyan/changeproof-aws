"""Chooses the local or AWS-backed adapter for each seam.

Local is always the default, so nothing reaches AWS unless a caller asks for it by
name. An AWS choice that is missing its configuration fails loudly.
"""

from __future__ import annotations

import os

from changeproof.adapters.local import DeterministicExplainer, load_graph
from changeproof.ports import Explainer, GraphSource


def build_explainer(kind: str = "local") -> Explainer:
    if kind == "local":
        return DeterministicExplainer()
    if kind == "bedrock":
        from .bedrock_explainer import BedrockExplainer

        return BedrockExplainer(
            region=os.environ.get("AWS_REGION", "us-east-1"),
            model_id=os.environ.get("CHANGEPROOF_BEDROCK_MODEL", ""),
        )
    if kind == "bedrock-lambda":
        from .lambda_explainer import LambdaBedrockExplainer

        return LambdaBedrockExplainer(
            function_name=os.environ.get("CHANGEPROOF_EXPLAIN_FUNCTION", ""),
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )
    raise ValueError(f"unknown explainer {kind!r}; expected 'local', 'bedrock' or 'bedrock-lambda'")


def build_graph(kind: str = "local") -> GraphSource:
    if kind == "local":
        return load_graph()
    if kind == "neptune":
        from .neptune_graph import NeptuneGraphSource, signed_runner

        endpoint = os.environ.get("CHANGEPROOF_NEPTUNE_ENDPOINT")
        if not endpoint:
            raise ValueError("set CHANGEPROOF_NEPTUNE_ENDPOINT to the cluster endpoint host")
        return NeptuneGraphSource(signed_runner(endpoint, os.environ.get("AWS_REGION", "us-east-1")))
    raise ValueError(f"unknown graph {kind!r}; expected 'local' or 'neptune'")

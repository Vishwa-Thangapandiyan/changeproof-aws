"""Lambda entry point: explain an evidence bundle with Amazon Bedrock.

Deploy this file together with `bedrock_core.py`. The function's role needs
`bedrock:InvokeModel` and the usual Lambda logging permission, nothing else.

    event    {"evidence": {...evidence bundle as JSON...}}
    returns  {"text": str, "model": str, "ungroundedFigures": [str], "accepted": bool}

The function never decides a verdict and never edits the model's answer. `accepted` is
False when the reply contains a figure absent from the evidence; the caller then uses
its deterministic explanation.
"""

from __future__ import annotations

import os
from typing import Any

try:  # in the repo
    from .bedrock_core import ask_bedrock, ungrounded_figures
except ImportError:  # zipped at the top level of a Lambda package
    from bedrock_core import ask_bedrock, ungrounded_figures  # type: ignore[no-redef]


def handler(event: dict[str, Any], context: Any = None, client: Any = None) -> dict[str, Any]:
    facts = event.get("evidence")
    if not isinstance(facts, dict) or not facts:
        return {"error": "event must contain a non-empty 'evidence' object", "accepted": False}

    model_id = os.environ.get("CHANGEPROOF_BEDROCK_MODEL", "")
    if not model_id:
        return {"error": "CHANGEPROOF_BEDROCK_MODEL is not set", "accepted": False}

    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime")

    text = ask_bedrock(client, model_id, facts)
    bad = ungrounded_figures(text, facts) if text else ("<empty response>",)
    return {"text": text, "model": model_id, "ungroundedFigures": list(bad), "accepted": not bad}

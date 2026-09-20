"""The model-facing half of the Bedrock explainer, with no dependency on the engine.

Kept separate so it can be zipped into a Lambda on its own: it needs only the standard
library, and boto3 (already present in the Lambda runtime).

The model receives the evidence bundle as its only factual input. Its answer is judged
by `ungrounded_figures`, which lists every number in the reply that the evidence does
not contain. The caller decides what to do with a rejected answer; this module never
edits or repairs one.
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = """You explain the result of an infrastructure change experiment to an engineer.

Rules, all mandatory:
- Use ONLY the facts in the evidence JSON you are given. Never add a number, metric, resource or dependency that is not in it.
- Never change or second-guess the verdict. State it exactly as recorded.
- If telemetry is marked simulated, say so plainly in your first paragraph.
- Explain: what changed, what was predicted, what was measured, which safety limits were breached and why that decided the verdict, and how accurate the prediction was.
- Plain prose, no markdown, no bullet symbols, under 220 words."""

_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_IDENTIFIER = re.compile(r"[A-Za-z][\w\-.]*\d[\w\-.]*")


def allowed_figures(evidence_dict: Any) -> set[float]:
    """Every number the evidence contains, plus the roundings a sentence would use."""
    found: set[float] = {0.0, 1.0}

    def add(value: float) -> None:
        for candidate in (value, abs(value), round(value), round(value, 1), round(value, 2)):
            found.add(float(candidate))
        found.add(round(abs(value) * 100))  # a 0.25 accuracy is read out as 25%
        found.add(round(abs(value) * 100, 1))

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add(float(node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            found.add(float(len(node)))
            for value in node:
                walk(value)

    walk(evidence_dict)
    return found


def ungrounded_figures(text: str, evidence_dict: Any) -> tuple[str, ...]:
    allowed = allowed_figures(evidence_dict)
    prose = _IDENTIFIER.sub(" ", text)
    bad: list[str] = []
    for match in _NUMBER.findall(prose):
        value = float(match.replace(",", ""))
        if value not in allowed and abs(value) not in allowed:
            bad.append(match)
    return tuple(dict.fromkeys(bad))


def ask_bedrock(client: Any, model_id: str, facts: dict[str, Any]) -> str:
    """One Converse call at temperature 0. Returns the model's text, unchecked."""
    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[
            {"role": "user", "content": [{"text": "Evidence bundle:\n" + json.dumps(facts, indent=1)}]}
        ],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(block.get("text", "") for block in blocks).strip()

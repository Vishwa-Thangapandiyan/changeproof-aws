"""Amazon Bedrock behind the `ports.Explainer` seam.

The model is an explanation layer and nothing else. It receives the stored evidence
bundle as its only factual input, and its answer is accepted only if every figure in
it can be found in that bundle. An answer that introduces a number the deterministic
engine did not produce is discarded and the deterministic explanation is returned
instead, with the rejection recorded in the provenance. The verdict is never read
from, or written by, this module.

Lives outside `backend/lambda/` on purpose: that tree is guaranteed SDK-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from changeproof.adapters.local import DeterministicExplainer
from changeproof.models import Evidence

from .bedrock_core import (  # noqa: F401  (re-exported for callers and tests)
    SYSTEM_PROMPT,
    allowed_figures,
    ask_bedrock,
    ungrounded_figures,
)


@dataclass(frozen=True)
class Explanation:
    text: str
    source: str  # "bedrock" or "deterministic-fallback"
    model_id: str
    rejected_figures: tuple[str, ...] = ()


def judge(evidence: Evidence, model_id: str, text: str, bad: tuple[str, ...]) -> tuple[str, Explanation]:
    """Turn a model answer into final prose. Shared by the direct and Lambda explainers."""
    deterministic = DeterministicExplainer().explain(evidence)
    if bad or not text:
        bad = bad or ("<empty response>",)
        note = (
            f"\n  Note: Bedrock output was rejected because it contained figures absent from "
            f"the evidence ({', '.join(bad)}); the deterministic explanation is shown instead."
        )
        return deterministic + note, Explanation(deterministic, "deterministic-fallback", model_id, bad)

    footer = [
        "",
        "PROVENANCE",
        f"  Explanation written by Amazon Bedrock ({model_id}) from the stored "
        "evidence bundle only. Every figure was checked against that bundle.",
        "  The verdict was produced by the deterministic engine, not by the model.",
    ]
    if evidence.observation.simulated:
        footer.append("  WARNING: telemetry is SIMULATED fixture data, not measured from AWS.")
    return text + "\n" + "\n".join(footer), Explanation(text, "bedrock", model_id)


class BedrockExplainer:
    """Satisfies `ports.Explainer`. Needs `bedrock:InvokeModel` and nothing else."""

    def __init__(self, region: str, model_id: str, client: Any | None = None) -> None:
        if not model_id:
            raise ValueError("a Bedrock model id is required (set CHANGEPROOF_BEDROCK_MODEL)")
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=region)
        self._client = client
        self._model_id = model_id
        self.last: Explanation | None = None

    def explain(self, evidence: Evidence) -> str:
        facts = evidence.to_dict()
        text = ask_bedrock(self._client, self._model_id, facts)
        bad = ungrounded_figures(text, facts) if text else ()
        final, self.last = judge(evidence, self._model_id, text, bad)
        return final

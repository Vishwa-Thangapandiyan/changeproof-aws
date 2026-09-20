"""Run the pipeline with a chosen graph and explainer backend.

    python scripts/run_live.py                                  # all local, zero AWS
    python scripts/run_live.py --explainer bedrock              # Bedrock explains
    python scripts/run_live.py --graph neptune --explainer bedrock

Telemetry is still the labelled fixture until a real experiment has been run, so the
evidence stays marked simulated whichever backends are chosen. The verdict comes from
the deterministic engine in every combination.

    --load-neptune   write the dependency fixture into Neptune, then exit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend" / "lambda"), str(ROOT / "backend")]

from aws_adapters.factory import build_explainer, build_graph  # noqa: E402
from changeproof.adapters.local import PHASE, FixtureTelemetrySource, LocalEvidenceStore  # noqa: E402
from changeproof.compare import compare  # noqa: E402
from changeproof.models import Evidence  # noqa: E402
from changeproof.parser import parse_plan_file  # noqa: E402
from changeproof.pipeline import PHASE_0_NOTES  # noqa: E402
from changeproof.predict import predict  # noqa: E402

FIXTURES = ROOT / "backend/lambda/changeproof/fixtures"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", choices=["local", "neptune"], default="local")
    ap.add_argument("--explainer", choices=["local", "bedrock", "bedrock-lambda"], default="local")
    ap.add_argument("--experiment", default="EXP-DEMO-001")
    ap.add_argument("--load-neptune", action="store_true")
    args = ap.parse_args()

    if args.load_neptune:
        import os
        from aws_adapters.neptune_graph import load_graph, signed_runner
        run = signed_runner(os.environ["CHANGEPROOF_NEPTUNE_ENDPOINT"], os.environ.get("AWS_REGION", "us-east-1"))
        doc = json.loads((FIXTURES / "dependency_graph.json").read_text(encoding="utf-8"))
        print(f"loaded {load_graph(run, doc)} statements into Neptune")
        return 0

    graph = build_graph(args.graph)
    explainer = build_explainer(args.explainer)

    change_set = parse_plan_file(FIXTURES / "terraform_plan.json")
    prediction = predict(change_set, graph)
    observation = FixtureTelemetrySource().collect(args.experiment)
    comparison = compare(prediction, observation)

    notes = PHASE_0_NOTES + (
        f"Dependency graph served by: {'Amazon Neptune' if args.graph == 'neptune' else 'local fixture'}.",
    )
    evidence = Evidence(args.experiment, PHASE, change_set, prediction, observation, comparison, notes)
    location = LocalEvidenceStore(ROOT / ".changeproof").store(evidence)
    try:
        explanation = explainer.explain(evidence)
    except Exception as error:
        if args.explainer == "local":
            raise
        # Fail loudly, and do not substitute another explainer behind the caller's back.
        print(f"\n{args.explainer} explainer unavailable: {error}", file=sys.stderr)
        print("Nothing was written. Re-run later, or use --explainer local.", file=sys.stderr)
        return 2

    out = Path(location).parent
    (out / f"explanation.{args.explainer}.txt").write_text(explanation, encoding="utf-8")
    (out / "backends.json").write_text(json.dumps(
        {"graph": args.graph, "explainer": args.explainer,
         "model": getattr(getattr(explainer, "last", None), "model_id", None),
         "explainerSource": getattr(getattr(explainer, "last", None), "source", "deterministic")},
        indent=1), encoding="utf-8")

    print(explanation)
    print(f"\nverdict: {comparison.verdict.value}   graph: {args.graph}   explainer: {args.explainer}")
    return 0 if comparison.verdict.value == "APPROVE" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run the Phase 0 pipeline end to end and print the result.

    python scripts/demo.py

Zero AWS credentials, zero AWS calls, zero cost. Telemetry comes from a fixture and
the output says so.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend" / "lambda"))

from changeproof.pipeline import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        default=REPO_ROOT / "backend" / "lambda" / "changeproof" / "fixtures" / "terraform_plan.json",
        help="Terraform plan JSON to verify",
    )
    parser.add_argument("--experiment-id", default="EXP-DEMO-001")
    parser.add_argument("--output", default=REPO_ROOT / ".changeproof")
    args = parser.parse_args()

    result = run(args.plan, experiment_id=args.experiment_id, output_root=args.output)

    print(result.explanation)
    print()
    print(f"Evidence written to: {result.evidence_path}")

    return 0 if result.evidence.comparison.verdict.value == "APPROVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

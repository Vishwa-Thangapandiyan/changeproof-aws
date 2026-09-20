"""Command line entry point for the experiment driver.

    python -m workload.driver.cli plan                      # offline, describes the run
    python -m workload.driver.cli run EXP-001 --authorize-aws-spend
    python -m workload.driver.cli cleanup EXP-001 --authorize-aws-spend

`plan` makes no AWS call and needs no credentials. `run` and `cleanup` refuse to
start without `--authorize-aws-spend`, because they create real, billable resources.
CLAUDE.md section 7 requires that authorisation to be explicit and per-invocation;
an environment variable that someone exported once and forgot is not explicit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .experiment import CHANGE_ADDRESS, CHANGE_ATTRIBUTE, ExperimentDriver
from .models import WorkloadSpec, comparability, iso, utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec(args: argparse.Namespace) -> WorkloadSpec:
    return WorkloadSpec(
        attempts=args.attempts,
        duration_seconds=args.duration,
        payload_bytes=args.payload_bytes,
        warmup_attempts=args.warmup,
        client_concurrency=args.client_concurrency,
    )


def _driver(args: argparse.Namespace) -> ExperimentDriver:
    return ExperimentDriver(
        experiment_id=args.experiment_id,
        region=args.region,
        spec=_spec(args),
        baseline_concurrency=args.baseline_concurrency,
        changed_concurrency=args.changed_concurrency,
        worker_concurrency=args.worker_concurrency,
        order=args.order,
        repo_root=REPO_ROOT,
        settle_seconds=args.settle,
    )


def command_plan(args: argparse.Namespace) -> int:
    """Describe the experiment without performing it.

    Deliberately emits no metrics, no resource names and no timestamps. Those are
    measurements, and inventing them here would be indistinguishable from a real
    result in the evidence bundle.
    """
    spec = _spec(args)
    driver = _driver(args)
    changed_first = args.order == "changed-first"
    first_label, second_label = ("run_changed", "run_baseline") if changed_first else ("run_baseline", "run_changed")
    second_concurrency = args.baseline_concurrency if changed_first else args.changed_concurrency

    document = {
        "experimentId": args.experiment_id,
        "region": args.region,
        "change": {
            "address": CHANGE_ADDRESS,
            "attribute": CHANGE_ATTRIBUTE,
            "before": args.baseline_concurrency,
            "after": args.changed_concurrency,
        },
        "workload": spec.to_dict(),
        "workloadFingerprint": spec.fingerprint(),
        "plannedActions": [
            f"create_experiment: terraform apply at reserved_concurrency={driver.first_concurrency}",
            f"{first_label}: warm up, drive {spec.attempts} attempts over {spec.duration_seconds}s, drain",
            f"{second_label}: apply {CHANGE_ATTRIBUTE}={second_concurrency}, repeat the identical workload",
            "cleanup_experiment: terraform destroy and verify the state is empty",
        ],
        "order": args.order,
        "estimatedWallClockSeconds": 2 * (spec.duration_seconds + args.settle * 2) + 240,
        "note": "No AWS call was made. Run with --authorize-aws-spend to execute.",
    }
    print(json.dumps(document, indent=2))
    return 0


def command_run(args: argparse.Namespace) -> int:
    driver = _driver(args)
    manifest: dict[str, Any] = {
        "experimentId": args.experiment_id,
        "source": "aws-experiment",
        "simulated": False,
        "startedAt": iso(utc_now()),
    }
    exit_code = 0

    try:
        environment = driver.create_experiment()
        manifest["environment"] = environment.to_dict()

        baseline, changed = driver.run_both()
        manifest["baseline"] = baseline.to_dict()
        manifest["changed"] = changed.to_dict()
        manifest["comparability"] = comparability(baseline, changed)
    except Exception as error:
        manifest["error"] = f"{type(error).__name__}: {error}"
        exit_code = 1
    finally:
        # Always, including on failure. An experiment that raised halfway is exactly
        # the case where resources get left running and billing.
        if not args.keep:
            manifest["teardown"] = driver.cleanup_experiment().to_dict()
        else:
            manifest["teardown"] = {"skipped": True, "note": "--keep was passed; resources are still billing"}

    destination = _write(args.experiment_id, manifest)
    print(json.dumps(manifest, indent=2))
    print(f"\nmanifest written to {destination}", file=sys.stderr)
    return exit_code


def command_cleanup(args: argparse.Namespace) -> int:
    """Destroy an experiment's resources using its own state file.

    Works after a crashed run: the Terraform working directory and state are on disk
    under .changeproof/experiments/<id>/, and that is all teardown needs.
    """
    from .terraform import Terraform

    working = REPO_ROOT / ".changeproof" / "experiments" / args.experiment_id / "terraform"
    if not working.is_dir():
        print(json.dumps({"experimentId": args.experiment_id, "note": "no working directory; nothing to destroy"}))
        return 0

    driver = _driver(args)
    driver._terraform = Terraform(working_dir=working, state_path=working / "terraform.tfstate")
    report = driver.cleanup_experiment()
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.state_empty else 1


def _write(experiment_id: str, manifest: dict[str, Any]) -> Path:
    destination = REPO_ROOT / ".changeproof" / "experiments" / experiment_id / "experiment-manifest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workload.driver.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser, *, needs_id: bool = True) -> None:
        if needs_id:
            target.add_argument("experiment_id")
        target.add_argument("--region", default="us-east-1")
        target.add_argument("--attempts", type=int, default=3000)
        target.add_argument("--duration", type=int, default=60, help="seconds to spread the attempts over")
        target.add_argument("--payload-bytes", type=int, default=256)
        target.add_argument("--warmup", type=int, default=50)
        target.add_argument("--client-concurrency", type=int, default=64)
        target.add_argument("--baseline-concurrency", type=int, default=10)
        target.add_argument("--changed-concurrency", type=int, default=100)
        target.add_argument("--worker-concurrency", type=int, default=5)
        target.add_argument("--settle", type=int, default=60, help="idle seconds between runs")
        target.add_argument(
            "--order",
            choices=["baseline-first", "changed-first"],
            default="baseline-first",
            help="which configuration is measured first. Re-running an experiment changed-first "
            "quantifies drift over the life of the experiment instead of assuming it away.",
        )

    plan = sub.add_parser("plan", help="describe the experiment; makes no AWS call")
    plan.add_argument("experiment_id", nargs="?", default="EXP-PLAN")
    common(plan, needs_id=False)
    plan.set_defaults(handler=command_plan, gated=False)

    run = sub.add_parser("run", help="provision, measure both configurations, destroy")
    common(run)
    run.add_argument("--keep", action="store_true", help="leave resources running after the run (they keep billing)")
    run.set_defaults(handler=command_run, gated=True)

    cleanup = sub.add_parser("cleanup", help="destroy an experiment's resources")
    common(cleanup)
    cleanup.set_defaults(handler=command_cleanup, gated=True)

    parser.add_argument(
        "--authorize-aws-spend",
        action="store_true",
        help="required for any command that creates or destroys real AWS resources",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gated and not args.authorize_aws_spend:
        parser.error(
            f"'{args.command}' creates or destroys real, billable AWS resources. "
            "Re-run with --authorize-aws-spend if that is what you intend."
        )

    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

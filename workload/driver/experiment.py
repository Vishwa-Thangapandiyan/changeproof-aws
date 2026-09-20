"""The experiment lifecycle: create, run baseline, run changed, clean up.

The whole design exists to protect one property: **the two runs must differ in
exactly one thing.** Everything else here — the single-attribute plan guardrail, the
drain gate, the settle period, the shared workload schedule, the fingerprint — is in
service of that.

Sequence:

    create_experiment()      terraform apply at the baseline configuration
    run_baseline()           warm up, drive the workload, wait for the queue to drain
                             (settle: let metrics fall back to idle)
    run_changed()            apply ONLY the concurrency change, then the same workload
    cleanup_experiment()     terraform destroy, then verify the state is empty

Both runs use the same environment, the same queue and the same table. Tearing down
and rebuilding between runs would introduce cold infrastructure as a second variable.

`order` controls which configuration is measured first. Baseline first is the natural
reading, but it confounds the change with anything that drifts over the life of the
experiment: account-level warmth, a noisy neighbour, time of day. Running the same
experiment a second time with `order="changed-first"` and comparing the two verdicts
is how that drift is quantified rather than assumed away.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .models import (
    ExperimentEnvironment,
    ResourceIdentity,
    RunRecord,
    TeardownReport,
    WorkloadResult,
    WorkloadSpec,
    utc_now,
)
from .terraform import Terraform, assert_single_attribute_change
from .workload import execute

#: The resource whose attribute the demo change moves, and the attribute itself.
CHANGE_ADDRESS = "aws_lambda_function.api"
CHANGE_ATTRIBUTE = "reserved_concurrent_executions"

#: CloudWatch publishes on whole-minute boundaries. A window that starts mid-minute
#: silently includes activity from before the run in its first datapoint, so both
#: edges are snapped outward and a tail is added for metrics that lag the workload
#: (queue depth and message age both peak after the producer has stopped).
WINDOW_TAIL_SECONDS = 60

DRAIN_POLL_SECONDS = 5
DRAIN_TIMEOUT_SECONDS = 300
SETTLE_SECONDS = 60

#: Messages sent immediately after provisioning, purely to wake the queue up.
#:
#: SQS publishes metrics only for queues CloudWatch considers *active*, and the docs
#: warn of "a delay of up to 15 minutes ... when a queue is activated from an inactive
#: state". A queue created seconds before the baseline run is exactly that case, so
#: the first run is the one at risk of returning no datapoints at all. Activating at
#: provisioning time starts that clock as early as possible instead of at the moment
#: measurement begins.
ACTIVATION_ATTEMPTS = 5
ACTIVATION_WAIT_SECONDS = 120


@dataclass
class ExperimentDriver:
    """Owns one experiment from provisioning to teardown."""

    experiment_id: str
    region: str = "us-east-1"
    spec: WorkloadSpec = field(default_factory=WorkloadSpec)
    baseline_concurrency: int = 10
    changed_concurrency: int = 100
    worker_concurrency: int = 5
    order: str = "baseline-first"
    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    settle_seconds: int = SETTLE_SECONDS
    activation_wait_seconds: int = ACTIVATION_WAIT_SECONDS
    drain_timeout_seconds: int = DRAIN_TIMEOUT_SECONDS
    sleep: Callable[[float], None] = time.sleep

    environment: ExperimentEnvironment | None = None
    _terraform: Terraform | None = None

    # --- lifecycle -------------------------------------------------------------------

    def create_experiment(self) -> ExperimentEnvironment:
        """Provision the stack at whichever configuration is measured first."""
        source = self.repo_root / "terraform"
        working = self.repo_root / ".changeproof" / "experiments" / self.experiment_id / "terraform"

        terraform = Terraform.prepare(source, working)
        terraform.init()
        terraform.apply(self._variables(self.first_concurrency))

        outputs = terraform.outputs()
        self._terraform = terraform
        self.environment = ExperimentEnvironment(
            experiment_id=self.experiment_id,
            region=self.region,
            environment_suffix=self._suffix(),
            terraform_dir=str(working),
            state_path=str(terraform.state_path),
            resources=_identities(outputs),
            dlq_url=outputs.get("work_dlq_url"),
            configuration={
                "reserved_concurrency": self.first_concurrency,
                "worker_concurrency": self.worker_concurrency,
            },
        )
        self._activate_queue()
        return self.environment

    def _activate_queue(self) -> None:
        """Wake the queue so CloudWatch starts publishing before the first run.

        Cheap insurance: a handful of messages, then an idle wait. Without it the
        baseline run can legitimately return an empty metric series, which is
        indistinguishable from a baseline of zero unless someone knows to look.
        """
        environment = self.environment
        if environment is None:
            return

        invoke = self._invoker(environment.resource(CHANGE_ADDRESS).physical_name)
        for index in range(ACTIVATION_ATTEMPTS):
            invoke({"runId": f"{self.experiment_id}-activation", "seq": index, "payloadBytes": 64})

        self._drain(environment)
        self.sleep(self.activation_wait_seconds)

    @property
    def first_concurrency(self) -> int:
        """The configuration the stack is provisioned at, and measured at first."""
        return self.changed_concurrency if self.order == "changed-first" else self.baseline_concurrency

    def run_baseline(self) -> RunRecord:
        """Measure the stack at the current configuration.

        Applies first only when the baseline is the *second* run, which is the case
        under `order="changed-first"`.
        """
        return self._run(
            "baseline",
            self.baseline_concurrency,
            apply_change=self.order == "changed-first",
        )

    def run_changed(self) -> RunRecord:
        """Measure the stack at the proposed configuration."""
        return self._run(
            "changed",
            self.changed_concurrency,
            apply_change=self.order != "changed-first",
        )

    def run_both(self) -> tuple[RunRecord, RunRecord]:
        """Both runs, executed in the configured order, returned by role.

        The tuple is always (baseline, changed) whatever the execution order, so a
        caller comparing them never has to care which ran first. `started_at` on each
        record, and `comparability(...)["order"]`, carry that.
        """
        if self.order == "changed-first":
            changed = self.run_changed()
            baseline = self.run_baseline()
        else:
            baseline = self.run_baseline()
            changed = self.run_changed()
        return baseline, changed

    def cleanup_experiment(self) -> TeardownReport:
        """Destroy everything, and prove it.

        Safe to call at any point, including when provisioning failed halfway. An
        experiment that cannot be cleaned up is a bill, so this reports what is left
        rather than assuming success.
        """
        terraform = self._terraform
        if terraform is None:
            return TeardownReport(
                destroyed_at=utc_now(),
                resources_destroyed=0,
                state_empty=True,
                notes=("no terraform working directory for this experiment; nothing was provisioned",),
            )

        before = len(terraform.state_resources())
        notes: list[str] = []
        try:
            terraform.destroy(self._variables(self.changed_concurrency))
        except Exception as error:
            notes.append(f"destroy failed: {error}")

        remaining = terraform.state_resources()
        if remaining:
            notes.append(
                "RESOURCES STILL EXIST and are still billable: "
                + ", ".join(remaining)
                + f". Re-run cleanup, or destroy by hand with -state={terraform.state_path}"
            )

        return TeardownReport(
            destroyed_at=utc_now(),
            resources_destroyed=before - len(remaining),
            state_empty=not remaining,
            notes=tuple(notes),
        )

    # --- one run ---------------------------------------------------------------------

    def _run(self, label: str, concurrency: int, apply_change: bool) -> RunRecord:
        environment, terraform = self._require_environment()
        started_at = utc_now()
        notes: list[str] = []

        if apply_change:
            variables = self._variables(concurrency)
            plan = terraform.plan_json(variables)
            assert_single_attribute_change(plan, CHANGE_ADDRESS, CHANGE_ATTRIBUTE)
            terraform.apply(variables)
            before = _before_value(plan, CHANGE_ADDRESS, CHANGE_ATTRIBUTE)
            notes.append(
                f"applied {CHANGE_ADDRESS}.{CHANGE_ATTRIBUTE}: "
                f"{before} -> {concurrency} (single-attribute update, verified against the plan)"
            )
            # A concurrency change takes effect immediately, but the settle period
            # keeps the apply itself out of the measurement window.
            self.sleep(self.settle_seconds)

        run_id = f"{self.experiment_id}-{label}"
        invoker = self._invoker(environment.resource(CHANGE_ADDRESS).physical_name)

        wall_start = utc_now()
        monotonic_start = time.monotonic()
        result, measured_from, measured_to = execute(self.spec, run_id, invoker)

        attempts_started_at = wall_start + timedelta(seconds=measured_from - monotonic_start)
        attempts_ended_at = wall_start + timedelta(seconds=measured_to - monotonic_start)

        drained, drained_at = self._drain(environment)
        dlq_depth = self._dead_letters(environment)
        if dlq_depth:
            notes.append(
                f"{dlq_depth} message(s) in the dead letter queue: they failed processing three times "
                "and never reached DynamoDB. Downstream metrics understate the work that was offered."
            )
        if not drained:
            notes.append(
                f"queue did not reach zero within {self.drain_timeout_seconds}s; "
                "the window closed with work still in flight and the runs may not be comparable"
            )
        if result.failed:
            notes.append(f"{result.failed} invocation(s) failed outright, distinct from {result.throttled} throttled")

        record = RunRecord(
            run_id=run_id,
            label=label,
            configuration={
                "reserved_concurrency": concurrency,
                "worker_concurrency": self.worker_concurrency,
            },
            spec=self.spec,
            result=result,
            started_at=started_at,
            attempts_started_at=attempts_started_at,
            attempts_ended_at=attempts_ended_at,
            drained_at=drained_at,
            window_start=_floor_minute(attempts_started_at),
            window_end=_ceil_minute(drained_at + timedelta(seconds=WINDOW_TAIL_SECONDS)),
            drained=drained,
            dlq_depth=dlq_depth,
            notes=tuple(notes),
        )

        # Leave the environment idle and empty for whatever runs next.
        self.sleep(self.settle_seconds)
        return record

    def _dead_letters(self, environment: ExperimentEnvironment) -> int:
        """How many messages failed processing outright.

        A drained queue is not the same as a successful run: messages that failed
        three times leave the queue for the DLQ, so the drain gate passes while work
        has silently gone missing. A run with dead letters is still reportable, but
        the comparison has to say so.
        """
        probe = self._dlq_probe(environment)
        if probe is None:
            return 0
        visible, in_flight = probe.depth()
        return visible + in_flight

    def _drain(self, environment: ExperimentEnvironment) -> tuple[bool, datetime]:
        """Wait until the queue is empty and nothing is in flight.

        Two consecutive clean polls, not one: `ApproximateNumberOfMessages` is
        eventually consistent and briefly reads zero while messages are still being
        handed to consumers.
        """
        probe = self._probe(environment.resource("aws_sqs_queue.work"))
        if probe is None:
            return True, utc_now()

        deadline = time.monotonic() + self.drain_timeout_seconds
        clean = 0
        while time.monotonic() < deadline:
            visible, in_flight = probe.depth()
            clean = clean + 1 if visible == 0 and in_flight == 0 else 0
            if clean >= 2:
                return True, utc_now()
            self.sleep(DRAIN_POLL_SECONDS)

        return False, utc_now()

    # --- seams, overridden in tests ---------------------------------------------------

    def _invoker(self, function_name: str):
        from .awsio import LambdaInvoker

        return LambdaInvoker(function_name=function_name, region=self.region)

    def _probe(self, queue: ResourceIdentity):
        from .awsio import QueueProbe

        if not queue.queue_url:
            return None
        return QueueProbe(queue_url=queue.queue_url, region=self.region)

    def _dlq_probe(self, environment: ExperimentEnvironment):
        from .awsio import QueueProbe

        if not environment.dlq_url:
            return None
        return QueueProbe(queue_url=environment.dlq_url, region=self.region)

    # --- helpers ---------------------------------------------------------------------

    def _require_environment(self) -> tuple[ExperimentEnvironment, Terraform]:
        if self.environment is None or self._terraform is None:
            raise RuntimeError("create_experiment() must run before a workload can be driven")
        return self.environment, self._terraform

    def _suffix(self) -> str:
        return self.experiment_id.lower().replace("_", "-")

    def _variables(self, concurrency: int) -> dict[str, Any]:
        return {
            "offline": "false",
            "region": self.region,
            "environment_suffix": self._suffix(),
            "reserved_concurrency": concurrency,
            "worker_concurrency": self.worker_concurrency,
        }


def _identities(outputs: dict[str, Any]) -> tuple[ResourceIdentity, ...]:
    """Map Terraform outputs onto the graph addresses the pipeline speaks in."""
    return (
        ResourceIdentity(
            graph_address="aws_lambda_function.api",
            resource_type="aws_lambda_function",
            physical_name=outputs["api_function_name"],
            arn=outputs.get("api_function_arn"),
            log_group=outputs.get("api_log_group"),
        ),
        ResourceIdentity(
            graph_address="aws_sqs_queue.work",
            resource_type="aws_sqs_queue",
            physical_name=outputs["work_queue_name"],
            arn=outputs.get("work_queue_arn"),
            queue_url=outputs.get("work_queue_url"),
        ),
        ResourceIdentity(
            graph_address="aws_lambda_function.worker",
            resource_type="aws_lambda_function",
            physical_name=outputs["worker_function_name"],
            arn=outputs.get("worker_function_arn"),
            log_group=outputs.get("worker_log_group"),
        ),
        ResourceIdentity(
            graph_address="aws_dynamodb_table.orders",
            resource_type="aws_dynamodb_table",
            physical_name=outputs["orders_table_name"],
            arn=outputs.get("orders_table_arn"),
        ),
    )


def _before_value(plan: dict[str, Any], address: str, attribute: str) -> Any:
    for change in plan.get("resource_changes", []):
        if change["address"] == address:
            return (change["change"].get("before") or {}).get(attribute)
    return None


def _floor_minute(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _ceil_minute(moment: datetime) -> datetime:
    floored = _floor_minute(moment)
    return floored if floored == moment else floored + timedelta(minutes=1)

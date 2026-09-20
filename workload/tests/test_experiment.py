"""The lifecycle, driven against a fake stack.

No AWS, no Terraform CLI: the point of these tests is the orchestration logic, which
is where comparability is either preserved or lost.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from workload.driver.experiment import CHANGE_ADDRESS, CHANGE_ATTRIBUTE, ExperimentDriver
from workload.driver.models import (
    ExperimentEnvironment,
    ResourceIdentity,
    WorkloadSpec,
    comparability,
)

OUTPUTS = {
    "api_function_name": "changeproof-exp-1-api",
    "api_function_arn": "arn:aws:lambda:us-east-1:000000000000:function:changeproof-exp-1-api",
    "worker_function_name": "changeproof-exp-1-worker",
    "work_queue_name": "changeproof-exp-1-work",
    "work_queue_url": "https://sqs.us-east-1.amazonaws.com/000000000000/changeproof-exp-1-work",
    "orders_table_name": "changeproof-exp-1-orders",
}


class FakeTerraform:
    """Records what the driver asked Terraform to do."""

    def __init__(self):
        self.applied: list[dict] = []
        self.destroyed = False
        self.resources = ["aws_lambda_function.api", "aws_sqs_queue.work"]
        self.plan_response = {
            "resource_changes": [
                {
                    "address": CHANGE_ADDRESS,
                    "change": {
                        "actions": ["update"],
                        "before": {CHANGE_ATTRIBUTE: 10},
                        "after": {CHANGE_ATTRIBUTE: 100},
                    },
                }
            ]
        }
        self.state_path = "/tmp/fake/terraform.tfstate"

    def plan_json(self, variables):
        return self.plan_response

    def apply(self, variables):
        self.applied.append(dict(variables))

    def destroy(self, variables):
        self.destroyed = True
        self.resources = []

    def state_resources(self):
        return list(self.resources)


class FakeProbe:
    """A queue that drains after a couple of polls."""

    def __init__(self, polls_until_empty: int = 2):
        self.remaining = polls_until_empty

    def depth(self):
        if self.remaining > 0:
            self.remaining -= 1
            return (5, 1)
        return (0, 0)


def driver_with_fakes(**overrides) -> tuple[ExperimentDriver, FakeTerraform, list]:
    terraform = FakeTerraform()
    invocations: list = []

    class Driver(ExperimentDriver):
        def _invoker(self, function_name):
            def invoke(payload):
                invocations.append((function_name, payload))
                return "accepted"

            return invoke

        def _probe(self, queue):
            return FakeProbe()

    driver = Driver(
        experiment_id="EXP-1",
        spec=WorkloadSpec(attempts=6, duration_seconds=0, warmup_attempts=2, client_concurrency=2),
        settle_seconds=0,
        sleep=lambda _: None,
        **overrides,
    )
    driver._terraform = terraform
    driver.environment = ExperimentEnvironment(
        experiment_id="EXP-1",
        region="us-east-1",
        environment_suffix="exp-1",
        terraform_dir="/tmp/fake",
        state_path=terraform.state_path,
        resources=(
            ResourceIdentity(CHANGE_ADDRESS, "aws_lambda_function", OUTPUTS["api_function_name"]),
            ResourceIdentity("aws_sqs_queue.work", "aws_sqs_queue", OUTPUTS["work_queue_name"], queue_url="url"),
        ),
        configuration={"reserved_concurrency": 10, "worker_concurrency": 5},
    )
    return driver, terraform, invocations


def test_baseline_applies_nothing():
    """The baseline must measure the stack exactly as provisioned."""
    driver, terraform, _ = driver_with_fakes()

    record = driver.run_baseline()

    assert terraform.applied == []
    assert record.configuration["reserved_concurrency"] == 10
    assert record.label == "baseline"


def test_changed_applies_the_single_variable_and_verifies_the_plan_first():
    driver, terraform, _ = driver_with_fakes()

    record = driver.run_changed()

    assert len(terraform.applied) == 1
    assert terraform.applied[0]["reserved_concurrency"] == 100
    assert terraform.applied[0]["offline"] == "false"
    assert record.configuration["reserved_concurrency"] == 100
    assert any("single-attribute update" in note for note in record.notes)


def test_a_multi_resource_plan_aborts_the_changed_run():
    """Better no experiment than an experiment with two variables in it."""
    from workload.driver.terraform import TerraformError

    driver, terraform, _ = driver_with_fakes()
    terraform.plan_response["resource_changes"].append(
        {
            "address": "aws_dynamodb_table.orders",
            "change": {"actions": ["update"], "before": {"billing_mode": "PAY_PER_REQUEST"}, "after": {"billing_mode": "PROVISIONED"}},
        }
    )

    with pytest.raises(TerraformError):
        driver.run_changed()

    assert terraform.applied == []


def test_both_runs_drive_the_producer_with_the_same_attempt_count():
    driver, _, invocations = driver_with_fakes()

    baseline = driver.run_baseline()
    changed = driver.run_changed()

    assert baseline.result.attempted == changed.result.attempted == 6
    assert {name for name, _ in invocations} == {OUTPUTS["api_function_name"]}

    report = comparability(baseline, changed)
    assert report["workloadFingerprintMatch"] is True
    assert report["attemptDeltaPct"] == 0.0
    assert report["singleVariable"] is True
    assert report["configurationDeltaKeys"] == ["reserved_concurrency"]
    assert report["bothRunsDrained"] is True


def test_measurement_window_snaps_to_whole_minutes_and_covers_the_drain():
    driver, _, _ = driver_with_fakes()

    record = driver.run_baseline()

    assert record.window_start.second == 0 and record.window_start.microsecond == 0
    assert record.window_end.second == 0 and record.window_end.microsecond == 0
    assert record.window_start <= record.attempts_started_at
    assert record.window_end >= record.drained_at + timedelta(seconds=59)
    assert record.drained is True


def test_window_reports_when_the_queue_never_drained():
    class Stuck(ExperimentDriver):
        def _invoker(self, function_name):
            return lambda payload: "accepted"

        def _probe(self, queue):
            class Never:
                def depth(self):
                    return (100, 3)

            return Never()

    driver, terraform, _ = driver_with_fakes()
    stuck = Stuck(
        experiment_id="EXP-1",
        spec=driver.spec,
        settle_seconds=0,
        sleep=lambda _: None,
        drain_timeout_seconds=0,
    )
    stuck._terraform = terraform
    stuck.environment = driver.environment

    record = stuck.run_baseline()

    assert record.drained is False
    assert any("did not reach zero" in note for note in record.notes)


def test_cleanup_reports_what_is_left_rather_than_assuming_success():
    driver, terraform, _ = driver_with_fakes()

    report = driver.cleanup_experiment()

    assert terraform.destroyed is True
    assert report.state_empty is True
    assert report.resources_destroyed == 2


def test_cleanup_flags_surviving_resources_as_billable():
    driver, terraform, _ = driver_with_fakes()

    def destroy_but_leave_one(variables):
        terraform.destroyed = True
        terraform.resources = ["aws_dynamodb_table.orders"]

    terraform.destroy = destroy_but_leave_one

    report = driver.cleanup_experiment()

    assert report.state_empty is False
    assert any("STILL EXIST" in note for note in report.notes)


def test_running_before_provisioning_is_an_error_not_a_guess():
    driver = ExperimentDriver(experiment_id="EXP-2")

    with pytest.raises(RuntimeError, match="create_experiment"):
        driver.run_baseline()


# --- run order, the control for drift over the life of the experiment ----------------


def test_baseline_first_is_the_default():
    driver, _, _ = driver_with_fakes()

    assert driver.order == "baseline-first"
    assert driver.first_concurrency == 10


def test_changed_first_provisions_at_the_changed_configuration():
    """Whichever configuration is measured first is the one the stack is built at."""
    driver, _, _ = driver_with_fakes(order="changed-first")

    assert driver.first_concurrency == 100


def test_changed_first_moves_the_apply_onto_the_baseline_run():
    driver, terraform, _ = driver_with_fakes(order="changed-first")
    terraform.plan_response["resource_changes"][0]["change"] = {
        "actions": ["update"],
        "before": {CHANGE_ATTRIBUTE: 100},
        "after": {CHANGE_ATTRIBUTE: 10},
    }

    baseline, changed = driver.run_both()

    assert len(terraform.applied) == 1
    assert terraform.applied[0]["reserved_concurrency"] == 10
    assert any("100 -> 10" in note for note in baseline.notes)
    assert changed.notes == ()


def test_run_both_returns_records_by_role_but_reports_execution_order():
    """The caller compares baseline against changed; the manifest says which ran first."""
    driver, terraform, _ = driver_with_fakes(order="changed-first")
    terraform.plan_response["resource_changes"][0]["change"] = {
        "actions": ["update"],
        "before": {CHANGE_ATTRIBUTE: 100},
        "after": {CHANGE_ATTRIBUTE: 10},
    }

    baseline, changed = driver.run_both()

    assert baseline.label == "baseline" and baseline.configuration["reserved_concurrency"] == 10
    assert changed.label == "changed" and changed.configuration["reserved_concurrency"] == 100
    assert changed.started_at <= baseline.started_at
    assert comparability(baseline, changed)["order"] == ["changed", "baseline"]


def test_baseline_first_reports_its_own_execution_order():
    driver, _, _ = driver_with_fakes()

    baseline, changed = driver.run_both()

    assert comparability(baseline, changed)["order"] == ["baseline", "changed"]


# --- a drained queue is not the same as a successful run -----------------------------


def test_dead_letters_are_reported_not_absorbed():
    """Messages that failed three times leave the queue, so the drain gate passes."""

    class WithDeadLetters(ExperimentDriver):
        def _invoker(self, function_name):
            return lambda payload: "accepted"

        def _probe(self, queue):
            return FakeProbe()

        def _dlq_probe(self, environment):
            class Dlq:
                def depth(self):
                    return (7, 0)

            return Dlq()

    driver, terraform, _ = driver_with_fakes()
    poisoned = WithDeadLetters(
        experiment_id="EXP-1",
        spec=driver.spec,
        settle_seconds=0,
        sleep=lambda _: None,
    )
    poisoned._terraform = terraform
    poisoned.environment = driver.environment

    record = poisoned.run_baseline()

    assert record.drained is True
    assert record.dlq_depth == 7
    assert any("dead letter queue" in note for note in record.notes)


def test_offered_and_delivered_rates_are_reported_separately():
    """The gap between them is the effect of the concurrency cap."""

    class HalfThrottled(ExperimentDriver):
        def _invoker(self, function_name):
            state = {"n": 0}

            def invoke(payload):
                state["n"] += 1
                return "accepted" if state["n"] % 2 else "throttled"

            return invoke

        def _probe(self, queue):
            return FakeProbe()

    driver, terraform, _ = driver_with_fakes()
    half = HalfThrottled(
        experiment_id="EXP-1",
        spec=WorkloadSpec(attempts=10, duration_seconds=0, warmup_attempts=0, client_concurrency=1),
        settle_seconds=0,
        sleep=lambda _: None,
    )
    half._terraform = terraform
    half.environment = driver.environment

    record = half.run_baseline()

    assert record.result.attempted == 10
    assert record.result.accepted == 5
    assert record.result.delivered_rate == pytest.approx(record.result.achieved_rate * 0.5)

    document = record.to_dict()["result"]
    assert document["offeredRatePerSecond"] > document["deliveredRatePerSecond"]

"""Structural validation of the Step Functions definition.

Step Functions Local would need Docker, which Phase 0 does not require. These checks
cover the properties that actually matter for correctness and cost: every state is
reachable, every terminal state terminates, and no provisioning failure can escape
without teardown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / "state_machine" / "workflow.json"

TERMINAL_TYPES = {"Succeed", "Fail"}
PROVISIONING_STATES = {"ProvisionTestEnv", "ApplyChange", "RunWorkload", "CollectTelemetry"}


@pytest.fixture(scope="module")
def definition():
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def states(definition):
    return definition["States"]


def _transitions(state: dict) -> set[str]:
    targets = set()
    if "Next" in state:
        targets.add(state["Next"])
    if "Default" in state:
        targets.add(state["Default"])
    for choice in state.get("Choices", []):
        if "Next" in choice:
            targets.add(choice["Next"])
    for catcher in state.get("Catch", []):
        if "Next" in catcher:
            targets.add(catcher["Next"])
    return targets


def test_definition_is_valid_json(definition):
    assert definition["StartAt"] in definition["States"]


def test_every_transition_targets_an_existing_state(states):
    for name, state in states.items():
        for target in _transitions(state):
            assert target in states, f"{name} transitions to unknown state {target}"


def test_every_state_is_reachable(definition, states):
    reachable = {definition["StartAt"]}
    frontier = [definition["StartAt"]]
    while frontier:
        current = frontier.pop()
        for target in _transitions(states[current]):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)

    assert set(states) == reachable, f"unreachable states: {set(states) - reachable}"


def test_non_terminal_states_have_a_transition(states):
    for name, state in states.items():
        if state["Type"] in TERMINAL_TYPES:
            continue
        assert _transitions(state), f"{name} has no outgoing transition"


def test_terminal_states_have_no_transition(states):
    for name, state in states.items():
        if state["Type"] in TERMINAL_TYPES:
            assert not _transitions(state), f"terminal state {name} has a transition"


def test_every_task_names_a_known_stage(states):
    from handler import _STAGES

    for name, state in states.items():
        if state["Type"] != "Task":
            continue
        stage = state["Parameters"]["stage"]
        assert stage in _STAGES, f"{name} invokes unknown stage {stage!r}"


def test_every_task_catches_errors(states):
    for name, state in states.items():
        if state["Type"] != "Task":
            continue
        assert state.get("Catch"), f"{name} has no Catch and could fail the execution silently"


def test_every_task_accumulates_via_result_path(states):
    """Without ResultPath a stage replaces the state object and loses prior evidence."""
    for name, state in states.items():
        if state["Type"] != "Task":
            continue
        assert "ResultPath" in state, f"{name} is missing ResultPath"
        assert state["ResultPath"].startswith("$."), f"{name} has a non-accumulating ResultPath"


def test_no_task_writes_over_the_root_state(states):
    for name, state in states.items():
        if state["Type"] != "Task":
            continue
        assert state["ResultPath"] != "$", f"{name} overwrites the entire state object"


# --- the properties that protect the budget -------------------------------------------


def test_every_provisioning_failure_reaches_teardown(states):
    """A failed experiment must never leave AWS resources running."""
    for name in PROVISIONING_STATES:
        catchers = states[name].get("Catch", [])
        targets = {c["Next"] for c in catchers}
        assert targets == {"TeardownAfterFailure"}, (
            f"{name} catches to {targets}, which does not guarantee teardown"
        )


def test_success_path_also_tears_down(states):
    assert states["CollectTelemetry"]["Next"] == "TeardownTestEnv"


def test_teardown_states_retry(states):
    for name in ("TeardownTestEnv", "TeardownAfterFailure"):
        retries = states[name].get("Retry", [])
        assert retries, f"{name} does not retry; a transient failure would leak resources"
        assert retries[0]["MaxAttempts"] >= 2


def test_teardown_failure_does_not_strand_the_workflow(states):
    """Even if teardown fails, the workflow must reach a terminal state."""
    for name in ("TeardownTestEnv", "TeardownAfterFailure"):
        assert states[name].get("Catch"), f"{name} has no Catch"


def test_long_running_states_have_timeouts(states):
    for name in PROVISIONING_STATES:
        assert "TimeoutSeconds" in states[name], f"{name} could hang indefinitely"


def test_critical_prediction_skips_the_test_environment(states):
    """Spending a test environment on an obviously unsafe change wastes budget."""
    choice = states["IsPredictionCritical"]
    critical = [c for c in choice["Choices"] if c.get("StringEquals") == "CRITICAL"]

    assert len(critical) == 1
    assert critical[0]["Next"] == "RejectWithoutTesting"
    assert choice["Default"] == "ProvisionTestEnv"


# --- the explanation must not be able to change the verdict ---------------------------


def test_evidence_is_stored_before_the_explanation(states):
    assert states["StoreEvidence"]["Next"] == "Explain"


def test_explanation_failure_does_not_fail_the_experiment(states):
    targets = {c["Next"] for c in states["Explain"]["Catch"]}

    assert targets == {"ReportResult"}


def test_the_verdict_is_read_from_the_comparison_stage(states):
    """The reported outcome must come from Compare, never from Explain."""
    for choice in states["ReportResult"]["Choices"]:
        assert choice["Variable"] == "$.compared.comparison.verdict"


def test_rejection_is_a_successful_verification(states):
    """A REJECT is a result, not a workflow failure; evidence must survive it."""
    assert states["Rejected"]["Type"] == "Succeed"
    assert states["Approved"]["Type"] == "Succeed"

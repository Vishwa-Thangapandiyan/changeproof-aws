"""The guardrail that keeps the experiment a single-variable test."""

from __future__ import annotations

import pytest

from workload.driver.terraform import TerraformError, assert_single_attribute_change, effective_changes

ADDRESS = "aws_lambda_function.api"
ATTRIBUTE = "reserved_concurrent_executions"


def plan(*changes):
    return {"resource_changes": list(changes)}


def change(address, actions, before, after):
    return {"address": address, "change": {"actions": list(actions), "before": before, "after": after}}


def test_accepts_the_demo_change():
    good = plan(
        change(ADDRESS, ["update"], {ATTRIBUTE: 10, "memory_size": 512}, {ATTRIBUTE: 100, "memory_size": 512}),
        change("aws_sqs_queue.work", ["no-op"], {}, {}),
    )

    assert_single_attribute_change(good, ADDRESS, ATTRIBUTE)
    assert len(effective_changes(good)) == 1


def test_rejects_a_second_changed_resource():
    """Two moving parts means the two runs differ in more than the change under test."""
    bad = plan(
        change(ADDRESS, ["update"], {ATTRIBUTE: 10}, {ATTRIBUTE: 100}),
        change("aws_lambda_function.worker", ["update"], {"memory_size": 512}, {"memory_size": 1024}),
    )

    with pytest.raises(TerraformError, match="exactly one changed resource"):
        assert_single_attribute_change(bad, ADDRESS, ATTRIBUTE)


def test_rejects_a_replacement():
    """A replaced function is a new function: cold, and not the thing we measured."""
    bad = plan(change(ADDRESS, ["delete", "create"], {ATTRIBUTE: 10}, {ATTRIBUTE: 100}))

    with pytest.raises(TerraformError, match="in-place update"):
        assert_single_attribute_change(bad, ADDRESS, ATTRIBUTE)


def test_rejects_an_extra_attribute_on_the_right_resource():
    bad = plan(change(ADDRESS, ["update"], {ATTRIBUTE: 10, "timeout": 15}, {ATTRIBUTE: 100, "timeout": 30}))

    with pytest.raises(TerraformError, match="timeout"):
        assert_single_attribute_change(bad, ADDRESS, ATTRIBUTE)


def test_rejects_an_empty_plan():
    """Nothing to apply means the changed run would repeat the baseline configuration."""
    with pytest.raises(TerraformError, match="changes nothing"):
        assert_single_attribute_change(plan(), ADDRESS, ATTRIBUTE)

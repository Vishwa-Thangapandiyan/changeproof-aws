from __future__ import annotations

import json

import pytest


def _plan(**overrides):
    document = {
        "format_version": "1.2",
        "terraform_version": "1.9.8",
        "resource_changes": [
            {
                "address": "aws_lambda_function.api",
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": "api",
                "change": {
                    "actions": ["update"],
                    "before": {"reserved_concurrent_executions": 10, "memory_size": 512},
                    "after": {"reserved_concurrent_executions": 100, "memory_size": 512},
                },
            }
        ],
    }
    document.update(overrides)
    return document


def test_parses_the_demo_change(plan_path):
    from changeproof.parser import parse_plan_file

    change_set = parse_plan_file(plan_path)

    assert change_set.terraform_version == "1.9.8"
    effective = change_set.effective_changes
    assert [c.address for c in effective] == ["aws_lambda_function.api"]

    concurrency = effective[0].attribute("reserved_concurrent_executions")
    assert concurrency is not None
    assert concurrency.before == 10
    assert concurrency.after == 100


def test_no_op_changes_are_excluded_from_effective_changes(plan_path):
    from changeproof.parser import parse_plan_file

    change_set = parse_plan_file(plan_path)

    assert any(c.actions == ("no-op",) for c in change_set.changes)
    assert all(c.actions != ("no-op",) for c in change_set.effective_changes)


def test_only_changed_attributes_are_reported():
    from changeproof.parser import parse_plan

    change = parse_plan(_plan()).changes[0]

    assert [a.attribute for a in change.attribute_changes] == [
        "reserved_concurrent_executions"
    ]


def test_noisy_attributes_are_ignored():
    from changeproof.parser import parse_plan

    document = _plan()
    document["resource_changes"][0]["change"]["before"]["source_code_hash"] = "aaa"
    document["resource_changes"][0]["change"]["after"]["source_code_hash"] = "bbb"

    change = parse_plan(document).changes[0]
    attributes = {a.attribute for a in change.attribute_changes}

    assert "source_code_hash" not in attributes


def test_create_action_yields_after_only_attributes():
    from changeproof.parser import parse_plan

    document = _plan()
    document["resource_changes"][0]["change"] = {
        "actions": ["create"],
        "before": None,
        "after": {"reserved_concurrent_executions": 100},
    }

    change = parse_plan(document).changes[0]
    attribute = change.attribute("reserved_concurrent_executions")

    assert attribute is not None
    assert attribute.before is None
    assert attribute.after == 100


@pytest.mark.parametrize(
    "document, expected",
    [
        ("not a dict", "must be a JSON object"),
        ({}, "missing 'format_version'"),
        ({"format_version": "2.0", "terraform_version": "1.9.8"}, "unsupported plan format_version"),
        ({"format_version": "1.2"}, "missing 'terraform_version'"),
        (
            {"format_version": "1.2", "terraform_version": "1.9.8", "resource_changes": {}},
            "must be a list",
        ),
    ],
)
def test_malformed_plans_raise_loudly(document, expected):
    from changeproof.parser import PlanParseError, parse_plan

    with pytest.raises(PlanParseError, match=expected):
        parse_plan(document)


def test_missing_change_object_raises():
    from changeproof.parser import PlanParseError, parse_plan

    document = _plan()
    del document["resource_changes"][0]["change"]

    with pytest.raises(PlanParseError, match="missing its 'change' object"):
        parse_plan(document)


def test_invalid_json_file_raises(tmp_path):
    from changeproof.parser import PlanParseError, parse_plan_file

    broken = tmp_path / "plan.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(PlanParseError, match="not valid JSON"):
        parse_plan_file(broken)


def test_change_set_round_trips_to_json(plan_path):
    from changeproof.parser import parse_plan_file

    change_set = parse_plan_file(plan_path)

    # Step Functions passes state as JSON; every stage output must survive that.
    assert json.loads(json.dumps(change_set.to_dict())) == change_set.to_dict()

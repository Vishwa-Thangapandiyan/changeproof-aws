"""Terraform plan JSON -> ChangeSet.

Input is the output of `terraform show -json <planfile>`, unmodified. The schema is
Terraform's documented plan representation, so this stage works identically on a plan
produced offline in Phase 0 and one produced against a real account later.

Plan JSON is treated as untrusted input: shape is validated before it is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AttributeChange, ChangeSet, ResourceChange

#: Plan format versions this parser has been checked against. Terraform bumps the
#: minor version for additive changes, so we accept the 1.x line and reject others
#: loudly rather than silently misreading a future schema.
SUPPORTED_FORMAT_MAJOR = "1"

#: Attributes whose value is noise for impact analysis; they change on most plans
#: and carry no dependency or pressure signal.
IGNORED_ATTRIBUTES = frozenset(
    {
        "tags_all",
        "last_modified",
        "qualified_arn",
        "version",
        "source_code_hash",
    }
)


class PlanParseError(ValueError):
    """The plan JSON could not be read. Never downgraded to a warning."""


def parse_plan_file(path: str | Path) -> ChangeSet:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"{path} is not valid JSON: {exc}") from exc
    return parse_plan(document)


def parse_plan(document: Any) -> ChangeSet:
    if not isinstance(document, dict):
        raise PlanParseError(f"plan must be a JSON object, got {type(document).__name__}")

    format_version = document.get("format_version")
    if not isinstance(format_version, str):
        raise PlanParseError("plan is missing 'format_version'; this is not Terraform plan JSON")
    if format_version.split(".")[0] != SUPPORTED_FORMAT_MAJOR:
        raise PlanParseError(
            f"unsupported plan format_version {format_version!r}; "
            f"this parser understands {SUPPORTED_FORMAT_MAJOR}.x"
        )

    terraform_version = document.get("terraform_version")
    if not isinstance(terraform_version, str):
        raise PlanParseError("plan is missing 'terraform_version'")

    resource_changes = document.get("resource_changes", [])
    if not isinstance(resource_changes, list):
        raise PlanParseError("'resource_changes' must be a list")

    changes = tuple(
        _parse_resource_change(entry, index) for index, entry in enumerate(resource_changes)
    )
    return ChangeSet(
        terraform_version=terraform_version,
        format_version=format_version,
        changes=changes,
    )


def _parse_resource_change(entry: Any, index: int) -> ResourceChange:
    where = f"resource_changes[{index}]"
    if not isinstance(entry, dict):
        raise PlanParseError(f"{where} must be an object")

    address = _require_str(entry, "address", where)
    resource_type = _require_str(entry, "type", where)
    name = _require_str(entry, "name", where)

    change = entry.get("change")
    if not isinstance(change, dict):
        raise PlanParseError(f"{where} is missing its 'change' object")

    actions = change.get("actions")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise PlanParseError(f"{where}.change.actions must be a list of strings")

    before = change.get("before")
    after = change.get("after")
    attribute_changes = _diff_attributes(before, after)

    return ResourceChange(
        address=address,
        resource_type=resource_type,
        name=name,
        actions=tuple(actions),
        attribute_changes=attribute_changes,
    )


def _diff_attributes(before: Any, after: Any) -> tuple[AttributeChange, ...]:
    """Top-level attribute diff between the before and after states.

    Nested blocks are compared as whole values rather than recursed into. That is
    sufficient for the change types in scope and keeps the diff readable in evidence;
    a rule that needs to reach inside a block can read the value itself.
    """
    before_map = before if isinstance(before, dict) else {}
    after_map = after if isinstance(after, dict) else {}

    attributes = sorted(set(before_map) | set(after_map))
    diffs: list[AttributeChange] = []
    for attribute in attributes:
        if attribute in IGNORED_ATTRIBUTES:
            continue
        old = before_map.get(attribute)
        new = after_map.get(attribute)
        if old != new:
            diffs.append(AttributeChange(attribute=attribute, before=old, after=new))
    return tuple(diffs)


def _require_str(entry: dict[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise PlanParseError(f"{where}.{key} must be a string, got {type(value).__name__}")
    return value

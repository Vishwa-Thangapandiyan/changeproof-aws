"""A thin, explicit wrapper around the Terraform CLI.

Every experiment gets its own working copy of `terraform/` with its own local state
file, under `.changeproof/experiments/<experiment-id>/terraform/`. That isolation is
the reason two experiments can run without colliding, and the reason cleanup can
always find exactly what a given experiment created.

This module shells out. It does not import an AWS SDK, and it never runs `apply`
without being told which variables to apply with.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TerraformError(RuntimeError):
    pass


@dataclass(frozen=True)
class Terraform:
    working_dir: Path
    state_path: Path

    @classmethod
    def prepare(cls, source_dir: Path, working_dir: Path) -> Terraform:
        """Copy the stack definition into an isolated working directory."""
        if shutil.which("terraform") is None:
            raise TerraformError(
                "terraform CLI not found on PATH. Install it from "
                "https://developer.hashicorp.com/terraform/install"
            )

        working_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.tf")):
            shutil.copy2(source, working_dir / source.name)

        build = source_dir / "build"
        if build.is_dir():
            shutil.copytree(build, working_dir / "build", dirs_exist_ok=True)

        return cls(working_dir=working_dir, state_path=working_dir / "terraform.tfstate")

    def _run(self, *args: str, capture: bool = False) -> str:
        command = ["terraform", f"-chdir={self.working_dir}", *args]
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=capture,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip() if capture else ""
            raise TerraformError(f"{' '.join(command)} failed with exit code {completed.returncode}\n{detail}")
        return completed.stdout if capture else ""

    def init(self) -> None:
        self._run("init", "-input=false", "-upgrade=false")

    def _var_args(self, variables: dict[str, Any]) -> list[str]:
        args = [f"-state={self.state_path}"]
        for key, value in sorted(variables.items()):
            args.extend(["-var", f"{key}={value}"])
        return args

    def plan_json(self, variables: dict[str, Any]) -> dict[str, Any]:
        """Plan against current state and return the machine-readable plan."""
        plan_file = self.working_dir / "plan.tfplan"
        self._run("plan", "-input=false", "-lock=false", f"-out={plan_file}", *self._var_args(variables))
        raw = self._run("show", "-json", str(plan_file), capture=True)
        plan_file.unlink(missing_ok=True)
        return json.loads(raw)

    def apply(self, variables: dict[str, Any]) -> None:
        self._run("apply", "-input=false", "-auto-approve", *self._var_args(variables))

    def destroy(self, variables: dict[str, Any]) -> None:
        self._run("destroy", "-input=false", "-auto-approve", *self._var_args(variables))

    def outputs(self) -> dict[str, Any]:
        raw = self._run("output", "-json", f"-state={self.state_path}", capture=True)
        return {key: entry["value"] for key, entry in json.loads(raw).items()}

    def state_resources(self) -> list[str]:
        if not self.state_path.exists():
            return []
        raw = self._run("state", "list", f"-state={self.state_path}", capture=True)
        return [line for line in raw.splitlines() if line.strip()]


def effective_changes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Resource changes in a plan, excluding no-ops."""
    return [
        change
        for change in plan.get("resource_changes", [])
        if change.get("change", {}).get("actions") not in (["no-op"], None)
    ]


def assert_single_attribute_change(plan: dict[str, Any], address: str, attribute: str) -> None:
    """Refuse to apply anything except the one change the experiment is testing.

    This is the guardrail that makes the two runs comparable. If a drifted state, an
    edited module or a provider upgrade would cause Terraform to touch a second
    resource, the experiment is no longer a single-variable test and must not
    proceed silently.
    """
    changes = effective_changes(plan)
    addresses = sorted(change["address"] for change in changes)
    if addresses != [address]:
        raise TerraformError(
            f"expected exactly one changed resource ({address}), plan changes {addresses or 'nothing'}. "
            "The runs would not be a single-variable comparison."
        )

    change = changes[0]["change"]
    if change.get("actions") != ["update"]:
        raise TerraformError(
            f"expected an in-place update of {address}, plan wants {change.get('actions')}. "
            "A replacement would destroy the resource being measured."
        )

    before, after = change.get("before") or {}, change.get("after") or {}
    differing = sorted(
        key for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    )
    if differing != [attribute]:
        raise TerraformError(
            f"expected only {attribute} to change on {address}, also changing: "
            f"{[key for key in differing if key != attribute]}"
        )

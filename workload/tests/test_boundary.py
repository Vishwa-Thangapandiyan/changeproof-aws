"""The driver's own boundary: nothing reaches AWS unless someone asked for it.

Phase 0's rule is that the ChangeProof package never touches AWS at all. The driver
is Phase 1 tooling and does touch AWS, so its rule is narrower but just as strict:
importing it must cost nothing, the SDK must be confined to one module, and spending
money must require an explicit flag every single time.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

WORKLOAD_ROOT = Path(__file__).resolve().parents[1]
SDK_MODULES = ("boto3", "botocore")

#: The only two files permitted to speak to AWS. `src/index.py` runs inside Lambda;
#: `driver/awsio.py` is the driver's single I/O seam.
SDK_ALLOWLIST = {"src/index.py", "driver/awsio.py"}


def _sources() -> list[Path]:
    return [path for path in sorted(WORKLOAD_ROOT.rglob("*.py")) if "tests" not in path.parts]


def test_sdk_use_is_confined_to_the_allowlist():
    pattern = re.compile(r"^\s*(?:import|from)\s+(" + "|".join(SDK_MODULES) + r")\b", re.MULTILINE)

    offenders = sorted(
        path.relative_to(WORKLOAD_ROOT).as_posix()
        for path in _sources()
        if pattern.search(path.read_text(encoding="utf-8"))
    )

    assert set(offenders) <= SDK_ALLOWLIST, f"AWS SDK used outside the allowlist: {offenders}"


def test_importing_the_driver_loads_no_sdk():
    """The pipeline may import this package without pulling boto3 into the process."""
    for module in SDK_MODULES:
        sys.modules.pop(module, None)

    import workload.driver  # noqa: F401
    import workload.driver.cli  # noqa: F401
    import workload.driver.experiment  # noqa: F401
    import workload.driver.workload  # noqa: F401

    assert [module for module in SDK_MODULES if module in sys.modules] == []


def test_plan_command_makes_no_aws_call_and_invents_no_measurements(capsys):
    import json

    from workload.driver.cli import main

    assert main(["plan", "EXP-DRY"]) == 0
    document = json.loads(capsys.readouterr().out)

    assert document["change"] == {
        "address": "aws_lambda_function.api",
        "attribute": "reserved_concurrent_executions",
        "before": 10,
        "after": 100,
    }
    assert "No AWS call was made" in document["note"]
    # A dry run describes intentions. Anything that looks like a measurement or a
    # provisioned resource would be indistinguishable from a real result later.
    assert "environment" not in document
    assert "baseline" not in document
    assert "result" not in document


@pytest.mark.parametrize("command", ["run", "cleanup"])
def test_spending_commands_refuse_without_explicit_authorisation(command, capsys):
    from workload.driver.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main([command, "EXP-1"])

    assert excinfo.value.code == 2
    assert "--authorize-aws-spend" in capsys.readouterr().err


def test_authorisation_is_per_invocation_not_an_environment_variable(monkeypatch):
    """An exported variable someone forgot about is not consent."""
    from workload.driver.cli import main

    monkeypatch.setenv("CHANGEPROOF_PHASE", "1")
    monkeypatch.setenv("AWS_PROFILE", "default")

    with pytest.raises(SystemExit):
        main(["run", "EXP-1"])

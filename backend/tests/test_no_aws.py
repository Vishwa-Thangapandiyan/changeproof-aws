"""The Phase 0 boundary, enforced as a test.

If any of these fail, Phase 0's core promise is broken: the pipeline must run with
zero AWS credentials and zero AWS spend.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

LAMBDA_ROOT = Path(__file__).resolve().parents[1] / "lambda"

#: Modules that indicate an AWS SDK is in play.
AWS_SDK_MODULES = ("boto3", "botocore", "aiobotocore", "aioboto3")


def _python_sources() -> list[Path]:
    return sorted(LAMBDA_ROOT.rglob("*.py"))


def test_source_tree_contains_no_sdk_import():
    """No file under backend/lambda/ may import an AWS SDK.

    `adapters/aws.py` is not exempt. It describes what the real implementation will
    call, in prose, and imports nothing.
    """
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(" + "|".join(AWS_SDK_MODULES) + r")\b",
        re.MULTILINE,
    )
    offenders = []
    for source in _python_sources():
        text = source.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(source.relative_to(LAMBDA_ROOT)))
    assert offenders == [], f"AWS SDK imported in Phase 0 source: {offenders}"


def test_importing_the_pipeline_loads_no_sdk():
    """Importing every Phase 0 module must not pull an SDK into sys.modules."""
    for module in AWS_SDK_MODULES:
        sys.modules.pop(module, None)

    import changeproof.adapters.aws  # noqa: F401
    import changeproof.adapters.local  # noqa: F401
    import changeproof.compare  # noqa: F401
    import changeproof.graph  # noqa: F401
    import changeproof.parser  # noqa: F401
    import changeproof.pipeline  # noqa: F401
    import changeproof.predict  # noqa: F401
    import handler  # noqa: F401

    loaded = [m for m in AWS_SDK_MODULES if m in sys.modules]
    assert loaded == [], f"AWS SDK loaded by Phase 0 imports: {loaded}"


def test_pipeline_makes_no_network_call(tmp_path, monkeypatch, plan_path):
    """Any socket the pipeline opens fails the test.

    This catches an HTTP call that does not go through an SDK, which the import
    checks above would miss.
    """
    import socket

    from changeproof.pipeline import run

    def refuse(*args, **kwargs):
        raise AssertionError("Phase 0 attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    result = run(plan_path, experiment_id="NET-001", output_root=tmp_path)
    assert result.evidence.experiment_id == "NET-001"


def test_pipeline_runs_without_aws_credentials(tmp_path, monkeypatch, plan_path):
    """Strip every AWS environment variable; the pipeline must be unaffected."""
    from changeproof.pipeline import run

    for name in list(sys.modules.get("os").environ):  # type: ignore[union-attr]
        if name.startswith("AWS_"):
            monkeypatch.delenv(name, raising=False)

    result = run(plan_path, experiment_id="NOCRED-001", output_root=tmp_path)
    assert result.evidence.comparison.verdict is not None


@pytest.mark.parametrize(
    "stage",
    ["provision_test_env", "apply_change", "run_workload", "teardown_test_env"],
)
def test_aws_stages_refuse_to_run(stage):
    """Deferred stages must raise, never return a plausible-looking result."""
    from changeproof import PhaseNotAuthorizedError
    from handler import handler

    with pytest.raises(PhaseNotAuthorizedError) as excinfo:
        handler({"stage": stage, "experimentId": "EXP-1"})

    assert "has not been authorized" in str(excinfo.value)


def test_every_deferred_stage_is_declared():
    """The DEFERRED_STAGES set must match the stages that actually refuse."""
    from changeproof import PhaseNotAuthorizedError
    from handler import DEFERRED_STAGES, _STAGES

    refusing = set()
    for name in _STAGES:
        try:
            handler_fn = _STAGES[name]
            handler_fn({"stage": name, "experimentId": "EXP-1"})
        except PhaseNotAuthorizedError:
            refusing.add(name)
        except Exception:
            pass

    assert refusing == set(DEFERRED_STAGES)


def test_aws_adapters_all_refuse():
    """Every method on every AWS adapter raises rather than returning."""
    from changeproof import PhaseNotAuthorizedError
    from changeproof.adapters import aws

    cases = [
        (aws.NeptuneGraphSource(), "known_addresses", ()),
        (aws.NeptuneGraphSource(), "downstream_paths", ("x", 1)),
        (aws.CloudWatchTelemetrySource(), "collect", ("EXP-1",)),
        (aws.S3DynamoEvidenceStore(), "store", (None,)),
        (aws.S3DynamoEvidenceStore(), "load", ("EXP-1",)),
        (aws.BedrockExplainer(), "explain", (None,)),
        (aws.TerraformTestEnvironment(), "provision", ("EXP-1",)),
        (aws.TerraformTestEnvironment(), "apply_change", ("EXP-1", "p")),
        (aws.TerraformTestEnvironment(), "run_workload", ("EXP-1", 10, 10)),
        (aws.TerraformTestEnvironment(), "teardown", ("EXP-1",)),
    ]

    for instance, method, args in cases:
        with pytest.raises(PhaseNotAuthorizedError):
            getattr(instance, method)(*args)


def test_fixtures_are_labelled_as_fixtures(fixtures_dir):
    """Every JSON fixture must carry a marker saying it is not real AWS data."""
    import json

    for path in sorted(fixtures_dir.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert "$fixture" in document, f"{path.name} is missing a '$fixture' label"
        assert "FIXTURE" in document["$fixture"].upper()

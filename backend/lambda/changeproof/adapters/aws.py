"""Phase 1/2 placeholders. Nothing in this module executes.

Every class here declares the AWS-backed shape of a `ports` Protocol and raises
`PhaseNotAuthorizedError` on use. There is deliberately:

- no `import boto3` anywhere in this file
- no client construction, no credential lookup, no region resolution
- no partial implementation that could be mistaken for a working integration

Each docstring records what the real implementation will call, so the work is
specified without being executable. Implement these only when the user explicitly
authorizes the corresponding phase.
"""

from __future__ import annotations

from .. import PhaseNotAuthorizedError
from ..models import DependencyPath, Evidence, Observation

_PHASE_1 = "Phase 1 (free-tier AWS)"
_PHASE_2 = "Phase 2 (Bedrock)"


def _refuse(capability: str, phase: str, detail: str) -> PhaseNotAuthorizedError:
    return PhaseNotAuthorizedError(
        f"{capability} requires {phase}, which has not been authorized. {detail} "
        "Phase 0 runs entirely locally with zero AWS calls; see CLAUDE.md section 5."
    )


class NeptuneGraphSource:
    """Dependency graph backed by Amazon Neptune. Satisfies `GraphSource`.

    Planned implementation: openCypher over the Neptune HTTPS endpoint from inside
    the VPC. `downstream_paths` becomes a variable-length MATCH bounded by max_depth.

    Neptune has no free tier and bills while idle, so this stays unimplemented until
    the user decides the budget can carry it. `adapters.local.load_graph` is the
    supported backend until then.
    """

    def known_addresses(self) -> tuple[str, ...]:
        raise _refuse("Neptune graph queries", _PHASE_1, "Neptune is also outside the budget.")

    def downstream_paths(self, origin: str, max_depth: int) -> tuple[DependencyPath, ...]:
        raise _refuse("Neptune graph queries", _PHASE_1, "Neptune is also outside the budget.")


class CloudWatchTelemetrySource:
    """Telemetry from CloudWatch. Satisfies `TelemetrySource`.

    Planned implementation: `cloudwatch:GetMetricData` over two windows, the baseline
    before the change is applied and the observation window during the synthetic
    workload, returning `Observation(simulated=False)`.
    """

    def collect(self, experiment_id: str) -> Observation:
        raise _refuse(
            "CloudWatch telemetry collection",
            _PHASE_1,
            "Use adapters.local.FixtureTelemetrySource, which flags its output as simulated.",
        )


class S3DynamoEvidenceStore:
    """Evidence in S3, experiment state in DynamoDB. Satisfies `EvidenceStore`.

    Planned implementation: `s3:PutObject` under
    `experiments/<experimentId>/evidence.json`, matching the local layout exactly,
    plus a `dynamodb:PutItem` carrying experiment status and the verdict.
    """

    def store(self, evidence: Evidence) -> str:
        raise _refuse("S3 and DynamoDB evidence storage", _PHASE_1, "Use adapters.local.LocalEvidenceStore.")

    def load(self, experiment_id: str) -> Evidence:
        raise _refuse("S3 and DynamoDB evidence retrieval", _PHASE_1, "Use adapters.local.LocalEvidenceStore.")


class BedrockExplainer:
    """Explanation via Amazon Bedrock. Satisfies `Explainer`.

    Planned implementation: `bedrock-runtime:InvokeModel` with the serialized
    evidence bundle as the sole factual input, under a system prompt that forbids
    introducing any value absent from that bundle.

    The output must remain an explanation of the verdict, never its source. If this
    class ever influences `ComparisonResult.verdict`, the architecture has been
    violated; see CLAUDE.md section 2.
    """

    def explain(self, evidence: Evidence) -> str:
        raise _refuse(
            "Bedrock explanation",
            _PHASE_2,
            "Use adapters.local.DeterministicExplainer, which produces the same "
            "evidence-grounded content without a model.",
        )


class TerraformTestEnvironment:
    """Provisions, mutates and destroys the isolated test environment.

    Planned implementation: Terraform apply/destroy against a dedicated test account
    or a hard-scoped role, with a teardown that runs on every exit path.

    There is no Phase 0 stand-in and there will not be one. Simulating a provisioned
    environment would mean reporting an AWS outcome that never happened.
    """

    def provision(self, experiment_id: str) -> dict[str, str]:
        raise _refuse("Test environment provisioning", _PHASE_1, "No local substitute exists.")

    def apply_change(self, experiment_id: str, plan_path: str) -> dict[str, str]:
        raise _refuse("Applying a change to a test environment", _PHASE_1, "No local substitute exists.")

    def run_workload(self, experiment_id: str, requests_per_second: int, seconds: int) -> dict[str, str]:
        raise _refuse("Synthetic workload generation", _PHASE_1, "No local substitute exists.")

    def teardown(self, experiment_id: str) -> dict[str, str]:
        raise _refuse("Test environment teardown", _PHASE_1, "No local substitute exists.")

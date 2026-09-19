"""Interfaces for every capability that will eventually be backed by AWS.

Each Protocol here is the seam between the deterministic engine and the outside
world. Phase 0 satisfies them from `adapters.local`; Phase 1/2 will satisfy them
from `adapters.aws` without the engine changing.

Nothing in this module imports an SDK or performs I/O.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import DependencyPath, Evidence, Observation


@runtime_checkable
class GraphSource(Protocol):
    """Dependency relationships between infrastructure resources.

    Phase 0: a local JSON fixture.
    Later: Amazon Neptune, queried with the same two methods.
    """

    def known_addresses(self) -> tuple[str, ...]:
        """Every resource address present in the graph."""
        ...

    def downstream_paths(self, origin: str, max_depth: int) -> tuple[DependencyPath, ...]:
        """Every path from `origin` to a resource it can exert pressure on."""
        ...


@runtime_checkable
class TelemetrySource(Protocol):
    """Measured behaviour of the test environment, before and after the change.

    Phase 0: a local JSON fixture, flagged `simulated=True`.
    Later: CloudWatch GetMetricData over the experiment window.
    """

    def collect(self, experiment_id: str) -> Observation:
        ...


@runtime_checkable
class EvidenceStore(Protocol):
    """Durable storage for the evidence bundle.

    Phase 0: JSON files on the local filesystem.
    Later: artifacts in S3, experiment state in DynamoDB.
    """

    def store(self, evidence: Evidence) -> str:
        """Persist the bundle and return a locator for it."""
        ...

    def load(self, experiment_id: str) -> Evidence:
        ...


@runtime_checkable
class Explainer(Protocol):
    """Turns a stored evidence bundle into prose.

    Phase 0: deterministic templates over the evidence.
    Later: Bedrock, given the same evidence and forbidden to add to it.

    The contract in both phases is identical and non-negotiable: an explainer may
    only restate values that already exist in the evidence bundle. It never
    originates a metric, a dependency or a verdict.
    """

    def explain(self, evidence: Evidence) -> str:
        ...


@runtime_checkable
class TestEnvironment(Protocol):
    """Provisioning, mutating and destroying the isolated test environment.

    Phase 0: not implemented anywhere. There is no local stand-in, because a
    simulated provisioning result would be a fabricated AWS observation.
    """

    def provision(self, experiment_id: str) -> dict[str, str]:
        ...

    def apply_change(self, experiment_id: str, plan_path: str) -> dict[str, str]:
        ...

    def run_workload(self, experiment_id: str, requests_per_second: int, seconds: int) -> dict[str, str]:
        ...

    def teardown(self, experiment_id: str) -> dict[str, str]:
        ...

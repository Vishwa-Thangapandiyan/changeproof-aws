"""Phase 1 experiment driver: provisions, measures and destroys the test environment.

Importing this package reaches nothing and costs nothing. AWS access is confined to
`awsio`, which is imported lazily, and every operation that spends money is gated
behind an explicit authorisation flag in `cli`.
"""

from __future__ import annotations

from .experiment import ExperimentDriver
from .models import (
    ExperimentEnvironment,
    ResourceIdentity,
    RunRecord,
    TeardownReport,
    WorkloadResult,
    WorkloadSpec,
    comparability,
)

__all__ = [
    "ExperimentDriver",
    "ExperimentEnvironment",
    "ResourceIdentity",
    "RunRecord",
    "TeardownReport",
    "WorkloadResult",
    "WorkloadSpec",
    "comparability",
]

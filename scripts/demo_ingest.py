#!/usr/bin/env python3
"""Complete ChangeProof demo ingestion for EXP-DEMO-001."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from backend.storage.changeproof_storage import ChangeProofStorage


EXPERIMENT_ID = "EXP-DEMO-001"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    storage = ChangeProofStorage(
        table_name=os.getenv("CHANGE_PROOF_TABLE", "changeproof-experiments"),
        bucket_name=os.environ["CHANGE_PROOF_BUCKET"],
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )

    observation = {"simulated": False}
    created = iso_now()

    # 1. Create the experiment before any environment work starts.
    storage.write_meta(
        EXPERIMENT_ID,
        observation=observation,
        status="CREATING_ENVIRONMENT",
        createdAt=created,
        phase="environment",
        telemetrySource="cloudwatch",
        predictedSeverity="MEDIUM",
        confidence=0.91,
        changeSummary="Demo infrastructure change investigation",
        changedAddresses={"aws_instance.web", "aws_instance.worker"},
        reason="Initial experiment creation",
    )

    # 2. Baseline and changed workload runs.
    storage.write_run(
        EXPERIMENT_ID,
        "baseline",
        configuration={"concurrency": 10, "durationSeconds": 30},
        workloadFingerprint="demo-baseline-v1",
        attempted=300,
        accepted=296,
        throttled=2,
        failed=2,
        achievedRatePerSecond=9.866,
        windowStartEpoch=1789887600,
        windowEndEpoch=1789887630,
        windowStartIso="2026-09-20T07:00:00Z",
        windowEndIso="2026-09-20T07:00:30Z",
        attemptsStartedAt="2026-09-20T07:00:00Z",
        attemptsEndedAt="2026-09-20T07:00:30Z",
        drainedAt="2026-09-20T07:00:31Z",
        drained=True,
        notes=["baseline workload"],
    )
    storage.write_run(
        EXPERIMENT_ID,
        "changed",
        configuration={"concurrency": 20, "durationSeconds": 30},
        workloadFingerprint="demo-changed-v1",
        attempted=600,
        accepted=560,
        throttled=12,
        failed=28,
        achievedRatePerSecond=18.667,
        windowStartEpoch=1789887700,
        windowEndEpoch=1789887730,
        windowStartIso="2026-09-20T07:01:40Z",
        windowEndIso="2026-09-20T07:02:10Z",
        attemptsStartedAt="2026-09-20T07:01:40Z",
        attemptsEndedAt="2026-09-20T07:02:10Z",
        drainedAt="2026-09-20T07:02:11Z",
        drained=True,
        notes=["changed workload"],
    )

    # 3. Three concrete breaches.
    breaches = [
        ("aws_instance.web", "cpu", 94.2, 80, "HIGH", "Changed workload exceeded CPU limit."),
        ("aws_instance.worker", "latency", 420.0, 300, "HIGH", "P95 latency exceeded threshold."),
        ("aws_instance.web", "error_rate", 7.5, 5.0, "MEDIUM", "Error rate exceeded policy threshold."),
    ]
    for address, metric, actual, limit, severity, description in breaches:
        storage.write_breach(
            EXPERIMENT_ID,
            address,
            metric,
            actualValue=actual,
            limit=limit,
            limitKind="ABSOLUTE",
            severity=severity,
            description=description,
        )

    # 4. Thirteen predictions/observations.
    metric_specs = [
        ("aws_instance.web", "cpu", "aws_instance", 20, 70, 35, 94.2, 169.14, "BREACH", "CPU-001"),
        ("aws_instance.web", "memory", "aws_instance", 30, 80, 55, 61, 10.91, "OK", "MEM-001"),
        ("aws_instance.web", "latency", "aws_instance", 100, 300, 180, 260, 44.44, "OK", "LAT-001"),
        ("aws_instance.web", "error_rate", "aws_instance", 0, 5, 1.2, 7.5, 525, "BREACH", "ERR-001"),
        ("aws_instance.web", "requests_per_second", "aws_instance", 8, 12, 10, 18.7, 87, "OK", "RPS-001"),
        ("aws_instance.worker", "cpu", "aws_instance", 15, 65, 30, 72, 140, "BREACH", "CPU-002"),
        ("aws_instance.worker", "memory", "aws_instance", 25, 75, 50, 63, 26, "OK", "MEM-002"),
        ("aws_instance.worker", "latency", "aws_instance", 120, 280, 170, 420, 147.06, "BREACH", "LAT-002"),
        ("aws_instance.worker", "error_rate", "aws_instance", 0, 4, 0.8, 3.1, 287.5, "OK", "ERR-002"),
        ("aws_instance.worker", "requests_per_second", "aws_instance", 7, 11, 9, 17.2, 91.11, "OK", "RPS-002"),
        ("aws_instance.db", "cpu", "aws_instance", 10, 60, 25, 43, 72, "OK", "CPU-003"),
        ("aws_instance.db", "connections", "aws_instance", 20, 100, 50, 91, 82, "OK", "CONN-001"),
        ("aws_instance.db", "latency", "aws_instance", 80, 220, 140, 215, 53.57, "OK", "LAT-003"),
    ]
    for address, metric, resource_type, low, high, baseline, observed, pct, outcome, rule_id in metric_specs:
        storage.write_metric(
            EXPERIMENT_ID,
            address,
            metric,
            resource_type=resource_type,
            observation=observation,
            predictedLow=low,
            predictedHigh=high,
            baselineValue=baseline,
            observedValue=observed,
            actualChangePct=pct,
            outcome=outcome,
            ruleId=rule_id,
        )

    # 5. Progressive META update after comparison.
    storage.update_meta(
        EXPERIMENT_ID,
        observation=observation,
        status="COMPLETED",
        verdict="CHANGED",
        observedSeverity="HIGH",
        predictionAccuracy=0.846,
        blastRadiusCount=3,
        phase="comparison",
        reason="Three policy breaches observed across changed workload metrics.",
        evidenceKey=f"experiments/{EXPERIMENT_ID}/evidence.json",
        completedAt=iso_now(),
    )

    # 6. Upload the evidence bundle.
    storage.upload_evidence_artifacts(
        EXPERIMENT_ID,
        {
            "evidence.json": {"experimentId": EXPERIMENT_ID, "simulated": False, "breaches": 3, "metrics": 13},
            "experiment-manifest.json": {"experimentId": EXPERIMENT_ID, "baseline": "RUN#baseline", "changed": "RUN#changed"},
            "terraform-plan.json": {"resource_changes": []},
            "explanation.txt": "The changed workload produced three observed policy breaches.",
            "telemetry/baseline.json": {"source": "cloudwatch", "simulated": False},
            "telemetry/changed.json": {"source": "cloudwatch", "simulated": False},
        },
    )

    print(f"Ingested {EXPERIMENT_ID}: 1 META + 2 RUN + 3 BREACH + 13 METRIC items")
    print(f"S3 prefix: experiments/{EXPERIMENT_ID}/")


if __name__ == "__main__":
    main()

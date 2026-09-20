"""The workload generator's contract: same spec in, same load out."""

from __future__ import annotations

import pytest

from workload.driver.models import WorkloadSpec, comparability
from workload.driver.workload import build_schedule, execute


def test_schedule_is_identical_for_identical_specs():
    """Comparability starts here: two runs must be driven by the same plan."""
    spec = WorkloadSpec(attempts=100, duration_seconds=10, warmup_attempts=5)

    first = [(a.seq, round(a.offset_seconds, 9), a.payload["seq"]) for a in build_schedule(spec, "run-a")]
    second = [(a.seq, round(a.offset_seconds, 9), a.payload["seq"]) for a in build_schedule(spec, "run-b")]

    assert first == second


def test_warmup_attempts_precede_the_measured_window():
    spec = WorkloadSpec(attempts=10, duration_seconds=10, warmup_attempts=3)
    schedule = build_schedule(spec, "run")

    warmups = [a for a in schedule if a.seq < 0]
    measured = [a for a in schedule if a.seq >= 0]

    assert len(warmups) == 3
    assert len(measured) == 10
    assert all(a.offset_seconds < 0 for a in warmups)
    assert all(a.offset_seconds >= 0 for a in measured)


def test_payload_is_deterministic_apart_from_the_run_id():
    spec = WorkloadSpec(attempts=5, duration_seconds=1, warmup_attempts=0)
    baseline = build_schedule(spec, "exp-baseline")
    changed = build_schedule(spec, "exp-changed")

    assert [a.payload["payloadBytes"] for a in baseline] == [a.payload["payloadBytes"] for a in changed]
    assert [a.payload["seq"] for a in baseline] == [a.payload["seq"] for a in changed]


def test_execute_counts_outcomes_and_excludes_warmup():
    spec = WorkloadSpec(attempts=9, duration_seconds=0, warmup_attempts=4, client_concurrency=4)
    seen: list[dict] = []

    def invoke(payload):
        seen.append(payload)
        if str(payload["runId"]).endswith("warmup"):
            return "accepted"
        return ["accepted", "throttled", "failed"][int(payload["seq"]) % 3]

    result, start, end = execute(spec, "run", invoke, sleep=lambda _: None)

    assert len(seen) == 13
    assert result.attempted == 9
    assert (result.accepted, result.throttled, result.failed) == (3, 3, 3)
    assert end >= start


def test_a_raising_invoker_is_counted_as_failed_not_fatal():
    spec = WorkloadSpec(attempts=4, duration_seconds=0, warmup_attempts=0, client_concurrency=2)

    def invoke(payload):
        raise ConnectionError("network went away")

    result, _, _ = execute(spec, "run", invoke, sleep=lambda _: None)

    assert result.attempted == 4
    assert result.failed == 4


def test_fingerprint_tracks_the_spec():
    assert WorkloadSpec(attempts=100).fingerprint() == WorkloadSpec(attempts=100).fingerprint()
    assert WorkloadSpec(attempts=100).fingerprint() != WorkloadSpec(attempts=101).fingerprint()
    assert WorkloadSpec(payload_bytes=256).fingerprint() != WorkloadSpec(payload_bytes=512).fingerprint()

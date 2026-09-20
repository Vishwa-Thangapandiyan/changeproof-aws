"""Generating identical load against two different configurations.

The workload is a fixed schedule of invocation attempts: `attempts` of them, paced
evenly across `duration_seconds`. The schedule is computed before the first request
goes out and is a pure function of the spec, so the baseline run and the changed run
are driven by the same plan, not merely by the same intent.

Nothing here imports boto3. The invoker is injected, which is what lets the pacing
and accounting logic be tested offline.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import WorkloadResult, WorkloadSpec


class Invoker(Protocol):
    def __call__(self, payload: dict[str, object]) -> str:
        """Invoke the producer once.

        Returns one of "accepted", "throttled" or "failed". Raising is also
        acceptable and is accounted as "failed"; the generator never lets a single
        bad invocation end the run, because a partial run is still measurable as
        long as the attempt count is recorded honestly.
        """


@dataclass(frozen=True)
class Attempt:
    seq: int
    offset_seconds: float
    payload: dict[str, object]


def build_schedule(spec: WorkloadSpec, run_id: str) -> list[Attempt]:
    """The full attempt schedule, warm-up first.

    Warm-up attempts carry negative offsets: they are issued before the measurement
    window opens and are excluded from the recorded result. They exist so that cold
    starts are paid for once per run rather than landing inside one run's window and
    not the other's.
    """
    interval = spec.duration_seconds / spec.attempts if spec.attempts else 0.0
    warmup_interval = 0.05

    schedule = [
        Attempt(
            seq=-(index + 1),
            offset_seconds=-(spec.warmup_attempts - index) * warmup_interval,
            payload={"runId": f"{run_id}-warmup", "seq": index, "payloadBytes": spec.payload_bytes},
        )
        for index in range(spec.warmup_attempts)
    ]
    schedule.extend(
        Attempt(
            seq=index,
            offset_seconds=index * interval,
            payload={"runId": run_id, "seq": index, "payloadBytes": spec.payload_bytes},
        )
        for index in range(spec.attempts)
    )
    return schedule


def execute(
    spec: WorkloadSpec,
    run_id: str,
    invoke: Invoker,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[WorkloadResult, float, float]:
    """Run the schedule and report what actually happened.

    Returns the result plus the monotonic timestamps of the first and last measured
    attempt, which the caller converts into a wall-clock measurement window.
    """
    schedule = build_schedule(spec, run_id)
    counts = {"accepted": 0, "throttled": 0, "failed": 0}
    slowest = 0.0
    measured_start: float | None = None
    measured_end = 0.0
    lock = threading.Lock()

    origin = clock() + max(0.0, -min((item.offset_seconds for item in schedule), default=0.0))

    def fire(attempt: Attempt) -> None:
        nonlocal slowest, measured_start, measured_end

        delay = origin + attempt.offset_seconds - clock()
        if delay > 0:
            sleep(delay)

        began = clock()
        try:
            outcome = invoke(attempt.payload)
        except Exception:
            outcome = "failed"
        finished = clock()

        if attempt.seq < 0:
            return

        with lock:
            counts[outcome if outcome in counts else "failed"] += 1
            slowest = max(slowest, (finished - began) * 1000.0)
            measured_start = began if measured_start is None else min(measured_start, began)
            measured_end = max(measured_end, finished)

    with ThreadPoolExecutor(max_workers=spec.client_concurrency) as pool:
        list(pool.map(fire, schedule))

    attempted = counts["accepted"] + counts["throttled"] + counts["failed"]
    start = measured_start if measured_start is not None else origin
    elapsed = max(measured_end - start, 1e-6)

    result = WorkloadResult(
        attempted=attempted,
        accepted=counts["accepted"],
        throttled=counts["throttled"],
        failed=counts["failed"],
        achieved_rate=attempted / elapsed,
        slowest_attempt_ms=slowest,
    )
    return result, start, measured_end

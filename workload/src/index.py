"""The demo application: a producer and a consumer sharing one deployment package.

This is the workload that the proposed change is applied TO. It is deliberately
small, deterministic and boring. Every millisecond of work it performs is fixed, so
that a baseline run and a changed run differ in exactly one thing: the reserved
concurrency of the producer.

`terraform/main.tf` gives both functions `handler = "index.handler"` and the same
zip, so dispatch happens here, on the shape of the event:

    SQS event (has "Records")  -> consumer path
    anything else              -> producer path

This module is the only place in the repository that imports boto3, and it is
outside `backend/lambda/` for that reason: the Phase 0 boundary test forbids an SDK
import anywhere under the ChangeProof package. This file never runs on a laptop and
is never imported by the pipeline. It runs only inside AWS, in an experiment
environment someone explicitly paid to create.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3

QUEUE_URL = os.environ.get("QUEUE_URL", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")

#: Fixed wall-clock work per message, in milliseconds.
#:
#: `time.sleep` rather than a busy loop, deliberately. The bottleneck this demo is
#: built to expose is a concurrency cap, not CPU contention, and sleep is far more
#: reproducible across invocations than a spin loop whose iteration count depends on
#: whatever the underlying host is doing. Reproducibility is what makes the two runs
#: comparable.
WORK_MS = int(os.environ.get("WORK_MS", "20"))

_sqs = boto3.client("sqs") if QUEUE_URL else None
_dynamodb = boto3.client("dynamodb") if TABLE_NAME else None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if "Records" in event:
        return _consume(event)
    return _produce(event)


# --- producer: invoked synchronously, one invocation per attempted order -------------


def _produce(event: dict[str, Any]) -> dict[str, Any]:
    """Enqueue exactly one order.

    Invoked with `{"runId": ..., "seq": ..., "payloadBytes": ...}`. The caller
    invokes this synchronously, so that a reserved-concurrency rejection comes back
    to the driver as a 429 and is counted, rather than disappearing into Lambda's
    internal async retry queue where it would be invisible to both the driver and
    the Throttles metric.
    """
    if _sqs is None:
        raise RuntimeError("QUEUE_URL is not set; this function is not configured as the producer")

    run_id = event["runId"]
    seq = int(event["seq"])
    padding_bytes = int(event.get("payloadBytes", 256))

    order_id = f"{run_id}#{seq:06d}"
    body = {
        "orderId": order_id,
        "runId": run_id,
        "seq": seq,
        "createdAt": _now_ms(),
        # Fixed content, not random: two runs with the same spec produce
        # byte-identical message bodies apart from the timestamp.
        "padding": "x" * padding_bytes,
    }

    _sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(body))
    return {"orderId": order_id}


# --- consumer: SQS event source, batch of up to 10 -----------------------------------


def _consume(event: dict[str, Any]) -> dict[str, Any]:
    """Process a batch of orders into DynamoDB.

    An unexpected failure is re-raised rather than swallowed. The event source
    mapping then retries the whole batch, and the redrive policy sends it to the DLQ
    after three attempts. A silent success on a failed write would corrupt the
    experiment: the queue would drain, the drain gate would pass, and the evidence
    would describe work that never happened.
    """
    if _dynamodb is None:
        raise RuntimeError("TABLE_NAME is not set; this function is not configured as the consumer")

    processed = 0
    for record in event["Records"]:
        body = json.loads(record["body"])
        time.sleep(WORK_MS / 1000.0)

        created_at = int(body["createdAt"])
        processed_at = _now_ms()
        _dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "orderId": {"S": body["orderId"]},
                "runId": {"S": body["runId"]},
                "seq": {"N": str(body["seq"])},
                "createdAt": {"N": str(created_at)},
                "processedAt": {"N": str(processed_at)},
                "queueLatencyMs": {"N": str(processed_at - created_at)},
            },
        )
        processed += 1

    return {"processed": processed}


def _now_ms() -> int:
    return int(time.time() * 1000)

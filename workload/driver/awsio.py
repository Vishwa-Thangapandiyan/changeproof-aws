"""The only Python in the driver that talks to AWS.

Isolated in one module so that everything else — pacing, scheduling, windowing,
manifest shaping — is testable with no credentials and no SDK. boto3 is imported
lazily inside the constructors for the same reason: importing this module costs
nothing and reaches nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LambdaInvoker:
    """Synchronous invocation of the producer function.

    Synchronous on purpose. An async (`Event`) invocation that hits the reserved
    concurrency cap is retried internally by Lambda for up to six hours, so the
    rejection never reaches the caller and the attempt count stops meaning anything.
    RequestResponse surfaces the rejection immediately as a 429, which is both
    counted here and published as the Throttles metric.
    """

    function_name: str
    region: str

    def __post_init__(self) -> None:
        import boto3
        from botocore.config import Config

        # Client-side retries would silently convert a throttle into a success and
        # destroy the measurement. The experiment wants the raw rejection.
        self._client = boto3.client(
            "lambda",
            region_name=self.region,
            config=Config(retries={"max_attempts": 0, "mode": "standard"}, read_timeout=30),
        )

    def __call__(self, payload: dict[str, object]) -> str:
        from botocore.exceptions import ClientError

        try:
            response = self._client.invoke(
                FunctionName=self.function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            return "throttled" if code in ("TooManyRequestsException", "ThrottlingException") else "failed"

        if response.get("FunctionError"):
            return "failed"
        return "accepted"


@dataclass
class QueueProbe:
    """Queue depth, used by the drain gate between runs."""

    queue_url: str
    region: str

    def __post_init__(self) -> None:
        import boto3

        self._client = boto3.client("sqs", region_name=self.region)

    def depth(self) -> tuple[int, int]:
        """(visible, in flight)."""
        response = self._client.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )
        attributes: dict[str, Any] = response["Attributes"]
        return (
            int(attributes["ApproximateNumberOfMessages"]),
            int(attributes["ApproximateNumberOfMessagesNotVisible"]),
        )

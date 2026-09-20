"""Read-only check of AwsMetricDataSource against real CloudWatch.

Needs only cloudwatch:GetMetricData. Creates nothing, writes nothing.

    AWS_PROFILE=changeproof python scripts/verify_cloudwatch.py \
        --region ap-south-2 --function my-func [--table my-table]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend" / "lambda"), str(ROOT / "backend")]

from changeproof.manifest import MetricWindow  # noqa: E402
from changeproof.telemetry import MetricQuery  # noqa: E402
from telemetry_aws.cloudwatch import AwsMetricDataSource  # noqa: E402


def window(seconds_ago_start: int, seconds_ago_end: int) -> MetricWindow:
    now = int(time.time())
    start, end = now - seconds_ago_start, now - seconds_ago_end
    iso = lambda e: datetime.fromtimestamp(e, tz=timezone.utc).isoformat()
    return MetricWindow(start, end, iso(start), iso(end))


def query(qid, namespace, metric, dims, stat) -> MetricQuery:
    return MetricQuery(qid, "check", metric, namespace, tuple(dims), stat, "Count")


def show(label: str, values: list[float] | None) -> None:
    values = values or []
    print(f"  {label:<46} {len(values)} datapoints  {values[:6]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--function", required=True)
    ap.add_argument("--table")
    args = ap.parse_args()

    source = AwsMetricDataSource(args.region)
    recent = window(3 * 3600, 0)

    print("1. Lambda Invocations, last 3h (expect datapoints if it had traffic)")
    q_real = query("real", "AWS/Lambda", "Invocations", [("FunctionName", args.function)], "Sum")
    q_ghost = query("ghost", "AWS/Lambda", "Invocations",
                    [("FunctionName", "changeproof-no-such-function")], "Sum")
    out = source.fetch((q_real, q_ghost), recent)
    show(args.function, out.get("real"))

    print("2. Idle/nonexistent resource (expect 0 datapoints, NOT zeros)")
    show("changeproof-no-such-function", out.get("ghost"))

    if args.table:
        print("3. DynamoDB SuccessfulRequestLatency, last 3h")
        base = [("TableName", args.table)]
        q_with = query("with_op", "AWS/DynamoDB", "SuccessfulRequestLatency",
                       base + [("Operation", "PutItem")], "Average")
        q_without = query("no_op", "AWS/DynamoDB", "SuccessfulRequestLatency", base, "Average")
        out = source.fetch((q_with, q_without), recent)
        show("with Operation=PutItem", out.get("with_op"))
        show("WITHOUT Operation (expect empty, silently)", out.get("no_op"))


if __name__ == "__main__":
    main()

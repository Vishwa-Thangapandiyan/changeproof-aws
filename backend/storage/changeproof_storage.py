"""Storage SDK for the ChangeProof experiment storage system.

The module deliberately keeps AWS concerns small and explicit so it can be used
by a pipeline, Lambda, CLI, or local ingestion process.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

import boto3
from boto3.dynamodb.conditions import Key, Attr

DEFAULT_TABLE_NAME = "changeproof-experiments"

ARTIFACTS = (
    "evidence.json",
    "experiment-manifest.json",
    "terraform-plan.json",
    "explanation.txt",
    "telemetry/baseline.json",
    "telemetry/changed.json",
)


def _clean_value(value: Any) -> Any:
    """Recursively make a Python value safe for DynamoDB.

    - float -> Decimal(str(float))
    - empty strings -> omitted by the parent container
    - mappings -> recursively cleaned mappings
    - lists/tuples -> empty strings are removed
    - sets -> empty strings are removed; values are recursively cleaned
    - bool remains bool (bool is a subclass of int in Python)
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            if item == "":
                continue
            cleaned_item = _clean_value(item)
            if cleaned_item == "":
                continue
            cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if item == "":
                continue
            cleaned_item = _clean_value(item)
            if cleaned_item != "":
                result.append(cleaned_item)
        return result
    if isinstance(value, set):
        result = set()
        for item in value:
            if item == "":
                continue
            cleaned_item = _clean_value(item)
            if cleaned_item != "":
                result.add(cleaned_item)
        return result
    return value


def clean_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursively cleaned DynamoDB item."""
    return _clean_value(item)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ttl(days: int = 30) -> int:
    return int(time.time()) + days * 24 * 60 * 60


class ChangeProofStorage:
    """DynamoDB + S3 storage adapter for ChangeProof."""

    def __init__(
        self,
        *,
        table_name: str = DEFAULT_TABLE_NAME,
        bucket_name: str | None = None,
        region_name: str | None = None,
        dynamodb_resource: Any | None = None,
        s3_client: Any | None = None,
    ) -> None:
        region_name = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        self.ddb = dynamodb_resource or boto3.resource("dynamodb", region_name=region_name)
        self.s3 = s3_client or boto3.client("s3", region_name=region_name)
        self.table = self.ddb.Table(table_name)
        self.table_name = table_name
        self.bucket_name = bucket_name or os.getenv("CHANGE_PROOF_BUCKET")
        if not self.bucket_name:
            raise ValueError("bucket_name or CHANGE_PROOF_BUCKET must be supplied")

    @staticmethod
    def experiment_pk(experiment_id: str) -> str:
        return f"EXP#{experiment_id}"

    def upload_evidence_artifacts(
        self,
        experiment_id: str,
        artifacts: Mapping[str, Any],
        *,
        content_type_json: str = "application/json",
    ) -> dict[str, str]:
        """Upload the defined experiment artifact set under one S3 prefix.

        Values may be JSON-serializable Python objects or strings/bytes.
        Returns a mapping of artifact name -> S3 key.
        """
        prefix = f"experiments/{experiment_id}/"
        uploaded: dict[str, str] = {}
        for name in ARTIFACTS:
            if name not in artifacts:
                continue
            value = artifacts[name]
            if isinstance(value, bytes):
                body = value
                content_type = "text/plain" if name.endswith(".txt") else content_type_json
            elif isinstance(value, str):
                body = value.encode("utf-8")
                content_type = "text/plain" if name.endswith(".txt") else content_type_json
            else:
                body = json.dumps(value, default=str, separators=(",", ":")).encode("utf-8")
                content_type = content_type_json

            key = f"{prefix}{name}"
            self.s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
            uploaded[name] = key
        return uploaded

    def _put(self, item: Mapping[str, Any]) -> dict[str, Any]:
        cleaned = clean_item(item)
        self.table.put_item(Item=cleaned)
        return cleaned

    def write_meta(
        self,
        experiment_id: str,
        *,
        observation: Mapping[str, Any],
        status: str,
        ttl: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Create/replace META. verdict is intentionally absent unless explicitly supplied."""
        now = fields.pop("updatedAt", None) or _now_iso()
        created_at = fields.pop("createdAt", None) or now
        item: dict[str, Any] = {
            "PK": self.experiment_pk(experiment_id),
            "SK": "META",
            "experimentId": experiment_id,
            "status": status,
            "GSI1PK": f"STATUS#{status}",
            "GSI1SK": created_at,
            "simulated": bool(observation.get("simulated", False)),
            "createdAt": created_at,
            "updatedAt": now,
            "ttl": ttl or _ttl(),
        }
        item.update(fields)
        # A caller can explicitly supply verdict during a later update.
        return self._put(item)

    def update_meta(self, experiment_id: str, *, observation: Mapping[str, Any], **updates: Any) -> dict[str, Any]:
        """Progressively update META while preserving unspecified fields."""
        existing = self.get_item(experiment_id, "META")
        if not existing:
            raise KeyError(f"META does not exist for {experiment_id}")
        status = updates.pop("status", existing.get("status"))
        now = updates.pop("updatedAt", None) or _now_iso()
        existing.update(updates)
        existing["status"] = status
        existing["GSI1PK"] = f"STATUS#{status}"
        existing["GSI1SK"] = existing.get("createdAt", now)
        existing["updatedAt"] = now
        existing["simulated"] = bool(observation.get("simulated", existing.get("simulated", False)))
        return self._put(existing)

    def write_run(self, experiment_id: str, label: str, **attributes: Any) -> dict[str, Any]:
        if label not in {"baseline", "changed"}:
            raise ValueError("label must be 'baseline' or 'changed'")
        item = {
            "PK": self.experiment_pk(experiment_id),
            "SK": f"RUN#{label}",
            "label": label,
            **attributes,
        }
        return self._put(item)

    def write_breach(self, experiment_id: str, resource_address: str, metric: str, **attributes: Any) -> dict[str, Any]:
        item = {
            "PK": self.experiment_pk(experiment_id),
            "SK": f"BREACH#{resource_address}#{metric}",
            "resourceAddress": resource_address,
            "metric": metric,
            **attributes,
        }
        return self._put(item)

    def write_metric(
        self,
        experiment_id: str,
        resource_address: str,
        metric: str,
        *,
        resource_type: str,
        observation: Mapping[str, Any],
        **attributes: Any,
    ) -> dict[str, Any]:
        created_at = attributes.pop("createdAt", None) or _now_iso()
        item = {
            "PK": self.experiment_pk(experiment_id),
            "SK": f"METRIC#{resource_address}#{metric}",
            "resourceAddress": resource_address,
            "metric": metric,
            "resourceType": resource_type,
            "GSI2PK": f"METRIC#{resource_type}#{metric}",
            "GSI2SK": created_at,
            "createdAt": created_at,
            "simulated": bool(observation.get("simulated", False)),
            **attributes,
        }
        return self._put(item)

    def get_item(self, experiment_id: str, sk: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"PK": self.experiment_pk(experiment_id), "SK": sk})
        return response.get("Item")

    def query_experiment(self, experiment_id: str) -> list[dict[str, Any]]:
        response = self.table.query(KeyConditionExpression=Key("PK").eq(self.experiment_pk(experiment_id)))
        return response.get("Items", [])

    def query_status(self, status: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq(f"STATUS#{status}"),
            "ScanIndexForward": True,
        }
        if limit:
            kwargs["Limit"] = limit
        return self.table.query(**kwargs).get("Items", [])

    def query_metric_history(
        self,
        resource_type: str,
        metric: str,
        *,
        include_simulated: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "IndexName": "GSI2",
            "KeyConditionExpression": Key("GSI2PK").eq(f"METRIC#{resource_type}#{metric}"),
            "ScanIndexForward": True,
        }
        if not include_simulated:
            kwargs["FilterExpression"] = Attr("simulated").eq(False)
        if limit:
            kwargs["Limit"] = limit
        return self.table.query(**kwargs).get("Items", [])

    def delete_experiment(self, experiment_id: str) -> int:
        """Delete all experiment items. Useful for tests/cleanup; not a production transaction."""
        items = self.query_experiment(experiment_id)
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        return len(items)


def build_storage_from_environment() -> ChangeProofStorage:
    return ChangeProofStorage(
        table_name=os.getenv("CHANGE_PROOF_TABLE", DEFAULT_TABLE_NAME),
        bucket_name=os.environ["CHANGE_PROOF_BUCKET"],
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )

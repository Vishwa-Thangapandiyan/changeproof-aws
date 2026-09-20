import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from backend.storage.changeproof_storage import ChangeProofStorage, clean_item


TABLE_NAME = "changeproof-experiments"
BUCKET = "changeproof-evidence-123456789012-ap-south-2"


@pytest.fixture
def storage():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="ap-south-2")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="ap-south-2")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-2"})
        yield ChangeProofStorage(
            table_name=TABLE_NAME,
            bucket_name=BUCKET,
            region_name="ap-south-2",
            dynamodb_resource=dynamodb,
            s3_client=s3,
        )


def test_clean_item_recursively_converts_floats_and_removes_empty_strings():
    value = clean_item({"a": 1.25, "b": "", "c": {"d": 2.5, "e": ""}, "f": ["", 3.75]})
    assert value == {"a": Decimal("1.25"), "c": {"d": Decimal("2.5")}, "f": [Decimal("3.75")]}


def test_meta_verdict_is_absent_initially_and_simulated_is_preserved(storage):
    item = storage.write_meta(
        "EXP-1",
        observation={"simulated": True},
        status="CREATING_ENVIRONMENT",
        observedSeverity="HIGH",
        predictedSeverity="MEDIUM",
        predictionAccuracy=0.75,
        changeSummary="test",
    )
    assert item["simulated"] is True
    assert item["predictionAccuracy"] == Decimal("0.75")
    assert "verdict" not in item

    updated = storage.update_meta("EXP-1", observation={"simulated": True}, status="COMPARING", verdict="PASS")
    assert updated["status"] == "COMPARING"
    assert updated["verdict"] == "PASS"
    assert updated["predictionAccuracy"] == Decimal("0.75")


def test_run_breach_metric_and_single_experiment_query(storage):
    storage.write_run("EXP-2", "baseline", attempted=10, achievedRatePerSecond=9.5, notes=["", "ok"])
    storage.write_run("EXP-2", "changed", attempted=12, achievedRatePerSecond=8.25)
    storage.write_breach("EXP-2", "aws_instance.web", "cpu", actualValue=95.5, limit=80, severity="HIGH", description="CPU breach")
    storage.write_metric(
        "EXP-2",
        "aws_instance.web",
        "cpu",
        resource_type="aws_instance",
        observation={"simulated": False},
        predictedLow=20.0,
        predictedHigh=70.0,
        baselineValue=30.0,
        observedValue=75.0,
        actualChangePct=150.0,
        outcome="BREACH",
        ruleId="R1",
    )
    items = storage.query_experiment("EXP-2")
    assert len(items) == 4
    baseline_run = next(item for item in items if item.get("SK") == "RUN#baseline")
    assert baseline_run["notes"] == ["ok"]
    metric = storage.get_item("EXP-2", "METRIC#aws_instance.web#cpu")
    assert metric["predictedLow"] == Decimal("20.0")


def test_s3_uploads_artifacts(storage):
    artifacts = {
        "evidence.json": {"experimentId": "EXP-3", "value": 1.2},
        "experiment-manifest.json": {"status": "complete"},
        "terraform-plan.json": {"resource_changes": []},
        "explanation.txt": "No breach.",
        "telemetry/baseline.json": {"cpu": 30.0},
        "telemetry/changed.json": {"cpu": 35.0},
    }
    uploaded = storage.upload_evidence_artifacts("EXP-3", artifacts)
    assert set(uploaded) == set(artifacts)
    obj = storage.s3.get_object(Bucket=BUCKET, Key="experiments/EXP-3/evidence.json")
    assert json.loads(obj["Body"].read()) == {"experimentId": "EXP-3", "value": 1.2}


def test_gsi1_status_query(storage):
    storage.write_meta("EXP-A", observation={"simulated": False}, status="COMPLETED", changeSummary="A")
    storage.write_meta("EXP-B", observation={"simulated": False}, status="FAILED", changeSummary="B")
    storage.write_meta("EXP-C", observation={"simulated": False}, status="COMPLETED", changeSummary="C")
    result = storage.query_status("COMPLETED")
    assert {item["experimentId"] for item in result} == {"EXP-A", "EXP-C"}


def test_gsi2_excludes_simulated(storage):
    storage.write_metric(
        "EXP-A", "aws_instance.a", "cpu", resource_type="aws_instance",
        observation={"simulated": False}, predictedLow=1.0, predictedHigh=5.0,
        baselineValue=2.0, observedValue=3.0, actualChangePct=50.0, outcome="OK",
    )
    storage.write_metric(
        "EXP-B", "aws_instance.b", "cpu", resource_type="aws_instance",
        observation={"simulated": True}, predictedLow=1.0, predictedHigh=5.0,
        baselineValue=2.0, observedValue=9.0, actualChangePct=350.0, outcome="BREACH",
    )
    result = storage.query_metric_history("aws_instance", "cpu")
    assert len(result) == 1
    assert result[0]["simulated"] is False

    all_result = storage.query_metric_history("aws_instance", "cpu", include_simulated=True)
    assert len(all_result) == 2


def test_delete_experiment(storage):
    storage.write_meta("EXP-DEL", observation={"simulated": False}, status="CREATING_ENVIRONMENT")
    storage.write_run("EXP-DEL", "baseline")
    assert storage.delete_experiment("EXP-DEL") == 2
    assert storage.query_experiment("EXP-DEL") == []

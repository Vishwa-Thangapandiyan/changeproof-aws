"""The engine's side of the driver boundary.

Everything here runs offline: the fixture is a local JSON file and the parser makes
no call of any kind. `test_no_aws.py` already asserts that nothing under
`backend/lambda/` imports an SDK, which covers `manifest.py` too.
"""

from __future__ import annotations

import json

import pytest

from changeproof.manifest import (
    ManifestParseError,
    parse_manifest,
    parse_manifest_file,
)


@pytest.fixture(scope="module")
def manifest_path(fixtures_dir):
    return fixtures_dir / "experiment_manifest.json"


@pytest.fixture(scope="module")
def document(manifest_path):
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest(manifest_path):
    return parse_manifest_file(manifest_path)


def _without(document, *path):
    """A deep copy of the fixture with one key removed, for rejection tests."""
    clone = json.loads(json.dumps(document))
    target = clone
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return clone


def _with(document, value, *path):
    clone = json.loads(json.dumps(document))
    target = clone
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return clone


# --- the fields the boundary exists to carry -----------------------------------------


def test_experiment_id(manifest):
    assert manifest.experiment_id == "EXP-FIXTURE-001"


def test_region(manifest):
    assert manifest.region == "us-east-1"


def test_every_resource_is_parsed(manifest):
    assert manifest.graph_addresses == (
        "aws_lambda_function.api",
        "aws_sqs_queue.work",
        "aws_lambda_function.worker",
        "aws_dynamodb_table.orders",
    )


def test_resource_lookup_by_the_join_key(manifest):
    resource = manifest.resource("aws_sqs_queue.work")

    assert resource.resource_type == "aws_sqs_queue"
    assert resource.physical_name == "changeproof-exp-fixture-001-work"


def test_unknown_address_raises_and_names_what_exists(manifest):
    with pytest.raises(ManifestParseError, match="no resource at address"):
        manifest.resource("aws_rds_instance.nope")


@pytest.mark.parametrize(
    "address, namespace, dimension_name",
    [
        ("aws_lambda_function.api", "AWS/Lambda", "FunctionName"),
        ("aws_lambda_function.worker", "AWS/Lambda", "FunctionName"),
        ("aws_sqs_queue.work", "AWS/SQS", "QueueName"),
        ("aws_dynamodb_table.orders", "AWS/DynamoDB", "TableName"),
    ],
)
def test_cloudwatch_namespace_and_dimension(manifest, address, namespace, dimension_name):
    target = manifest.resource(address).cloudwatch

    assert target.namespace == namespace
    assert len(target.dimensions) == 1
    assert target.dimensions[0].name == dimension_name


def test_dimension_value_is_the_physical_name(manifest):
    resource = manifest.resource("aws_lambda_function.worker")

    assert resource.cloudwatch.dimensions[0].value == resource.physical_name


def test_dimension_serializes_in_cloudwatch_casing(manifest):
    dimension = manifest.resource("aws_sqs_queue.work").cloudwatch.dimensions[0]

    assert dimension.to_dict() == {"Name": "QueueName", "Value": "changeproof-exp-fixture-001-work"}


def test_arn_is_read_when_present(manifest):
    assert manifest.resource("aws_dynamodb_table.orders").arn.endswith(
        "table/changeproof-exp-fixture-001-orders"
    )


def test_queue_url_is_read_for_the_queue(manifest):
    assert manifest.resource("aws_sqs_queue.work").queue_url.startswith("https://sqs.")


def test_optional_fields_are_none_when_absent(manifest):
    """Only the queue has a queueUrl; the others must not invent one."""
    assert manifest.resource("aws_lambda_function.api").queue_url is None
    assert manifest.resource("aws_dynamodb_table.orders").queue_url is None


def test_baseline_window(manifest):
    window = manifest.baseline_window

    assert window.start_epoch == 1789905840
    assert window.end_epoch == 1789906080
    assert window.start_iso == "2026-09-20T12:04:00Z"
    assert window.duration_seconds == 240


def test_changed_window(manifest):
    window = manifest.changed_window

    assert window.start_epoch == 1789906260
    assert window.end_epoch == 1789906620
    assert window.duration_seconds == 360


def test_the_two_windows_are_distinct(manifest):
    """One ObservedMetric draws a value from each, so they must not be the same period."""
    assert manifest.baseline_window.end_epoch <= manifest.changed_window.start_epoch


# --- the parser reads the real document, not a trimmed one ---------------------------


def test_keys_the_engine_does_not_read_are_ignored(document):
    """The fixture carries the driver's full manifest; parsing must not object."""
    for key in ("workload", "result", "timestamps", "drained", "dlqDepth"):
        assert key in document["baseline"]
    for key in ("comparability", "teardown", "source", "simulated"):
        assert key in document

    assert parse_manifest(document).experiment_id == "EXP-FIXTURE-001"


def test_an_unrecognised_key_does_not_break_parsing(document):
    assert parse_manifest(_with(document, {"anything": 1}, "futureSection")).region == "us-east-1"


def test_to_dict_uses_the_manifest_key_paths(manifest):
    """Serialisation speaks the driver's schema, not a projection of our own.

    The paths asserted here are the ones `parse_manifest` reads. If they drift, the
    engine can emit a document it cannot then read back.
    """
    payload = manifest.to_dict()

    assert payload["experimentId"] == "EXP-FIXTURE-001"
    assert payload["environment"]["region"] == "us-east-1"
    assert payload["environment"]["resources"][0]["graphAddress"] == "aws_lambda_function.api"
    assert payload["baseline"]["metricWindow"]["startEpoch"] == 1789905840
    assert payload["changed"]["metricWindow"]["endEpoch"] == 1789906620

    # A flat layout would be unreadable by the parser that produced it.
    assert "region" not in payload
    assert "baselineWindow" not in payload
    assert "changedWindow" not in payload


def test_to_dict_output_is_valid_manifest_input(manifest):
    """The real compatibility check: feed our own output back through the parser."""
    reparsed = parse_manifest(json.loads(json.dumps(manifest.to_dict())))

    assert reparsed == manifest


# --- untrusted input -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("not a dict", "must be a JSON object"),
        (["a", "list"], "must be a JSON object"),
        (42, "must be a JSON object"),
    ],
)
def test_non_object_documents_are_rejected(value, expected):
    with pytest.raises(ManifestParseError, match=expected):
        parse_manifest(value)


@pytest.mark.parametrize(
    "path, expected",
    [
        (("experimentId",), "experimentId must be a non-empty string"),
        (("environment",), "missing its 'environment' object"),
        (("environment", "region"), "region must be a non-empty string"),
        (("environment", "resources"), "must be a non-empty list"),
        (("baseline",), "missing its 'baseline' run"),
        (("changed",), "missing its 'changed' run"),
        (("baseline", "metricWindow"), "missing its 'metricWindow' object"),
    ],
)
def test_missing_required_sections_are_rejected(document, path, expected):
    with pytest.raises(ManifestParseError, match=expected):
        parse_manifest(_without(document, *path))


def test_empty_resource_list_is_rejected(document):
    with pytest.raises(ManifestParseError, match="non-empty list"):
        parse_manifest(_with(document, [], "environment", "resources"))


@pytest.mark.parametrize("key", ["graphAddress", "resourceType", "physicalName"])
def test_a_resource_missing_a_required_field_is_rejected(document, key):
    clone = json.loads(json.dumps(document))
    del clone["environment"]["resources"][1][key]

    with pytest.raises(ManifestParseError, match=rf"resources\[1\]\.{key}"):
        parse_manifest(clone)


def test_a_resource_without_cloudwatch_is_rejected(document):
    clone = json.loads(json.dumps(document))
    del clone["environment"]["resources"][0]["cloudwatch"]

    with pytest.raises(ManifestParseError, match="missing its 'cloudwatch' object"):
        parse_manifest(clone)


def test_empty_dimensions_are_rejected(document):
    """A namespace with no dimensions queries every resource in the account."""
    clone = json.loads(json.dumps(document))
    clone["environment"]["resources"][0]["cloudwatch"]["dimensions"] = []

    with pytest.raises(ManifestParseError, match="dimensions must be a non-empty list"):
        parse_manifest(clone)


def test_a_malformed_dimension_is_rejected(document):
    clone = json.loads(json.dumps(document))
    clone["environment"]["resources"][0]["cloudwatch"]["dimensions"] = [{"Name": "FunctionName"}]

    with pytest.raises(ManifestParseError, match=r"dimensions\[0\]\.Value"):
        parse_manifest(clone)


def test_duplicate_graph_address_is_rejected(document):
    """It is the join key; two rows under one key silently drops a resource."""
    clone = json.loads(json.dumps(document))
    clone["environment"]["resources"][2]["graphAddress"] = "aws_sqs_queue.work"

    with pytest.raises(ManifestParseError, match="duplicate graphAddress"):
        parse_manifest(clone)


def test_experiment_id_disagreement_is_rejected(document):
    clone = _with(document, "EXP-SOMETHING-ELSE", "environment", "experimentId")

    with pytest.raises(ManifestParseError, match="describes two experiments"):
        parse_manifest(clone)


@pytest.mark.parametrize("bad", ["1789905840", None, 1.5, True])
def test_non_integer_epochs_are_rejected(document, bad):
    clone = _with(document, bad, "baseline", "metricWindow", "startEpoch")

    with pytest.raises(ManifestParseError, match="startEpoch must be an integer"):
        parse_manifest(clone)


@pytest.mark.parametrize("end", [1789905840, 1789905000])
def test_a_window_with_no_duration_is_rejected(document, end):
    clone = _with(document, end, "baseline", "metricWindow", "endEpoch")

    with pytest.raises(ManifestParseError, match="ends at or before it starts"):
        parse_manifest(clone)


def test_invalid_json_file_is_rejected(tmp_path):
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(ManifestParseError, match="not valid JSON"):
        parse_manifest_file(broken)


def test_the_fixture_is_labelled_as_a_fixture(document):
    """Repeated here so the label is a property of this boundary, not only of the glob."""
    assert "FIXTURE" in document["$fixture"].upper()
    assert "NO EXPERIMENT HAS BEEN RUN" in document["$fixture"]

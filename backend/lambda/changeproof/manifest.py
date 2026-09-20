"""Experiment manifest JSON -> ExperimentManifest.

Input is the document the experiment driver writes to
`.changeproof/experiments/<id>/experiment-manifest.json`. It carries no
measurements: it says which resources exist, how CloudWatch addresses them, and
over which two windows they should be queried.

This module is the engine's side of that boundary. It deliberately does not import
`workload.driver`: the driver's dataclasses are its own, and coupling the two would
put an AWS SDK on an import path the engine forbids. The manifest JSON is the
contract, so the JSON is what gets parsed.

The manifest is produced by a separate process, so it is treated as untrusted input:
shape is validated before it is read, exactly as `parser.py` treats plan JSON.

Only the fields the engine needs are exposed. Unknown keys are ignored, so the full
manifest -- workload spec, results, timestamps, drain flags, comparability, teardown
-- parses without this module knowing those keys exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestParseError(ValueError):
    """The manifest could not be read. Never downgraded to a warning."""


@dataclass(frozen=True)
class MetricDimension:
    """One CloudWatch dimension, in the casing the CloudWatch API uses."""

    name: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"Name": self.name, "Value": self.value}


@dataclass(frozen=True)
class CloudWatchTarget:
    """Where a resource's metrics live: a namespace and the dimensions to filter on."""

    namespace: str
    dimensions: tuple[MetricDimension, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


@dataclass(frozen=True)
class ManifestResource:
    """One resource in the experiment environment.

    `graph_address` is the Terraform address, which is also the node id in the
    dependency graph and `ObservedMetric.resource_address`. It is the join key
    across the whole system; `physical_name` changes per experiment and must never
    be used to key anything.
    """

    graph_address: str
    resource_type: str
    physical_name: str
    cloudwatch: CloudWatchTarget
    arn: str | None = None
    queue_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "graphAddress": self.graph_address,
            "resourceType": self.resource_type,
            "physicalName": self.physical_name,
            "cloudwatch": self.cloudwatch.to_dict(),
        }
        if self.arn:
            document["arn"] = self.arn
        if self.queue_url:
            document["queueUrl"] = self.queue_url
        return document


@dataclass(frozen=True)
class MetricWindow:
    """The period one run should be measured over.

    Already floored and ceilinged to whole minutes by the driver, with a tail past
    drain, because queue depth and message age peak after the producer stops.
    Consumers query this rather than the run's raw timestamps.
    """

    start_epoch: int
    end_epoch: int
    start_iso: str
    end_iso: str

    @property
    def duration_seconds(self) -> int:
        return self.end_epoch - self.start_epoch

    def to_dict(self) -> dict[str, Any]:
        return {
            "startIso": self.start_iso,
            "endIso": self.end_iso,
            "startEpoch": self.start_epoch,
            "endEpoch": self.end_epoch,
        }


@dataclass(frozen=True)
class ExperimentManifest:
    """What the engine needs from a completed experiment.

    Measurements are not here. This says what to ask CloudWatch about, and when.
    """

    experiment_id: str
    region: str
    resources: tuple[ManifestResource, ...]
    baseline_window: MetricWindow
    changed_window: MetricWindow

    @property
    def graph_addresses(self) -> tuple[str, ...]:
        return tuple(resource.graph_address for resource in self.resources)

    def resource(self, graph_address: str) -> ManifestResource:
        """Look a resource up by the join key.

        Raises rather than returning None: asking for an address the experiment did
        not contain is a programming error, not a condition to branch on.
        """
        for resource in self.resources:
            if resource.graph_address == graph_address:
                return resource
        known = ", ".join(self.graph_addresses)
        raise ManifestParseError(
            f"no resource at address {graph_address!r} in experiment "
            f"{self.experiment_id}; it has: {known}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise back into the manifest's own shape.

        Key paths match what the driver writes, so this output is valid input to
        `parse_manifest`. A flatter projection would mean the engine held two
        incompatible ideas of what a manifest looks like, and anything that wrote
        one back out would produce a document nothing could read.

        Only the subset the engine reads is reconstructed. Keys it ignores -- the
        workload spec, results, timestamps, drain flags, comparability, teardown --
        are not invented here.
        """
        return {
            "experimentId": self.experiment_id,
            "environment": {
                "experimentId": self.experiment_id,
                "region": self.region,
                "resources": [r.to_dict() for r in self.resources],
            },
            "baseline": {"metricWindow": self.baseline_window.to_dict()},
            "changed": {"metricWindow": self.changed_window.to_dict()},
        }


def parse_manifest_file(path: str | Path) -> ExperimentManifest:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestParseError(f"{path} is not valid JSON: {exc}") from exc
    return parse_manifest(document)


def parse_manifest(document: Any) -> ExperimentManifest:
    if not isinstance(document, dict):
        raise ManifestParseError(
            f"manifest must be a JSON object, got {type(document).__name__}"
        )

    experiment_id = _require_str(document, "experimentId", "manifest")

    environment = document.get("environment")
    if not isinstance(environment, dict):
        raise ManifestParseError("manifest is missing its 'environment' object")

    # The id appears at both levels. They describe the same experiment, so a
    # disagreement means the document was assembled from two different runs.
    nested_id = environment.get("experimentId")
    if isinstance(nested_id, str) and nested_id != experiment_id:
        raise ManifestParseError(
            f"experimentId disagrees between manifest ({experiment_id!r}) and "
            f"environment ({nested_id!r}); this manifest describes two experiments"
        )

    region = _require_str(environment, "region", "manifest.environment")

    raw_resources = environment.get("resources")
    if not isinstance(raw_resources, list) or not raw_resources:
        raise ManifestParseError(
            "manifest.environment.resources must be a non-empty list"
        )

    resources = tuple(
        _parse_resource(entry, index) for index, entry in enumerate(raw_resources)
    )

    seen: set[str] = set()
    for resource in resources:
        if resource.graph_address in seen:
            raise ManifestParseError(
                f"duplicate graphAddress {resource.graph_address!r}; it is the join "
                "key and must identify exactly one resource"
            )
        seen.add(resource.graph_address)

    return ExperimentManifest(
        experiment_id=experiment_id,
        region=region,
        resources=resources,
        baseline_window=_parse_window(document, "baseline"),
        changed_window=_parse_window(document, "changed"),
    )


def _parse_resource(entry: Any, index: int) -> ManifestResource:
    where = f"manifest.environment.resources[{index}]"
    if not isinstance(entry, dict):
        raise ManifestParseError(f"{where} must be an object")

    cloudwatch = entry.get("cloudwatch")
    if not isinstance(cloudwatch, dict):
        raise ManifestParseError(f"{where} is missing its 'cloudwatch' object")

    raw_dimensions = cloudwatch.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise ManifestParseError(f"{where}.cloudwatch.dimensions must be a non-empty list")

    dimensions = []
    for position, raw in enumerate(raw_dimensions):
        at = f"{where}.cloudwatch.dimensions[{position}]"
        if not isinstance(raw, dict):
            raise ManifestParseError(f"{at} must be an object")
        dimensions.append(
            MetricDimension(name=_require_str(raw, "Name", at), value=_require_str(raw, "Value", at))
        )

    return ManifestResource(
        graph_address=_require_str(entry, "graphAddress", where),
        resource_type=_require_str(entry, "resourceType", where),
        physical_name=_require_str(entry, "physicalName", where),
        cloudwatch=CloudWatchTarget(
            namespace=_require_str(cloudwatch, "namespace", f"{where}.cloudwatch"),
            dimensions=tuple(dimensions),
        ),
        arn=_optional_str(entry, "arn", where),
        queue_url=_optional_str(entry, "queueUrl", where),
    )


def _parse_window(document: dict[str, Any], label: str) -> MetricWindow:
    run = document.get(label)
    if not isinstance(run, dict):
        raise ManifestParseError(
            f"manifest is missing its {label!r} run; both runs are required, because "
            "one ObservedMetric carries a value from each"
        )

    where = f"manifest.{label}.metricWindow"
    window = run.get("metricWindow")
    if not isinstance(window, dict):
        raise ManifestParseError(f"manifest.{label} is missing its 'metricWindow' object")

    start_epoch = _require_int(window, "startEpoch", where)
    end_epoch = _require_int(window, "endEpoch", where)
    if end_epoch <= start_epoch:
        raise ManifestParseError(
            f"{where} ends at or before it starts ({start_epoch} -> {end_epoch}); "
            "a window with no duration cannot be queried"
        )

    return MetricWindow(
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        start_iso=_require_str(window, "startIso", where),
        end_iso=_require_str(window, "endIso", where),
    )


def _require_str(entry: dict[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestParseError(
            f"{where}.{key} must be a non-empty string, got {value!r}"
        )
    return value


def _require_int(entry: dict[str, Any], key: str, where: str) -> int:
    value = entry.get(key)
    # bool is an int subclass, and True would silently become epoch 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestParseError(
            f"{where}.{key} must be an integer, got {type(value).__name__}"
        )
    return value


def _optional_str(entry: dict[str, Any], key: str, where: str) -> str | None:
    if key not in entry or entry[key] is None:
        return None
    return _require_str(entry, key, where)

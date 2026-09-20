"""Amazon Neptune behind the `ports.GraphSource` seam.

Dependency relationships live in Neptune as `(:Resource)-[:DEPENDS_ON]->(:Resource)`.
The adapter reads them with openCypher and hands the rows to the engine's own
`DependencyGraph`, so the breadth-first walk, shortest-path rule and propagation
maths are the same code that runs locally. A parity test holds that to account.

Neptune is reachable only from inside its VPC, and it bills while idle. Nothing here
creates a cluster; `load_graph` fills one that already exists.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

from changeproof.graph import DependencyGraph
from changeproof.models import DependencyPath

#: (query, parameters) -> list of result rows, each a dict of column -> value.
Runner = Callable[[str, dict[str, Any]], list[dict[str, Any]]]

NODES_QUERY = (
    "MATCH (n:Resource) RETURN n.address AS address, n.resourceType AS resourceType, "
    "n.attributes AS attributes"
)
EDGES_QUERY = (
    "MATCH (a:Resource)-[r:DEPENDS_ON]->(b:Resource) RETURN a.address AS source, "
    "b.address AS target, r.relation AS relation, r.propagation AS propagation"
)


def signed_runner(endpoint: str, region: str, port: int = 8182) -> Runner:
    """openCypher over Neptune's HTTPS endpoint, signed with SigV4 (IAM auth)."""
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    credentials = boto3.Session().get_credentials()
    url = f"https://{endpoint}:{port}/openCypher"

    def run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        body = urllib.parse.urlencode({"query": query, "parameters": json.dumps(parameters)})
        request = AWSRequest(
            method="POST", url=url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        SigV4Auth(credentials, "neptune-db", region).add_auth(request)
        http = urllib.request.Request(url, data=body.encode(), headers=dict(request.headers), method="POST")
        with urllib.request.urlopen(http, timeout=20) as response:
            return json.loads(response.read())["results"]

    return run


class NeptuneGraphSource:
    """Satisfies `ports.GraphSource`."""

    def __init__(self, run: Runner) -> None:
        self._run = run
        self._graph: DependencyGraph | None = None

    def _load(self) -> DependencyGraph:
        if self._graph is None:
            nodes = [
                {
                    "address": row["address"],
                    "resourceType": row["resourceType"],
                    "attributes": json.loads(row["attributes"]) if row.get("attributes") else {},
                }
                for row in self._run(NODES_QUERY, {})
            ]
            edges = [
                {
                    "source": row["source"], "target": row["target"],
                    "relation": row["relation"], "propagation": row["propagation"],
                }
                for row in self._run(EDGES_QUERY, {})
            ]
            self._graph = DependencyGraph.from_dict({"nodes": nodes, "edges": edges})
        return self._graph

    def known_addresses(self) -> tuple[str, ...]:
        return self._load().known_addresses()

    def downstream_paths(self, origin: str, max_depth: int) -> tuple[DependencyPath, ...]:
        return self._load().downstream_paths(origin, max_depth)


def load_graph(run: Runner, document: dict[str, Any]) -> int:
    """Write a graph fixture into Neptune. Idempotent (MERGE). Returns statements run."""
    count = 0
    for node in document["nodes"]:
        run(
            "MERGE (n:Resource {address: $address}) "
            "SET n.resourceType = $resourceType, n.attributes = $attributes",
            {
                "address": node["address"],
                "resourceType": node["resourceType"],
                "attributes": json.dumps(node.get("attributes") or {}),
            },
        )
        count += 1
    for edge in document["edges"]:
        run(
            "MATCH (a:Resource {address: $source}), (b:Resource {address: $target}) "
            "MERGE (a)-[r:DEPENDS_ON {relation: $relation}]->(b) SET r.propagation = $propagation",
            {k: edge[k] for k in ("source", "target", "relation", "propagation")},
        )
        count += 1
    return count

"""NeptuneGraphSource against a fake openCypher runner. No cluster, no network.

The fake serves the same rows Neptune would return for the two read queries, taken
from the local fixture, so a parity test can show the AWS-backed source and the local
graph produce identical paths.
"""

from __future__ import annotations

import json

import pytest

from aws_adapters import neptune_graph as N
from changeproof.adapters.local import load_graph
from changeproof.ports import GraphSource


@pytest.fixture(scope="module")
def document(fixtures_dir):
    return json.loads((fixtures_dir / "dependency_graph.json").read_text(encoding="utf-8"))


class FakeNeptune:
    """Stores what load_graph writes and answers the two read queries from it."""

    def __init__(self):
        self.nodes, self.edges, self.writes = {}, [], []

    def __call__(self, query, params):
        if query == N.NODES_QUERY:
            return [{"address": a, **v} for a, v in self.nodes.items()]
        if query == N.EDGES_QUERY:
            return list(self.edges)
        self.writes.append((query, params))
        if "MERGE (n:Resource" in query:
            self.nodes[params["address"]] = {
                "resourceType": params["resourceType"], "attributes": params["attributes"]}
        else:
            self.edges.append(dict(params))
        return []


@pytest.fixture
def populated(document):
    fake = FakeNeptune()
    N.load_graph(fake, document)
    return fake


def test_satisfies_the_seam(populated):
    assert isinstance(N.NeptuneGraphSource(populated), GraphSource)


def test_loader_writes_every_node_and_edge(document, populated):
    assert len(populated.nodes) == len(document["nodes"])
    assert len(populated.edges) == len(document["edges"])


def test_loader_is_idempotent_by_using_merge(populated):
    assert all("MERGE" in q for q, _ in populated.writes)


def test_loader_never_interpolates_values_into_the_query(document, populated):
    for query, params in populated.writes:
        for value in params.values():
            assert str(value) not in query


def test_paths_match_the_local_graph_exactly(populated):
    local = load_graph()
    remote = N.NeptuneGraphSource(populated)
    assert set(remote.known_addresses()) == set(local.known_addresses())
    for origin in local.known_addresses():
        assert [p.to_dict() for p in remote.downstream_paths(origin, 5)] == \
               [p.to_dict() for p in local.downstream_paths(origin, 5)]


def test_the_graph_is_read_once(populated):
    reads = []
    source = N.NeptuneGraphSource(lambda q, p: reads.append(q) or populated(q, p))
    source.known_addresses(); source.downstream_paths("aws_lambda_function.api", 3)
    assert reads.count(N.NODES_QUERY) == 1 and reads.count(N.EDGES_QUERY) == 1


def test_an_empty_neptune_yields_an_empty_graph_not_a_guess():
    source = N.NeptuneGraphSource(lambda q, p: [])
    assert source.known_addresses() == ()

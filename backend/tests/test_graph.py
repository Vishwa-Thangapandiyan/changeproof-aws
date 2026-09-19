from __future__ import annotations

import pytest


def _graph(edges):
    from changeproof.graph import DependencyGraph

    addresses = {e["source"] for e in edges} | {e["target"] for e in edges}
    return DependencyGraph.from_dict(
        {
            "nodes": [
                {"address": a, "resourceType": "aws_lambda_function"} for a in sorted(addresses)
            ],
            "edges": edges,
        }
    )


def test_fixture_graph_loads(graph):
    assert set(graph.known_addresses()) == {
        "aws_lambda_function.api",
        "aws_sqs_queue.work",
        "aws_lambda_function.worker",
        "aws_dynamodb_table.orders",
    }


def test_downstream_paths_follow_the_demo_chain(graph):
    paths = graph.downstream_paths("aws_lambda_function.api", max_depth=4)
    by_target = {p.target: p for p in paths}

    assert by_target["aws_sqs_queue.work"].depth == 1
    assert by_target["aws_lambda_function.worker"].depth == 2
    assert by_target["aws_dynamodb_table.orders"].depth == 3


def test_propagation_is_the_product_of_hop_factors(graph):
    paths = {p.target: p for p in graph.downstream_paths("aws_lambda_function.api", 4)}

    assert paths["aws_sqs_queue.work"].propagation == pytest.approx(0.9)
    assert paths["aws_lambda_function.worker"].propagation == pytest.approx(0.9 * 0.95)
    assert paths["aws_dynamodb_table.orders"].propagation == pytest.approx(0.9 * 0.95 * 0.8)


def test_propagation_decreases_with_depth(graph):
    paths = sorted(graph.downstream_paths("aws_lambda_function.api", 4), key=lambda p: p.depth)
    factors = [p.propagation for p in paths]

    assert factors == sorted(factors, reverse=True)


def test_max_depth_truncates_traversal(graph):
    paths = graph.downstream_paths("aws_lambda_function.api", max_depth=1)

    assert [p.target for p in paths] == ["aws_sqs_queue.work"]


def test_zero_depth_returns_nothing(graph):
    assert graph.downstream_paths("aws_lambda_function.api", max_depth=0) == ()


def test_terminal_node_has_no_downstream(graph):
    assert graph.downstream_paths("aws_dynamodb_table.orders", max_depth=4) == ()


def test_cycles_terminate():
    graph = _graph(
        [
            {"source": "a", "target": "b", "relation": "calls", "propagation": 0.9},
            {"source": "b", "target": "c", "relation": "calls", "propagation": 0.9},
            {"source": "c", "target": "a", "relation": "calls", "propagation": 0.9},
        ]
    )

    targets = {p.target for p in graph.downstream_paths("a", max_depth=10)}

    assert targets == {"b", "c"}


def test_blast_radius_includes_origins(graph):
    radius = graph.blast_radius(("aws_lambda_function.api",), 4)

    assert radius.affected_addresses[0] == "aws_lambda_function.api"
    assert len(radius.affected_addresses) == 4
    assert radius.to_dict()["resourceCount"] == 4


def test_blast_radius_keeps_the_strongest_path_to_a_shared_target():
    graph = _graph(
        [
            {"source": "weak", "target": "shared", "relation": "calls", "propagation": 0.2},
            {"source": "strong", "target": "shared", "relation": "calls", "propagation": 0.95},
        ]
    )

    radius = graph.blast_radius(("weak", "strong"), 4)
    path = next(p for p in radius.paths if p.target == "shared")

    assert path.origin == "strong"
    assert path.propagation == pytest.approx(0.95)


def test_unknown_origin_raises(graph):
    from changeproof.graph import GraphError

    with pytest.raises(GraphError, match="not present in the dependency graph"):
        graph.downstream_paths("aws_lambda_function.nope", max_depth=1)


@pytest.mark.parametrize(
    "document, expected",
    [
        ("nope", "must be an object"),
        ({"nodes": {}, "edges": []}, "'nodes' must be a list"),
        ({"nodes": [], "edges": {}}, "'edges' must be a list"),
        ({"nodes": [{"address": "a"}], "edges": []}, "needs string 'address' and 'resourceType'"),
    ],
)
def test_malformed_graph_raises(document, expected):
    from changeproof.graph import DependencyGraph, GraphError

    with pytest.raises(GraphError, match=expected):
        DependencyGraph.from_dict(document)


def test_duplicate_node_raises():
    from changeproof.graph import DependencyGraph, GraphError

    document = {
        "nodes": [
            {"address": "a", "resourceType": "aws_lambda_function"},
            {"address": "a", "resourceType": "aws_lambda_function"},
        ],
        "edges": [],
    }

    with pytest.raises(GraphError, match="duplicate node address"):
        DependencyGraph.from_dict(document)


def test_edge_to_unknown_node_raises():
    from changeproof.graph import DependencyGraph, GraphError

    document = {
        "nodes": [{"address": "a", "resourceType": "aws_lambda_function"}],
        "edges": [{"source": "a", "target": "ghost", "relation": "calls", "propagation": 0.9}],
    }

    with pytest.raises(GraphError, match="unknown node 'ghost'"):
        DependencyGraph.from_dict(document)


@pytest.mark.parametrize("propagation", [0, -0.5, 1.5, "0.9", None])
def test_invalid_propagation_raises(propagation):
    from changeproof.graph import DependencyGraph, GraphError

    document = {
        "nodes": [
            {"address": "a", "resourceType": "aws_lambda_function"},
            {"address": "b", "resourceType": "aws_lambda_function"},
        ],
        "edges": [{"source": "a", "target": "b", "relation": "calls", "propagation": propagation}],
    }

    with pytest.raises(GraphError, match="propagation must be a number"):
        DependencyGraph.from_dict(document)

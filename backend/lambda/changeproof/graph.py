"""Dependency graph traversal.

The blast radius of a change is every resource reachable downstream of it, together
with how strongly pressure propagates along each path. Propagation factors live on
the edges as data, not as constants in the traversal code, so the graph fixture and
a future Neptune backend express the same model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import BlastRadius, DependencyHop, DependencyPath


class GraphError(ValueError):
    """The graph data is malformed or a lookup failed."""


@dataclass(frozen=True)
class GraphNode:
    address: str
    resource_type: str
    attributes: dict[str, Any]


class DependencyGraph:
    """An in-memory directed graph of infrastructure dependencies.

    Satisfies the `GraphSource` port. Neptune replaces this class later; the two
    methods that the engine depends on are the whole surface.
    """

    def __init__(self, nodes: dict[str, GraphNode], edges: tuple[DependencyHop, ...]) -> None:
        self._nodes = nodes
        self._edges = edges
        self._outgoing: dict[str, list[DependencyHop]] = {}
        for edge in edges:
            self._outgoing.setdefault(edge.source, []).append(edge)

    @classmethod
    def from_dict(cls, document: Any) -> "DependencyGraph":
        if not isinstance(document, dict):
            raise GraphError("graph document must be an object")

        raw_nodes = document.get("nodes")
        if not isinstance(raw_nodes, list):
            raise GraphError("graph 'nodes' must be a list")

        nodes: dict[str, GraphNode] = {}
        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                raise GraphError(f"nodes[{index}] must be an object")
            address = raw.get("address")
            resource_type = raw.get("resourceType")
            if not isinstance(address, str) or not isinstance(resource_type, str):
                raise GraphError(f"nodes[{index}] needs string 'address' and 'resourceType'")
            if address in nodes:
                raise GraphError(f"duplicate node address {address!r}")
            attributes = raw.get("attributes") or {}
            if not isinstance(attributes, dict):
                raise GraphError(f"nodes[{index}].attributes must be an object")
            nodes[address] = GraphNode(address, resource_type, attributes)

        raw_edges = document.get("edges")
        if not isinstance(raw_edges, list):
            raise GraphError("graph 'edges' must be a list")

        edges: list[DependencyHop] = []
        for index, raw in enumerate(raw_edges):
            if not isinstance(raw, dict):
                raise GraphError(f"edges[{index}] must be an object")
            source = raw.get("source")
            target = raw.get("target")
            relation = raw.get("relation")
            propagation = raw.get("propagation")
            if not isinstance(source, str) or not isinstance(target, str):
                raise GraphError(f"edges[{index}] needs string 'source' and 'target'")
            if not isinstance(relation, str):
                raise GraphError(f"edges[{index}].relation must be a string")
            if not isinstance(propagation, (int, float)) or not 0.0 < propagation <= 1.0:
                raise GraphError(
                    f"edges[{index}].propagation must be a number in (0, 1], got {propagation!r}"
                )
            for endpoint in (source, target):
                if endpoint not in nodes:
                    raise GraphError(f"edges[{index}] references unknown node {endpoint!r}")
            edges.append(DependencyHop(source, target, relation, float(propagation)))

        return cls(nodes, tuple(edges))

    def node(self, address: str) -> GraphNode:
        try:
            return self._nodes[address]
        except KeyError:
            raise GraphError(f"resource {address!r} is not present in the dependency graph") from None

    def known_addresses(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def downstream_paths(self, origin: str, max_depth: int) -> tuple[DependencyPath, ...]:
        """Shortest path from `origin` to each resource reachable downstream of it.

        Breadth-first, so the first path found to a target is the shortest one. Cycles
        terminate because a node is only expanded once.
        """
        self.node(origin)
        if max_depth < 1:
            return ()

        paths: list[DependencyPath] = []
        visited = {origin}
        frontier: list[tuple[str, tuple[DependencyHop, ...]]] = [(origin, ())]

        while frontier:
            current, hops = frontier.pop(0)
            if len(hops) >= max_depth:
                continue
            for edge in self._outgoing.get(current, []):
                if edge.target in visited:
                    continue
                visited.add(edge.target)
                extended = hops + (edge,)
                paths.append(DependencyPath(origin=origin, target=edge.target, hops=extended))
                frontier.append((edge.target, extended))

        return tuple(paths)

    def blast_radius(self, origins: tuple[str, ...], max_depth: int) -> BlastRadius:
        """Combined downstream reach of several changed resources.

        Where two origins reach the same target, the path with the stronger
        propagation is kept, because the worst case is what the prediction must cover.
        """
        strongest: dict[str, DependencyPath] = {}
        for origin in origins:
            for path in self.downstream_paths(origin, max_depth):
                existing = strongest.get(path.target)
                if existing is None or path.propagation > existing.propagation:
                    strongest[path.target] = path

        ordered = tuple(sorted(strongest.values(), key=lambda p: (p.depth, p.target)))
        return BlastRadius(origin_addresses=origins, paths=ordered)

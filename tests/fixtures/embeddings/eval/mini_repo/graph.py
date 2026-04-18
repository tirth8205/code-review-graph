"""Mini graph store for MRR eval fixture."""

from collections import deque


class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def add_node(self, name):
        self.nodes[name] = name
        self.edges.setdefault(name, [])

    def add_edge(self, src, dst):
        self.edges.setdefault(src, []).append(dst)

    def neighbors(self, name):
        return list(self.edges.get(name, []))

    def impact_radius(self, start, depth):
        visited = set()
        queue = deque([(start, 0)])
        result = []
        while queue:
            node, d = queue.popleft()
            if node in visited or d > depth:
                continue
            visited.add(node)
            result.append(node)
            for nbr in self.edges.get(node, []):
                queue.append((nbr, d + 1))
        return result

"""Same-named TypeScript object methods in one file keep a node each (#585).

Two object literals with a ``handle()`` method give two definitions with the
same qualified name. The second is stored as ``<base>:S<hash>``, and both keep
their identity when lines above them move.
"""

from __future__ import annotations

import re
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo

SOURCE = (
    "export const routes = [\n"
    "  { handle() { return 'a'; } },\n"
    "  { handle() { return 'b'; } },\n"
    "];\n"
    "export const savers = [\n"
    "  { save(a: string) { return a; } },\n"
    "  { save(a: number) { return a; } },\n"
    "];\n"
)

_SUFFIX = re.compile(r":S[0-9a-f]{12}(:\d+)?")


def _index(store: GraphStore, path: Path, source: str) -> dict[int, str]:
    """Write and store *path*; return line -> qualified name of each method."""
    path.write_text(source, encoding="utf-8")
    nodes, edges = CodeParser().parse_file(path)
    store.store_file_nodes_edges(str(path), nodes, edges)
    return {
        node.line_start: node.qualified_name
        for node in store.get_nodes_by_file(str(path))
        if node.name in ("handle", "save")
    }


def test_duplicate_methods_keep_separate_nodes_and_identities(tmp_path):
    path = tmp_path / "routes.ts"
    store = GraphStore(tmp_path / "graph.db")
    try:
        before = _index(store, path, SOURCE)

        assert sorted(before) == [2, 3, 6, 7]
        base = f"{path.as_posix()}::handle"
        assert before[2] == base
        assert before[3].startswith(base) and _SUFFIX.fullmatch(before[3][len(base):])
        # Different parameters are still one base name, told apart by the suffix.
        assert before[6] != before[7]
        assert all(name.startswith(f"{path.as_posix()}::save") for name in (before[6], before[7]))

        # A caller pointed at the second ``handle`` keeps it after a line shift.
        caller_file = str(tmp_path / "main.ts")
        store.store_file_nodes_edges(
            caller_file,
            [NodeInfo(kind="Function", name="run", file_path=caller_file,
                      line_start=1, line_end=3, language="typescript")],
            [EdgeInfo(kind="CALLS", source=f"{caller_file}::run", target=before[3],
                      file_path=caller_file, line=2)],
        )

        after = _index(store, path, "// moved down one line\n" + SOURCE)

        assert sorted(after) == [3, 4, 7, 8]
        assert [after[line] for line in sorted(after)] == [
            before[line] for line in sorted(before)
        ]
        callers = [
            edge.source_qualified for edge in store.get_edges_by_target(before[3])
            if edge.kind == "CALLS"
        ]
        assert callers == [f"{caller_file}::run"]
    finally:
        store.close()

"""C# outer bindings must survive the process pool (issue #1214).

A call from a nested class to a field of its outer class carries a
``parser._OuterBinding`` in ``extra["receiver_type"]``. The process-pool
parser pickles every worker result, so the value has to round-trip with its
``levels``; before the fix the parent failed with ``TypeError`` and the pool
broke with ``BrokenProcessPool``.
"""

import pickle
from pathlib import Path
from unittest import mock

from code_review_graph import incremental
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, _OuterBinding

_SOURCE = """\
namespace Demo;

public class Service { public void Run() { } }

public class Outer
{
    private static Service _service = new Service();

    public class Inner
    {
        public void Call() { _service.Run(); }
    }
}
"""


def test_outer_binding_pickle_round_trip_keeps_value_and_levels():
    restored = pickle.loads(pickle.dumps(_OuterBinding("Service", 2)))

    assert isinstance(restored, _OuterBinding)
    assert restored == "Service"
    assert restored.levels == 2


def test_parsed_nested_call_edge_pickles_with_its_receiver_levels(tmp_path):
    path = tmp_path / "Outer.cs"
    path.write_text(_SOURCE, encoding="utf-8")

    nodes, edges = CodeParser(tmp_path).parse_bytes(path, _SOURCE.encode())
    receivers = [
        edge.extra["receiver_type"]
        for edge in edges
        if edge.kind == "CALLS" and "receiver_type" in edge.extra
    ]
    assert any(isinstance(receiver, _OuterBinding) for receiver in receivers)

    restored_nodes, restored_edges = pickle.loads(pickle.dumps((nodes, edges)))

    restored = [
        edge.extra["receiver_type"]
        for edge in restored_edges
        if edge.kind == "CALLS" and "receiver_type" in edge.extra
    ]
    assert [(str(r), getattr(r, "levels", 0)) for r in restored] == [
        (str(r), getattr(r, "levels", 0)) for r in receivers
    ]
    assert len(restored_nodes) == len(nodes)


def test_full_build_with_process_pool_survives_nested_outer_field_call(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "process")
    monkeypatch.delenv("CRG_SERIAL_PARSE", raising=False)
    # Eight or more files take the parallel path.
    for index in range(9):
        (tmp_path / f"O{index}.cs").write_text(
            _SOURCE.replace("Outer", f"Outer{index}").replace("Service", f"Service{index}"),
            encoding="utf-8",
        )

    # The executor is built only on the parallel path; CRG_PARSE_EXECUTOR=process
    # makes it a process pool, so a serial fallback would fail this test.
    with mock.patch.object(
        incremental, "_make_executor", wraps=incremental._make_executor,
    ) as make_executor:
        with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
            result = full_build(tmp_path, store)

    make_executor.assert_called_once()
    assert result["files_parsed"] == 9
    assert result["errors"] == []

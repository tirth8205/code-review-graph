"""Dead-code detection must honour C# polymorphic dispatch (PR #964)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.refactor import find_dead_code


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "INHERITS targets stay bare while C# parent_name now carries "
        "the namespace, so the override check never matches (#964)"
    ),
)
def test_dead_code_keeps_csharp_overrides_of_called_base_members(tmp_path):
    # Callers of ISink.Emit / SinkBase.Flush reach every implementation at
    # run time. find_dead_code honours that by matching the INHERITS target
    # against the base member's parent_name; a namespace in parent_name must
    # not turn every C# implementation into "unused" code.
    (tmp_path / ".git").mkdir()
    _write(tmp_path, {
        "Contracts.cs": (
            "namespace Lib {\n"
            "  public interface ISink { void Emit(); }\n"
            "  public abstract class SinkBase { public abstract void Flush(); }\n"
            "}\n"
        ),
        "Sinks/Sinks.cs": (
            "using Lib;\n"
            "namespace Lib.Sinks {\n"
            "  public class ConsoleSink : ISink { public void Emit() {} }\n"
            "  public class FileSink : SinkBase { public override void Flush() {} }\n"
            "}\n"
        ),
        "Logger.cs": (
            "namespace Lib {\n"
            "  public class Logger {\n"
            "    private readonly ISink _sink;\n"
            "    private readonly SinkBase _base;\n"
            "    public void Write() { _sink.Emit(); _base.Flush(); }\n"
            "  }\n"
            "}\n"
        ),
    })
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        dead_functions = {
            d["name"] for d in find_dead_code(store) if d["kind"] == "Function"
        }

    assert "Emit" not in dead_functions
    assert "Flush" not in dead_functions

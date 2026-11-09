"""Dead-code detection must honour C# polymorphic dispatch (PR #964)."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.refactor import find_dead_code


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


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


def test_dead_code_still_reports_a_csharp_namesake_that_overrides_nothing(tmp_path):
    # Matching the base by its namespaced parent must not keep every same-named
    # method alive: a type with no INHERITS edge to the called base stays dead.
    (tmp_path / ".git").mkdir()
    _write(tmp_path, {
        "Contracts.cs": "namespace Lib { public interface ISink { void Emit(); } }\n",
        "Loose.cs": "namespace Lib.Other { public class Loose { public void Emit() {} } }\n",
        "Logger.cs": (
            "namespace Lib { public class Logger { private readonly ISink _sink;"
            " public void Write() { _sink.Emit(); } } }\n"
        ),
    })
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        dead = {
            d["qualified_name"].rsplit("::", 1)[-1]
            for d in find_dead_code(store) if d["kind"] == "Function"
        }

    assert "Lib.Other.Loose.Emit" in dead

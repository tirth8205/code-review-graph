"""Regression coverage for C++ and Qt header indexing (issue #463)."""

import shutil
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).parent / "fixtures" / "cpp_qt_headers"

QT_HEADER = """#pragma once
#include <QMainWindow>

QT_BEGIN_NAMESPACE namespace Ui { class MyWidgetClass; };
QT_END_NAMESPACE

class MyWidget : public QMainWindow {
  Q_OBJECT

 public:
  MyWidget(QWidget* parent = nullptr);
  ~MyWidget();

 protected Q_SLOTS:
  void onButtonClicked();

 public Q_SLOTS:
  void onReset();

 Q_SIGNALS:
  void dataReady(int result);
  void errorOccurred(const QString& msg);
};
"""


def _parse(tmp_path: Path, name: str, source: str):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path, *CodeParser().parse_file(path)


def _file_language(nodes) -> str:
    return next(node.language for node in nodes if node.kind == "File")


def test_h_file_uses_cpp_when_source_has_strong_cpp_evidence(tmp_path: Path) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "MyWidgetPlain.h",
        """#pragma once

class MyWidgetPlain {
 public:
  void reset();
};
""",
    )

    assert _file_language(nodes) == "cpp"
    assert any(node.kind == "Class" and node.name == "MyWidgetPlain" for node in nodes)


def test_h_file_without_cpp_evidence_remains_c(tmp_path: Path) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "plain.h",
        """#pragma once

typedef struct record {
  int value;
} record;

int read_record(const record *value);
""",
    )

    assert _file_language(nodes) == "c"


def test_c_header_cpp_compatibility_guard_is_not_cpp_evidence(tmp_path: Path) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "compat.h",
        """#pragma once

#ifdef __cplusplus
extern "C" {
#endif

int library_version(void);

#ifdef __cplusplus
}
#endif
""",
    )

    assert _file_language(nodes) == "c"


def test_h_file_uses_cpp_for_scoped_enums_and_modern_function_syntax(
    tmp_path: Path,
) -> None:
    sources = {
        "ScopedEnum.h": "enum class Color { Red, Blue };\n",
        "Constexpr.h": "constexpr int answer() noexcept;\n",
        "TrailingReturn.h": "auto answer() -> int;\n",
    }

    for name, source in sources.items():
        _, nodes, _ = _parse(tmp_path, name, source)
        assert _file_language(nodes) == "cpp", name


def test_inactive_or_recovered_cpp_syntax_does_not_promote_c_headers(
    tmp_path: Path,
) -> None:
    sources = {
        "disabled.h": """#if 0
class Disabled {};
#endif
typedef int value;
""",
        "objective_c.h": """@class Forward;
typedef int value;
""",
    }

    for name, source in sources.items():
        _, nodes, _ = _parse(tmp_path, name, source)
        assert _file_language(nodes) == "c", name


def test_c_auto_and_c23_constexpr_are_not_cpp_evidence(tmp_path: Path) -> None:
    sources = {
        "auto_storage.h": "auto int value;\n",
        "c23_constexpr.h": """constexpr int limit = 16;
typedef struct item {
  int value;
} item;
""",
    }

    for name, source in sources.items():
        _, nodes, _ = _parse(tmp_path, name, source)
        assert _file_language(nodes) == "c", name


def test_qt_structural_macros_do_not_hide_classes_or_become_functions(
    tmp_path: Path,
) -> None:
    _, nodes, _ = _parse(tmp_path, "MyWidget.hpp", QT_HEADER)

    class_names = {node.name for node in nodes if node.kind == "Class"}
    function_names = {node.name for node in nodes if node.kind == "Function"}

    assert {"MyWidget", "MyWidgetClass"} <= class_names
    assert function_names.isdisjoint({
        "QT_BEGIN_NAMESPACE",
        "QT_END_NAMESPACE",
        "Q_OBJECT",
        "Q_SLOTS",
        "Q_SIGNALS",
    })


def test_qt_macro_shielding_preserves_class_source_span(tmp_path: Path) -> None:
    _, nodes, _ = _parse(tmp_path, "MyWidget.hpp", QT_HEADER)

    widget = next(
        node for node in nodes if node.kind == "Class" and node.name == "MyWidget"
    )
    assert (widget.line_start, widget.line_end) == (7, 23)


def test_qt_macro_shielding_leaves_literals_comments_and_directives_unchanged(
    tmp_path: Path,
) -> None:
    source = b'''#define Q_OBJECT custom_object
#include "Q_OBJECT"
constexpr auto marker = R"tag(Q_SIGNALS \" Q_EMIT)tag";
// Q_SLOTS
/* QT_BEGIN_NAMESPACE */
/* multiline comment
*/#define Q_SIGNALS custom_signals
class Widget {
  Q_OBJECT
 Q_SIGNALS:
  void ready();
};
'''

    masked = CodeParser._mask_cpp_qt_macros(source)

    assert len(masked) == len(source)
    assert masked.splitlines()[:5] == source.splitlines()[:5]
    assert b"*/#define Q_SIGNALS custom_signals" in masked
    assert b"  Q_OBJECT\n" not in masked
    assert b" Q_SIGNALS:\n" not in masked

    path = tmp_path / "Widget.hpp"
    nodes, edges = CodeParser().parse_bytes(path, source)
    imports = [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]
    assert imports == ["Q_OBJECT"]
    assert any(node.kind == "Class" and node.name == "Widget" for node in nodes)


def test_cpp_callable_declarations_are_indexed_without_variables(tmp_path: Path) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "Widget.hpp",
        """class Widget {
 public:
  Widget();
  ~Widget();
  void reset();
  int value() const;
  int count;
};

void top_level(int value);
extern int global_value;
""",
    )

    function_names = [node.name for node in nodes if node.kind == "Function"]
    assert function_names == ["Widget", "~Widget", "reset", "value", "top_level"]
    assert "count" not in function_names
    assert "global_value" not in function_names


def test_cpp_function_pointer_variables_are_not_indexed_as_functions(
    tmp_path: Path,
) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "Callbacks.hpp",
        """class Callbacks {
 public:
  void run(int value);
  void (*callback)(int);
  void (Callbacks::*handler)(int);
};

void free_function(int value);
void (*global_callback)(int);
void (*factory())(int);
""",
    )

    function_names = [node.name for node in nodes if node.kind == "Function"]
    assert function_names == ["run", "free_function", "factory"]
    factory = next(node for node in nodes if node.name == "factory")
    assert factory.identity_name == "factory()"
    assert factory.params == "()"


def test_cpp_local_function_prototypes_are_not_indexed(tmp_path: Path) -> None:
    _, nodes, _ = _parse(
        tmp_path,
        "LocalPrototype.cpp",
        """void outer() {
  void local_prototype(int value);
  local_prototype(1);
}
""",
    )

    function_names = [node.name for node in nodes if node.kind == "Function"]
    assert function_names == ["outer"]


def test_qt_member_declarations_survive_macro_shielding(tmp_path: Path) -> None:
    _, nodes, _ = _parse(tmp_path, "MyWidget.hpp", QT_HEADER)

    function_names = {node.name for node in nodes if node.kind == "Function"}
    assert {
        "MyWidget",
        "~MyWidget",
        "onButtonClicked",
        "onReset",
        "dataReady",
        "errorOccurred",
    } <= function_names


def test_four_file_cpp_qt_fixture_survives_full_build(tmp_path: Path) -> None:
    for fixture in FIXTURES.iterdir():
        shutil.copy2(fixture, tmp_path / fixture.name)

    expected_functions = {
        "MyWidgetPlain.h": {
            "MyWidgetPlain",
            "~MyWidgetPlain",
            "doSomething",
            "calculateValue",
            "onButtonClicked",
            "onDataReceived",
            "onReset",
        },
        "MyWidgetPlain.cpp": {
            "MyWidgetPlain",
            "~MyWidgetPlain",
            "doSomething",
            "calculateValue",
            "onButtonClicked",
            "onDataReceived",
            "onReset",
        },
        "MyWidget.h": {
            "MyWidget",
            "~MyWidget",
            "doSomething",
            "calculateValue",
            "onButtonClicked",
            "onDataReceived",
            "onReset",
            "dataReady",
            "errorOccurred",
        },
        "MyWidget.cpp": {
            "MyWidget",
            "~MyWidget",
            "doSomething",
            "calculateValue",
            "onButtonClicked",
            "onDataReceived",
            "onReset",
        },
    }

    with GraphStore(":memory:") as store:
        result = full_build(tmp_path, store)

        assert result["errors"] == []
        assert result["files_parsed"] == 4
        for filename, expected in expected_functions.items():
            path = tmp_path / filename
            stored = store.get_nodes_by_file(str(path))
            assert {node.name for node in stored if node.kind == "Function"} == expected
            assert all(node.language == "cpp" for node in stored)

        qt_header_nodes = store.get_nodes_by_file(str(tmp_path / "MyWidget.h"))
        widget = next(
            node
            for node in qt_header_nodes
            if node.kind == "Class" and node.name == "MyWidget"
        )
        assert (widget.line_start, widget.line_end) == (7, 26)
        assert all(
            node.name
            not in {
                "QT_BEGIN_NAMESPACE",
                "QT_END_NAMESPACE",
                "Q_OBJECT",
                "Q_SLOTS",
                "Q_SIGNALS",
                "Q_EMIT",
            }
            for node in store.get_all_nodes()
        )

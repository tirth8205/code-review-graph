"""A rename rewrites the import statements that name the symbol (#1107).

``rename_preview`` used to look for import sites through ``IMPORTS_FROM``
edges whose target was the symbol's qualified name. The Python parser points
those edges at the imported *module file*, so no real import ever matched:
renaming ``helper`` rewrote the definition and the calls but left
``from pkg.util import helper`` alone, and the applied code raised
``ImportError``. The unit fixture hand-built a ``file::symbol`` edge, which
is why it never noticed.

Every test here builds the graph with the real parser, previews and applies
the rename through the same functions the MCP tools call, and then checks
the files on disk. The tests that matter most import the package in a fresh
interpreter afterwards: the rename is only right if the code still runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.refactor import (
    _pending_refactors,
    _refactor_lock,
    apply_refactor,
)
from code_review_graph.tools.refactor_tools import apply_refactor_func, refactor_func


@pytest.fixture(autouse=True)
def _clear_pending():
    with _refactor_lock:
        _pending_refactors.clear()
    yield
    with _refactor_lock:
        _pending_refactors.clear()


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_bytes(content.encode("utf-8"))


def _build(root: Path) -> None:
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        full_build(root, store)
    finally:
        store.close()


def _repo(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, files)
    _build(root)
    return root


def _rename(root: Path, old: str, new: str) -> dict:
    preview = refactor_func(
        mode="rename", old_name=old, new_name=new, repo_root=str(root), max_results=150,
    )
    assert preview["status"] == "ok", preview
    return preview


def _apply(root: Path, preview: dict) -> dict:
    result = apply_refactor_func(preview["refactor_id"], repo_root=str(root))
    assert result["status"] == "ok", result
    return result


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


def _run(root: Path, code: str) -> str:
    """Run *code* in a fresh interpreter with the scratch repo importable."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env.pop("PYTHONSTARTUP", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestIssueReproduction:
    """The exact shape from #1107: build, preview, apply, import."""

    def test_renamed_package_still_imports_and_calls(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(x):\n    return x * 2\n",
            "pkg/main.py": (
                "from pkg.util import helper\n"
                "\n"
                "\n"
                "def main():\n"
                "    return [helper(i) for i in range(3)]\n"
            ),
        })

        preview = _rename(root, "helper", "double")
        sites = {(Path(e["file"]).name, e["line"]) for e in preview["edits"]}
        assert ("main.py", 1) in sites, preview["edits"]

        result = _apply(root, preview)
        assert result["edits_applied"] == 3

        assert _read(root, "pkg/main.py").startswith("from pkg.util import double\n")
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "[0, 2, 4]"
        assert _run(root, "from pkg.util import double; print(double(21))") == "42"


def _rename_and_apply(root: Path, old: str, new: str) -> dict:
    preview = _rename(root, old, new)
    _apply(root, preview)
    return preview


_UTIL = (
    "def helper(x):\n"
    "    return x * 2\n"
    "\n"
    "\n"
    "def a():\n"
    "    return 'a'\n"
    "\n"
    "\n"
    "def b():\n"
    "    return 'b'\n"
    "\n"
    "\n"
    "class Widget:\n"
    "    def render(self):\n"
    "        return 'w'\n"
)


class TestPythonImportForms:
    """Each import spelling is rewritten, and only the imported name."""

    def test_name_among_others_on_one_line(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import a, helper, b\n"
                "\n"
                "\n"
                "def main():\n"
                "    return a() + b() + str(helper(2))\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import a, double, b\n"
            "\n"
            "\n"
            "def main():\n"
            "    return a() + b() + str(double(2))\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "ab4"

    def test_parenthesized_multiline_import_with_comments(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import (\n"
                "    a,  # helper is below\n"
                "    helper,  # the helper\n"
                "    b,\n"
                ")\n"
                "\n"
                "\n"
                "def main():\n"
                "    return a() + b() + str(helper(2))\n"
            ),
        })
        preview = _rename_and_apply(root, "helper", "double")
        imports = [e for e in preview["edits"] if e["site"] == "import"]
        assert [(e["line"], e["column"]) for e in imports] == [(3, 5)]
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import (\n"
            "    a,  # helper is below\n"
            "    double,  # the helper\n"
            "    b,\n"
            ")\n"
            "\n"
            "\n"
            "def main():\n"
            "    return a() + b() + str(double(2))\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "ab4"

    def test_alias_keeps_its_name_and_its_uses(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import helper as h\n"
                "\n"
                "\n"
                "def main():\n"
                "    return h(5)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import double as h\n"
            "\n"
            "\n"
            "def main():\n"
            "    return h(5)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "10"

    def test_relative_imports_one_and_two_levels_up(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/near.py": "from .util import helper\n\nNEAR = helper(1)\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/far.py": "from ..util import helper\n\nFAR = helper(2)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/near.py") == "from .util import double\n\nNEAR = double(1)\n"
        assert _read(root, "pkg/sub/far.py") == (
            "from ..util import double\n\nFAR = double(2)\n"
        )
        assert _run(
            root, "import pkg.near, pkg.sub.far; print(pkg.near.NEAR, pkg.sub.far.FAR)"
        ) == "2 4"

    def test_package_reexport_and_all(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": (
                "from .util import Widget, helper\n"
                "\n"
                '__all__ = ["Widget", "helper"]\n'
            ),
            "pkg/util.py": _UTIL,
            "app/__init__.py": "",
            "app/direct.py": "from pkg import helper\n\nVALUE = helper(3)\n",
            "app/star.py": "from pkg import *\n\nVALUE = helper(4)\n",
            "app/attr.py": "import pkg\n\nVALUE = pkg.helper(5)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/__init__.py") == (
            "from .util import Widget, double\n"
            "\n"
            '__all__ = ["Widget", "double"]\n'
        )
        assert _read(root, "app/direct.py") == "from pkg import double\n\nVALUE = double(3)\n"
        assert _read(root, "app/star.py") == "from pkg import *\n\nVALUE = double(4)\n"
        assert _read(root, "app/attr.py") == "import pkg\n\nVALUE = pkg.double(5)\n"
        assert _run(root, (
            "import app.direct, app.star, app.attr, pkg; "
            "print(app.direct.VALUE, app.star.VALUE, app.attr.VALUE, pkg.__all__)"
        )) == "6 8 10 ['Widget', 'double']"

    def test_module_import_then_attribute_access(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "import pkg.util\n"
                "import pkg.util as pu\n"
                "from pkg import util\n"
                "\n"
                "REF = util.helper\n"
                "\n"
                "\n"
                "def main():\n"
                "    return pkg.util.helper(1) + pu.helper(2) + REF(3)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "import pkg.util\n"
            "import pkg.util as pu\n"
            "from pkg import util\n"
            "\n"
            "REF = util.double\n"
            "\n"
            "\n"
            "def main():\n"
            "    return pkg.util.double(1) + pu.double(2) + REF(3)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "12"

    def test_import_inside_a_function_and_non_call_uses(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import helper\n"
                "\n"
                "CALLBACKS = [helper]\n"
                "\n"
                "\n"
                "@staticmethod\n"
                "def unused(x=helper):\n"
                "    return x\n"
                "\n"
                "\n"
                "def lazy():\n"
                "    from pkg.util import helper\n"
                "    return list(map(helper, [1, 2]))\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import double\n"
            "\n"
            "CALLBACKS = [double]\n"
            "\n"
            "\n"
            "@staticmethod\n"
            "def unused(x=double):\n"
            "    return x\n"
            "\n"
            "\n"
            "def lazy():\n"
            "    from pkg.util import double\n"
            "    return list(map(double, [1, 2]))\n"
        )
        assert _run(
            root, "import pkg.main; print(pkg.main.lazy(), pkg.main.CALLBACKS[0](4))"
        ) == "[2, 4] 8"


class TestWhatARenameMustNotTouch:
    """Same-named symbols elsewhere, locals, comments and strings stay."""

    def test_same_named_symbol_in_another_module(self, tmp_path):
        other = "def helper():\n    return 'other'\n\n\ndef use():\n    return helper()\n"
        other_user = "from pkg.other import helper\n\nOTHER = helper()\n"
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/other.py": other,
            "pkg/other_user.py": other_user,
            "pkg/main.py": "from pkg.util import helper\n\nMAIN = helper(1)\n",
        })

        # A bare name shared by two symbols renames one of them, named in
        # the preview, and touches only that symbol's own sites.
        bare = _rename(root, "helper", "double")
        picked = bare["qualified_name"].rsplit("/", 1)[-1]
        assert picked in {"util.py::helper", "other.py::helper"}
        expected = (
            {"util.py", "main.py"} if picked == "util.py::helper"
            else {"other.py", "other_user.py"}
        )
        assert {Path(e["file"]).name for e in bare["edits"]} == expected

        preview = _rename_and_apply(root, "pkg/util.py::helper", "double")
        assert preview["qualified_name"].endswith("pkg/util.py::helper")
        assert {Path(e["file"]).name for e in preview["edits"]} == {"util.py", "main.py"}
        # The picked symbol really is renamed, not just left runnable.
        assert _read(root, "pkg/util.py").startswith("def double(x):\n")
        assert _read(root, "pkg/main.py") == "from pkg.util import double\n\nMAIN = double(1)\n"
        assert _read(root, "pkg/other.py") == other
        assert _read(root, "pkg/other_user.py") == other_user
        assert _run(
            root, "import pkg.main, pkg.other_user; print(pkg.main.MAIN, pkg.other_user.OTHER)"
        ) == "2 other"

    def test_locals_comments_strings_and_longer_names(self, tmp_path):
        main = (
            "from pkg.util import helper  # import helper\n"
            "\n"
            "\n"
            "def main():\n"
            "    helper_count = 2  # call helper here\n"
            '    label = "helper"\n'
            "    return helper(helper_count), label, f'{helper_count} helper'\n"
            "\n"
            "\n"
            "def shadow(helper):\n"
            "    return helper + 1\n"
            "\n"
            "\n"
            "def local():\n"
            "    helper = 40\n"
            "    return helper + 2\n"
            "\n"
            "\n"
            "class Holder:\n"
            "    helper = 'class attribute'\n"
            "\n"
            "    def get(self):\n"
            "        return self.helper\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": main,
        })
        preview = _rename_and_apply(root, "helper", "double")
        assert [(e["line"], e["site"]) for e in preview["edits"]
                if e["file"].endswith("main.py")] == [(1, "import"), (7, "call")]
        assert _read(root, "pkg/main.py") == (
            main.replace("from pkg.util import helper  #", "from pkg.util import double  #")
            .replace("return helper(helper_count)", "return double(helper_count)")
        )
        assert _run(root, (
            "import pkg.main as m; "
            "print(m.main(), m.shadow(1), m.local(), m.Holder().get())"
        )) == "(4, 'helper', '2 helper') 2 42 class attribute"

    def test_rename_never_falls_back_to_a_longer_name(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper_count():\n    return 1\n",
        })
        result = refactor_func(
            mode="rename", old_name="helper", new_name="double", repo_root=str(root),
        )
        assert result["status"] == "not_found", result
        assert _read(root, "pkg/util.py") == "def helper_count():\n    return 1\n"


class TestPythonScopes:
    """A Python name is renamed where it resolves to the symbol, by scope."""

    _SCOPES = (
        "from pkg.util import helper as helper\n"
        "\n"
        "\n"
        "def uses_global():\n"
        "    global helper\n"
        "    return helper(1)\n"
        "\n"
        "\n"
        "def comprehension():\n"
        "    return [helper for helper in range(2)], [helper(i) for i in range(2)]\n"
        "\n"
        "\n"
        "def catches():\n"
        "    try:\n"
        "        raise ValueError('x')\n"
        "    except ValueError as helper:\n"
        "        return str(helper)\n"
        "\n"
        "\n"
        "def defaults(f=lambda helper=helper: helper):\n"
        "    return f()(3)\n"
        "\n"
        "\n"
        "def closure():\n"
        "    helper = 5\n"
        "\n"
        "    def inner():\n"
        "        nonlocal helper\n"
        "        return helper\n"
        "    return inner()\n"
        "\n"
        "\n"
        "class Holder:\n"
        "    helper = staticmethod(helper)\n"
        "\n"
        "    def call(self):\n"
        "        return self.helper(4), helper(5)\n"
    )

    def test_each_scope_resolves_its_own_binding(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/scopes.py": self._SCOPES,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/scopes.py") == (
            self._SCOPES
            .replace("import helper as helper", "import double as double")
            .replace("global helper\n    return helper(1)", "global double\n    return double(1)")
            .replace("[helper(i) for", "[double(i) for")
            .replace("lambda helper=helper:", "lambda helper=double:")
            # The class body reads the module's name before binding its own.
            .replace("helper = staticmethod(helper)", "helper = staticmethod(double)")
            .replace("self.helper(4), helper(5)", "self.helper(4), double(5)")
        )
        assert _run(root, (
            "import pkg.scopes as s; "
            "print(s.uses_global(), s.comprehension(), s.catches(), s.defaults(), "
            "s.closure(), s.Holder().call())"
        )) == "2 ([0, 1], [0, 2]) x 6 5 (8, 10)"

    def test_nested_function_rename_stays_in_its_function(self, tmp_path):
        util = (
            "def outer():\n"
            "    def inner(n):\n"
            "        return n + 1\n"
            "    alias = inner\n"
            "    return inner(1) + alias(2) + [inner for inner in range(1)][0]\n"
            "\n"
            "\n"
            "def inner_elsewhere():\n"
            "    inner = 'unrelated'\n"
            "    return inner\n"
        )
        root = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/util.py": util})
        _rename_and_apply(root, "pkg/util.py::inner", "step")
        assert _read(root, "pkg/util.py") == (
            util.replace("def inner(n)", "def step(n)")
            .replace("alias = inner\n", "alias = step\n")
            .replace("return inner(1)", "return step(1)")
        )
        assert _run(
            root, "import pkg.util as u; print(u.outer(), u.inner_elsewhere())"
        ) == "5 unrelated"


class TestClassesAndMethods:
    def test_rename_a_class_everywhere_it_is_named(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from .util import Widget\n\n__all__ = ['Widget']\n",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg import Widget\n"
                "\n"
                "\n"
                "class Fancy(Widget):\n"
                "    pass\n"
                "\n"
                "\n"
                "def make() -> Widget:\n"
                "    w = Fancy()\n"
                "    assert isinstance(w, Widget)\n"
                "    return w\n"
            ),
        })
        _rename_and_apply(root, "Widget", "Gadget")
        assert "class Gadget:" in _read(root, "pkg/util.py")
        assert _read(root, "pkg/__init__.py") == (
            "from .util import Gadget\n\n__all__ = ['Gadget']\n"
        )
        assert _read(root, "pkg/main.py") == (
            "from pkg import Gadget\n"
            "\n"
            "\n"
            "class Fancy(Gadget):\n"
            "    pass\n"
            "\n"
            "\n"
            "def make() -> Gadget:\n"
            "    w = Fancy()\n"
            "    assert isinstance(w, Gadget)\n"
            "    return w\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.make().render())") == "w"

    def test_rename_a_method_only_as_an_attribute(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import Widget\n"
                "\n"
                "\n"
                "def render():\n"
                "    return 'module-level render'\n"
                "\n"
                "\n"
                "def main():\n"
                "    w = Widget()\n"
                "    return w.render() + render()\n"
            ),
        })
        preview = _rename_and_apply(root, "Widget.render", "paint")
        assert preview["qualified_name"].endswith("pkg/util.py::Widget.render")
        assert "    def paint(self):\n" in _read(root, "pkg/util.py")
        assert _read(root, "pkg/main.py").endswith("    return w.paint() + render()\n")
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "wmodule-level render"

    def test_method_calls_in_a_chain_or_an_fstring(self, tmp_path):
        """The parser dates a call by the line its expression starts on, so a
        call chained over several lines sits on its receiver's line, where
        the method name may appear only in a comment. Before Python 3.12 an
        f-string is a single token. Neither may move the edit into a comment
        or into the f-string's text."""
        main = (
            "from pkg.util import Widget\n"
            "\n"
            "\n"
            "def main():\n"
            "    chained = (Widget()  # render it\n"
            "               .render())\n"
            "    return f\"render: {Widget().render()}\", chained\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "Widget.render", "paint")
        assert _read(root, "pkg/main.py") == (
            main.replace("               .render())", "               .paint())")
            .replace("{Widget().render()}", "{Widget().paint()}")
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "('render: w', 'w')"

    def test_js_method_name_in_a_comment_is_not_edited(self, tmp_path):
        main = (
            "import { Widget } from './util.mjs';\n"
            "const w = new Widget();\n"
            "export const v = w // render later\n"
            "  .render();\n"
        )
        root = _repo(tmp_path, {"src/util.mjs": _JS_UTIL, "src/main.mjs": main})
        preview = _rename(root, "Widget.render", "paint")
        # Whatever line the graph dates a call by, the comment is not a site.
        assert all(
            "//" not in _read(root, "src/main.mjs").splitlines()[e["line"] - 1][: e["column"]]
            for e in preview["edits"] if e["file"].endswith("main.mjs")
        )
        _apply(root, preview)
        assert _read(root, "src/main.mjs") == main.replace(".render()", ".paint()")


class TestIdempotency:
    def test_preview_twice_gives_the_same_edits(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from .util import helper\n",
            "pkg/util.py": _UTIL,
            "pkg/main.py": "from pkg import helper\n\nVALUE = helper(1)\n",
        })
        first = _rename(root, "helper", "double")
        second = _rename(root, "helper", "double")
        assert first["refactor_id"] != second["refactor_id"]
        assert first["edits"] == second["edits"]
        assert len(first["edits"]) == 4

    def test_after_apply_the_old_name_is_gone(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from .util import helper\n",
            "pkg/util.py": _UTIL,
            "pkg/main.py": "from pkg import helper\n\nVALUE = helper(1)\n",
        })
        _rename_and_apply(root, "helper", "double")
        _build(root)
        again = refactor_func(
            mode="rename", old_name="helper", new_name="double", repo_root=str(root),
        )
        assert again["status"] == "not_found", again
        back = _rename(root, "double", "helper")
        assert {(Path(e["file"]).name, e["line"]) for e in back["edits"]} == {
            ("__init__.py", 1), ("util.py", 1), ("main.py", 1), ("main.py", 3),
        }

    def test_stale_graph_after_apply_previews_nothing(self, tmp_path):
        """Without a rebuild the graph still names the old symbol; the
        preview checks the file and reports nothing to rename."""
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
        })
        _rename_and_apply(root, "helper", "double")
        stale = refactor_func(
            mode="rename", old_name="helper", new_name="double", repo_root=str(root),
        )
        assert stale["status"] == "not_found", stale


class TestFileEncodings:
    def test_crlf_line_endings_survive(self, tmp_path):
        main = (
            "from pkg.util import (\r\n"
            "    a,\r\n"
            "    helper,\r\n"
            ")\r\n"
            "\r\n"
            "\r\n"
            "def main():\r\n"
            "    return a() + str(helper(2))\r\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL.replace("\n", "\r\n"),
            "pkg/main.py": main.encode("utf-8"),
        })
        _rename_and_apply(root, "helper", "double")
        after = (root / "pkg" / "main.py").read_bytes()
        assert after == main.replace("helper", "double").encode("utf-8")
        util = (root / "pkg" / "util.py").read_bytes()
        assert util.count(b"\r\n") == _UTIL.count("\n")
        assert b"\n" not in util.replace(b"\r\n", b"")
        assert util.startswith(b"def double(x):\r\n")
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "a4"

    def test_non_ascii_text_before_the_name_on_the_same_line(self, tmp_path):
        main = (
            "# -*- coding: utf-8 -*-\n"
            "from pkg.util import helper  # générateur ☕\n"
            "\n"
            "\n"
            "def main():\n"
            '    ñ = "日本語"; return helper(len(ñ)), "naïve helper"\n'
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": "\ufeff" + _UTIL,
            "pkg/main.py": main,
        })
        preview = _rename_and_apply(root, "helper", "double")
        call = next(e for e in preview["edits"] if e["site"] == "call")
        line = main.splitlines()[5]
        assert call["column"] == line.index("helper(") + 1
        assert _read(root, "pkg/main.py") == (
            main.replace("import helper", "import double")
            .replace("return helper(", "return double(")
        )
        util = (root / "pkg" / "util.py").read_bytes()
        assert util.startswith("\ufeffdef double(x):".encode("utf-8"))
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "(6, 'naïve helper')"


class TestApplyIsExact:
    """``apply_refactor`` replaces the name where the edit says, or nowhere."""

    def _pending(self, rid: str, edits: list[dict]) -> None:
        import time
        with _refactor_lock:
            _pending_refactors[rid] = {
                "refactor_id": rid, "type": "rename", "old_name": "helper",
                "new_name": "double", "edits": edits,
                "stats": {"high": len(edits), "medium": 0, "low": 0},
                "created_at": time.time(),
            }

    def test_column_that_no_longer_matches_is_skipped(self, tmp_path):
        (tmp_path / ".git").mkdir()
        target = tmp_path / "m.py"
        target.write_text("x = 1\nhelper_count = helper()\n", encoding="utf-8")
        self._pending("stale", [
            {"file": str(target), "line": 2, "column": 1, "old": "helper", "new": "double"},
            {"file": str(target), "line": 1, "column": 1, "old": "helper", "new": "double"},
            {"file": str(target), "line": 2, "column": 16, "old": "helper", "new": "double"},
        ])
        result = apply_refactor("stale", tmp_path)
        assert result["edits_applied"] == 1
        assert result["edits_skipped"] == 2
        assert target.read_text(encoding="utf-8") == "x = 1\nhelper_count = double()\n"

    def test_edit_without_column_takes_the_whole_word_on_its_line(self, tmp_path):
        (tmp_path / ".git").mkdir()
        target = tmp_path / "m.py"
        target.write_text("helper = 1\nhelper_count = helper()\n", encoding="utf-8")
        self._pending("lineonly", [
            {"file": str(target), "line": 2, "old": "helper", "new": "double"},
        ])
        result = apply_refactor("lineonly", tmp_path)
        assert result["edits_applied"] == 1
        assert target.read_text(encoding="utf-8") == "helper = 1\nhelper_count = double()\n"

    def test_missing_name_on_the_line_is_not_replaced_elsewhere(self, tmp_path):
        (tmp_path / ".git").mkdir()
        target = tmp_path / "m.py"
        target.write_text("x = 1\nhelper()\n", encoding="utf-8")
        self._pending("elsewhere", [
            {"file": str(target), "line": 1, "old": "helper", "new": "double"},
        ])
        result = apply_refactor("elsewhere", tmp_path)
        assert result["edits_applied"] == 0
        assert result["edits_skipped"] == 1
        assert target.read_text(encoding="utf-8") == "x = 1\nhelper()\n"

    def test_file_that_is_not_utf8_is_left_alone(self, tmp_path):
        (tmp_path / ".git").mkdir()
        target = tmp_path / "m.py"
        original = "# caf\xe9\nhelper()\n".encode("latin-1")
        target.write_bytes(original)
        self._pending("latin1", [
            {"file": str(target), "line": 2, "column": 1, "old": "helper", "new": "double"},
        ])
        result = apply_refactor("latin1", tmp_path)
        assert result["edits_applied"] == 0
        assert target.read_bytes() == original


_JS_UTIL = (
    "export function helper(x) {\n"
    "  return x * 2;\n"
    "}\n"
    "\n"
    "export class Widget {\n"
    "  render() { return 'w'; }\n"
    "}\n"
)


class TestJavaScriptAndTypeScript:
    """Named imports are part of a JS/TS rename too."""

    def _js_repo(self, tmp_path: Path) -> Path:
        return _repo(tmp_path, {
            "src/util.mjs": _JS_UTIL,
            "src/main.mjs": (
                "import { helper } from './util.mjs';\n"
                "import { helper as h2, Widget } from \"./util.mjs\";\n"
                "import {\n"
                "  Widget as W,\n"
                "  helper as h3, // helper alias\n"
                "} from './util.mjs';\n"
                "import * as U from './util.mjs';\n"
                "import { v } from './consumer.mjs';\n"
                "\n"
                "const helperCount = 1; // helper\n"
                "const table = { helper, label: 'helper' };\n"
                "console.log(helper(1) + h2(2) + h3(3) + U.helper(4) + helperCount,\n"
                "  table.helper(5), v, new W() instanceof Widget);\n"
            ),
            "src/index.mjs": (
                "export { helper } from './util.mjs';\n"
                "export { helper as aliasHelper } from './util.mjs';\n"
            ),
            "src/consumer.mjs": (
                "import { helper } from './index.mjs';\n"
                "export const v = helper(9);\n"
            ),
            "src/other.mjs": (
                "function helper() { return 'other'; }\n"
                "export function use() { return helper(); }\n"
            ),
        })

    def test_named_aliased_namespace_and_barrel_imports(self, tmp_path):
        root = self._js_repo(tmp_path)
        other = _read(root, "src/other.mjs")
        _rename_and_apply(root, "src/util.mjs::helper", "double")
        assert _read(root, "src/util.mjs").startswith("export function double(x) {\n")
        assert _read(root, "src/main.mjs") == (
            "import { double } from './util.mjs';\n"
            "import { double as h2, Widget } from \"./util.mjs\";\n"
            "import {\n"
            "  Widget as W,\n"
            "  double as h3, // helper alias\n"
            "} from './util.mjs';\n"
            "import * as U from './util.mjs';\n"
            "import { v } from './consumer.mjs';\n"
            "\n"
            "const helperCount = 1; // helper\n"
            "const table = { helper: double, label: 'helper' };\n"
            "console.log(double(1) + h2(2) + h3(3) + U.double(4) + helperCount,\n"
            "  table.helper(5), v, new W() instanceof Widget);\n"
        )
        assert _read(root, "src/index.mjs") == (
            "export { double } from './util.mjs';\n"
            "export { double as aliasHelper } from './util.mjs';\n"
        )
        assert _read(root, "src/consumer.mjs") == (
            "import { double } from './index.mjs';\n"
            "export const v = double(9);\n"
        )
        assert _read(root, "src/other.mjs") == other

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_renamed_modules_still_run_under_node(self, tmp_path):
        root = self._js_repo(tmp_path)
        _rename_and_apply(root, "src/util.mjs::helper", "double")
        assert "export function double(" in _read(root, "src/util.mjs")
        assert "helper" not in _read(root, "src/consumer.mjs")
        proc = subprocess.run(
            ["node", str(root / "src" / "main.mjs")],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "21 10 18 true"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_shadowing_declarations_keep_the_old_name(self, tmp_path):
        main = (
            "import { helper } from './util.mjs';\n"
            "\n"
            "function local(helper) {\n"
            "  return helper + 1;\n"
            "}\n"
            "\n"
            "const arrow = helper => helper * 10;\n"
            "\n"
            "function block() {\n"
            "  {\n"
            "    const helper = 7;\n"
            "    void helper;\n"
            "  }\n"
            "  return helper(3);\n"
            "}\n"
            "\n"
            "const named = function helper(n) { return n ? helper(n - 1) : 'done'; };\n"
            "const refs = [helper, { helper }, `${helper(4)}`];\n"
            "export default helper;\n"
            "console.log(local(1), arrow(2), block(), named(2), refs[0](5),\n"
            "  refs[1].helper(6), refs[2]);\n"
        )
        root = _repo(tmp_path, {
            "src/util.mjs": "export function helper(x) { return x * 2; }\n",
            "src/main.mjs": main,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/main.mjs") == (
            main.replace("import { helper }", "import { double }")
            .replace("  return helper(3);", "  return double(3);")
            .replace(
                "[helper, { helper }, `${helper(4)}`]",
                "[double, { helper: double }, `${double(4)}`]",
            )
            .replace("export default helper;", "export default double;")
        )
        proc = subprocess.run(
            ["node", str(root / "src" / "main.mjs")],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "2 20 6 done 10 12 8"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_default_import_and_require_keep_their_local_names(self, tmp_path):
        """A default import or a ``require`` binds a local name of its own.
        The graph resolves the calls through it to the symbol, but renaming
        them without the binding would leave them undefined."""
        root = _repo(tmp_path, {
            "esm/util.mjs": (
                "export default function helper(x) { return x * 2; }\n"
                "export const k = helper(1);\n"
            ),
            "esm/main.mjs": (
                "import helper, { k } from './util.mjs';\n"
                "console.log(helper(2), k);\n"
            ),
        })
        esm_main = _read(root, "esm/main.mjs")
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "esm/util.mjs") == (
            "export default function double(x) { return x * 2; }\n"
            "export const k = double(1);\n"
        )
        assert _read(root, "esm/main.mjs") == esm_main

        (tmp_path / "cjs").mkdir()
        cjs_root = _repo(tmp_path / "cjs", {
            "util.js": "function helper(x) { return x * 2; }\nmodule.exports = { helper };\n",
            "destructure.js": (
                "const { helper } = require('./util');\nconsole.log(helper(2));\n"
            ),
            "member.js": "const util = require('./util');\nconsole.log(util.helper(3));\n",
        })
        users = {rel: _read(cjs_root, rel) for rel in ("destructure.js", "member.js")}
        _rename_and_apply(cjs_root, "helper", "double")
        # The export keeps its key, so every consumer still finds it.
        assert _read(cjs_root, "util.js") == (
            "function double(x) { return x * 2; }\nmodule.exports = { helper: double };\n"
        )
        assert {rel: _read(cjs_root, rel) for rel in users} == users

        for script in (root / "esm" / "main.mjs", cjs_root / "destructure.js",
                       cjs_root / "member.js"):
            proc = subprocess.run(
                ["node", str(script)], capture_output=True, text=True, timeout=60,
            )
            assert proc.returncode == 0, (script, proc.stderr)
        assert proc.stdout.strip() == "6"

    def test_typescript_named_and_aliased_imports(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.ts": (
                "export interface WidgetProps { title: string }\n"
                "export function helper(x: number): number {\n"
                "  return x * 2;\n"
                "}\n"
            ),
            "src/main.ts": (
                "import { helper, type WidgetProps } from './util';\n"
                "import { helper as twice } from './util';\n"
                "\n"
                "export function main(p: WidgetProps): number {\n"
                "  const helperish = p.title.length; // helper\n"
                "  return helper(helperish) + twice(1);\n"
                "}\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/main.ts") == (
            "import { double, type WidgetProps } from './util';\n"
            "import { double as twice } from './util';\n"
            "\n"
            "export function main(p: WidgetProps): number {\n"
            "  const helperish = p.title.length; // helper\n"
            "  return double(helperish) + twice(1);\n"
            "}\n"
        )
        _rename_and_apply(root, "WidgetProps", "GadgetProps")
        assert _read(root, "src/main.ts").splitlines()[:4] == [
            "import { double, type GadgetProps } from './util';",
            "import { double as twice } from './util';",
            "",
            "export function main(p: GadgetProps): number {",
        ]
        assert _read(root, "src/util.ts").startswith("export interface GadgetProps {")


class TestOtherLanguagesStillRename:
    """Languages without import handling keep their definition and calls,
    now matched as whole code tokens."""

    def test_go_function_rename(self, tmp_path):
        root = _repo(tmp_path, {
            "go.mod": "module example.com/demo\n\ngo 1.21\n",
            "util.go": (
                "package main\n"
                "\n"
                "// Helper doubles; HelperCount is unrelated.\n"
                "func Helper(x int) int { return x * 2 }\n"
                "\n"
                "var HelperCount = 3\n"
            ),
            "main.go": (
                "package main\n"
                "\n"
                "import \"fmt\"\n"
                "\n"
                "func main() {\n"
                "\tfmt.Println(\"Helper\", HelperCount, Helper(2)) // Helper\n"
                "}\n"
            ),
        })
        preview = _rename_and_apply(root, "Helper", "Twice")
        assert {(Path(e["file"]).name, e["site"]) for e in preview["edits"]} == {
            ("util.go", "definition"), ("main.go", "call"),
        }
        assert _read(root, "util.go").splitlines()[2:4] == [
            "// Helper doubles; HelperCount is unrelated.",
            "func Twice(x int) int { return x * 2 }",
        ]
        assert _read(root, "main.go").splitlines()[5] == (
            "\tfmt.Println(\"Helper\", HelperCount, Twice(2)) // Helper"
        )


class TestRepositoryLayouts:
    """Import edges resolve in the layouts real projects use, so the
    import statements are found and rewritten in each of them."""

    def test_src_layout(self, tmp_path):
        root = _repo(tmp_path, {
            "pyproject.toml": "[project]\nname = 'pkg'\n",
            "src/pkg/__init__.py": "",
            "src/pkg/util.py": _UTIL,
            "src/pkg/main.py": "from pkg.util import helper\n\nVALUE = helper(1)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/pkg/main.py") == (
            "from pkg.util import double\n\nVALUE = double(1)\n"
        )
        assert _run(root / "src", "import pkg.main; print(pkg.main.VALUE)") == "2"

    def test_tests_directory_imports_the_package(self, tmp_path):
        test_module = (
            "from pkg.util import helper\n"
            "\n"
            "\n"
            "def test_helper():\n"
            "    assert helper(2) == 4\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "tests/test_util.py": test_module,
        })
        _rename_and_apply(root, "helper", "double")
        # The test function keeps its own name; only the symbol is renamed.
        assert _read(root, "tests/test_util.py") == (
            test_module.replace("import helper", "import double")
            .replace("assert helper(2)", "assert double(2)")
        )

    def test_namespace_package_without_init(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/util.py": _UTIL,
            "pkg/main.py": "from pkg.util import helper\n\nVALUE = helper(3)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == "from pkg.util import double\n\nVALUE = double(3)\n"
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "6"

    def test_flat_scripts_next_to_each_other(self, tmp_path):
        root = _repo(tmp_path, {
            "util.py": _UTIL,
            "main.py": (
                "from util import helper\nimport util\n\nVALUE = helper(3) + util.helper(1)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "main.py") == (
            "from util import double\nimport util\n\nVALUE = double(3) + util.double(1)\n"
        )
        assert _run(root, "import main; print(main.VALUE)") == "8"

    def test_symbol_defined_in_a_package_init(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util/__init__.py": _UTIL,
            "pkg/main.py": (
                "from pkg.util import helper\n"
                "import pkg.util\n"
                "\n"
                "VALUE = helper(3) + pkg.util.helper(1)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import double\n"
            "import pkg.util\n"
            "\n"
            "VALUE = double(3) + pkg.util.double(1)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "8"


class TestMorePythonImportShapes:
    def test_star_reexport_renames_the_home_modules_all(self, tmp_path):
        """``pkg/__init__.py`` never spells the name, yet re-exports it
        through ``import *``; the home module's ``__all__`` decides what the
        star passes on, so it must follow the rename."""
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from .util import *\n",
            "pkg/util.py": "__all__ = ['helper', 'a']\n\n\n" + _UTIL,
            "app/__init__.py": "",
            "app/use.py": "from pkg import helper\n\nVALUE = helper(3)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/__init__.py") == "from .util import *\n"
        assert _read(root, "pkg/util.py").startswith("__all__ = ['double', 'a']\n")
        assert _read(root, "app/use.py") == "from pkg import double\n\nVALUE = double(3)\n"
        assert _run(root, "import app.use; print(app.use.VALUE)") == "6"

    def test_reexport_through_two_packages(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from .sub import helper\n\n__all__ = ['helper']\n",
            "pkg/sub/__init__.py": "from .util import helper\n",
            "pkg/sub/util.py": _UTIL,
            "app/__init__.py": "",
            "app/use.py": (
                "from pkg import helper\n"
                "from pkg.sub import helper as h\n"
                "\n"
                "VALUE = helper(3) + h(1)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/__init__.py") == "from .sub import double\n\n__all__ = ['double']\n"
        assert _read(root, "pkg/sub/__init__.py") == "from .util import double\n"
        assert _read(root, "app/use.py") == (
            "from pkg import double\n"
            "from pkg.sub import double as h\n"
            "\n"
            "VALUE = double(3) + h(1)\n"
        )
        assert _run(root, "import app.use; print(app.use.VALUE)") == "8"

    def test_relative_import_of_the_module_then_attribute(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": (
                "from . import util\n"
                "from pkg import util as u\n"
                "\n"
                "VALUE = util.helper(1) + u.helper(2)\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from . import util\n"
            "from pkg import util as u\n"
            "\n"
            "VALUE = util.double(1) + u.double(2)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "6"

    def test_type_checking_block_and_try_except_fallback(self, tmp_path):
        main = (
            "from typing import TYPE_CHECKING\n"
            "\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.util import Widget\n"
            "\n"
            "try:\n"
            "    from pkg.util import helper\n"
            "except ImportError:\n"
            "    helper = None\n"
            "\n"
            "\n"
            "def make(w: 'Widget') -> int:\n"
            "    return helper(1)\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "helper", "double")
        # The fallback binds the same module name, so it is renamed with it.
        assert _read(root, "pkg/main.py") == (
            main.replace("import helper\n", "import double\n")
            .replace("    helper = None\n", "    double = None\n")
            .replace("return helper(1)", "return double(1)")
        )
        assert _run(root, "import pkg.main; print(pkg.main.make(None))") == "2"


class TestUsesBeyondPlainCalls:
    def test_function_used_as_a_decorator_and_a_keyword_value(self, tmp_path):
        main = (
            "from pkg.deco import retry\n"
            "\n"
            "\n"
            "@retry()\n"
            "def one():\n"
            "    return 1\n"
            "\n"
            "\n"
            "@retry(times=3)\n"
            "def two():\n"
            "    return 2\n"
            "\n"
            "\n"
            "def call(retry=None):\n"
            "    return retry\n"
            "\n"
            "\n"
            "VALUE = (one(), two(), call(retry=retry) is retry)\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/deco.py": (
                "def retry(times=1):\n"
                "    def wrap(fn):\n"
                "        return fn\n"
                "    return wrap\n"
            ),
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "retry", "with_retry")
        # The parameter and the keyword argument's name are not the symbol.
        assert _read(root, "pkg/main.py") == (
            main.replace("import retry\n", "import with_retry\n")
            .replace("@retry(", "@with_retry(")
            .replace("call(retry=retry) is retry", "call(retry=with_retry) is with_retry")
        )
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "(1, 2, True)"

    def test_decorated_definition(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": (
                "import functools\n"
                "\n"
                "\n"
                "@functools.lru_cache(maxsize=None)\n"
                "def helper(x):\n"
                "    return x * 2\n"
            ),
            "pkg/main.py": "from pkg.util import helper\n\nVALUE = helper(2)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/util.py").splitlines()[3:5] == [
            "@functools.lru_cache(maxsize=None)", "def double(x):",
        ]
        assert _read(root, "pkg/main.py") == "from pkg.util import double\n\nVALUE = double(2)\n"
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "4"

    def test_exception_class_in_bases_raise_and_except(self, tmp_path):
        main = (
            "from pkg.errors import AppError, NotFound\n"
            "\n"
            "\n"
            "def run(flag):\n"
            "    try:\n"
            "        if flag:\n"
            "            raise NotFound('x')\n"
            "        raise AppError('y')\n"
            "    except (NotFound, AppError) as exc:\n"
            "        return type(exc).__name__\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/errors.py": (
                "class AppError(Exception):\n"
                "    pass\n"
                "\n"
                "\n"
                "class NotFound(AppError):\n"
                "    pass\n"
            ),
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "AppError", "ServiceError")
        assert _read(root, "pkg/errors.py") == (
            "class ServiceError(Exception):\n"
            "    pass\n"
            "\n"
            "\n"
            "class NotFound(ServiceError):\n"
            "    pass\n"
        )
        assert _read(root, "pkg/main.py") == main.replace("AppError", "ServiceError")
        assert _run(
            root, "import pkg.main as m; print(m.run(0), m.run(1))"
        ) == "ServiceError NotFound"

    def test_class_in_annotations_generics_and_class_body(self, tmp_path):
        main = (
            "from __future__ import annotations\n"
            "\n"
            "from typing import Optional\n"
            "\n"
            "from pkg.util import Widget\n"
            "\n"
            "\n"
            "def first(items: list[Widget], default: Optional[Widget] = None) -> Widget | None:\n"
            "    return items[0] if items else default\n"
            "\n"
            "\n"
            "class Box:\n"
            "    item: Widget\n"
            "\n"
            "    def __init__(self) -> None:\n"
            "        self.item = Widget()\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "Widget", "Gadget")
        assert _read(root, "pkg/main.py") == main.replace("Widget", "Gadget")
        assert _run(
            root, "import pkg.main as m; print(m.first([m.Box().item]).render())"
        ) == "w"

    def test_several_edits_on_one_line_with_a_longer_name(self, tmp_path):
        """Replacements on one line are made right to left, so a longer new
        name cannot shift the column of the next edit."""
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": "from pkg.util import helper\n\nVALUE = helper(helper(1)) + helper(2)\n",
        })
        _rename_and_apply(root, "helper", "twice_over")
        assert _read(root, "pkg/main.py") == (
            "from pkg.util import twice_over\n"
            "\n"
            "VALUE = twice_over(twice_over(1)) + twice_over(2)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "8"

    def test_dry_run_shows_the_import_edit_and_writes_nothing(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": "from pkg.util import helper\n\nVALUE = helper(1)\n",
        })
        preview = _rename(root, "helper", "double")
        before = {rel: _read(root, rel) for rel in ("pkg/util.py", "pkg/main.py")}
        dry = apply_refactor_func(preview["refactor_id"], repo_root=str(root), dry_run=True)
        assert dry["status"] == "ok" and dry["edits_applied"] == 3, dry
        assert {rel: _read(root, rel) for rel in before} == before
        diff = next(text for path, text in dry["diffs"].items() if path.endswith("main.py"))
        assert "-from pkg.util import helper\n" in diff
        assert "+from pkg.util import double\n" in diff
        # The same preview still applies after the dry run.
        _apply(root, preview)
        assert _run(root, "import pkg.main; print(pkg.main.VALUE)") == "2"


def _node_run(script: Path) -> str:
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestMoreJavaScriptAndTypeScript:
    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_export_star_barrel_and_local_export_list(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.mjs": "function helper(x) { return x * 2; }\n\nexport { helper };\n",
            "src/index.mjs": "export * from './util.mjs';\n",
            "src/main.mjs": "import { helper } from './index.mjs';\nconsole.log(helper(4));\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/util.mjs") == (
            "function double(x) { return x * 2; }\n\nexport { double };\n"
        )
        assert _read(root, "src/index.mjs") == "export * from './util.mjs';\n"
        assert _read(root, "src/main.mjs") == (
            "import { double } from './index.mjs';\nconsole.log(double(4));\n"
        )
        assert _node_run(root / "src" / "main.mjs") == "8"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_default_export_object_keeps_its_key(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.mjs": "function helper(x) { return x * 2; }\nexport default { helper };\n",
            "src/main.mjs": "import utils from './util.mjs';\nconsole.log(utils.helper(4));\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/util.mjs") == (
            "function double(x) { return x * 2; }\nexport default { helper: double };\n"
        )
        assert _read(root, "src/main.mjs") == (
            "import utils from './util.mjs';\nconsole.log(utils.helper(4));\n"
        )
        assert _node_run(root / "src" / "main.mjs") == "8"

    def test_exported_arrow_function_const(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.ts": "export const helper = (x: number): number => x * 2;\n",
            "src/main.ts": "import { helper } from './util';\n\nexport const v = helper(3);\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/util.ts") == (
            "export const double = (x: number): number => x * 2;\n"
        )
        assert _read(root, "src/main.ts") == (
            "import { double } from './util';\n\nexport const v = double(3);\n"
        )

    def test_tsx_component_in_opening_and_closing_tags(self, tmp_path):
        app = (
            "import { Button } from './Button';\n"
            "\n"
            "export function App() {\n"
            "  return (\n"
            "    <div>\n"
            "      <Button label=\"a\" />\n"
            "      <Button label=\"b\"></Button>\n"
            "    </div>\n"
            "  );\n"
            "}\n"
        )
        root = _repo(tmp_path, {
            "src/Button.tsx": (
                "export function Button(props: { label: string }) {\n"
                "  return <button>{props.label}</button>;\n"
                "}\n"
            ),
            "src/App.tsx": app,
        })
        _rename_and_apply(root, "Button", "ActionButton")
        # The module path './Button' is a string and keeps its spelling.
        assert _read(root, "src/App.tsx") == (
            app.replace("Button", "ActionButton").replace("'./ActionButton'", "'./Button'")
        )
        assert _read(root, "src/Button.tsx").startswith("export function ActionButton(props")
        # The intrinsic <button> element is not the component.
        assert "<button>{props.label}</button>" in _read(root, "src/Button.tsx")

    def test_type_only_import_and_type_only_barrel(self, tmp_path):
        main = (
            "import type { WidgetProps } from './index';\n"
            "\n"
            "export function title(p: WidgetProps): string {\n"
            "  return p.title;\n"
            "}\n"
        )
        root = _repo(tmp_path, {
            "src/types.ts": "export interface WidgetProps { title: string }\n",
            "src/index.ts": "export type { WidgetProps } from './types';\n",
            "src/main.ts": main,
        })
        _rename_and_apply(root, "WidgetProps", "GadgetProps")
        assert _read(root, "src/types.ts") == "export interface GadgetProps { title: string }\n"
        assert _read(root, "src/index.ts") == "export type { GadgetProps } from './types';\n"
        assert _read(root, "src/main.ts") == main.replace("WidgetProps", "GadgetProps")

    def test_tsconfig_path_alias_js_extension_and_directory_index(self, tmp_path):
        root = _repo(tmp_path, {
            "tsconfig.json": (
                '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}\n'
            ),
            "src/lib/util.ts": (
                "export function helper(x: number): number {\n  return x * 2;\n}\n"
            ),
            "src/lib/index.ts": "export { helper } from './util';\n",
            "src/alias.ts": "import { helper } from '@/lib/util';\n\nexport const a = helper(1);\n",
            "src/esm.ts": (
                "import { helper } from './lib/util.js';\n\nexport const b = helper(2);\n"
            ),
            "src/dir.ts": "import { helper } from './lib';\n\nexport const c = helper(3);\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/lib/index.ts") == "export { double } from './util';\n"
        assert _read(root, "src/alias.ts") == (
            "import { double } from '@/lib/util';\n\nexport const a = double(1);\n"
        )
        assert _read(root, "src/esm.ts") == (
            "import { double } from './lib/util.js';\n\nexport const b = double(2);\n"
        )
        assert _read(root, "src/dir.ts") == (
            "import { double } from './lib';\n\nexport const c = double(3);\n"
        )

    def test_class_in_extends_new_and_type_position(self, tmp_path):
        root = _repo(tmp_path, {
            "src/base.ts": (
                "export interface Shape { area(): number }\n"
                "export class Base implements Shape {\n"
                "  area(): number { return 1; }\n"
                "}\n"
            ),
            "src/main.ts": (
                "import { Base, Shape } from './base';\n"
                "\n"
                "export class Square extends Base implements Shape {}\n"
                "export const s: Base = new Base();\n"
            ),
        })
        _rename_and_apply(root, "Base", "BaseShape")
        assert _read(root, "src/main.ts") == (
            "import { BaseShape, Shape } from './base';\n"
            "\n"
            "export class Square extends BaseShape implements Shape {}\n"
            "export const s: BaseShape = new BaseShape();\n"
        )
        assert _read(root, "src/base.ts").splitlines()[1] == (
            "export class BaseShape implements Shape {"
        )


class TestCallsThroughAPackageAttribute:
    """Calls no import statement spells: ``pkg.util.helper()`` after
    ``import pkg``, ``sub.util.helper()`` after ``from pkg import sub``, and
    a module bound by ``importlib``. Only the graph's call edge finds them,
    so a rename must still use it."""

    def test_import_package_then_submodule_attribute(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from . import util\n",
            "pkg/util.py": _UTIL,
            "pkg/main.py": "import pkg\n\n\ndef main():\n    return pkg.util.helper(3)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "import pkg\n\n\ndef main():\n    return pkg.util.double(3)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "6"

    def test_from_package_import_subpackage_then_attribute_chain(self, tmp_path):
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "from . import util\n",
            "pkg/sub/util.py": _UTIL,
            "pkg/main.py": "from pkg import sub\n\n\ndef main():\n    return sub.util.helper(3)\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == (
            "from pkg import sub\n\n\ndef main():\n    return sub.util.double(3)\n"
        )
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "6"

    def test_module_bound_by_importlib(self, tmp_path):
        main = (
            "import importlib\n"
            "\n"
            "\n"
            "def main():\n"
            "    util = importlib.import_module('pkg.util')\n"
            "    return util.helper(21)\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": _UTIL,
            "pkg/main.py": main,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "pkg/main.py") == main.replace("util.helper(21)", "util.double(21)")
        assert _run(root, "import pkg.main; print(pkg.main.main())") == "42"


class TestJsNamespaceObjectsTheScanDoesNotLink:
    """``ns.helper`` through a namespace the scan cannot tie to the home
    module by itself: ``import * as`` of an ``export *`` barrel, a dynamic
    ``import()``, and TypeScript's ``import x = require()``."""

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_namespace_import_of_an_export_star_barrel(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.mjs": "export function helper(x) { return x * 2; }\n",
            "src/index.mjs": "export * from './util.mjs';\n",
            "src/main.mjs": "import * as lib from './index.mjs';\nconsole.log(lib.helper(4));\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/main.mjs") == (
            "import * as lib from './index.mjs';\nconsole.log(lib.double(4));\n"
        )
        assert _node_run(root / "src" / "main.mjs") == "8"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_dynamic_import_namespace(self, tmp_path):
        main = (
            "async function run() {\n"
            "  const m = await import('./util.mjs');\n"
            "  return m.helper(4);\n"
            "}\n"
            "run().then((v) => console.log(v));\n"
        )
        root = _repo(tmp_path, {
            "src/util.mjs": "export function helper(x) { return x * 2; }\n",
            "src/main.mjs": main,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/main.mjs") == main.replace("m.helper(4)", "m.double(4)")
        assert _node_run(root / "src" / "main.mjs") == "8"

    def test_typescript_import_equals_require(self, tmp_path):
        main = "import U = require('./util');\n\nexport const v = U.helper(1);\n"
        root = _repo(tmp_path, {
            "src/util.ts": "export function helper(x: number): number {\n  return x * 2;\n}\n",
            "src/main.ts": main,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/util.ts").startswith("export function double(")
        assert _read(root, "src/main.ts") == main.replace("U.helper(1)", "U.double(1)")


class TestJsDefinitionWithSameNamedInnerDeclaration:
    """The definition edit lands on the symbol, not on a later declaration
    of the same name inside it (a local, or a named function expression)."""

    def test_function_with_a_same_named_local(self, tmp_path):
        root = _repo(tmp_path, {
            "src/app.js": (
                "function settings() {\n"
                "  const settings = { a: 1 };\n"
                "  return settings;\n"
                "}\n"
                "\n"
                "console.log(settings().a);\n"
            ),
        })
        _rename_and_apply(root, "settings", "config")
        assert _read(root, "src/app.js") == (
            "function config() {\n"
            "  const settings = { a: 1 };\n"
            "  return settings;\n"
            "}\n"
            "\n"
            "console.log(config().a);\n"
        )

    def test_named_function_expression(self, tmp_path):
        root = _repo(tmp_path, {
            "src/u.js": (
                "const helper = function helper(n) {\n"
                "  return n * 2;\n"
                "};\n"
                "console.log(helper(2));\n"
            ),
        })
        _rename_and_apply(root, "helper", "double")
        text = _read(root, "src/u.js")
        assert text.startswith("const double = function"), text
        assert "console.log(double(2));" in text, text

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_importer_still_runs_under_node(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.mjs": (
                "export function settings() {\n"
                "  const settings = { a: 1 };\n"
                "  return settings;\n"
                "}\n"
            ),
            "src/main.mjs": (
                "import { settings } from './util.mjs';\n"
                "console.log(settings().a);\n"
            ),
        })
        _rename_and_apply(root, "settings", "config")
        assert _read(root, "src/util.mjs").startswith("export function config() {\n")
        assert _read(root, "src/main.mjs") == (
            "import { config } from './util.mjs';\nconsole.log(config().a);\n"
        )
        assert _node_run(root / "src" / "main.mjs") == "1"


_SHELL_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "\n"
    "get_version() {\n"
    "  echo \"1.2.3\"\n"
    "}\n"
    "\n"
    "main() {\n"
    "  local v\n"
    "  v=\"$(get_version)\"\n"
    "  echo \"version $v\"\n"
    "}\n"
    "\n"
    "main\n"
)


class TestShellCommandSubstitution:
    def test_call_inside_quoted_command_substitution_is_renamed(self, tmp_path):
        """``v="$(get_version)"`` is code inside a string node; the call in
        it is renamed, while plain string text is not."""
        root = _repo(tmp_path, {"build.sh": _SHELL_SCRIPT})
        _rename_and_apply(root, "get_version", "read_version")
        assert _read(root, "build.sh") == _SHELL_SCRIPT.replace("get_version", "read_version")
        if shutil.which("bash"):
            proc = subprocess.run(
                ["bash", str(root / "build.sh")], capture_output=True, text=True, timeout=60,
            )
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "version 1.2.3"


class TestBarrelsWithoutANamedImportConsumer:
    """``export { name } from`` carries no graph edge; a barrel must still
    be found when no file imports the name from it by name."""

    def test_public_barrel_with_no_consumer_in_the_repo(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.ts": "export function helper(x: number): number {\n  return x * 2;\n}\n",
            "src/index.ts": "export { helper } from './util';\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/util.ts").startswith("export function double(")
        assert _read(root, "src/index.ts") == "export { double } from './util';\n"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_named_barrel_reached_only_through_a_namespace_import(self, tmp_path):
        root = _repo(tmp_path, {
            "src/util.mjs": "export function helper(x) { return x * 2; }\n",
            "src/index.mjs": "export { helper } from './util.mjs';\n",
            "src/main.mjs": "import * as lib from './index.mjs';\nconsole.log(lib.helper(4));\n",
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/index.mjs") == "export { double } from './util.mjs';\n"
        assert _read(root, "src/main.mjs") == (
            "import * as lib from './index.mjs';\nconsole.log(lib.double(4));\n"
        )
        assert _node_run(root / "src" / "main.mjs") == "8"


class TestCallEdgesAreResolvedNotGuessed:
    """A call edge to the bare name is read through the calling module's
    imports, so it finds ``pkg.util.name()`` without renaming every
    ``obj.name()`` that happens to share the name."""

    def test_unrelated_attribute_calls_keep_a_common_name(self, tmp_path):
        worker = (
            "import threading\n"
            "\n"
            "\n"
            "class Worker:\n"
            "    def run(self):\n"
            "        return 'worker'\n"
            "\n"
            "    def go(self):\n"
            "        thread = threading.Thread(target=print)\n"
            "        thread.run()\n"
            "        return self.run()\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/util.py": "def run(x):\n    return x * 2\n",
            "pkg/worker.py": worker,
        })
        preview = _rename_and_apply(root, "pkg/util.py::run", "double")
        assert {Path(e["file"]).name for e in preview["edits"]} == {"util.py"}
        assert _read(root, "pkg/worker.py") == worker
        assert _run(root, "from pkg.worker import Worker; print(Worker().go())") == "worker"

    def test_attribute_chains_resolve_to_their_own_module(self, tmp_path):
        app = (
            "from pkg.util import helper as h\n"
            "import pkg\n"
            "from pkg import sub\n"
            "import pkg.sub.tool\n"
            "\n"
            "\n"
            "def run():\n"
            "    return h(1), pkg.util.helper(2), sub.tool.helper(3), pkg.sub.tool.helper(4)\n"
        )
        root = _repo(tmp_path, {
            "pkg/__init__.py": "from . import util\n",
            "pkg/util.py": "def helper(x):\n    return x * 2\n",
            "pkg/sub/__init__.py": "from . import tool\n",
            "pkg/sub/tool.py": "def helper(x):\n    return x + 100\n",
            "app.py": app,
        })
        _rename_and_apply(root, "pkg/util.py::helper", "double")
        assert _read(root, "app.py") == (
            app.replace("import helper as h", "import double as h")
            .replace("pkg.util.helper(2)", "pkg.util.double(2)")
        )
        _build(root)
        _rename_and_apply(root, "pkg/sub/tool.py::helper", "plus")
        assert _read(root, "app.py").splitlines()[-1] == (
            "    return h(1), pkg.util.double(2), sub.tool.plus(3), pkg.sub.tool.plus(4)"
        )
        assert _run(root, "import app; print(app.run())") == "(2, 4, 103, 104)"


class TestJsNamespaceObjectsInDepth:
    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_scoped_dynamic_imports_then_callbacks_and_barrel_chains(self, tmp_path):
        main = (
            "import { helper, h } from './index.mjs';\n"
            "import * as all from './index.mjs';\n"
            "async function a() {\n"
            "  const m = await import('./util.mjs');\n"
            "  return m.helper(1);\n"
            "}\n"
            "function b() {\n"
            "  const m = { helper: (x) => x };\n"
            "  return m.helper(2);\n"
            "}\n"
            "const t = await import('./util.mjs').then((q) => q.helper(3));\n"
            "console.log(helper(4), h(5), all.helper(6), await a(), b(), t);\n"
        )
        root = _repo(tmp_path, {
            "src/util.mjs": "export function helper(x) { return x * 2; }\n",
            "src/lib/index.mjs": (
                "export { helper } from '../util.mjs';\n"
                "export { helper as h } from '../util.mjs';\n"
            ),
            "src/index.mjs": "export * from './lib/index.mjs';\n",
            "src/main.mjs": main,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "src/lib/index.mjs") == (
            "export { double } from '../util.mjs';\n"
            "export { double as h } from '../util.mjs';\n"
        )
        assert _read(root, "src/index.mjs") == "export * from './lib/index.mjs';\n"
        assert _read(root, "src/main.mjs") == (
            main.replace("import { helper, h }", "import { double, h }")
            .replace("m.helper(1)", "m.double(1)")
            .replace("q.helper(3)", "q.double(3)")
            .replace("console.log(helper(4), h(5), all.helper(6)",
                     "console.log(double(4), h(5), all.double(6)")
        )
        assert _node_run(root / "src" / "main.mjs") == "8 10 12 2 2 6"

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_commonjs_exports_keep_their_key_for_every_consumer(self, tmp_path):
        """``module.exports = { helper }`` becomes ``{ helper: double }``, so
        a namespace import, a named import, a dynamic import, a ``require``
        and a barrel all keep spelling ``helper``."""
        consumers = {
            "cjs/main.mjs": (
                "import * as ns from './util.js';\n"
                "import { helper } from './util.js';\n"
                "import { helper as viaBarrel } from './barrel.mjs';\n"
                "const m = await import('./util.js');\n"
                "console.log(ns.helper(1), helper(2), m.helper(3), viaBarrel(4));\n"
            ),
            "cjs/barrel.mjs": "export { helper } from './util.js';\n",
            "cjs/c.js": "const u = require('./util');\nconsole.log(u.helper(5));\n",
        }
        root = _repo(tmp_path, {
            "cjs/util.js": "function helper(x) { return x * 2; }\nmodule.exports = { helper };\n",
            **consumers,
        })
        _rename_and_apply(root, "helper", "double")
        assert _read(root, "cjs/util.js") == (
            "function double(x) { return x * 2; }\nmodule.exports = { helper: double };\n"
        )
        assert {rel: _read(root, rel) for rel in consumers} == consumers
        assert _node_run(root / "cjs" / "main.mjs") == "2 4 6 8"
        assert _node_run(root / "cjs" / "c.js") == "10"


class TestShellStringsStayText:
    def test_plain_string_text_before_the_call_is_not_edited(self, tmp_path):
        script = (
            "get_version() {\n"
            "  echo \"1.2.3\"\n"
            "}\n"
            "\n"
            "echo \"get_version says $(get_version)\"\n"
        )
        root = _repo(tmp_path, {"run.sh": script})
        _rename_and_apply(root, "get_version", "read_version")
        assert _read(root, "run.sh") == (
            script.replace("get_version() {", "read_version() {")
            .replace("$(get_version)", "$(read_version)")
        )

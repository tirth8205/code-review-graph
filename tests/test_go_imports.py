"""Go module-to-file import resolution.

One test per Go import form. Every assertion names the exact IMPORTS_FROM
target and requires it to be a file that exists in the repository, because
the defect being fixed was a graph full of confident-looking bare strings
(``"github.com/cli/cli/v2/pkg/iostreams"``) that matched no node, so
``importers_of`` and ``get_impact_radius`` answered 0 for packages with
hundreds of real importers.

A Go import names a *package*, which is a DIRECTORY of files, not a single
file, so one in-repo import produces exactly one edge, and that edge names
the package directory. The read path expands a directory to its files when a
caller asks (``importers_of``, ``get_impact_radius``).

One edge per file in the package was the alternative, and it does not scale:
on kubernetes it produced 73,507 edges for a single imported package, made an
incremental update disagree with a rebuild (the edge's target set depended on
which files were in the package when the importing file happened to be
parsed), and multiplied every response that lists edges.
"""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser, normalize_file_path


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pkg(name: str, body: str = "") -> str:
    return f"package {name}\n\n{body}"


def _targets(edges) -> list[str]:
    return [e.target for e in edges if e.kind == "IMPORTS_FROM"]


def _target_lines(edges) -> list[tuple[str, int]]:
    return [(e.target, e.line) for e in edges if e.kind == "IMPORTS_FROM"]


def _want(path: Path) -> str:
    return normalize_file_path(path.resolve())


def _scopes(edges) -> list[object]:
    return [
        e.extra.get("import_scope")
        for e in edges if e.kind == "IMPORTS_FROM"
    ]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small Go module: two importable packages plus a vendored one."""
    _write(tmp_path, "go.mod", "module github.com/org/repo\n\ngo 1.22\n")
    _write(tmp_path, "pkg/sub/sub.go", _pkg("sub", "func Sub() {}\n"))
    _write(tmp_path, "pkg/sub/extra.go", _pkg("sub", "func Extra() {}\n"))
    _write(tmp_path, "pkg/sub/sub_test.go", _pkg("sub", "func TestSub() {}\n"))
    _write(tmp_path, "internal/util/util.go", _pkg("util", "func Util() {}\n"))
    _write(
        tmp_path,
        "vendor/github.com/ext/lib/lib.go",
        _pkg("lib", "func Lib() {}\n"),
    )
    return tmp_path


def _parse(repo: Path, rel: str, source: str):
    path = _write(repo, rel, source)
    parser = CodeParser(repo_root=str(repo))
    _nodes, edges = parser.parse_file(path)
    return edges


class TestGoImportForms:
    def test_single_in_repo_import_names_the_package_directory(self, repo):
        """`import "<module>/pkg/sub"` -> one edge naming pkg/sub itself.

        One edge, not one per file in the directory: the package has two
        importable files and kubernetes' busiest has thirty.
        """
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/sub"\n',
        )
        assert _targets(edges) == [_want(repo / "pkg/sub")]
        assert _scopes(edges) == ["package"]
        assert Path(_targets(edges)[0]).is_dir()

    def test_grouped_block_resolves_each_spec_at_its_own_line(self, repo):
        """Every spec in `import ( ... )` keeps its own line, not the block's."""
        source = (
            "package main\n"
            "\n"
            "import (\n"
            '\t"fmt"\n'
            '\t"github.com/org/repo/pkg/sub"\n'
            '\t"github.com/org/repo/internal/util"\n'
            ")\n"
        )
        edges = _parse(repo, "main.go", source)
        by_target = dict(_target_lines(edges))
        assert by_target["fmt"] == 4
        assert by_target[_want(repo / "pkg/sub")] == 5
        assert by_target[_want(repo / "internal/util")] == 6

    def test_named_import_resolves_and_drops_the_alias(self, repo):
        edges = _parse(
            repo, "main.go",
            "package main\n\n"
            'import alias "github.com/org/repo/internal/util"\n',
        )
        assert _targets(edges) == [_want(repo / "internal/util")]
        assert Path(_targets(edges)[0]).is_dir()

    def test_blank_import_resolves_like_a_normal_import(self, repo):
        """`_ "pkg"` is a real side-effect dependency, not a comment."""
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport _ "github.com/org/repo/internal/util"\n',
        )
        assert _targets(edges) == [_want(repo / "internal/util")]

    def test_dot_import_resolves_like_a_normal_import(self, repo):
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport . "github.com/org/repo/internal/util"\n',
        )
        assert _targets(edges) == [_want(repo / "internal/util")]

    def test_vendored_third_party_stays_bare_because_vendor_is_not_indexed(
        self, repo,
    ):
        """``**/vendor/**`` is a default ignore, so nothing in it is a node.

        73,507 kubernetes import edges used to name a file under ``vendor/``.
        None of those files is indexed, so every one of them was a confident
        path that resolved to nothing -- worse than the bare import string it
        replaced, because a bare string is visibly external.
        """
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/ext/lib"\n',
        )
        assert _targets(edges) == ["github.com/ext/lib"]
        assert _scopes(edges) == [None]

    def test_internal_package_resolves(self, repo):
        """3495 of cli/cli's 8689 go import edges name an in-repo package,
        1317 of them under `internal/`."""
        edges = _parse(
            repo, "pkg/sub/user.go",
            "package sub\n\n"
            'import "github.com/org/repo/internal/util"\n',
        )
        assert _targets(edges) == [_want(repo / "internal/util")]

    def test_standard_library_stays_a_bare_string(self, repo):
        source = (
            "package main\n"
            "\n"
            "import (\n"
            '\t"fmt"\n'
            '\t"net/http"\n'
            '\t"path/filepath"\n'
            '\t"testing"\n'
            ")\n"
        )
        edges = _parse(repo, "main.go", source)
        assert _targets(edges) == ["fmt", "net/http", "path/filepath", "testing"]

    def test_unvendored_third_party_stays_a_bare_string(self, repo):
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/spf13/cobra"\n',
        )
        assert _targets(edges) == ["github.com/spf13/cobra"]


class TestGoModuleAnchoring:
    def test_module_path_with_major_version_suffix(self, tmp_path):
        """cli/cli declares `module github.com/cli/cli/v2`."""
        _write(tmp_path, "go.mod", "module github.com/cli/cli/v2\n\ngo 1.22\n")
        _write(tmp_path, "pkg/iostreams/iostreams.go", _pkg("iostreams"))
        edges = _parse(
            tmp_path, "cmd/gh/main.go",
            "package main\n\n"
            'import "github.com/cli/cli/v2/pkg/iostreams"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "pkg/iostreams")]

    def test_import_equal_to_the_module_path_names_the_root_package(self, tmp_path):
        _write(tmp_path, "go.mod", "module github.com/spf13/cobra\n\ngo 1.15\n")
        _write(tmp_path, "command.go", _pkg("cobra"))
        _write(tmp_path, "args.go", _pkg("cobra"))
        edges = _parse(
            tmp_path, "doc/md_docs.go",
            'package doc\n\nimport "github.com/spf13/cobra"\n',
        )
        assert _targets(edges) == [_want(tmp_path)]

    def test_nested_go_mod_wins_over_the_ancestor(self, tmp_path):
        """A file under a nested module resolves against that module first."""
        _write(tmp_path, "go.mod", "module github.com/org/outer\n\ngo 1.22\n")
        _write(tmp_path, "pkg/thing/thing.go", _pkg("thing"))
        _write(tmp_path, "tools/go.mod", "module example.com/tools\n\ngo 1.22\n")
        _write(tmp_path, "tools/helper/helper.go", _pkg("helper"))
        edges = _parse(
            tmp_path, "tools/main.go",
            'package main\n\nimport "example.com/tools/helper"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "tools/helper")]

    def test_nested_module_still_reaches_the_outer_module(self, tmp_path):
        _write(tmp_path, "go.mod", "module github.com/org/outer\n\ngo 1.22\n")
        _write(tmp_path, "pkg/thing/thing.go", _pkg("thing"))
        _write(tmp_path, "tools/go.mod", "module example.com/tools\n\ngo 1.22\n")
        edges = _parse(
            tmp_path, "tools/main.go",
            'package main\n\nimport "github.com/org/outer/pkg/thing"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "pkg/thing")]

    def test_local_replace_directive_is_honoured(self, tmp_path):
        _write(
            tmp_path, "go.mod",
            "module github.com/org/app\n"
            "\n"
            "go 1.22\n"
            "\n"
            "replace github.com/org/lib => ./third_party/lib\n",
        )
        _write(tmp_path, "third_party/lib/lib.go", _pkg("lib"))
        edges = _parse(
            tmp_path, "main.go",
            'package main\n\nimport "github.com/org/lib"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "third_party/lib")]

    def test_local_replace_block_form_is_honoured(self, tmp_path):
        _write(
            tmp_path, "go.mod",
            "module github.com/org/app\n"
            "\n"
            "go 1.22\n"
            "\n"
            "replace (\n"
            "\tgithub.com/org/lib => ./third_party/lib\n"
            ")\n",
        )
        _write(tmp_path, "third_party/lib/sub/sub.go", _pkg("sub"))
        edges = _parse(
            tmp_path, "main.go",
            'package main\n\nimport "github.com/org/lib/sub"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "third_party/lib/sub")]

    def test_no_go_mod_leaves_every_import_bare(self, tmp_path):
        _write(tmp_path, "pkg/sub/sub.go", _pkg("sub"))
        edges = _parse(
            tmp_path, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/sub"\n',
        )
        assert _targets(edges) == ["github.com/org/repo/pkg/sub"]


class TestGoNonFabrication:
    def test_stdlib_name_is_not_bound_to_a_same_named_repo_directory(self, tmp_path):
        """The upward-directory-name walk would bind 149 of cli/cli's 3575
        stdlib imports to repo directories (context 133, embed 11, io 5)."""
        _write(tmp_path, "go.mod", "module github.com/org/repo\n\ngo 1.22\n")
        _write(tmp_path, "context/context.go", _pkg("context"))
        _write(tmp_path, "io/io.go", _pkg("io"))
        edges = _parse(
            tmp_path, "cmd/main.go",
            "package main\n\nimport (\n\t\"context\"\n\t\"io\"\n)\n",
        )
        assert _targets(edges) == ["context", "io"]

    def test_missing_package_directory_stays_bare(self, repo):
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/nope"\n',
        )
        assert _targets(edges) == ["github.com/org/repo/pkg/nope"]

    def test_package_directory_without_go_files_stays_bare(self, repo):
        (repo / "pkg" / "docsonly").mkdir(parents=True)
        (repo / "pkg" / "docsonly" / "README.md").write_text("x", encoding="utf-8")
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/docsonly"\n',
        )
        assert _targets(edges) == ["github.com/org/repo/pkg/docsonly"]

    def test_no_file_is_ever_an_import_target(self, repo):
        """A Go import names a package, so the edge names a directory.

        Nothing in the edge depends on which files the package contains,
        which is what makes an incremental update equal a rebuild.
        """
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/sub"\n',
        )
        assert not any(t.endswith(".go") for t in _targets(edges))

    def test_target_outside_the_repository_root_is_not_emitted(self, tmp_path):
        """A `replace` pointing above the root must not escape it."""
        outside = tmp_path / "outside"
        _write(outside, "lib/lib.go", _pkg("lib"))
        root = tmp_path / "repo"
        _write(
            root, "go.mod",
            "module github.com/org/app\n\ngo 1.22\n"
            "\nreplace github.com/org/lib => ../outside/lib\n",
        )
        edges = _parse(
            root, "main.go",
            'package main\n\nimport "github.com/org/lib"\n',
        )
        assert _targets(edges) == ["github.com/org/lib"]

    def test_resolution_is_case_exact(self, repo):
        """`pkg/Sub` is a different package from `pkg/sub` in Go."""
        edges = _parse(
            repo, "main.go",
            'package main\n\nimport "github.com/org/repo/pkg/Sub"\n',
        )
        assert _targets(edges) == ["github.com/org/repo/pkg/Sub"]

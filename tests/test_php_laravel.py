"""Focused coverage for the PHP, Composer, Blade, and Laravel parser port."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import parser as parser_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser


def _write_composer(repo: Path, data: object) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    composer = repo / "composer.json"
    composer.write_text(json.dumps(data), encoding="utf-8")
    return composer


def _write_php(path: Path, source: str = "<?php\nclass Placeholder {}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _php_import_target(parser: CodeParser, caller: Path, module: str) -> str:
    _write_php(
        caller,
        "<?php\n"
        f"use {module};\n"
        "class Caller {}\n",
    )
    _, edges = parser.parse_file(caller)
    imports = [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]
    assert len(imports) == 1, imports
    return imports[0]


@pytest.fixture(autouse=True)
def _clear_composer_loader_cache():
    clear = getattr(parser_module._read_php_composer_psr4, "cache_clear", None)
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


class TestComposerPsr4:
    def test_composer_resolves_standard_psr4_mapping(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        target = _write_php(repo / "app/Models/User.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "app/Services/Report.php",
            "App\\Models\\User",
        )

        assert resolved == target.resolve().as_posix()

    def test_composer_uses_longest_matching_prefix(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(
            repo,
            {
                "autoload": {
                    "psr-4": {
                        "App\\": "fallback/",
                        "App\\Domain\\": "domain/",
                    },
                },
            },
        )
        _write_php(repo / "fallback/Domain/Thing.php")
        target = _write_php(repo / "domain/Thing.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/Caller.php",
            "App\\Domain\\Thing",
        )

        assert resolved == target.resolve().as_posix()

    def test_composer_checks_every_directory_for_prefix(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(
            repo,
            {"autoload": {"psr-4": {"App\\": ["missing/", "src/"]}}},
        )
        target = _write_php(repo / "src/Thing.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "app/Caller.php",
            "App\\Thing",
        )

        assert resolved == target.resolve().as_posix()

    def test_composer_merges_autoload_and_autoload_dev_directories(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(
            repo,
            {
                "autoload": {"psr-4": {"App\\": "app/"}},
                "autoload-dev": {"psr-4": {"App\\": "dev/"}},
            },
        )
        target = _write_php(repo / "dev/Tests/Factory.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "tests/Caller.php",
            "App\\Tests\\Factory",
        )

        assert resolved == target.resolve().as_posix()

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {"autoload": []},
            {"autoload": {"psr-4": []}},
            {"autoload": {"psr-4": {"App\\": 7}}},
            {"autoload": {"psr-4": {"App\\": [7, None]}}},
            {"autoload-dev": None},
        ],
    )
    def test_composer_malformed_shapes_are_ignored(self, tmp_path, data):
        repo = tmp_path / "repo"
        _write_composer(repo, data)

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/Caller.php",
            "App\\Missing",
        )

        assert resolved == "App\\Missing"

    def test_composer_rejects_parent_traversal_outside_repo(self, tmp_path):
        repo = tmp_path / "repo"
        outside = tmp_path / "outside"
        _write_composer(
            repo,
            {"autoload": {"psr-4": {"Evil\\": "../outside/"}}},
        )
        _write_php(outside / "Secret.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/Caller.php",
            "Evil\\Secret",
        )

        assert resolved == "Evil\\Secret"

    def test_composer_rejects_absolute_mapping_outside_repo(self, tmp_path):
        repo = tmp_path / "repo"
        outside = tmp_path / "outside"
        target = _write_php(outside / "Secret.php")
        _write_composer(
            repo,
            {"autoload": {"psr-4": {"Evil\\": str(outside)}}},
        )

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/Caller.php",
            "Evil\\Secret",
        )

        assert resolved != str(target.resolve())
        assert resolved == "Evil\\Secret"

    def test_composer_rejects_symlink_mapping_outside_repo(self, tmp_path):
        repo = tmp_path / "repo"
        outside = tmp_path / "outside"
        _write_php(outside / "Secret.php")
        _write_composer(
            repo,
            {"autoload": {"psr-4": {"Evil\\": "linked/"}}},
        )
        try:
            (repo / "linked").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/Caller.php",
            "Evil\\Secret",
        )

        assert resolved == "Evil\\Secret"

    def test_composer_does_not_resolve_caller_outside_configured_repo(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        _write_php(repo / "app/User.php")
        caller = _write_php(tmp_path / "outside/Caller.php")

        resolved = CodeParser(repo)._resolve_module_to_file(
            "App\\User", str(caller), "php",
        )

        assert resolved is None

    def test_php_ancestor_fallback_does_not_escape_configured_repo(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"Other\\": "other/"}}})
        caller = _write_php(repo / "src/Caller.php")
        _write_php(tmp_path / "Outside/Foo.php")

        resolved = CodeParser(repo)._resolve_module_to_file(
            "Outside\\Foo", str(caller), "php",
        )

        assert resolved is None

    def test_php_ancestor_fallback_without_repo_does_not_climb_above_caller(
        self, tmp_path,
    ):
        caller = _write_php(tmp_path / "project/src/Caller.php")
        _write_php(tmp_path / "project/Outside/Foo.php")

        resolved = CodeParser()._resolve_module_to_file(
            "Outside\\Foo", str(caller), "php",
        )

        assert resolved is None

    def test_php_ancestor_fallback_rejects_symlink_target_outside_repo(
        self, tmp_path,
    ):
        repo = tmp_path / "repo"
        outside = tmp_path / "outside"
        _write_composer(repo, {"autoload": {"psr-4": {"Other\\": "other/"}}})
        caller = _write_php(repo / "src/Caller.php")
        _write_php(outside / "Foo.php")
        try:
            (repo / "src/Outside").symlink_to(
                outside,
                target_is_directory=True,
            )
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        resolved = CodeParser(repo)._resolve_module_to_file(
            "Outside\\Foo", str(caller), "php",
        )

        assert resolved is None

    def test_php_ancestor_fallback_fails_soft_when_start_resolution_raises(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "repo"
        caller = _write_php(repo / "src/Caller.php")
        target = _write_php(repo / "src/App/Foo.php")
        parser = CodeParser(repo)
        monkeypatch.setattr(
            parser,
            "_resolve_php_composer_module",
            lambda _module, _caller_dir: None,
        )
        original_resolve = Path.resolve

        def raise_for_caller(path, *args, **kwargs):
            if path == caller.parent:
                raise RuntimeError("synthetic resolution failure")
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", raise_for_caller)

        resolved = parser._resolve_module_to_file(
            "App\\Foo", str(caller), "php",
        )

        assert target.is_file()
        assert resolved is None

    def test_composer_keeps_existing_php_ancestor_fallback(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"Other\\": "other/"}}})
        target = _write_php(repo / "src/App/Domain/Thing.php")

        resolved = _php_import_target(
            CodeParser(repo),
            repo / "src/App/Service/Caller.php",
            "App\\Domain\\Thing",
        )

        assert resolved == target.resolve().as_posix()


class TestComposerCache:
    def test_composer_cache_is_bounded(self):
        cache_info = getattr(
            parser_module._read_php_composer_psr4,
            "cache_info",
            None,
        )

        assert cache_info is not None
        assert cache_info().maxsize == 128

    def test_composer_cache_reuses_unchanged_file_across_parsers(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "repo"
        composer = _write_composer(
            repo,
            {"autoload": {"psr-4": {"App\\": "app/"}}},
        ).resolve()
        target = _write_php(repo / "app/User.php")
        caller = _write_php(repo / "src/Caller.php")
        original_read_text = Path.read_text
        composer_reads = 0

        def counting_read_text(path, *args, **kwargs):
            nonlocal composer_reads
            if path.resolve() == composer:
                composer_reads += 1
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        first = CodeParser(repo)._resolve_module_to_file(
            "App\\User", str(caller), "php",
        )
        second = CodeParser(repo)._resolve_module_to_file(
            "App\\User", str(caller), "php",
        )

        assert first == second == target.resolve().as_posix()
        assert composer_reads == 1

    def test_composer_cache_reloads_after_stat_change(self, tmp_path):
        repo = tmp_path / "repo"
        composer = _write_composer(
            repo,
            {"autoload": {"psr-4": {"App\\": "app/"}}},
        )
        _write_php(repo / "app/User.php")
        caller = _write_php(repo / "src/Caller.php")

        first = CodeParser(repo)._resolve_module_to_file(
            "App\\User", str(caller), "php",
        )

        composer.write_text(
            json.dumps(
                {"autoload": {"psr-4": {"App\\": "updated-source/"}}},
            ),
            encoding="utf-8",
        )
        updated = _write_php(repo / "updated-source/User.php")
        second = CodeParser(repo)._resolve_module_to_file(
            "App\\User", str(caller), "php",
        )

        assert first != second
        assert second == updated.resolve().as_posix()

    def test_composer_cache_isolated_between_repositories(self, tmp_path):
        first_repo = tmp_path / "first"
        second_repo = tmp_path / "second"
        _write_composer(
            first_repo,
            {"autoload": {"psr-4": {"App\\": "one/"}}},
        )
        _write_composer(
            second_repo,
            {"autoload": {"psr-4": {"App\\": "two/"}}},
        )
        first_target = _write_php(first_repo / "one/User.php")
        second_target = _write_php(second_repo / "two/User.php")
        first_caller = _write_php(first_repo / "src/Caller.php")
        second_caller = _write_php(second_repo / "src/Caller.php")

        first = CodeParser(first_repo)._resolve_module_to_file(
            "App\\User", str(first_caller), "php",
        )
        second = CodeParser(second_repo)._resolve_module_to_file(
            "App\\User", str(second_caller), "php",
        )

        assert first == first_target.resolve().as_posix()
        assert second == second_target.resolve().as_posix()


class TestBladeParsing:
    def test_blade_compound_extension_and_directives(self, tmp_path):
        template = tmp_path / "resources/views/home.blade.php"
        source = b"""{{-- @include('commented.out') --}}
@@include('escaped.out')
@extends('layouts.app')
@include("partials.header")
@component('components.alert')
@livewire('counter')
"""
        parser = CodeParser(tmp_path)

        nodes, edges = parser.parse_bytes(template, source)

        assert parser.detect_language(template) == "blade"
        assert len(nodes) == 1
        assert nodes[0].kind == "File"
        assert nodes[0].name == template.as_posix()
        assert nodes[0].file_path == template.as_posix()
        assert nodes[0].language == "blade"
        assert nodes[0].line_end == 7

        imports = {
            edge.target: edge.line
            for edge in edges
            if edge.kind == "IMPORTS_FROM"
        }
        references = {
            edge.target: edge.line
            for edge in edges
            if edge.kind == "REFERENCES"
        }
        assert imports == {
            "layouts.app": 3,
            "partials.header": 4,
            "components.alert": 5,
        }
        assert references == {"counter": 6}

    def test_blade_detection_is_case_insensitive(self):
        assert CodeParser().detect_language(Path("HOME.BLADE.PHP")) == "blade"

    def test_blade_ignores_multiline_and_unterminated_comments(self, tmp_path):
        parser = CodeParser(tmp_path)
        commented = b"""{{-- start
@include('hidden.one')
--}}
@include('visible')
"""
        unterminated = b"""@extends('visible.before')
{{-- @include('hidden.two')
@livewire('hidden.three')
"""

        _, commented_edges = parser.parse_bytes(
            tmp_path / "commented.blade.php",
            commented,
        )
        _, unterminated_edges = parser.parse_bytes(
            tmp_path / "unterminated.blade.php",
            unterminated,
        )

        assert [(edge.target, edge.line) for edge in commented_edges] == [
            ("visible", 4),
        ]
        assert [(edge.target, edge.line) for edge in unterminated_edges] == [
            ("visible.before", 1),
        ]

    def test_blade_ignores_all_escaped_directive_forms(self, tmp_path):
        source = b"""@@extends('escaped.layout')
@@include('escaped.partial')
@@component('escaped.component')
@@livewire('escaped.livewire')
"""

        _, edges = CodeParser(tmp_path).parse_bytes(
            tmp_path / "escaped.blade.php",
            source,
        )

        assert edges == []

    def test_blade_replaces_invalid_utf8_without_losing_directive(self, tmp_path):
        source = b"\xff\n@extends('layouts.app')\n"

        nodes, edges = CodeParser(tmp_path).parse_bytes(
            tmp_path / "invalid.blade.php",
            source,
        )

        assert nodes[0].line_end == 3
        assert [(edge.target, edge.line) for edge in edges] == [
            ("layouts.app", 2),
        ]

    def test_blade_handling_does_not_change_regular_php(self, tmp_path):
        path = tmp_path / "ordinary.php"
        source = b"<?php\n// @include('not.blade')\nclass Ordinary {}\n"

        nodes, edges = CodeParser(tmp_path).parse_bytes(path, source)

        assert any(
            node.kind == "File" and node.language == "php"
            for node in nodes
        )
        assert any(
            node.kind == "Class" and node.name == "Ordinary"
            for node in nodes
        )
        assert not any(
            edge.kind == "IMPORTS_FROM" and edge.target == "not.blade"
            for edge in edges
        )


def _laravel_edges(edges, kind: str | None = None):
    return [
        edge for edge in edges
        if edge.extra.get("framework") == "laravel"
        and (kind is None or edge.kind == kind)
    ]


class TestLaravelSemantics:
    def test_laravel_route_alias_resolves_grouped_controller_import(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        controller = _write_php(
            repo / "app/Http/Controllers/UserController.php",
            "<?php\nnamespace App\\Http\\Controllers;\n"
            "class UserController { public function index(): void {} }\n",
        )
        source = br"""<?php
use Illuminate\Support\Facades\Route as Router;
use App\Http\Controllers\{UserController as Users};
Router::get('/users', [Users::class, 'index']);
"""

        _, edges = CodeParser(repo).parse_bytes(repo / "routes/web.php", source)

        semantic = _laravel_edges(edges, "CALLS")
        assert [(edge.target, edge.extra["laravel_kind"]) for edge in semantic] == [
            (f"{controller.resolve().as_posix()}::UserController.index", "route"),
        ]
        assert len([
            edge for edge in edges
            if edge.kind == "CALLS" and edge.target == "Router::get"
        ]) == 1

    def test_laravel_route_accepts_fully_qualified_framework_and_controller(
        self, tmp_path,
    ):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        controller = _write_php(
            repo / "app/Http/Controllers/UserController.php",
        )
        source = br"""<?php
\Illuminate\Support\Facades\Route::post(
    '/users',
    [\App\Http\Controllers\UserController::class, "store"]
);
"""

        _, edges = CodeParser(repo).parse_bytes(repo / "routes/api.php", source)

        semantic = _laravel_edges(edges, "CALLS")
        assert [edge.target for edge in semantic] == [
            f"{controller.resolve().as_posix()}::UserController.store",
        ]
        assert len([
            edge for edge in edges
            if edge.kind == "CALLS"
            and edge.target == "Illuminate\\Support\\Facades\\Route::post"
        ]) == 1

    def test_laravel_eloquent_aliases_resolve_model_reference(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        post = _write_php(
            repo / "app/Models/Post.php",
            "<?php\nnamespace App\\Models;\nclass Post {}\n",
        )
        source = br"""<?php
namespace App\Models;
use Illuminate\Database\Eloquent\Model as BaseModel;
use App\Models\Post as Article;

class User extends BaseModel {
    public function posts() {
        return $this->hasMany(Article::class);
    }
}
"""

        _, edges = CodeParser(repo).parse_bytes(repo / "app/Models/User.php", source)

        semantic = _laravel_edges(edges, "REFERENCES")
        assert [(edge.target, edge.extra["relationship"]) for edge in semantic] == [
            (f"{post.resolve().as_posix()}::Post", "hasMany"),
        ]
        assert len([
            edge for edge in edges
            if edge.kind == "CALLS" and edge.target == "hasMany"
        ]) == 1

    def test_laravel_eloquent_accepts_fully_qualified_model_names(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        post = _write_php(repo / "app/Models/Post.php")
        source = br"""<?php
namespace App\Models;
class User extends \Illuminate\Database\Eloquent\Model {
    public function post() {
        return $this->belongsTo(\App\Models\Post::class);
    }
}
"""

        _, edges = CodeParser(repo).parse_bytes(repo / "app/Models/User.php", source)

        semantic = _laravel_edges(edges, "REFERENCES")
        assert [edge.target for edge in semantic] == [
            f"{post.resolve().as_posix()}::Post",
        ]
        assert semantic[0].extra["relationship"] == "belongsTo"

    def test_laravel_namespace_block_imports_do_not_leak(self, tmp_path):
        repo = tmp_path / "repo"
        _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
        post = _write_php(repo / "app/Models/Post.php")
        source = br"""<?php
namespace App\Good {
    use Illuminate\Database\Eloquent\Model;
    use App\Models\Post;
    class User extends Model {
        public function posts() { return $this->hasOne(Post::class); }
    }
}
namespace App\Bad {
    class Pretender extends Model {
        public function posts() { return $this->hasOne(Post::class); }
    }
}
"""

        _, edges = CodeParser(repo).parse_bytes(repo / "app/Mixed.php", source)

        semantic = _laravel_edges(edges, "REFERENCES")
        assert [edge.target for edge in semantic] == [
            f"{post.resolve().as_posix()}::Post",
        ]
        assert semantic[0].source.endswith("::User.posts")

    def test_laravel_requires_route_framework_and_static_handler_evidence(
        self, tmp_path,
    ):
        source = br"""<?php
use Acme\Routing\Route;
use App\Http\Controllers\UserController;
Route::get('/unrelated', [UserController::class, 'index']);

use Illuminate\Support\Facades\Route as Router;
$method = 'show';
Router::get('/dynamic', [UserController::class, $method]);
"""

        _, edges = CodeParser(tmp_path).parse_bytes(tmp_path / "routes.php", source)

        assert _laravel_edges(edges, "CALLS") == []
        generic_targets = {
            edge.target for edge in edges if edge.kind == "CALLS"
        }
        assert {"Route::get", "Router::get"} <= generic_targets

    def test_laravel_requires_route_import_when_short_facade_is_used(self, tmp_path):
        source = br"""<?php
Route::get('/users', [UserController::class, 'index']);
"""

        _, edges = CodeParser(tmp_path).parse_bytes(tmp_path / "routes.php", source)

        assert _laravel_edges(edges, "CALLS") == []
        assert any(
            edge.kind == "CALLS" and edge.target == "Route::get"
            for edge in edges
        )

    def test_laravel_requires_model_class_receiver_and_class_argument(self, tmp_path):
        source = br"""<?php
use Illuminate\Database\Eloquent\Model;
use App\Models\Post;

class Plain {
    public function falsePositive() { return $this->hasMany(Post::class); }
}
class User extends Model {
    public function wrongReceiver($builder) {
        return $builder->hasMany(Post::class);
    }
    public function similarName() {
        return $this->hasManyCustom(Post::class);
    }
    public function stringArgument() {
        return $this->belongsTo('Post');
    }
}
"""

        _, edges = CodeParser(tmp_path).parse_bytes(tmp_path / "Models.php", source)

        assert _laravel_edges(edges, "REFERENCES") == []
        generic_targets = [
            edge.target for edge in edges if edge.kind == "CALLS"
        ]
        assert generic_targets.count("hasMany") == 2
        assert "hasManyCustom" in generic_targets
        assert "belongsTo" in generic_targets

    def test_laravel_unresolved_model_keeps_stable_short_target(self, tmp_path):
        source = br"""<?php
use Illuminate\Database\Eloquent\Model;
use App\Models\Missing;
class User extends Model {
    public function missing() { return $this->morphOne(Missing::class); }
}
"""

        _, edges = CodeParser(tmp_path).parse_bytes(tmp_path / "User.php", source)

        semantic = _laravel_edges(edges, "REFERENCES")
        assert [edge.target for edge in semantic] == ["Missing"]


def _graph_snapshot(store: GraphStore) -> tuple[list[tuple], list[tuple]]:
    nodes = sorted(
        (
            node.kind,
            node.name,
            node.qualified_name,
            node.file_path,
            node.language,
            json.dumps(node.extra, sort_keys=True),
        )
        for node in store.get_all_nodes(exclude_files=False)
    )
    edges = sorted(
        (
            edge.kind,
            edge.source_qualified,
            edge.target_qualified,
            edge.file_path,
            edge.line,
            json.dumps(edge.extra, sort_keys=True),
        )
        for edge in store.get_all_edges()
    )
    return nodes, edges


def test_composer_process_pool_matches_serial_build(tmp_path):
    repo = tmp_path / "repo"
    _write_composer(repo, {"autoload": {"psr-4": {"App\\": "app/"}}})
    user = _write_php(
        repo / "app/Models/User.php",
        "<?php\nnamespace App\\Models;\nclass User {}\n",
    )
    callers = []
    for index in range(8):
        callers.append(_write_php(
            repo / f"app/Services/Service{index}.php",
            "<?php\n"
            f"namespace App\\Services;\nuse App\\Models\\User;\n"
            f"class Service{index} {{\n"
            "    public function build(): User { return new User(); }\n"
            "}\n",
        ))
    tracked = [
        str(path.relative_to(repo))
        for path in [user, *callers]
    ]

    serial_store = GraphStore(repo / "serial.db")
    parallel_store = GraphStore(repo / "parallel.db")
    try:
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=tracked,
        ):
            with patch.dict(
                "os.environ",
                {"CRG_SERIAL_PARSE": "1", "CRG_PARSE_EXECUTOR": "process"},
            ):
                serial_result = full_build(repo, serial_store)
            parser_module._read_php_composer_psr4.cache_clear()
            with patch.dict(
                "os.environ",
                {"CRG_SERIAL_PARSE": "", "CRG_PARSE_EXECUTOR": "process"},
            ):
                parallel_result = full_build(repo, parallel_store)

        assert serial_result["errors"] == []
        assert parallel_result["errors"] == []
        assert serial_result["files_parsed"] == parallel_result["files_parsed"] == 9
        assert _graph_snapshot(serial_store) == _graph_snapshot(parallel_store)
    finally:
        serial_store.close()
        parallel_store.close()

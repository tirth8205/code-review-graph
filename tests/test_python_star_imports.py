"""Regression tests for repository-bounded Python wildcard imports."""

import json
from pathlib import Path

from code_review_graph.parser import CodeParser


def _call_targets(repo_root: Path, source_file: Path) -> set[str]:
    _, edges = CodeParser(repo_root).parse_file(source_file)
    return {edge.target for edge in edges if edge.kind == "CALLS"}


def test_star_import_resolves_public_function_from_direct_module(tmp_path: Path) -> None:
    helper = tmp_path / "helpers.py"
    helper.write_text(
        "def public_helper():\n"
        "    return 1\n\n"
        "def _private_helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.py"
    caller.write_text(
        "from helpers import *\n\n"
        "def run():\n"
        "    public_helper()\n"
        "    _private_helper()\n",
        encoding="utf-8",
    )

    targets = _call_targets(tmp_path, caller)

    assert f"{helper.as_posix()}::public_helper" in targets
    assert "_private_helper" in targets


def test_star_import_honors_explicit_dunder_all(tmp_path: Path) -> None:
    helper = tmp_path / "helpers.py"
    helper.write_text(
        "__all__ = ['_selected_helper']\n\n"
        "def public_helper():\n"
        "    return 1\n\n"
        "def _selected_helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.py"
    caller.write_text(
        "from helpers import *\n\n"
        "def run():\n"
        "    public_helper()\n"
        "    _selected_helper()\n",
        encoding="utf-8",
    )

    targets = _call_targets(tmp_path, caller)

    assert "public_helper" in targets
    assert f"{helper.as_posix()}::_selected_helper" in targets


def test_star_import_resolves_transitive_relative_export(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    leaf = package / "leaf.py"
    leaf.write_text(
        "def transitive_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (package / "bridge.py").write_text(
        "from .leaf import *\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.py"
    caller.write_text(
        "from package.bridge import *\n\n"
        "def run():\n"
        "    transitive_helper()\n",
        encoding="utf-8",
    )

    targets = _call_targets(tmp_path, caller)

    assert f"{leaf.as_posix()}::transitive_helper" in targets


def test_star_import_does_not_follow_symlink_outside_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_helper = outside / "helpers.py"
    outside_helper.write_text(
        "def external_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    outside_caller = outside / "caller.py"
    outside_caller.write_text(
        "from helpers import *\n\n"
        "def run():\n"
        "    external_helper()\n",
        encoding="utf-8",
    )
    linked_caller = repo / "caller.py"
    linked_caller.symlink_to(outside_caller)

    outside_reads = 0
    original_read_bytes = Path.read_bytes

    def count_outside_reads(path: Path) -> bytes:
        nonlocal outside_reads
        if path == outside_helper:
            outside_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", count_outside_reads)

    targets = _call_targets(repo, linked_caller)

    assert outside_reads == 0
    assert "external_helper" in targets


def test_parse_worker_reuses_star_export_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from code_review_graph.incremental import _parse_single_file

    helper = tmp_path / "helpers.py"
    helper.write_text(
        "def cached_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    for filename in ("caller_a.py", "caller_b.py"):
        (tmp_path / filename).write_text(
            "from helpers import *\n\n"
            "def run():\n"
            "    cached_helper()\n",
            encoding="utf-8",
        )

    helper_reads = 0
    original_read_bytes = Path.read_bytes

    def counting_read_bytes(path: Path) -> bytes:
        nonlocal helper_reads
        if path == helper:
            helper_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    _parse_single_file(("caller_a.py", str(tmp_path)))
    _parse_single_file(("caller_b.py", str(tmp_path)))

    assert helper_reads == 1


def test_concurrent_parsers_compute_star_exports_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    helper = tmp_path / "helpers.py"
    helper.write_text(
        "def concurrent_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    callers = []
    for index in range(8):
        caller = tmp_path / f"caller_{index}.py"
        caller.write_text(
            "from helpers import *\n\n"
            "def run():\n"
            "    concurrent_helper()\n",
            encoding="utf-8",
        )
        callers.append(caller)

    helper_reads = 0
    count_lock = threading.Lock()
    original_read_bytes = Path.read_bytes

    def slow_counting_read_bytes(path: Path) -> bytes:
        nonlocal helper_reads
        if path == helper:
            with count_lock:
                helper_reads += 1
            time.sleep(0.02)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", slow_counting_read_bytes)

    with ThreadPoolExecutor(max_workers=len(callers)) as executor:
        targets = list(
            executor.map(lambda caller: _call_targets(tmp_path, caller), callers)
        )

    assert helper_reads == 1
    assert all(f"{helper.as_posix()}::concurrent_helper" in result for result in targets)


def test_star_export_parse_error_leaves_caller_parseable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    helper = tmp_path / "helpers.py"
    helper.write_text(
        "def broken_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.py"
    caller.write_text(
        "from helpers import *\n\n"
        "def run():\n"
        "    broken_helper()\n",
        encoding="utf-8",
    )
    parser = CodeParser(tmp_path)
    original_get_parser = parser._get_parser
    get_parser_calls = 0

    class BrokenParser:
        def parse(self, _source: bytes):
            raise RuntimeError("broken imported-module parser")

    def fail_on_imported_module(language: str):
        nonlocal get_parser_calls
        get_parser_calls += 1
        if get_parser_calls == 2:
            return BrokenParser()
        return original_get_parser(language)

    monkeypatch.setattr(parser, "_get_parser", fail_on_imported_module)

    nodes, edges = parser.parse_file(caller)

    assert any(node.kind == "File" for node in nodes)
    assert "broken_helper" in {
        edge.target for edge in edges if edge.kind == "CALLS"
    }


def test_notebook_star_import_resolves_repository_module(tmp_path: Path) -> None:
    helper = tmp_path / "helpers.py"
    helper.write_text(
        "def notebook_helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    notebook = tmp_path / "analysis.ipynb"
    notebook.write_text(
        json.dumps({
            "cells": [{
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "from helpers import *\n",
                    "notebook_helper()\n",
                ],
            }],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                },
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }),
        encoding="utf-8",
    )

    targets = _call_targets(tmp_path, notebook)

    assert f"{helper.as_posix()}::notebook_helper" in targets

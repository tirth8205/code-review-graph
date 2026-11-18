"""Pydantic surfaces and overrides are invoked by the runtime, not by call sites.

A computed field, a cached property, a validator and an ``@override`` method have
no caller in the source: the model machinery or the supertype calls them. They
are live code, and reporting them dead sends a reviewer to delete the contract.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

SOURCE = '''\
from functools import cached_property
from typing import override

from pydantic import BaseModel, computed_field, field_validator


class Catalog(BaseModel):
    name: str

    @computed_field
    @cached_property
    def managed_file_specs(self) -> tuple[str, ...]:
        return (self.name,)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return value

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Catalog) and other.name == self.name
'''


def _build(root: Path) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def test_computed_field_and_cached_property_are_not_dead(tmp_path):
    store = _build(tmp_path)
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "managed_file_specs" not in dead
    finally:
        store.close()


def test_pydantic_validator_is_not_dead(tmp_path):
    store = _build(tmp_path)
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "check_name" not in dead
    finally:
        store.close()


def test_override_method_is_not_dead(tmp_path):
    store = _build(tmp_path)
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "__eq__" not in dead
    finally:
        store.close()


def test_model_used_as_a_field_type_is_not_dead(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text('''\
from typing import Annotated

from pydantic import BaseModel, Field


class PathRulesConfig(BaseModel):
    allowed: tuple[str, ...] = ()


class Catalog(BaseModel):
    path_rules: PathRulesConfig = Field(default_factory=PathRulesConfig)
    nested: Annotated[PathRulesConfig, Field(description="field type use")]
''', encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "PathRulesConfig" not in dead


def test_a_protocol_member_of_a_nested_protocol_is_not_dead(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text('''\
from typing import Protocol


class Facade:
    class Stream(Protocol):
        def isatty(self) -> bool:
            ...


class Source:
    class Reader(Protocol):
        def scan_file(self, path: str) -> int:
            ...
''', encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "isatty" not in dead
    assert "scan_file" not in dead

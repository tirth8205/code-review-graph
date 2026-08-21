"""Keep declared dependency floors synchronized with minimum-version CI."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


EXPECTED_FLOORS = {
    "mcp": "1.29.0",
    "fastmcp": "3.2.4",
    "tree-sitter": "0.26.0",
    "tree-sitter-language-pack": "0.13.0",
    "pyyaml": "6.0",
    "networkx": "3.2",
    "watchdog": "4.0.0",
}


def test_project_declares_tested_dependency_floors() -> None:
    with Path("pyproject.toml").open("rb") as source:
        project = tomllib.load(source)

    declared = {
        dependency.split(">=", 1)[0]: dependency.split(">=", 1)[1].split(",", 1)[0]
        for dependency in project["project"]["dependencies"]
        if ">=" in dependency
    }

    for package, floor in EXPECTED_FLOORS.items():
        assert declared[package] == floor


def test_minimum_ci_pins_match_declared_floors() -> None:
    requirements = {
        line.split("==", 1)[0]: line.split("==", 1)[1].split(";", 1)[0]
        for line in Path(".github/requirements-minimum.txt").read_text().splitlines()
        if line and not line.startswith("#")
    }

    expected = dict(EXPECTED_FLOORS)
    if sys.version_info < (3, 11):
        expected["tomli"] = "2.0.0"

    assert requirements == expected


def test_minimum_ci_checks_dependency_resolution() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert "minimum-dependencies:" in workflow
    assert "python -m pip check" in workflow

"""Keep the declared dependency floors in step with the minimum-version CI job.

``pyproject.toml`` is the one source of the floors, and the pinned file must
match it in both directions, so a new dependency cannot skip the job. See: #896
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

REPO = Path(__file__).parents[1]


def _project() -> dict:
    with (REPO / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]


def _declared_floors() -> dict[str, str]:
    floors = {}
    for dependency in _project()["dependencies"]:
        requirement = dependency.split(";", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9._-]+)>=([^,]+),<.+", requirement)
        assert match, f"{dependency!r} must declare a floor and an upper bound"
        floors[match[1]] = match[2]
    return floors


def _pinned_floors() -> dict[str, str]:
    pins = {}
    text = (REPO / ".github" / "requirements-minimum.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line.startswith("#"):
            package, version = line.split(";", 1)[0].strip().split("==", 1)
            pins[package] = version
    return pins


def test_every_declared_dependency_has_a_pinned_floor() -> None:
    assert _pinned_floors() == _declared_floors()


def test_minimum_job_checks_and_tests_the_floors() -> None:
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["minimum-dependencies"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])

    assert "-r .github/requirements-minimum.txt" in commands
    assert "python -m pip check" in commands
    assert re.search(r"^pytest\b", commands, re.MULTILINE)


def test_minimum_job_covers_every_supported_python() -> None:
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    supported = [
        classifier.rsplit(" ", 1)[1]
        for classifier in _project()["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", classifier)
    ]

    job = workflow["jobs"]["minimum-dependencies"]
    assert job["strategy"]["matrix"]["python-version"] == supported

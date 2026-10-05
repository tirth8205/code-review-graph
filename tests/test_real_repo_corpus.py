"""The parser survives real code: eight pinned repositories, one build each.

Every other parser test writes a small fixture that exercises one construct. A
regression that only shows on real code -- a resolver that stops resolving, an
ignore rule that swallows a source tree, a grammar that raises on a real file --
passes all of them and reaches a release.

This module clones eight real projects at exact commits (Python, Go, TypeScript,
Java, C#, Rust, Ruby, PHP), builds the graph over each, and compares twelve
measured properties against ``tests/corpus_baselines.json``. The properties are
the ones a human would notice if they broke: the file inventory, files that
produced no node, node and edge counts, how much of the graph actually resolves
to real nodes, how much of the import graph points at a file in the repository,
whether the parser named any symbol two different ways, control characters in
node names, the build's time budget, and whether the repository's own language
survived at all.

Runtime and cost
----------------
Roughly one to two minutes of build time on a warm clone cache, plus the first
run's clones (shallow, single commit). It needs network access on the first run.

Run it::

    pytest -m corpus -q

The normal suite skips it: ``tests/conftest.py`` skips every ``corpus``-marked
item unless the run's ``-m`` expression names the marker, so neither
``pytest tests/`` nor CI's ``-m "not browser"`` job pays for it.

Re-record the baselines after an intentional change::

    python -m tests.real_repo_corpus --record

Teeth
-----
A check that quietly measures nothing passes forever. Two canaries run inside
this module against the same real builds the comparison uses:

* ``test_canary_every_property_was_actually_compared`` asserts the build was
  real (a non-trivial graph, checked out at the pinned SHA) and that the
  comparison evaluated the full property set, not a subset.
* ``test_canary_perturbing_each_property_fails_the_comparison`` takes the real
  measurement, moves one property at a time into the regressing direction, and
  requires the comparison to report exactly that property with a message naming
  it and the size of the move.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from tests.real_repo_corpus import (
    PROPERTY_NAMES,
    Bands,
    Measurement,
    compare,
    ensure_clone,
    failures,
    head_sha,
    load_baselines,
    measure,
    repo_specs,
    spec_by_name,
)

pytestmark = pytest.mark.corpus

REPO_NAMES = [spec.name for spec in repo_specs()]

#: One-property perturbations, each in the direction that means "regression",
#: sized to clear the band by a wide margin. ``primary_language_present`` is
#: perturbed through ``languages`` because it is derived, not stored.
PERTURBATIONS: dict[str, Callable[[Measurement], Measurement]] = {
    "files_parsed": lambda m: replace(m, files_parsed=int(m.files_parsed * 0.80)),
    "parse_errors": lambda m: replace(m, parse_errors=m.parse_errors + 1),
    "files_without_nodes": lambda m: replace(
        m, files_without_nodes=m.files_without_nodes + 1
    ),
    "total_nodes": lambda m: replace(m, total_nodes=int(m.total_nodes * 0.75)),
    "total_edges": lambda m: replace(m, total_edges=int(m.total_edges * 0.75)),
    "file_nodes": lambda m: replace(m, file_nodes=int(m.file_nodes * 0.80)),
    "resolved_edge_share": lambda m: replace(
        m, resolved_edge_share=m.resolved_edge_share - 0.20
    ),
    "imports_resolved_share": lambda m: replace(
        m, imports_resolved_share=m.imports_resolved_share - 0.20
    ),
    "dangling_contains_edges": lambda m: replace(
        m, dangling_contains_edges=m.dangling_contains_edges + 1
    ),
    "control_char_names": lambda m: replace(m, control_char_names=1),
    "build_seconds": lambda m: replace(m, build_seconds=m.build_seconds * 10 + 100),
    "primary_language_present": lambda m: replace(m, languages=[]),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def baselines() -> dict:
    return load_baselines()


@pytest.fixture(scope="session")
def bands(baselines: dict) -> Bands:
    return Bands.from_dict(baselines["bands"])


@pytest.fixture(scope="session")
def built(tmp_path_factory) -> object:
    """Clone-and-build each repository at most once per session."""
    db_dir = tmp_path_factory.mktemp("corpus-db")
    cache: dict[str, tuple[Measurement, Path]] = {}

    def _build(name: str) -> tuple[Measurement, Path]:
        if name not in cache:
            spec = spec_by_name(name)
            repo_path = ensure_clone(spec)
            cache[name] = (
                measure(spec, repo_path, db_dir / f"{name}.db"),
                repo_path,
            )
        return cache[name]

    return _build


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REPO_NAMES)
def test_repository_matches_its_recorded_baseline(
    name: str, built, baselines: dict, bands: Bands
) -> None:
    """Every measured property stays inside the band recorded for this repo."""
    measurement, _ = built(name)
    baseline = baselines["repos"][name]
    assert baseline["commit"] == measurement.commit, (
        f"{name}: baseline was recorded at {baseline['commit']} but the corpus "
        f"now pins {measurement.commit}; re-record with "
        f"`python -m tests.real_repo_corpus --record --repo {name}`"
    )

    results = compare(measurement, baseline, bands)
    broken = failures(results)
    assert not broken, (
        f"{name} ({measurement.language}) @ {measurement.commit[:12]}: "
        f"{len(broken)} of {len(results)} properties moved out of band\n"
        + "\n".join(f"  - {r.message}" for r in broken)
    )


# ---------------------------------------------------------------------------
# Canaries: prove the check ran and compared something
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REPO_NAMES)
def test_canary_every_property_was_actually_compared(
    name: str, built, baselines: dict, bands: Bands
) -> None:
    """The build was real and the comparison evaluated the whole property set."""
    measurement, repo_path = built(name)

    assert head_sha(repo_path) == measurement.commit, (
        f"{name}: checkout is not at the pinned commit, so the numbers below "
        f"describe some other revision"
    )
    assert measurement.files_parsed > 50, (
        f"{name}: only {measurement.files_parsed} files were collected; the "
        f"build measured an empty or wrong tree"
    )
    assert measurement.total_nodes > 500, (
        f"{name}: only {measurement.total_nodes} nodes; the build produced "
        f"nothing worth comparing"
    )
    assert measurement.total_edges > 500, (
        f"{name}: only {measurement.total_edges} edges; the build produced "
        f"nothing worth comparing"
    )

    results = compare(measurement, baselines["repos"][name], bands)
    assert tuple(r.name for r in results) == PROPERTY_NAMES, (
        f"{name}: the comparison evaluated {[r.name for r in results]}, not the "
        f"declared property set {list(PROPERTY_NAMES)}"
    )
    assert all(r.measured is not None and r.baseline is not None for r in results)


@pytest.mark.parametrize("name", REPO_NAMES)
def test_canary_perturbing_each_property_fails_the_comparison(
    name: str, built, baselines: dict, bands: Bands
) -> None:
    """Break each guarded property in turn; the comparison must catch each one.

    This runs against the real measurement, so it proves the comparison has
    teeth on the numbers it was just handed, not on a synthetic stand-in.
    """
    measurement, _ = built(name)
    baseline = baselines["repos"][name]
    assert set(PERTURBATIONS) == set(PROPERTY_NAMES), (
        "every declared property needs a perturbation, or the canary would "
        "quietly stop testing one"
    )

    for prop, perturb in PERTURBATIONS.items():
        broken = failures(compare(perturb(measurement), baseline, bands))
        names = [r.name for r in broken]
        assert names == [prop], (
            f"{name}: perturbing {prop} should fail exactly that property, "
            f"got {names}"
        )
        message = broken[0].message
        assert prop in message, f"{name}: failure message does not name {prop}"
        assert message.strip(), f"{name}: failure for {prop} carried no message"

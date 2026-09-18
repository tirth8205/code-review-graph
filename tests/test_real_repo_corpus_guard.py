"""Always-on guards for the pinned real-repository corpus.

``tests/test_real_repo_corpus.py`` is slow, needs the network, and is skipped
unless a run asks for ``-m corpus``. Everything here is fast, offline, and runs
in the normal suite, because the parts of that check most likely to rot quietly
are the parts that do not need a clone:

* the corpus and the evaluation corpus drifting apart, so the corpus measures a
  commit nobody else pins;
* a baseline file missing a repository, a property, or a band, so the slow check
  raises ``KeyError`` (or worse, compares nothing) the day someone runs it;
* the comparison losing its teeth, so every band passes.

The perturbation table below is the same shape as the one the slow check runs
against real builds; this one runs against a synthetic measurement so a broken
comparator is caught in seconds instead of minutes.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.real_repo_corpus import (
    BASELINE_PATH,
    PROPERTY_NAMES,
    Bands,
    Measurement,
    _load_eval_pin,
    compare,
    corpus_selected,
    failures,
    load_baselines,
    repo_specs,
)
from tests.test_real_repo_corpus import PERTURBATIONS

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Languages the corpus exists to protect. One repository each, at minimum.
REQUIRED_LANGUAGES = {
    "python",
    "typescript",
    "go",
    "java",
    "csharp",
    "rust",
    "ruby",
    "php",
}

BASELINE_FIELDS = {
    "repo",
    "language",
    "commit",
    "files_parsed",
    "parse_errors",
    "files_without_nodes",
    "total_nodes",
    "total_edges",
    "file_nodes",
    "resolved_edge_share",
    "imports_resolved_share",
    "dangling_contains_edges",
    "control_char_names",
    "build_seconds",
    "languages",
}


def _baseline_to_measurement(name: str, baseline: dict) -> Measurement:
    """Rebuild a Measurement from a recorded baseline row."""
    return Measurement(
        repo=name,
        language=baseline["language"],
        commit=baseline["commit"],
        files_parsed=baseline["files_parsed"],
        parse_errors=baseline["parse_errors"],
        files_without_nodes=baseline["files_without_nodes"],
        total_nodes=baseline["total_nodes"],
        total_edges=baseline["total_edges"],
        file_nodes=baseline["file_nodes"],
        resolved_edge_share=baseline["resolved_edge_share"],
        imports_resolved_share=baseline["imports_resolved_share"],
        dangling_contains_edges=baseline["dangling_contains_edges"],
        control_char_names=baseline["control_char_names"],
        build_seconds=baseline["build_seconds"],
        languages=list(baseline["languages"]),
    )


# ---------------------------------------------------------------------------
# Corpus definition
# ---------------------------------------------------------------------------


def test_corpus_covers_every_language_it_claims_to_protect() -> None:
    specs = repo_specs()
    assert 6 <= len(specs) <= 8, "the corpus is meant to hold six to eight repos"
    covered = {spec.language for spec in specs}
    missing = REQUIRED_LANGUAGES - covered
    assert not missing, f"no repository in the corpus covers {sorted(missing)}"


def test_every_pin_is_a_full_sha() -> None:
    """A tag or a branch name would make the measurement drift with upstream."""
    for spec in repo_specs():
        assert SHA_RE.match(spec.commit), f"{spec.name}: {spec.commit!r} is not a SHA"


def test_pins_shared_with_the_evaluation_corpus_stay_in_step() -> None:
    """The eval configs own their pins; the corpus must not fork a second one.

    When ``code_review_graph/eval/configs/<name>.yaml`` moves to a new commit,
    the recorded baseline stops describing the tree that will be measured. This
    fails at that moment rather than at the next slow run.
    """
    baselines = load_baselines()["repos"]
    reused = [spec for spec in repo_specs() if spec.eval_config]
    assert reused, (
        "the corpus is supposed to reuse the evaluation corpus rather than "
        "invent a second set of pins"
    )
    for spec in reused:
        eval_url, eval_commit = _load_eval_pin(spec.eval_config or "")
        assert eval_url.rstrip("/") == spec.url.rstrip("/")
        assert spec.commit == eval_commit
        assert baselines[spec.name]["commit"] == eval_commit, (
            f"{spec.name}: eval config now pins {eval_commit} but the baseline "
            f"was recorded at {baselines[spec.name]['commit']}; re-record with "
            f"`python -m tests.real_repo_corpus --record --repo {spec.name}`"
        )


# ---------------------------------------------------------------------------
# Baseline file
# ---------------------------------------------------------------------------


def test_baseline_file_is_complete() -> None:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    names = {spec.name for spec in repo_specs()}
    assert set(data["repos"]) == names, (
        f"baseline repos {sorted(data['repos'])} do not match the corpus "
        f"{sorted(names)}"
    )
    for name, row in data["repos"].items():
        assert set(row) == BASELINE_FIELDS, (
            f"{name}: baseline fields {sorted(set(row) ^ BASELINE_FIELDS)} "
            f"differ from what the comparison reads"
        )
        assert row["total_nodes"] > 0 and row["total_edges"] > 0
        assert row["language"] in row["languages"], (
            f"{name}: the recorded baseline itself does not contain the "
            f"repository's primary language"
        )


def test_bands_are_complete_and_sane() -> None:
    bands = Bands.from_dict(load_baselines()["bands"])
    for field, value in vars(bands).items():
        if field.endswith("_low"):
            assert 0.5 < value <= 1.0, f"{field}={value} is not a shrink tolerance"
        elif field.endswith("_high"):
            assert 1.0 <= value < 3.0, f"{field}={value} is not a growth tolerance"
        elif field.endswith("_drop"):
            assert 0.0 < value < 0.5, f"{field}={value} is not a percentage-point floor"
    assert bands.build_seconds_multiplier >= 1.0
    assert bands.build_seconds_floor >= 0.0


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(load_baselines()["repos"]))
def test_recorded_baseline_compares_clean_against_itself(name: str) -> None:
    """A measurement identical to the baseline must produce no failure.

    If this ever fails, some band excludes the number it was recorded from and
    the slow check would fail on an unchanged codebase.
    """
    data = load_baselines()
    baseline = data["repos"][name]
    results = compare(
        _baseline_to_measurement(name, baseline),
        baseline,
        Bands.from_dict(data["bands"]),
    )
    assert tuple(r.name for r in results) == PROPERTY_NAMES
    assert not failures(results), [r.message for r in failures(results)]


@pytest.mark.parametrize("prop", PROPERTY_NAMES)
def test_each_property_can_actually_fail(prop: str) -> None:
    """Teeth: move one property, and only that property must be reported."""
    data = load_baselines()
    name = sorted(data["repos"])[0]
    baseline = data["repos"][name]
    bands = Bands.from_dict(data["bands"])
    perturbed = PERTURBATIONS[prop](_baseline_to_measurement(name, baseline))

    broken = failures(compare(perturbed, baseline, bands))
    assert [r.name for r in broken] == [prop]
    message = broken[0].message
    assert message.startswith(prop), f"message should lead with the property: {message}"
    assert "baseline=" in message and "measured=" in message, (
        f"the failure must say what moved and by how much: {message}"
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("", False),
        (None, False),
        ("not browser", False),
        ("not corpus", False),
        ("e2e", False),
        ("not e2e and not corpus", False),
        ("corpus", True),
        ("corpus and not slow", True),
        ("not browser and corpus", True),
    ],
)
def test_only_an_explicit_marker_selection_runs_the_corpus(
    expression: str | None, expected: bool
) -> None:
    """The default suite and CI's own filters must not pull in eight clones.

    ``-m "not corpus"`` mentions the marker but excludes it, which is why this
    is a parsed decision rather than a substring test.
    """
    assert corpus_selected(expression) is expected


def test_a_clean_comparison_reports_no_message() -> None:
    data = load_baselines()
    name = sorted(data["repos"])[0]
    baseline = data["repos"][name]
    results = compare(
        _baseline_to_measurement(name, baseline),
        baseline,
        Bands.from_dict(data["bands"]),
    )
    assert all(r.ok and r.message == "" for r in results)

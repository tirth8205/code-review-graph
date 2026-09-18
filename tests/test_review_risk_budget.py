"""Risk-ordered allocation of the ``get_review_context`` source-line budget.

``_MAX_REVIEW_SOURCE_LINES`` used to be spent first come first served over
the changed-file list, which is the order Git happened to emit. One
alphabetically early 500-line file could take 500 of the 800 lines while the
riskiest changed function in the pull request got nothing at all.

Ranking the files fixed who got served but not what they got: the snippet
builder had no idea where in a file the change was, so a per-file line quota
bought the top of a merged window. The budget is therefore spent on whole
changed regions instead.

These tests pin the replacement contract:

* the file list is ranked by the same risk score ``changes.py`` computes,
* a served file gets whole regions, never a fragment of one,
* regions come from the diff hunks when Git can supply them,
* a one-region file costs one region and a many-region file gets many turns,
* no single file may take more than its capped share,
* ``truncated`` / ``source_truncated`` / ``source_regions`` describe what
  really happened,
* the 800-line total is never exceeded.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import review as review_mod
from code_review_graph.tools.review import get_review_context

_NUMBERED_LINE = re.compile(r"^\d+: ")

# Four low-risk padded modules sort before the risky one alphabetically, and
# each is long enough to take the whole 200-line default per-file share. Under
# first-come-first-served that is 4 x 200 = 800 lines, leaving exactly zero for
# ``zzz_session_token.py``.
_PADDING_FUNCS = 30
_PADDING_BODY = 8


def _emitted_lines(snippet: str) -> int:
    """Count real source lines, ignoring the ``...`` range separators."""
    return sum(1 for line in snippet.splitlines() if _NUMBERED_LINE.match(line))


def _total_emitted(snippets: dict[str, str]) -> int:
    return sum(_emitted_lines(s) for s in snippets.values())


def _line_numbers(snippet: str) -> set[int]:
    """The 1-based source line numbers a snippet actually shows."""
    return {
        int(line.split(":", 1)[0])
        for line in snippet.splitlines()
        if _NUMBERED_LINE.match(line)
    }



def _padded_module(prefix: str) -> str:
    """A long, well-tested, non-security module: low risk, many lines."""
    lines = [f'"""Padding module {prefix}."""', ""]
    for fn in range(_PADDING_FUNCS):
        lines.append(f"def {prefix}_step_{fn}(value):")
        lines.append(f'    """Step {fn}."""')
        for step in range(_PADDING_BODY):
            lines.append(f"    value = value + {step}  # step {step}")
        lines.append("    return value")
        lines.append("")
    return "\n".join(lines)


def _padding_tests(prefix: str) -> str:
    lines = [f"from {prefix} import *", ""]
    for fn in range(_PADDING_FUNCS):
        lines.append(f"def test_{prefix}_step_{fn}():")
        lines.append(f"    assert {prefix}_step_{fn}(1) is not None")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def risky_repo(tmp_path_factory) -> dict[str, Any]:
    """A repo whose riskiest changed file sorts last in the diff order."""
    root = tmp_path_factory.mktemp("risk-budget-repo")
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)

    padding = [f"aaa_pad{i}" for i in range(4)]
    for prefix in padding:
        (root / f"{prefix}.py").write_text(_padded_module(prefix), encoding="utf-8")
        (root / f"test_{prefix}.py").write_text(
            _padding_tests(prefix), encoding="utf-8",
        )

    # The risky file: security-sensitive names, no tests, many cross-file
    # callers. Short enough that a fair share covers most of it.
    risky = ['"""Session token handling."""', ""]
    risky.append("def validate_session_token(token):")
    risky.append('    """Validate an auth token."""')
    for step in range(20):
        risky.append(f"    token = token + {step}  # check {step}")
    risky.append("    return token")
    risky.append("")
    risky.append("def decrypt_password_hash(secret):")
    risky.append('    """Decrypt a stored credential."""')
    for step in range(20):
        risky.append(f"    secret = secret + {step}  # round {step}")
    risky.append("    return validate_session_token(secret)")
    risky.append("")
    (root / "zzz_session_token.py").write_text(
        "\n".join(risky), encoding="utf-8",
    )

    # Callers in every padding module raise the risky file's caller count and
    # make it cross-community.
    for prefix in padding:
        path = root / f"{prefix}.py"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n\nfrom zzz_session_token import validate_session_token\n"
            + f"\n\ndef {prefix}_forward(value):\n"
            + "    return validate_session_token(value)\n",
            encoding="utf-8",
        )

    db_path = root / ".code-review-graph" / "graph.db"
    os.environ["CRG_SERIAL_PARSE"] = "1"
    with GraphStore(db_path) as store:
        full_build(root, store)

    changed = [f"{p}.py" for p in padding] + ["zzz_session_token.py"]
    return {"root": str(root), "changed": changed}


def _context(risky_repo: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    result = get_review_context(
        changed_files=list(risky_repo["changed"]),
        repo_root=risky_repo["root"],
        include_source=True,
        **kwargs,
    )
    assert result["status"] == "ok"
    return result["context"]


class TestRiskOrderedAllocation:
    def test_riskiest_file_receives_source(self, risky_repo):
        """The file that got nothing under first-come-first-served is served."""
        snippets = _context(risky_repo)["source_snippets"]
        assert "zzz_session_token.py" in snippets
        assert _emitted_lines(snippets["zzz_session_token.py"]) > 0

    def test_changed_file_list_is_risk_ranked(self, risky_repo):
        context = _context(risky_repo)
        assert context["changed_files"][0] == "zzz_session_token.py"
        risk = context["file_risk"]
        assert risk["zzz_session_token.py"] > max(
            score for name, score in risk.items()
            if name != "zzz_session_token.py"
        )

    def test_riskiest_file_is_served_whole(self, risky_repo):
        """Ranking must drive the allocation, not just the list order."""
        snippets = _context(risky_repo)["source_snippets"]
        emitted = _emitted_lines(snippets["zzz_session_token.py"])
        whole = len(
            (Path(risky_repo["root"]) / "zzz_session_token.py")
            .read_text(encoding="utf-8").splitlines()
        )
        assert emitted == whole

    def test_no_served_file_is_empty(self, risky_repo):
        context = _context(risky_repo)
        for name, text in context["source_snippets"].items():
            assert _line_numbers(text), f"{name} was served an empty snippet"

    def test_incomplete_files_are_reported_not_hidden(self, risky_repo):
        context = _context(risky_repo)
        regions = context["source_regions"]
        assert regions["shown"] < regions["total"]
        assert regions["incomplete"], (
            "a budget that could not show every region must name the files"
        )
        for name, (shown, total) in regions["incomplete"].items():
            assert shown < total
            assert f"{total - shown} more changed region(s) not shown" in (
                context["source_snippets"][name]
            )

    def test_total_budget_is_never_exceeded(self, risky_repo):
        context = _context(risky_repo, max_lines_per_file=10_000)
        assert _total_emitted(context["source_snippets"]) <= (
            review_mod._MAX_REVIEW_SOURCE_LINES
        )

    def test_no_single_file_takes_more_than_its_share(self, risky_repo):
        context = _context(risky_repo, max_lines_per_file=10_000)
        cap = review_mod._MAX_REVIEW_SOURCE_LINES * (
            review_mod._MAX_SOURCE_SHARE_PER_FILE
        )
        for name, text in context["source_snippets"].items():
            assert _emitted_lines(text) <= cap + 1, (
                f"{name} starved the rest of the ranked list"
            )

    def test_source_truncated_is_honest_when_files_are_trimmed(self, risky_repo):
        context = _context(risky_repo)
        assert context["source_truncated"] is True
        assert context["truncated"] is True

    def test_source_truncated_absent_when_everything_fits(self, risky_repo):
        result = get_review_context(
            changed_files=["zzz_session_token.py"],
            repo_root=risky_repo["root"],
            include_source=True,
        )
        assert result["status"] == "ok"
        context = result["context"]
        assert context.get("source_truncated") is not True
        emitted = _emitted_lines(context["source_snippets"]["zzz_session_token.py"])
        whole = len(
            (Path(risky_repo["root"]) / "zzz_session_token.py")
            .read_text(encoding="utf-8").splitlines()
        )
        assert emitted == whole


class _Node:
    """The only two attributes the region builder reads off a graph node."""

    def __init__(self, line_start: int, line_end: int) -> None:
        self.line_start = line_start
        self.line_end = line_end


class TestRegionBuilder:
    """Where the budget is spent: the diff hunks, not the whole file."""

    def test_hunks_far_apart_stay_separate_regions(self):
        regions = review_mod._file_regions(
            line_count=400, hunks=[(10, 12), (200, 202)], nodes=[],
            region_cap=120,
        )
        assert len(regions) == 2
        assert regions[0][1] < regions[1][0]

    def test_a_hunk_is_widened_to_a_small_enclosing_definition(self):
        node = _Node(20, 50)  # 31 lines, worth reading whole
        regions = review_mod._file_regions(
            line_count=400, hunks=[(30, 31)], nodes=[node], region_cap=120,
        )
        assert len(regions) == 1
        start, end = regions[0]
        assert start + 1 <= node.line_start
        assert end >= node.line_end

    def test_a_hunk_in_a_huge_definition_keeps_its_own_window(self):
        """A 400-line function is not worth half the shared budget."""
        node = _Node(1, 400)
        regions = review_mod._file_regions(
            line_count=400, hunks=[(200, 201)], nodes=[node], region_cap=120,
        )
        assert len(regions) == 1
        start, end = regions[0]
        assert end - start <= 2 * review_mod._REGION_CONTEXT + 6
        assert end - start < node.line_end - node.line_start

    def test_merging_never_exceeds_the_region_cap(self):
        """The old builder merged every span into one file-wide window."""
        hunks = [(i, i + 1) for i in range(1, 400, 4)]
        regions = review_mod._file_regions(
            line_count=500, hunks=hunks, nodes=[], region_cap=60,
        )
        assert len(regions) > 1
        assert all(end - start <= 60 for start, end in regions)

    def test_a_small_file_is_still_shown_whole(self):
        regions = review_mod._file_regions(
            line_count=40, hunks=[(5, 6)], nodes=[], region_cap=120,
        )
        assert regions == [(0, 40)]

    def test_a_file_the_graph_and_git_both_miss_falls_back_to_its_head(self):
        regions = review_mod._file_regions(
            line_count=900, hunks=[], nodes=[], region_cap=120,
        )
        assert regions == [(0, review_mod._FALLBACK_HEAD_LINES)]


class TestRegionAllocation:
    """The allocator is a pure function over the ranked region lists."""

    @staticmethod
    def _regions(count: int, size: int = 10, gap: int = 100):
        return [(i * gap, i * gap + size) for i in range(count)]

    def test_regions_are_granted_whole(self):
        granted, _ = review_mod._allocate_regions(
            ["a.py"], {"a.py": self._regions(3, size=30)}, 800, 500,
        )
        assert all(end - start == 30 for start, end in granted["a.py"])

    def test_a_one_region_file_costs_one_region(self):
        granted, omitted = review_mod._allocate_regions(
            ["small.py", "big.py"],
            {"small.py": self._regions(1), "big.py": self._regions(20)},
            800, 500,
        )
        assert len(granted["small.py"]) == 1
        assert omitted.get("small.py") is None
        assert len(granted["big.py"]) == 20, (
            "a many-region file must get many turns, not one share"
        )

    def test_round_robin_serves_every_file_before_any_file_twice(self):
        files = [f"f{i}.py" for i in range(8)]
        regions = {f: self._regions(6, size=30) for f in files}
        granted, _ = review_mod._allocate_regions(files, regions, 240, 500)
        assert sorted(len(v) for v in granted.values()) == [1] * 8

    def test_the_riskiest_files_win_when_the_budget_runs_dry(self):
        files = ["hot.py", "warm.py", "cold.py"]
        regions = {f: self._regions(1, size=40) for f in files}
        granted, omitted = review_mod._allocate_regions(
            files, regions, 100, 500,
        )
        assert set(granted) == {"hot.py", "warm.py"}
        assert omitted == {"cold.py": 1}

    def test_no_file_exceeds_its_share_of_the_budget(self):
        files = ["a.py", "b.py", "c.py"]
        regions = {f: self._regions(40, size=20) for f in files}
        granted, _ = review_mod._allocate_regions(files, regions, 800, 500)
        cap = review_mod._source_share_cap(800, 500)
        for name, got in granted.items():
            held = sum(end - start for start, end in got)
            assert held <= cap, f"{name} starved the rest of the ranking"

    def test_per_file_limit_is_respected(self):
        granted, _ = review_mod._allocate_regions(
            ["a.py", "b.py"],
            {"a.py": self._regions(10), "b.py": self._regions(10)},
            800, 25,
        )
        for got in granted.values():
            assert sum(end - start for start, end in got) <= 25

    def test_the_total_budget_is_never_exceeded(self):
        files = [f"f{i}.py" for i in range(30)]
        regions = {f: self._regions(30, size=17) for f in files}
        granted, _ = review_mod._allocate_regions(files, regions, 800, 500)
        total = sum(
            end - start for got in granted.values() for start, end in got
        )
        assert total <= 800

    def test_a_region_larger_than_the_cap_is_truncated_not_dropped(self):
        granted, _ = review_mod._allocate_regions(
            ["a.py"], {"a.py": [(0, 5_000)]}, 800, 500,
        )
        (start, end), = granted["a.py"]
        assert end - start == min(
            review_mod._MAX_REGION_LINES,
            review_mod._source_share_cap(800, 500),
        )

    def test_empty_inputs_allocate_nothing(self):
        assert review_mod._allocate_regions([], {}, 800, 500) == ({}, {})
        granted, omitted = review_mod._allocate_regions(
            ["a.py"], {"a.py": self._regions(2)}, 0, 500,
        )
        assert granted == {}
        assert omitted == {"a.py": 2}


# ---------------------------------------------------------------------------
# What the reviewer actually receives, measured against the real diff hunks.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        capture_output=True, check=True, cwd=repo,
        stdin=subprocess.DEVNULL, text=True, timeout=30,
    )


def _spread_module(bodies: int, filler: int) -> list[str]:
    """A module whose functions sit far enough apart not to merge."""
    lines = ['"""Spread module."""', ""]
    for fn in range(bodies):
        lines.append(f"def spread_step_{fn}(value):")
        lines.append(f'    """Step {fn}."""')
        lines.append(f"    value = value + {fn}")
        lines.append("    return value")
        lines.append("")
        lines.extend(f"# filler {fn}.{i}" for i in range(filler))
        lines.append("")
    return lines


@pytest.fixture(scope="module")
def hunky_repo(tmp_path_factory) -> dict[str, Any]:
    """A committed repo with six well-separated changes in one file."""
    root = tmp_path_factory.mktemp("hunky-repo")
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")

    lines = _spread_module(bodies=6, filler=30)
    (root / "spread.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "other.py").write_text(
        "\n".join(_spread_module(bodies=2, filler=30)) + "\n", encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")

    # One changed line inside each of the six functions: six hunks, spread
    # across ~230 lines, which is exactly the shape the old single merged
    # window could not serve.
    edited = [
        line.replace("    return value", "    return value + 1")
        for line in lines
    ]
    (root / "spread.py").write_text("\n".join(edited) + "\n", encoding="utf-8")

    os.environ["CRG_SERIAL_PARSE"] = "1"
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        full_build(root, store)

    hunks = len(re.findall(
        r"^@@ ",
        subprocess.run(
            ["git", "diff", "--unified=0", "HEAD", "--", "spread.py"],
            capture_output=True, check=True, cwd=root, text=True,
        ).stdout,
        re.MULTILINE,
    ))
    return {"root": str(root), "hunks": hunks}


class TestHunkCoverage:
    """File count is not the metric: complete changed regions are."""

    def test_the_fixture_really_has_several_spread_hunks(self, hunky_repo):
        assert hunky_repo["hunks"] == 6

    def test_every_hunk_is_shown_when_the_budget_allows(self, hunky_repo):
        result = get_review_context(
            repo_root=hunky_repo["root"], base="HEAD", include_source=True,
        )
        assert result["status"] == "ok"
        context = result["context"]
        numbers = _line_numbers(context["source_snippets"]["spread.py"])
        source = (
            Path(hunky_repo["root"]) / "spread.py"
        ).read_text(encoding="utf-8").splitlines()
        changed = {
            i + 1 for i, line in enumerate(source)
            if line.strip() == "return value + 1"
        }
        assert len(changed) == hunky_repo["hunks"]
        assert changed <= numbers, (
            "the budget was spent somewhere other than the changed lines"
        )
        assert context["source_regions"]["shown"] == (
            context["source_regions"]["total"]
        )

    def test_a_tight_budget_still_shows_whole_regions_and_says_what_is_missing(
        self, hunky_repo,
    ):
        result = get_review_context(
            repo_root=hunky_repo["root"], base="HEAD", include_source=True,
            max_lines_per_file=12,
        )
        context = result["context"]
        snippet = context["source_snippets"]["spread.py"]
        numbers = _line_numbers(snippet)
        source = (
            Path(hunky_repo["root"]) / "spread.py"
        ).read_text(encoding="utf-8").splitlines()
        changed = {
            i + 1 for i, line in enumerate(source)
            if line.strip() == "return value + 1"
        }
        shown_changes = changed & numbers
        assert shown_changes, "a tight budget must still buy a whole region"
        assert len(shown_changes) < len(changed), "expected a tight budget"
        # Whole regions: each shown change carries its definition around it.
        for line_no in shown_changes:
            assert line_no - 2 in numbers and line_no - 1 in numbers
        assert "more changed region(s) not shown" in snippet
        assert context["source_truncated"] is True

    def test_a_clipped_region_is_declared_as_a_cut(self, hunky_repo):
        """A region too big for its cap is served clipped, and says so."""
        result = get_review_context(
            repo_root=hunky_repo["root"], base="HEAD", include_source=True,
            max_lines_per_file=5,
        )
        context = result["context"]
        assert _emitted_lines(context["source_snippets"]["spread.py"]) == 5
        assert context["source_truncated"] is True

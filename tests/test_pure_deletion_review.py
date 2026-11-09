"""A diff that only deletes code: what changed, what was removed, who still uses it.

A hunk that removes lines and adds none (``@@ -395,96 +394,0 @@``) has no new
lines to point at. Read on the new side alone it is a zero-width position, and
the code around that position is code the diff never touched. Widening it to
the size of the hole (the fix first tried in PR #1167, which reported the
case) marks every function that moved up into the hole, so deleting a 300-line
function marked every later function in the file as changed.

The answer comes from the old side instead: the old file is parsed, a symbol
whose lines were deleted and that no longer exists is reported as removed,
with the code that still calls it, and a symbol that survives but lost lines
is marked changed. No untouched neighbour is ever marked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph.changes import (
    DiffRanges,
    _parse_unified_diff,
    analyze_changes,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.postprocessing import run_post_processing


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )


@pytest.fixture()
def make_repo(tmp_path: Path):
    """Commit *files*, build the graph, and hand back ``(repo, store)``."""
    stores: list[GraphStore] = []

    def _make(files: dict[str, str]) -> tuple[Path, GraphStore]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
        store = GraphStore(repo / ".code-review-graph" / "graph.db")
        stores.append(store)
        full_build(repo, store)
        run_post_processing(store)
        return repo, store

    yield _make
    for store in stores:
        store.close()


def _review(
    repo: Path, store: GraphStore, edits: dict[str, str | None],
) -> dict:
    """Apply *edits* to the working tree, update the graph, analyse vs HEAD."""
    for rel, text in edits.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
    incremental_update(repo, store, base="HEAD", changed_files=list(edits))
    # Flows and communities feed the risk score; the CLI update runs this too.
    run_post_processing(store)
    return analyze_changes(
        store, changed_files=list(edits), repo_root=str(repo), base="HEAD",
    )


def _changed(result: dict) -> set[str]:
    return {f["name"] for f in result["changed_functions"]}


def _removed(result: dict) -> dict[str, set[str]]:
    return {
        r["name"]: {u["name"] for u in r["used_by"]}
        for r in result["removed_symbols"]
    }


def _functions(names: list[str], body_lines: int = 3) -> str:
    """Top-level functions separated by two blank lines."""
    blocks = []
    for name in names:
        body = "".join(f"    x = x + {i}\n" for i in range(body_lines))
        blocks.append(f"def {name}(x):\n{body}    return x\n")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Parsing: the old side of a pure deletion is kept
# ---------------------------------------------------------------------------


class TestDiffParsing:
    def test_pure_deletion_keeps_its_old_lines(self):
        diff = (
            "diff --git a/m.py b/m.py\n"
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -395,96 +394,0 @@ def kept():\n"
            "-def gone():\n"
            "@@ -500,2 +404,3 @@ def other():\n"
            "+    y\n"
        )
        result = _parse_unified_diff(diff)
        assert isinstance(result, DiffRanges)
        # The mapping itself is unchanged: the deletion is still a position.
        assert result["m.py"] == [(394, 394), (404, 406)]
        deletions = result.deletions["m.py"]
        assert deletions.old_path == "m.py"
        assert [(h.old_start, h.old_end, h.anchor) for h in deletions.hunks] == [
            (395, 490, 394),
        ]
        # Only the hunk that added lines is an edit.
        assert result.edit_ranges() == {"m.py": [(404, 406)]}

    def test_omitted_old_count_is_one_line(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -7 +6,0 @@\n-    gone\n"
        hunk = _parse_unified_diff(diff).deletions["x.py"].hunks[0]
        assert (hunk.old_start, hunk.old_end, hunk.anchor) == (7, 7, 6)

    def test_a_deleted_file_is_left_out_as_before(self):
        diff = (
            "diff --git a/old.py b/old.py\n"
            "deleted file mode 100644\n"
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,4 +0,0 @@\n"
            "-def gone():\n"
        )
        result = _parse_unified_diff(diff)
        assert result == {}
        assert result.deletions == {}

    def test_renamed_file_reads_its_old_side_from_the_old_path(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 90%\n"
            "rename from old.py\n"
            "rename to new.py\n"
            "--- a/old.py\n"
            "+++ b/new.py\n"
            "@@ -3,2 +2,0 @@\n"
            "-x\n"
        )
        result = _parse_unified_diff(diff)
        assert result.deletions["new.py"].old_path == "old.py"

    def test_a_deleted_line_that_looks_like_a_header_is_not_one(self):
        # A removed SQL comment "-- a/b" is written "--- a/b" in the diff.
        diff = (
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -2,1 +1,0 @@\n"
            "--- a/b\n"
            "@@ -9,1 +8,0 @@\n"
            "-x\n"
        )
        result = _parse_unified_diff(diff)
        assert result.deletions["q.sql"].old_path == "q.sql"
        assert len(result.deletions["q.sql"].hunks) == 2

    def test_a_diff_with_no_deletions_has_none(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n+y\n"
        result = _parse_unified_diff(diff)
        assert result.deletions == {}
        assert result.edit_ranges() == {"x.py": [(1, 2)]}


# ---------------------------------------------------------------------------
# Analysis against real repositories
# ---------------------------------------------------------------------------


def test_deleting_a_whole_function_marks_no_survivor_and_names_its_callers(
    make_repo,
):
    a_py = (
        "def helper(x):\n"
        "    return x + 1\n"
        "\n\n"
        "def gone(x):\n"
        "    y = helper(x)\n"
        "    return y * 2\n"
        "\n\n"
        "def keeper(x):\n"
        "    return gone(x) + 1\n"
    )
    b_py = (
        "from a import gone\n"
        "\n\n"
        "def far_caller(v):\n"
        "    return gone(v)\n"
        "\n\n"
        "def bystander(v):\n"
        "    return v\n"
    )
    repo, store = make_repo({"a.py": a_py, "b.py": b_py})
    after = a_py.replace(
        "def gone(x):\n    y = helper(x)\n    return y * 2\n\n\n", "",
    )

    result = _review(repo, store, {"a.py": after})

    assert _changed(result) == set(), "no surviving function was edited"
    assert _removed(result) == {"gone": {"keeper", "far_caller"}}
    removed = result["removed_symbols"][0]
    assert (removed["old_line_start"], removed["old_line_end"]) == (5, 7)
    assert removed["used_by_total"] == 2
    assert result["removed_symbols_total"] == 1
    assert "1 removed function(s)/class(es)" in result["summary"]
    assert "Removed but still used: gone (risk " in result["summary"]


def test_a_large_deletion_does_not_mark_the_functions_after_it(make_repo):
    names = ["first", "huge", "after_1", "after_2", "after_3", "after_4"]
    source = _functions(names[:1]) + "\n\n" + _functions(["huge"], 300)
    source += "\n\n" + _functions(names[2:])
    repo, store = make_repo({"m.py": source})
    after = _functions(names[:1]) + "\n\n" + _functions(names[2:])

    result = _review(repo, store, {"m.py": after})

    assert _changed(result) == set()
    assert set(_removed(result)) == {"huge"}


def test_deleting_a_block_inside_a_function_marks_only_that_function(make_repo):
    source = _functions(["before", "target", "after"], body_lines=6)
    repo, store = make_repo({"m.py": source})
    after = source.replace(
        "def target(x):\n    x = x + 0\n    x = x + 1\n    x = x + 2\n",
        "def target(x):\n    x = x + 0\n",
    )
    assert after != source

    result = _review(repo, store, {"m.py": after})

    assert _changed(result) == {"target"}
    assert result["removed_symbols"] == []


def test_deleting_the_tail_of_a_function_marks_that_function(make_repo):
    source = (
        "def first(x):\n"
        "    a = x\n"
        "    b = a + 1\n"
        "    return b\n"
        "def second(x):\n"
        "    return x\n"
    )
    repo, store = make_repo({"m.py": source})
    after = source.replace("    b = a + 1\n    return b\n", "")

    result = _review(repo, store, {"m.py": after})

    # The hole sits right after first's last surviving line: only the old
    # side can say the lines were first's.
    assert _changed(result) == {"first"}
    assert result["removed_symbols"] == []


def test_deleting_a_function_flush_against_its_neighbour_marks_neither(
    make_repo,
):
    source = (
        "def first(x):\n"
        "    return x\n"
        "def middle(x):\n"
        "    return x * 2\n"
        "def last(x):\n"
        "    return middle(x)\n"
    )
    repo, store = make_repo({"m.py": source})
    after = source.replace("def middle(x):\n    return x * 2\n", "")

    result = _review(repo, store, {"m.py": after})

    # The hole sits right after first's last line, and first is untouched.
    assert _changed(result) == set()
    assert _removed(result) == {"middle": {"last"}}


def test_deleting_a_method_names_it_and_its_callers(make_repo):
    source = (
        "class Box:\n"
        "    def open(self):\n"
        "        self.close()\n"
        "        return 1\n"
        "\n"
        "    def close(self):\n"
        "        return 2\n"
        "\n\n"
        "def unrelated(path):\n"
        "    handle = open(path)\n"
        "    handle.close()\n"
    )
    repo, store = make_repo({"box.py": source})
    after = source.replace(
        "\n    def close(self):\n        return 2\n", "",
    )

    result = _review(repo, store, {"box.py": after})

    # The class lost a method, so the class itself changed; its other method
    # and the unrelated function did not.
    assert _changed(result) == {"Box"}
    # ``handle.close()`` is a file's close, not Box.close.
    assert _removed(result) == {"close": {"open"}}


def test_a_call_through_a_module_receiver_is_a_user(make_repo):
    repo, store = make_repo({
        "lib.py": _functions(["kept", "gone"]),
        "app.py": "import lib\n\n\ndef main():\n    return lib.gone(1)\n",
    })

    result = _review(repo, store, {"lib.py": _functions(["kept"])})

    assert _changed(result) == set()
    assert _removed(result) == {"gone": {"main"}}


def test_a_removed_symbol_that_is_still_used_is_listed_first(make_repo):
    repo, store = make_repo({
        "lib.py": _functions(["unused", "kept", "used"]),
        "app.py": "from lib import used\n\n\ndef main():\n    return used(1)\n",
    })

    result = _review(repo, store, {"lib.py": _functions(["kept"])})

    assert [r["name"] for r in result["removed_symbols"]] == ["used", "unused"]
    assert "Removed but still used: used (risk " in result["summary"]
    assert ", by main)" in result["summary"]


def test_without_the_old_side_a_deletion_between_functions_marks_neither(
    make_repo, monkeypatch,
):
    source = _functions(["first", "gone", "last"], body_lines=4)
    repo, store = make_repo({"m.py": source})
    after = source.replace(
        "def gone(x):\n" + "".join(f"    x = x + {i}\n" for i in range(4))
        + "    return x\n\n\n",
        "",
    )
    inside = source.replace("    x = x + 1\n    x = x + 2\n", "", 1)

    monkeypatch.setattr(
        "code_review_graph.changes._old_side_nodes", lambda *a, **k: None,
    )
    between = _review(repo, store, {"m.py": after})
    assert _changed(between) == set()
    assert between["removed_symbols"] == []
    # Saying nothing was removed would be a claim the tool did not check.
    assert "old version of 1 file(s) could not be read" in between["summary"]

    # A hole with surviving lines of one function on both sides is still
    # that function's, old side or not.
    _git(repo, "checkout", "--", "m.py")
    incremental_update(repo, store, base="HEAD", changed_files=["m.py"])
    within = _review(repo, store, {"m.py": inside})
    assert _changed(within) == {"first"}


def test_detect_changes_tool_reports_removed_symbols_in_both_modes(make_repo):
    from code_review_graph.tools.review import detect_changes_func

    lib = _functions([f"gone_{i}" for i in range(4)] + ["kept"])
    callers = "from lib import gone_0, gone_1, gone_2, gone_3\n\n\n"
    callers += "\n\n".join(
        f"def use_{i}_{j}():\n    return gone_{i}(1)\n"
        for i in range(4) for j in range(8)
    )
    repo, store = make_repo({"lib.py": lib, "app.py": callers})
    (repo / "lib.py").write_text(_functions(["kept"]), encoding="utf-8")
    incremental_update(repo, store, base="HEAD", changed_files=["lib.py"])

    standard = detect_changes_func(
        base="HEAD", repo_root=str(repo), max_results=3,
    )
    assert standard["status"] == "ok", standard
    assert standard["removed_symbols_total"] == 4
    assert len(standard["removed_symbols"]) == 3
    assert standard["truncated"] is True
    first = standard["removed_symbols"][0]
    assert first["used_by_total"] == 8
    assert len(first["used_by"]) == 3, "users shown per symbol are capped"
    assert {f["name"] for f in standard["changed_functions"]} == set()

    minimal = detect_changes_func(
        base="HEAD", repo_root=str(repo), detail_level="minimal",
    )
    assert minimal["removed_symbol_count"] == 4
    assert "4 removed function(s)/class(es)" in minimal["summary"]
    # Bounded: four symbols with 32 callers between them stay small.
    assert len(json.dumps(minimal)) < 4_000


def test_removed_symbols_stay_inside_the_detect_changes_budget(make_repo):
    """Many removed symbols, each with many users, still cost a bounded amount.

    The hard ceilings are 25 symbols per response and 3 users per symbol;
    ``tests/test_token_budget.py`` pins both numbers.
    """
    from code_review_graph.tools.review import detect_changes_func

    names = [f"gone_{i}" for i in range(60)]
    callers = "from lib import " + ", ".join(names) + "\n\n\n"
    callers += "\n\n".join(
        f"def use_{i}_{j}():\n    return {name}(1)\n"
        for i, name in enumerate(names) for j in range(6)
    )
    repo, store = make_repo({
        "lib.py": _functions([*names, "kept"]), "app.py": callers,
    })
    (repo / "lib.py").write_text(_functions(["kept"]), encoding="utf-8")
    incremental_update(repo, store, base="HEAD", changed_files=["lib.py"])

    worst = detect_changes_func(
        base="HEAD", repo_root=str(repo), max_results=10**6,
        include_source=True, max_flows=10**6,
    )

    assert worst["removed_symbols_total"] == 60
    assert len(worst["removed_symbols"]) == 25
    assert all(len(r["used_by"]) <= 3 for r in worst["removed_symbols"])
    assert all(r["used_by_total"] == 6 for r in worst["removed_symbols"])
    # ~120 tokens a row at the documented len/4 estimate, plus the repository
    # path five times over (it is in every qualified name), which is left out
    # here so the number does not depend on where pytest puts its temp dirs.
    rows = json.dumps(worst["removed_symbols"]).replace(str(repo), "")
    assert len(rows) // 4 < 25 * 150


# ---------------------------------------------------------------------------
# Risk: a removed symbol that is still used breaks its callers
# ---------------------------------------------------------------------------


def _many_callers(name: str, count: int) -> str:
    calls = "\n\n".join(
        f"def use_{j}():\n    return {name}(1)\n" for j in range(count)
    )
    return f"from lib import {name}\n\n\n{calls}"


def test_a_removed_symbol_still_used_scores_at_least_what_changing_it_would(
    make_repo,
):
    repo, store = make_repo({
        "lib.py": _functions(["kept", "gone"]),
        "app.py": _many_callers("gone", 20),
    })
    lib = (repo / "lib.py").read_text(encoding="utf-8")
    edited = lib.replace(
        "def gone(x):\n    x = x + 0\n", "def gone(x):\n    x = x + 7\n",
    )
    assert edited != lib
    changed = _review(repo, store, {"lib.py": edited})
    change_score = {
        f["name"]: f["risk_score"] for f in changed["changed_functions"]
    }["gone"]

    removed = _review(repo, store, {"lib.py": _functions(["kept"])})

    entry = removed["removed_symbols"][0]
    assert entry["name"] == "gone"
    assert entry["risk_score"] >= change_score > 0
    # Nothing that survived changed, so the removal is the whole risk.
    assert removed["changed_functions"] == []
    assert removed["risk_score"] == entry["risk_score"]
    assert f"Overall risk score: {entry['risk_score']:.2f}" in removed["summary"]


def test_a_removed_symbol_with_no_users_adds_no_risk(make_repo):
    repo, store = make_repo({"lib.py": _functions(["kept", "dead"])})

    result = _review(repo, store, {"lib.py": _functions(["kept"])})

    assert result["removed_symbols"][0]["name"] == "dead"
    assert result["removed_symbols"][0]["risk_score"] == 0.0
    assert result["risk_score"] == 0.0


def test_the_action_risk_gate_sees_a_removed_symbol(make_repo, tmp_path):
    """``fail-on-risk`` reads ``risk_score``; a still-used removal reaches it."""
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "render_pr_comment.py"
    spec = importlib.util.spec_from_file_location("render_pr_comment", script)
    assert spec is not None and spec.loader is not None
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)

    # Untested (0.30), security-sensitive (0.20), 20 callers (0.10), all of
    # them in another file's community (0.15): over the 0.70 "high" bar.
    repo, store = make_repo({
        "lib.py": _functions(["kept", "check_token"]),
        "app.py": _many_callers("check_token", 20),
    })
    result = _review(repo, store, {"lib.py": _functions(["kept"])})
    assert result["changed_functions"] == []
    assert result["risk_score"] >= render.RISK_THRESHOLDS["high"]

    report = tmp_path / "report.json"
    report.write_text(json.dumps(result, default=str), encoding="utf-8")
    exit_code = render.main(
        ["--input", str(report), "--fail-on-risk", "high", "--quiet"],
    )
    assert exit_code == 3

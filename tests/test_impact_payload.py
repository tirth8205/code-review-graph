"""Payload shape of ``get_impact_radius`` in standard detail.

Why this file exists
--------------------
``detail_level`` defaults to ``"standard"``, and the standard branch used to
emit every connecting edge, every changed node and every impacted file with
no cap -- ``max_results`` only ever reached the impacted-node list. Every
``file_path``, every ``qualified_name`` and both endpoints of every edge also
carried the repository's absolute path, so the same query cost a different
number of tokens depending on how deep the checkout sat on disk.

Measured on django (3,005 files) before the caps: one call for the single
changed file ``django/db/models/query.py`` returned 410,834 estimated tokens
across 1.6 MB, of which 1,083,461 characters were the repeated absolute
prefix, and nine qualified names were silently cut off at the 256-character
sanitizer limit -- long enough that feeding them back into ``query_graph``
returned no results. At the same node budget afterwards: 63,854 tokens, no
absolute prefixes, no truncated identifiers.

These cases pin the shape rather than the django numbers: a cap that is
removed, or a path that goes back to being absolute, fails here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import query as query_module

# Sized so every cap binds. The hub carries more functions than the
# changed-node ceiling, and the blast radius is spread thin across more
# modules than the impacted-file ceiling, which is the shape that made the
# uncapped response expensive on a real repository.
_HUB_FUNCS = 220
_MODULES = 260


def _write_fan_out_repo(root: Path) -> None:
    """A hub module that many one-function modules import, one call each."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    hub = ['"""Hub."""', ""]
    for fn in range(_HUB_FUNCS):
        hub.append(f"def hub_{fn}(value):")
        hub.append(f"    return value + {fn}")
        hub.append("")
    (root / "pkg" / "hub.py").write_text("\n".join(hub), encoding="utf-8")

    for mod in range(_MODULES):
        target = mod % _HUB_FUNCS
        (root / "pkg" / f"mod{mod}.py").write_text(
            "\n".join([
                f'"""Consumer {mod}."""',
                f"from pkg.hub import hub_{target}",
                "",
                f"def consumer_{mod}(value):",
                f"    return hub_{target}(value)",
                "",
            ]),
            encoding="utf-8",
        )


@pytest.fixture(scope="module")
def fan_out_repo(tmp_path_factory) -> str:
    """Build the fan-out graph once for the whole module."""
    root = tmp_path_factory.mktemp("impact-payload-repo")
    _write_fan_out_repo(root)
    os.environ["CRG_SERIAL_PARSE"] = "1"
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        full_build(root, store)
    return str(root)


def _impact(repo: str, **kwargs):
    return query_module.get_impact_radius(
        changed_files=["pkg/hub.py"], repo_root=repo, **kwargs,
    )


def test_every_list_is_capped_by_max_results(fan_out_repo):
    """``max_results`` must bound edges and files, not only impacted nodes."""
    result = _impact(fan_out_repo, max_results=5)

    assert result["status"] == "ok"
    assert len(result["impacted_nodes"]) <= 5
    assert len(result["changed_nodes"]) <= 5
    assert len(result["edges"]) <= 5
    assert len(result["impacted_files"]) <= 5


def test_hard_ceilings_bind_above_max_results(fan_out_repo):
    """A caller asking for everything still gets a bounded response.

    The fixture is sized so each list would overflow its ceiling without a
    cap, so these are equalities, not just bounds.
    """
    result = _impact(fan_out_repo, max_results=10**6)

    assert len(result["impacted_nodes"]) == query_module._MAX_IMPACT_NODES
    assert len(result["changed_nodes"]) == query_module._MAX_IMPACT_CHANGED_NODES
    assert len(result["edges"]) == query_module._MAX_IMPACT_EDGES
    assert len(result["impacted_files"]) == query_module._MAX_IMPACT_FILES
    assert result["truncated"] is True


def test_truncation_is_reported_not_hidden(fan_out_repo):
    """Every capped list reports its true total and what it dropped."""
    result = _impact(fan_out_repo, max_results=5)

    assert result["truncated"] is True
    assert result["total_edges"] > len(result["edges"])
    assert result["edges_omitted"] == (
        result["total_edges"] - len(result["edges"])
    )
    assert result["total_changed"] > len(result["changed_nodes"])
    assert result["changed_nodes_omitted"] == (
        result["total_changed"] - len(result["changed_nodes"])
    )
    assert result["impacted_files_omitted"] == (
        result["total_impacted_files"] - len(result["impacted_files"])
    )
    assert "Edges truncated" in result["summary"]


def test_impacted_files_are_capped_and_counted(fan_out_repo):
    """The file list is bounded too, and still reports the true total."""
    result = _impact(fan_out_repo, max_results=10**6)

    assert len(result["impacted_files"]) == query_module._MAX_IMPACT_FILES
    assert result["total_impacted_files"] > len(result["impacted_files"])
    assert result["impacted_files_omitted"] == (
        result["total_impacted_files"] - len(result["impacted_files"])
    )


def test_paths_are_repo_relative_and_the_prefix_appears_once(fan_out_repo):
    """The absolute prefix belongs in ``repo_root``, nowhere else."""
    result = _impact(fan_out_repo)
    prefix = f"{fan_out_repo}/"

    assert result["repo_root"] == fan_out_repo
    for node in result["changed_nodes"] + result["impacted_nodes"]:
        assert not node["file_path"].startswith(prefix)
        assert not node["qualified_name"].startswith(prefix)
        assert not node["name"].startswith(prefix)
    for edge in result["edges"]:
        assert not edge["source"].startswith(prefix)
        assert not edge["target"].startswith(prefix)
        assert not (edge["file_path"] or "").startswith(prefix)
    for path in result["impacted_files"]:
        assert not path.startswith(prefix)
    assert any(
        node["file_path"] == "pkg/hub.py" for node in result["changed_nodes"]
    )


def test_relative_qualified_names_still_resolve(fan_out_repo):
    """A relative identifier must be usable as a ``query_graph`` target.

    Relative names are only cheaper, not better, if an agent can feed one
    straight back into the next call.
    """
    result = _impact(fan_out_repo)
    targets = [
        node["qualified_name"] for node in result["impacted_nodes"]
        if node["kind"] == "Function"
    ]
    assert targets, "fixture produced no function-level impacted nodes"

    resolved = query_module.query_graph(
        "callers_of", targets[0], repo_root=fan_out_repo,
        detail_level="minimal",
    )
    assert resolved["status"] in {"ok", "ambiguous"}
    assert "Unknown pattern" not in str(resolved.get("error", ""))


def test_kept_edges_are_the_ones_nearest_the_change(fan_out_repo):
    """Capping must drop the periphery, not an arbitrary prefix."""
    result = _impact(fan_out_repo, max_results=10)

    changed = {node["qualified_name"] for node in result["changed_nodes"]}
    impacted = {node["qualified_name"] for node in result["impacted_nodes"]}
    known = changed | impacted
    assert result["edges"], "fixture produced no connecting edges"
    assert any(
        edge["source"] in known or edge["target"] in known
        for edge in result["edges"]
    )


def test_minimal_detail_still_counts_every_impacted_file(fan_out_repo):
    """Capping the standard list must not shrink the minimal-mode count."""
    minimal = _impact(fan_out_repo, max_results=10**6, detail_level="minimal")
    standard = _impact(fan_out_repo, max_results=10**6)

    assert minimal["impacted_file_count"] == standard["total_impacted_files"]
    assert minimal["impacted_file_count"] > len(standard["impacted_files"])

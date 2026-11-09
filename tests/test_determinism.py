"""Determinism gate: the same input must produce the same graph.

Nothing else in the suite checks this. If a build is not reproducible then
every benchmark number, every ``detect-changes`` diff and every "what changed"
answer is measuring build noise as well as code, and nobody would notice --
the failure mode is silent by construction.

What this module does
---------------------

It builds one corpus repeatedly, each build in its own subprocess writing its
own database, and compares the *contents* of every table each build produced:
``nodes``, ``edges``, ``metadata``, ``flows``, ``flow_memberships``,
``communities``, the per-node community assignment, ``community_summaries``,
``flow_snapshots``, ``risk_index``, the FTS5 search index (both what it
answers and what it is on disk), and the embedding pipeline. Sorted contents,
never counts. ``tests/determinism_dump.py`` holds the dump and diff, including
the enumerated list of columns that are allowed to differ and why
(``determinism_dump.IGNORED`` -- timestamps and autoincrement surrogate keys,
and the surrogate keys are resolved to the names they point at rather than
dropped).

Rather than waiting for luck it then attacks the likely sources directly:
the parallel executor against ``CRG_SERIAL_PARSE=1``, a process pool against
a thread pool, two fixed ``PYTHONHASHSEED`` values, the same file inventory
presented in a different order, and the same tree built at two different
absolute paths.

Why it is opt-in
----------------

Each build parses a real multi-language corpus end to end and runs the full
post-processing pipeline; the module runs several of them. Expect roughly two
to three minutes. ``tests/conftest.py`` skips everything marked
``determinism`` unless the marker is selected explicitly::

    pytest -m determinism

Canaries
--------

A check that passes because it silently compared nothing is worse than no
check. Four canaries run first and guard the rest:

``test_canary_every_build_did_real_work``
    the build actually parsed the corpus and reported no errors.
``test_canary_every_section_has_rows``
    every dump section cleared a floor, so no section can pass by being
    empty, and a newly added section must declare a floor to be accepted.
``test_canary_dump_covers_every_table``
    no table in the database escapes the dump.
``test_canary_comparator_detects_an_injected_difference``
    mutating a single row is detected and reported with a usable message.
``test_canary_path_normalisation_is_not_a_no_op``
    the raw values really do contain the absolute path the dump removes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests import determinism_dump as dd

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(__file__).resolve().parent / "determinism_build_runner.py"

# Directories copied out of this checkout to form the corpus. Together they
# cover the Python package under test plus the multi-language fixture tree,
# which is what makes the parse wide enough for this gate to mean something.
CORPUS_SOURCES = (
    ("code_review_graph", "code_review_graph"),
    ("tests/fixtures", "fixtures"),
    ("scripts", "scripts"),
)

BUILD_TIMEOUT = 900

try:
    from code_review_graph.communities import IGRAPH_AVAILABLE
except ImportError:  # pragma: no cover - the package is always importable here
    IGRAPH_AVAILABLE = False

pytestmark = [
    pytest.mark.determinism,
    pytest.mark.skipif(
        shutil.which("git") is None,
        reason="the corpus needs git so the file inventory is the sorted "
               "`git ls-files` order rather than filesystem order",
    ),
]

# Floor for every dump section. A section missing from this table is a
# failure, not a pass: adding a section without a floor would let it compare
# empty-to-empty forever. Floors sit near half the observed size so a real
# parser regression trips them without making the gate brittle.
MIN_ROWS: dict[str, int] = {
    "nodes": 1000,
    "node_id_order": 1000,
    "edges": 5000,
    "metadata": 5,
    "flows": 20,
    "flow_memberships": 500,
    "flow_snapshots": 20,
    "communities": 5,
    "node_communities": 500,
    "community_summaries": 5,
    "risk_index": 500,
    "fts_query_results": 60,
    "fts_index_bytes": 1,
    # One row per indexed node, so it tracks the nodes floor rather than a
    # number of its own: a mirror that fell far below it would mean the index
    # had lost the values it needs to delete entries with.
    "nodes_fts_state": 1000,
    "embedding_texts": 1000,
    "embeddings": 1000,
}

# Sections the path-independence comparison sets aside, each with the reason.
# They are not waved through: the two tests after it own them.
PATH_SECTIONS_HANDLED_SEPARATELY = {
    "fts_index_bytes": (
        "nodes_fts indexes the absolute file_path as text, so the serialized "
        "index cannot be path-normalised; fts_query_results covers what the "
        "index answers"
    ),
    "fts_query_results": (
        "owned by test_path_independence_of_search_ranking"
    ),
    "community_summaries": (
        "owned by test_community_summary_purpose_does_not_embed_the_checkout_dir"
    ),
}


# ---------------------------------------------------------------------------
# Corpus and build farm
# ---------------------------------------------------------------------------


def _make_corpus(destination: Path) -> Path:
    """Copy a slice of this checkout into *destination* and commit it.

    Committing matters: ``collect_all_files`` prefers ``git ls-files``, whose
    output is sorted, so the inventory is a property of the content rather
    than of the filesystem. (The unsorted fallback is the subject of
    ``test_non_git_file_inventory_is_ordered``.)
    """
    destination.mkdir(parents=True, exist_ok=True)
    for source, name in CORPUS_SOURCES:
        src = REPO_ROOT / source
        if not src.is_dir():
            pytest.skip(f"corpus source missing from this checkout: {source}")
        shutil.copytree(
            src,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".code-review-graph"),
        )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "determinism",
        "GIT_AUTHOR_EMAIL": "determinism@example.invalid",
        "GIT_COMMITTER_NAME": "determinism",
        "GIT_COMMITTER_EMAIL": "determinism@example.invalid",
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
    }
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "corpus"]):
        subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", *argv],
            cwd=str(destination),
            env=env,
            check=True,
            capture_output=True,
        )
    return destination


@dataclass
class BuiltGraph:
    label: str
    db: Path
    root: Path
    stats: dict


@dataclass
class BuildFarm:
    """Builds the corpus on demand and caches databases and dumps by label."""

    workdir: Path
    crg_home: Path
    builds: dict[str, BuiltGraph] = field(default_factory=dict)
    dumps: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def build(
        self,
        label: str,
        repo_root: Path,
        *,
        env: dict[str, str] | None = None,
        order: str = "asis",
    ) -> BuiltGraph:
        if label in self.builds:
            return self.builds[label]
        out_db = self.workdir / f"{label}.db"
        child_env = {
            **os.environ,
            # Pin the per-user state directory for the subprocess: the autouse
            # conftest fixture only covers this process.
            "CRG_HOME": str(self.crg_home),
        }
        child_env.pop("VIRTUAL_ENV", None)
        child_env.update(env or {})
        completed = subprocess.run(  # nosec B603 - fixed argv, no shell
            [
                sys.executable,
                str(RUNNER),
                str(repo_root),
                str(out_db),
                "--order",
                order,
            ],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            cwd=str(self.workdir),
        )
        assert completed.returncode == 0, (
            f"build {label!r} failed with exit code {completed.returncode}\n"
            f"stdout:\n{completed.stdout[-2000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
        stats = json.loads(completed.stdout.strip().splitlines()[-1])
        built = BuiltGraph(label=label, db=out_db, root=repo_root, stats=stats)
        self.builds[label] = built
        return built

    def dump(self, built: BuiltGraph) -> dict[str, list[str]]:
        if built.label not in self.dumps:
            # Run the real embedding pipeline against a deterministic offline
            # provider so the embeddings table is populated with something to
            # compare; see determinism_dump._StubEmbeddingProvider.
            dd.populate_stub_embeddings(built.db)
            self.dumps[built.label] = dd.dump_database(built.db, built.root)
        return self.dumps[built.label]


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_corpus(tmp_path_factory.mktemp("crg-corpus") / "repo")


@pytest.fixture(scope="session")
def corpus_at_another_path(
    tmp_path_factory: pytest.TempPathFactory,
    corpus: Path,
) -> Path:
    """The identical tree, including ``.git``, at a longer absolute path.

    Copying ``.git`` keeps the commit SHA identical, so ``metadata`` stays
    comparable and the only thing that varies is where the tree lives.
    """
    clone = tmp_path_factory.mktemp("crg-corpus-copy-at-a-deliberately-longer-path")
    destination = clone / "repository-under-a-longer-directory-name"
    shutil.copytree(corpus, destination)
    shutil.rmtree(destination / ".code-review-graph", ignore_errors=True)
    return destination


@pytest.fixture(scope="session")
def farm(tmp_path_factory: pytest.TempPathFactory) -> BuildFarm:
    return BuildFarm(
        workdir=tmp_path_factory.mktemp("crg-determinism-dbs"),
        crg_home=tmp_path_factory.mktemp("crg-determinism-home"),
    )


@pytest.fixture(scope="session")
def baseline(farm: BuildFarm, corpus: Path) -> BuiltGraph:
    """Default configuration: parallel parse, unpinned interpreter hash seed."""
    return farm.build("baseline", corpus)


def _assert_same(
    farm: BuildFarm,
    left: BuiltGraph,
    right: BuiltGraph,
    *,
    skip: tuple[str, ...] = (),
) -> None:
    diffs = dd.compare(farm.dump(left), farm.dump(right), skip=skip)
    assert not diffs, dd.describe(diffs, left.label, right.label)


# ---------------------------------------------------------------------------
# Canaries -- these prove the rest of the module actually compared something
# ---------------------------------------------------------------------------


def test_canary_every_build_did_real_work(baseline: BuiltGraph) -> None:
    """A build that parsed nothing would make every comparison below vacuous."""
    assert baseline.stats["status"] == "ok", baseline.stats
    assert not baseline.stats["errors"], baseline.stats["errors"]
    assert not baseline.stats["warnings"], baseline.stats["warnings"]
    assert baseline.stats["files_parsed"] >= 100, baseline.stats
    assert baseline.stats["total_nodes"] >= 1000, baseline.stats
    assert baseline.stats["total_edges"] >= 5000, baseline.stats


def test_canary_every_section_has_rows(farm: BuildFarm, baseline: BuiltGraph) -> None:
    """Every section compares real rows, and no section may skip declaring so."""
    dump = farm.dump(baseline)
    undeclared = sorted(set(dump) - set(MIN_ROWS))
    assert not undeclared, (
        f"dump sections without a row floor in MIN_ROWS: {undeclared}. "
        "A section with no floor could compare empty-to-empty forever."
    )
    missing = sorted(set(MIN_ROWS) - set(dump))
    assert not missing, f"MIN_ROWS declares sections the dump never produced: {missing}"
    thin = {
        name: (len(rows), MIN_ROWS[name])
        for name, rows in dump.items()
        if len(rows) < MIN_ROWS[name]
    }
    assert not thin, f"sections below their floor (actual, floor): {thin}"


def test_canary_dump_covers_every_table(baseline: BuiltGraph) -> None:
    """A derived table added later must not silently escape this gate."""
    uncovered = dd.uncovered_tables(baseline.db)
    assert not uncovered, (
        f"tables present in the database that no dump section reads: "
        f"{sorted(uncovered)}. Add a section in tests/determinism_dump.py, or "
        f"list the table in _EXEMPT_TABLES with a reason."
    )


def test_canary_comparator_detects_an_injected_difference(
    farm: BuildFarm,
    baseline: BuiltGraph,
) -> None:
    """Break one row and prove the comparison notices and says something useful."""
    clean = farm.dump(baseline)
    assert not dd.compare(clean, clean), "a dump must equal itself"

    for section in ("nodes", "edges", "flows", "communities", "embeddings"):
        tampered = {name: list(rows) for name, rows in clean.items()}
        original = tampered[section][0]
        tampered[section][0] = original + "\x1fTAMPERED"

        diffs = dd.compare(clean, tampered)
        assert [d.section for d in diffs] == [section], (
            f"tampering with {section} was not detected as a {section} difference: "
            f"{[d.section for d in diffs]}"
        )
        assert diffs[0].rows == 2, diffs[0]
        message = dd.describe(diffs, "clean", "tampered")
        assert "TAMPERED" in message, message
        assert section in message, message

    # A dropped row must be caught too, not only a changed one.
    truncated = {name: list(rows) for name, rows in clean.items()}
    dropped = truncated["nodes"].pop()
    diffs = dd.compare(clean, truncated)
    assert [d.section for d in diffs] == ["nodes"]
    assert diffs[0].only_left == [dropped]
    assert diffs[0].only_right == []


def test_canary_path_normalisation_is_not_a_no_op(
    farm: BuildFarm,
    baseline: BuiltGraph,
) -> None:
    """The dump must remove an absolute path that is genuinely in the data."""
    raw = Path(baseline.db).read_bytes()
    root_bytes = str(baseline.root).encode()
    assert root_bytes in raw, (
        "the built database does not contain the repository's absolute path, so "
        "path normalisation would be comparing nothing"
    )

    dump = farm.dump(baseline)
    for variant in dd.path_variants(baseline.root):
        offenders = [
            f"{section}: {row[:200]}"
            for section, rows in dump.items()
            for row in rows
            if variant in row
        ]
        assert not offenders, (
            f"normalised dump still contains the absolute path {variant!r}: "
            f"{offenders[:3]}"
        )
    assert any(
        dd.PLACEHOLDER in row for row in dump["nodes"]
    ), "no node row carries the <REPO> placeholder, so nothing was normalised"


# ---------------------------------------------------------------------------
# 1. The same input twice
# ---------------------------------------------------------------------------


def test_same_repository_built_twice_is_identical(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """Two builds of one unchanged tree, into two databases, must agree.

    Neither build pins ``PYTHONHASHSEED``, so each interpreter starts with its
    own randomised hash seed; anything that leaked set or dict iteration order
    into stored output would show up here as well.
    """
    repeat = farm.build("repeat", corpus)
    _assert_same(farm, baseline, repeat)


# ---------------------------------------------------------------------------
# 2. Attacking the likely sources
# ---------------------------------------------------------------------------


def test_serial_and_parallel_parse_agree(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """``CRG_SERIAL_PARSE=1`` takes a completely separate loop in full_build."""
    serial = farm.build("serial", corpus, env={"CRG_SERIAL_PARSE": "1"})
    _assert_same(farm, baseline, serial)


def test_thread_and_process_executors_agree(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """The MCP stdio server parses in threads; the CLI parses in processes.

    Different worker counts too, so a result that depended on how the work
    happened to be chunked would not survive.
    """
    threaded = farm.build(
        "threaded",
        corpus,
        env={"CRG_PARSE_EXECUTOR": "thread", "CRG_PARSE_WORKERS": "3"},
    )
    _assert_same(farm, baseline, threaded)


def test_python_hash_seed_does_not_change_the_graph(
    farm: BuildFarm,
    corpus: Path,
) -> None:
    """Two fixed, very different interpreter hash seeds must build one graph.

    ``PYTHONHASHSEED`` can only be chosen before the interpreter starts, which
    is why every build here is a subprocess. Set iteration order is the classic
    way a pipeline becomes irreproducible across machines.
    """
    low = farm.build("hashseed-0", corpus, env={"PYTHONHASHSEED": "0"})
    high = farm.build("hashseed-987654321", corpus, env={"PYTHONHASHSEED": "987654321"})
    _assert_same(farm, low, high)


# -- File inventory order ---------------------------------------------------

# Everything the build stores about the code itself survives a reordering of
# the inventory; the id numbering and the summaries do not. The split below is
# deliberate -- collapsing it into one test would hide the half that passes.

ORDER_CONTENT_SECTIONS = (
    "nodes",
    "edges",
    "metadata",
    "flows",
    "flow_memberships",
    "flow_snapshots",
    "risk_index",
    "embedding_texts",
    "embeddings",
    "fts_query_results",
)


def test_file_order_does_not_change_graph_content(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """The same files in a different order must yield the same code graph."""
    reordered = farm.build("reversed-order", corpus, order="reversed")
    left, right = farm.dump(baseline), farm.dump(reordered)
    skip = tuple(set(left) - set(ORDER_CONTENT_SECTIONS))
    diffs = dd.compare(left, right, skip=skip)
    assert not diffs, dd.describe(diffs, baseline.label, reordered.label)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BUG, not fixed here: node ids are assigned in file-inventory "
        "order, and flows.path_json plus flow_memberships persist those raw "
        "ids, so the same tree presented in a different order stores a "
        "different numbering and a different FTS5 index image. Reported, not "
        "repaired, in this pull request."
    ),
)
def test_file_order_does_not_change_node_id_assignment(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    reordered = farm.build("reversed-order", corpus, order="reversed")
    _assert_same(
        farm,
        baseline,
        reordered,
        skip=tuple(
            s for s in farm.dump(baseline)
            if s not in ("node_id_order", "fts_index_bytes")
        ),
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BUG, not fixed here: community_summaries.key_symbols is built "
        "from a stable sort on edge count alone, so ties are broken by node "
        "insertion order and the list changes when the file inventory is "
        "reordered. It also selects bare node names, so one name can appear "
        "twice. With igraph installed the Leiden partition itself is "
        "order-sensitive as well. Reported, not repaired, in this pull request."
    ),
)
def test_file_order_does_not_change_community_summaries(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    reordered = farm.build("reversed-order", corpus, order="reversed")
    sections = {"community_summaries", "communities", "node_communities"}
    _assert_same(
        farm,
        baseline,
        reordered,
        skip=tuple(s for s in farm.dump(baseline) if s not in sections),
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BUG, not fixed here: collect_all_files falls back to "
        "Path.rglob when a tree is not under git or svn, and rglob yields "
        "filesystem readdir order. Two machines, or two filesystems, can "
        "therefore present the same tree in different orders -- which the "
        "order tests above show changes what gets stored. Reported, not "
        "repaired, in this pull request."
    ),
)
def test_non_git_file_inventory_is_ordered(tmp_path: Path) -> None:
    """A tree with no VCS must still yield a deterministic file inventory."""
    from code_review_graph.incremental import collect_all_files

    tree = tmp_path / "no-vcs"
    (tree / "pkg").mkdir(parents=True)
    for index in range(30):
        (tree / "pkg" / f"mod_{index:02d}.py").write_text(
            f"def f_{index:02d}():\n    return {index}\n",
            encoding="utf-8",
        )
    files = collect_all_files(tree)
    assert len(files) == 30, files
    assert files == sorted(files), (
        "inventory is in filesystem order, not a content-determined order; "
        f"first entries: {files[:5]}"
    )


# ---------------------------------------------------------------------------
# 3. Community detection: randomised, or not?
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not IGRAPH_AVAILABLE,
    reason="Leiden needs igraph; the file-based fallback is covered below",
)
def test_leiden_seed_is_what_makes_community_detection_reproducible(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """igraph's Leiden is randomised, and the pinned seed is doing the work.

    ``communities._LEIDEN_SEED`` (42, override ``CRG_LEIDEN_SEED``) is applied
    before every Leiden call, which is why
    ``test_same_repository_built_twice_is_identical`` can demand exact
    equality of the partition rather than settling for a weaker property.

    This is the canary for that claim: run the same corpus under a different
    seed and the partition *must* move. If it does not, the seeding is no
    longer reaching a randomised algorithm and equality above proves nothing
    about it.
    """
    other_seed = farm.build("leiden-seed-99", corpus, env={"CRG_LEIDEN_SEED": "99"})
    sections = ("communities", "node_communities", "community_summaries")
    diffs = dd.compare(
        farm.dump(baseline),
        farm.dump(other_seed),
        skip=tuple(s for s in farm.dump(baseline) if s not in sections),
    )
    assert diffs, (
        "changing CRG_LEIDEN_SEED changed nothing, so the Leiden path is not "
        "actually running and the seeded-equality claim is untested"
    )

    # And the rest of the graph must be untouched by the seed.
    _assert_same(farm, baseline, other_seed, skip=sections)


@pytest.mark.skipif(
    IGRAPH_AVAILABLE,
    reason="igraph is installed, so Leiden runs and the test above applies",
)
def test_file_based_community_fallback_is_deterministic_by_construction(
    farm: BuildFarm,
    corpus: Path,
    baseline: BuiltGraph,
) -> None:
    """Without igraph, communities come from directory grouping, not sampling.

    There is no RNG on this path, so the equality asserted by
    ``test_same_repository_built_twice_is_identical`` is the right property and
    no weaker one is needed. Changing the Leiden seed must be a no-op here --
    if it is not, the backend in use is not the one this test believes.
    """
    other_seed = farm.build("leiden-seed-99", corpus, env={"CRG_LEIDEN_SEED": "99"})
    _assert_same(farm, baseline, other_seed)
    assert farm.dump(baseline)["communities"], "no communities were detected at all"


# ---------------------------------------------------------------------------
# 4. A build should not depend on where the repository lives
# ---------------------------------------------------------------------------


def test_graph_content_is_independent_of_the_absolute_path(
    farm: BuildFarm,
    corpus: Path,
    corpus_at_another_path: Path,
    baseline: BuiltGraph,
) -> None:
    """The same tree at two paths must build the same graph once relativised.

    Node identity embeds the absolute file path, so the dump rewrites every
    occurrence of each repository root to ``<REPO>`` before comparing; the
    normalisation canary proves that rewrite is not a no-op. The two trees
    share a ``.git`` directory, so the recorded commit is the same on both
    sides and ``metadata`` is compared rather than excluded.
    """
    elsewhere = farm.build("other-path", corpus_at_another_path)
    diffs = dd.compare(
        farm.dump(baseline),
        farm.dump(elsewhere),
        skip=tuple(PATH_SECTIONS_HANDLED_SEPARATELY),
    )
    assert not diffs, dd.describe(diffs, baseline.label, elsewhere.label)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BUG, not fixed here: nodes_fts indexes the absolute file_path "
        "as searchable text, so BM25 document lengths -- and therefore scores "
        "and result order -- depend on how deep the checkout directory is. "
        "Two clones of one repository rank search results differently. "
        "Reported, not repaired, in this pull request."
    ),
)
def test_path_independence_of_search_ranking(
    farm: BuildFarm,
    corpus_at_another_path: Path,
    baseline: BuiltGraph,
) -> None:
    elsewhere = farm.build("other-path", corpus_at_another_path)
    _assert_same(
        farm,
        baseline,
        elsewhere,
        skip=tuple(
            s for s in farm.dump(baseline) if s != "fts_query_results"
        ),
    )


@pytest.mark.xfail(
    IGRAPH_AVAILABLE,
    strict=True,
    reason=(
        "KNOWN BUG, not fixed here: community_summaries.purpose is the last "
        "component of os.path.commonprefix over the members' absolute file "
        "paths. A community spanning several top-level directories therefore "
        "stores the checkout directory's own name -- local filesystem naming "
        "in a field the MCP tools hand back. It only surfaces when Leiden "
        "produces such a community, which needs igraph. Reported, not "
        "repaired, in this pull request."
    ),
)
def test_community_summary_purpose_does_not_embed_the_checkout_dir(
    farm: BuildFarm,
    corpus: Path,
    corpus_at_another_path: Path,
    baseline: BuiltGraph,
) -> None:
    elsewhere = farm.build("other-path", corpus_at_another_path)
    _assert_same(
        farm,
        baseline,
        elsewhere,
        skip=tuple(
            s for s in farm.dump(baseline) if s != "community_summaries"
        ),
    )
    for label, root in ((baseline.label, corpus), ("other-path", corpus_at_another_path)):
        purposes = {
            row.split("\x1f")[2]
            for row in farm.dumps[label]["community_summaries"]
        }
        assert root.name not in purposes, (
            f"{label}: a community summary's purpose is the checkout directory "
            f"name {root.name!r}"
        )

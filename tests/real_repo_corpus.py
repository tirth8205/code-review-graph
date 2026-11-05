"""Support code for the pinned real-repository parser corpus.

Every other parser test in this suite writes a handful of synthetic files into
``tmp_path`` to exercise one construct at a time. That catches a broken rule; it
cannot catch a regression that only appears at project scale -- a resolver that
drops most of its edges, an ignore rule that swallows a source tree, a grammar
that starts raising on a real file. This module supplies the machinery for a
check that builds the graph over eight real projects pinned to exact commits and
compares the result against numbers recorded in ``corpus_baselines.json``.

Design notes:

* The pins for repositories that ``code_review_graph/eval/configs`` already
  tracks are *derived from* those configs rather than copied. ``repo_specs()``
  reads the YAML and fails loudly when the two disagree, so the project keeps a
  single source of truth for "which commit of fastapi do we measure".
* Clones are shallow single-commit fetches of the pinned SHA, so the corpus is
  reproducible and cheap. GitHub serves an arbitrary reachable SHA to
  ``git fetch --depth 1``.
* Measurement is deterministic: a fixed commit parsed by a fixed build produces
  the same counts every time. The bands below therefore do not model
  measurement noise (there is none); they model how much *intentional* change
  the project may make before someone has to re-record the baseline.

The slow check lives in ``tests/test_real_repo_corpus.py`` behind the ``corpus``
marker. The comparison logic here is exercised by the fast, always-on guard
tests in ``tests/test_real_repo_corpus_guard.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
BASELINE_PATH = TESTS_DIR / "corpus_baselines.json"
EVAL_CONFIGS_DIR = REPO_ROOT / "code_review_graph" / "eval" / "configs"

#: Wall-clock ceiling for one clone. A stalled fetch must fail the check rather
#: than hang a CI job forever.
CLONE_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Corpus definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoSpec:
    """One pinned repository in the corpus."""

    name: str
    language: str
    url: str
    commit: str
    #: Name of the ``code_review_graph/eval/configs/<name>.yaml`` this pin is
    #: taken from, when the evaluation corpus already tracks the repository.
    eval_config: Optional[str] = None

    @property
    def sha(self) -> str:
        return self.commit


#: The corpus. Eight projects, one per language that this parser is expected to
#: handle well, each substantial enough that a real regression shows up in the
#: counts but small enough to clone and build in seconds.
#:
#: ``commit`` is ``None`` for entries whose pin is owned by the evaluation
#: corpus: ``repo_specs()`` fills it in from the YAML config and raises when the
#: config is missing, so the two can never drift apart silently.
_CORPUS: tuple[dict, ...] = (
    {
        "name": "fastapi",
        "language": "python",
        "url": "https://github.com/tiangolo/fastapi",
        "commit": None,
        "eval_config": "fastapi",
    },
    {
        "name": "gin",
        "language": "go",
        "url": "https://github.com/gin-gonic/gin",
        "commit": None,
        "eval_config": "gin",
    },
    {
        "name": "zod",
        "language": "typescript",
        "url": "https://github.com/colinhacks/zod",
        "commit": "59bbc03e10c636b9eb3c393dfeb552819774ec21",
        "eval_config": None,
    },
    {
        "name": "gson",
        "language": "java",
        "url": "https://github.com/google/gson",
        "commit": "854c8255b625cf1e13c701a83ea9ccb4caaa576a",
        "eval_config": None,
    },
    {
        "name": "newtonsoft-json",
        "language": "csharp",
        "url": "https://github.com/JamesNK/Newtonsoft.Json",
        "commit": "09bb545d72969ad7fb4ea07db0d5c34f4fc07877",
        "eval_config": None,
    },
    {
        "name": "ripgrep",
        "language": "rust",
        "url": "https://github.com/BurntSushi/ripgrep",
        "commit": "3fce3b5bb0236da2df6d99672afb8a719642eca7",
        "eval_config": None,
    },
    {
        "name": "sinatra",
        "language": "ruby",
        "url": "https://github.com/sinatra/sinatra",
        "commit": "cb22afd7902b566b6eaba6c4ea89739494a65d12",
        "eval_config": None,
    },
    {
        "name": "guzzle",
        "language": "php",
        "url": "https://github.com/guzzle/guzzle",
        "commit": "93939470950a9b11e2e84204166ef5e048c55fe4",
        "eval_config": None,
    },
)


#: ``-m`` expressions that positively select the corpus. ``-m "not corpus"``
#: mentions the marker but excludes it, so a substring test is not enough.
_CORPUS_SELECTED = re.compile(r"(?<![\w-])corpus\b")
_CORPUS_EXCLUDED = re.compile(r"\bnot\s+corpus\b")


def corpus_selected(mark_expression: str | None) -> bool:
    """True when a ``-m`` expression asks for the corpus marker.

    Used by ``tests/conftest.py`` to skip the corpus unless a run names it, and
    kept here as a plain function so it can be tested without a subprocess.
    """
    expression = (mark_expression or "").strip()
    if not expression:
        return False
    if _CORPUS_EXCLUDED.search(expression):
        return False
    return bool(_CORPUS_SELECTED.search(expression))


class CorpusConfigError(RuntimeError):
    """The corpus definition and the evaluation configs disagree."""


def _load_eval_pin(config_name: str) -> tuple[str, str]:
    """Return ``(url, commit)`` from an evaluation config.

    Parsed with pyyaml when available and with a deliberately small line reader
    otherwise, so the corpus keeps working in the lint/type environments that do
    not install the ``eval`` extra.
    """
    path = EVAL_CONFIGS_DIR / f"{config_name}.yaml"
    if not path.exists():
        raise CorpusConfigError(
            f"corpus entry reuses eval config {config_name!r} but "
            f"{path} does not exist; the evaluation corpus moved and the "
            f"real-repo corpus must be updated with it"
        )
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
        url, commit = data.get("url"), data.get("commit")
    except ImportError:
        url = commit = None
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key == "url":
                url = value.strip()
            elif key == "commit":
                commit = value.split("#")[0].strip()
    if not url or not commit:
        raise CorpusConfigError(f"{path}: missing url or commit pin")
    return str(url), str(commit)


def repo_specs() -> list[RepoSpec]:
    """Return the corpus, resolving eval-owned pins from their YAML configs."""
    specs: list[RepoSpec] = []
    for entry in _CORPUS:
        url = entry["url"]
        commit = entry["commit"]
        config_name = entry["eval_config"]
        if config_name:
            eval_url, eval_commit = _load_eval_pin(config_name)
            if eval_url.rstrip("/") != str(url).rstrip("/"):
                raise CorpusConfigError(
                    f"{entry['name']}: eval config {config_name!r} points at "
                    f"{eval_url} but the corpus expects {url}"
                )
            commit = eval_commit
        if not commit:
            raise CorpusConfigError(f"{entry['name']}: no commit pin")
        specs.append(
            RepoSpec(
                name=str(entry["name"]),
                language=str(entry["language"]),
                url=str(url),
                commit=str(commit),
                eval_config=config_name,
            )
        )
    return specs


def spec_by_name(name: str) -> RepoSpec:
    for spec in repo_specs():
        if spec.name == name:
            return spec
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------


def corpus_cache_dir() -> Path:
    """Directory holding the shallow clones.

    Override with ``CRG_CORPUS_CACHE`` to keep the clones between runs; the
    default lands in the system temp directory so a plain run leaves no trace in
    the developer's checkout.
    """
    override = os.environ.get("CRG_CORPUS_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "crg-real-repo-corpus"


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - list args, no shell
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )


def _assert_standalone_repo(path: Path) -> None:
    """Refuse to run git commands in a directory that is not its own repo.

    Without this, a half-finished clone under a cache directory that happens to
    sit inside another checkout makes ``git -C <dir> checkout <sha>`` operate on
    the *enclosing* repository, rewriting the developer's working tree. The
    evaluation runner learned this the hard way; the corpus inherits the guard.
    """
    proc = _git(["rev-parse", "--show-toplevel"], cwd=path)
    toplevel = (
        Path(proc.stdout.strip()).resolve()
        if proc.returncode == 0 and proc.stdout.strip()
        else None
    )
    if toplevel != path.resolve():
        raise RuntimeError(
            f"{path} exists but is not a standalone git repository (git "
            f"resolves it to {toplevel or 'no repository'}). Refusing to fetch "
            f"or check out there. Remove {path} and re-run."
        )


def ensure_clone(spec: RepoSpec, cache_dir: Path | None = None) -> Path:
    """Shallow-clone *spec* at its pinned SHA, reusing an existing checkout.

    Returns the checkout path. Raises when the checkout cannot be placed at the
    pinned SHA -- a corpus that silently measured "whatever upstream looks like
    today" would be worse than no corpus at all.
    """
    cache_dir = cache_dir or corpus_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / spec.name

    if path.exists():
        _assert_standalone_repo(path)
        if head_sha(path) == spec.commit:
            return path

    if not path.exists():
        path.mkdir(parents=True)
        proc = _git(["init", "-q"], cwd=path)
        if proc.returncode != 0:
            raise RuntimeError(f"git init failed in {path}: {proc.stderr.strip()}")
        proc = _git(["remote", "add", "origin", spec.url], cwd=path)
        if proc.returncode != 0:
            raise RuntimeError(f"git remote add failed in {path}: {proc.stderr.strip()}")

    proc = _git(["fetch", "--depth", "1", "origin", spec.commit], cwd=path)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git fetch of {spec.name} @ {spec.commit} from {spec.url} failed: "
            f"{proc.stderr.strip()}"
        )
    proc = _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=path)
    if proc.returncode != 0:
        raise RuntimeError(f"git checkout failed in {path}: {proc.stderr.strip()}")

    actual = head_sha(path)
    if actual != spec.commit:
        raise RuntimeError(
            f"{spec.name}: checkout is at {actual}, expected the pinned "
            f"{spec.commit}; the measurement would not be reproducible"
        )
    return path


def head_sha(path: Path) -> str:
    proc = _git(["rev-parse", "HEAD"], cwd=path)
    return proc.stdout.strip() if proc.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """Everything the corpus check compares, for one repository."""

    repo: str
    language: str
    commit: str
    files_parsed: int
    parse_errors: int
    files_without_nodes: int
    total_nodes: int
    total_edges: int
    file_nodes: int
    resolved_edge_share: float
    imports_resolved_share: float
    dangling_contains_edges: int
    control_char_names: int
    build_seconds: float
    languages: list[str] = field(default_factory=list)
    #: First few offending paths/names, for the failure message only. Not
    #: compared, not recorded in the baseline file.
    samples: dict = field(default_factory=dict)

    def to_baseline(self) -> dict:
        data = asdict(self)
        data.pop("samples", None)
        data["resolved_edge_share"] = round(self.resolved_edge_share, 4)
        data["imports_resolved_share"] = round(self.imports_resolved_share, 4)
        data["build_seconds"] = round(self.build_seconds, 2)
        return data


def _has_control_character(text: str) -> bool:
    """True when *text* holds a C0/C1 control character."""
    return any(unicodedata.category(char) == "Cc" for char in text)


def measure(spec: RepoSpec, repo_path: Path, db_path: Path) -> Measurement:
    """Build the graph for *repo_path* and return the measured properties."""
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    # ``full_build`` canonicalises the root before anchoring every stored
    # ``file_path`` to it; resolve here too so the inventory comparison below
    # compares the same spelling (notably /tmp -> /private/tmp on macOS).
    repo_path = repo_path.expanduser().resolve()
    store = GraphStore(db_path)
    try:
        started = time.perf_counter()
        build = full_build(repo_path, store)
        build_seconds = time.perf_counter() - started

        stats = store.get_stats()
        conn = store._conn

        files_with_nodes = {
            row[0]
            for row in conn.execute("SELECT DISTINCT file_path FROM nodes")
        }
        parsed_abs = [
            (repo_path / rel).resolve().as_posix()
            for rel in _parsed_relative_paths(repo_path)
        ]
        missing = [p for p in parsed_abs if p not in files_with_nodes]

        resolved = conn.execute(
            "SELECT COUNT(*) FROM edges e "
            "WHERE EXISTS (SELECT 1 FROM nodes n "
            "WHERE n.qualified_name = e.target_qualified)"
        ).fetchone()[0]
        total_edges = stats.total_edges
        share = (resolved / total_edges) if total_edges else 0.0

        # Imports specifically: the share of IMPORTS_FROM edges whose target is
        # a node in this repository. This is what "changing file X shows you
        # every file that depends on it" is made of, and it is the first thing
        # a broken module resolver takes away.
        imports_total, imports_resolved = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN EXISTS "
            "(SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified) "
            "THEN 1 ELSE 0 END), 0) FROM edges e WHERE e.kind = 'IMPORTS_FROM'"
        ).fetchone()
        imports_share = (imports_resolved / imports_total) if imports_total else 0.0

        # A CONTAINS edge is emitted by the parser between two nodes it just
        # created, so a source that is not a node means the two halves of the
        # build disagree about a symbol's identity and every children_of query
        # on that symbol comes back empty.
        dangling_contains = conn.execute(
            "SELECT COUNT(*) FROM edges e WHERE e.kind = 'CONTAINS' "
            "AND NOT EXISTS (SELECT 1 FROM nodes n "
            "WHERE n.qualified_name = e.source_qualified)"
        ).fetchone()[0]

        bad_names = [
            row[0]
            for row in conn.execute("SELECT name, qualified_name FROM nodes")
            if _has_control_character(row[0]) or _has_control_character(row[1])
        ]

        return Measurement(
            repo=spec.name,
            language=spec.language,
            commit=spec.commit,
            files_parsed=int(build["files_parsed"]),
            parse_errors=len(build["errors"]),
            files_without_nodes=len(missing),
            total_nodes=stats.total_nodes,
            total_edges=total_edges,
            file_nodes=stats.files_count,
            resolved_edge_share=share,
            imports_resolved_share=imports_share,
            dangling_contains_edges=int(dangling_contains),
            control_char_names=len(bad_names),
            build_seconds=build_seconds,
            languages=sorted(stats.languages),
            samples={
                "files_without_nodes": [
                    Path(p).name for p in missing[:5]
                ],
                "parse_errors": [e.get("file") for e in build["errors"][:5]],
                "control_char_names": bad_names[:5],
            },
        )
    finally:
        store.close()


def _parsed_relative_paths(repo_path: Path) -> list[str]:
    """Repo-relative paths ``full_build`` attempted, files that raised included.

    ``full_build`` reports only a count, so re-run the same collector it used.
    ``collect_all_files`` is pure (``git ls-files`` plus the ignore rules) and
    the checkout does not change between the two calls.
    """
    from code_review_graph.incremental import collect_all_files

    return collect_all_files(repo_path)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class PropertyResult:
    name: str
    measured: float
    baseline: float
    ok: bool
    message: str


@dataclass(frozen=True)
class Bands:
    """Tolerances, loaded from the baseline file so drift shows in a diff."""

    files_parsed_low: float
    files_parsed_high: float
    total_nodes_low: float
    total_nodes_high: float
    total_edges_low: float
    total_edges_high: float
    file_nodes_low: float
    file_nodes_high: float
    resolved_edge_share_drop: float
    imports_resolved_share_drop: float
    build_seconds_multiplier: float
    build_seconds_floor: float

    @classmethod
    def from_dict(cls, data: dict) -> "Bands":
        return cls(**{k: float(v) for k, v in data.items() if not k.startswith("_")})


def _pct(measured: float, baseline: float) -> str:
    if baseline == 0:
        return "n/a" if measured == 0 else "+inf%"
    return f"{(measured - baseline) / baseline * 100:+.1f}%"


def _band_result(
    name: str,
    measured: float,
    baseline: float,
    low_factor: float,
    high_factor: float,
) -> PropertyResult:
    low = baseline * low_factor
    high = baseline * high_factor
    ok = low <= measured <= high
    direction = "fell below" if measured < low else "rose above"
    message = (
        f"{name} {direction} its band: baseline={baseline:g} "
        f"measured={measured:g} delta={measured - baseline:+g} "
        f"({_pct(measured, baseline)}), allowed [{low:.1f}, {high:.1f}] "
        f"(x{low_factor:g}..x{high_factor:g})"
    )
    return PropertyResult(name, measured, baseline, ok, "" if ok else message)


def _ceiling_result(name: str, measured: float, baseline: float, note: str) -> PropertyResult:
    ok = measured <= baseline
    message = (
        f"{name} rose above its recorded ceiling: baseline={baseline:g} "
        f"measured={measured:g} delta={measured - baseline:+g}. {note}"
    )
    return PropertyResult(name, measured, baseline, ok, "" if ok else message)


def compare(measurement: Measurement, baseline: dict, bands: Bands) -> list[PropertyResult]:
    """Compare one measurement against its baseline. Order is stable."""
    results = [
        _band_result(
            "files_parsed",
            measurement.files_parsed,
            baseline["files_parsed"],
            bands.files_parsed_low,
            bands.files_parsed_high,
        ),
        _ceiling_result(
            "parse_errors",
            measurement.parse_errors,
            baseline["parse_errors"],
            f"files that raised: {measurement.samples.get('parse_errors')}",
        ),
        _ceiling_result(
            "files_without_nodes",
            measurement.files_without_nodes,
            baseline["files_without_nodes"],
            "a collected source file that yields no node at all is invisible to "
            f"every query. Sample: {measurement.samples.get('files_without_nodes')}",
        ),
        _band_result(
            "total_nodes",
            measurement.total_nodes,
            baseline["total_nodes"],
            bands.total_nodes_low,
            bands.total_nodes_high,
        ),
        _band_result(
            "total_edges",
            measurement.total_edges,
            baseline["total_edges"],
            bands.total_edges_low,
            bands.total_edges_high,
        ),
        _band_result(
            "file_nodes",
            measurement.file_nodes,
            baseline["file_nodes"],
            bands.file_nodes_low,
            bands.file_nodes_high,
        ),
    ]

    floor = baseline["resolved_edge_share"] - bands.resolved_edge_share_drop
    share = measurement.resolved_edge_share
    results.append(
        PropertyResult(
            "resolved_edge_share",
            share,
            baseline["resolved_edge_share"],
            share >= floor,
            ""
            if share >= floor
            else (
                "resolved_edge_share fell below its floor: "
                f"baseline={baseline['resolved_edge_share']:.4f} "
                f"measured={share:.4f} "
                f"delta={share - baseline['resolved_edge_share']:+.4f} "
                f"({_pct(share, baseline['resolved_edge_share'])}), "
                f"floor={floor:.4f}. Edge targets stopped resolving to real "
                "nodes, so callers/callees queries return less than before."
            ),
        )
    )

    import_floor = (
        baseline["imports_resolved_share"] - bands.imports_resolved_share_drop
    )
    import_share = measurement.imports_resolved_share
    results.append(
        PropertyResult(
            "imports_resolved_share",
            import_share,
            baseline["imports_resolved_share"],
            import_share >= import_floor,
            ""
            if import_share >= import_floor
            else (
                "imports_resolved_share fell below its floor: "
                f"baseline={baseline['imports_resolved_share']:.4f} "
                f"measured={import_share:.4f} "
                f"delta={import_share - baseline['imports_resolved_share']:+.4f} "
                f"({_pct(import_share, baseline['imports_resolved_share'])}), "
                f"floor={import_floor:.4f}. Fewer imports now point at a file in "
                "the repository, so importers_of and get_impact_radius under-"
                "report which files depend on a change."
            ),
        )
    )

    results.append(
        _ceiling_result(
            "dangling_contains_edges",
            measurement.dangling_contains_edges,
            baseline["dangling_contains_edges"],
            "A CONTAINS edge whose source is not a node means the parser named "
            "the same symbol two different ways, so children_of on it returns "
            "nothing.",
        )
    )

    results.append(
        PropertyResult(
            "control_char_names",
            measurement.control_char_names,
            0,
            measurement.control_char_names == 0,
            ""
            if measurement.control_char_names == 0
            else (
                "control_char_names rose above zero: baseline=0 "
                f"measured={measurement.control_char_names} "
                f"delta={measurement.control_char_names:+d}. Node names carrying "
                "C0/C1 controls reach MCP clients verbatim from every path that "
                "does not call _sanitize_name. Sample: "
                f"{measurement.samples.get('control_char_names')}"
            ),
        )
    )

    budget = max(
        baseline["build_seconds"] * bands.build_seconds_multiplier,
        baseline["build_seconds"] + bands.build_seconds_floor,
    )
    results.append(
        PropertyResult(
            "build_seconds",
            measurement.build_seconds,
            baseline["build_seconds"],
            measurement.build_seconds <= budget,
            ""
            if measurement.build_seconds <= budget
            else (
                "build_seconds rose above its budget: "
                f"baseline={baseline['build_seconds']:.2f}s "
                f"measured={measurement.build_seconds:.2f}s "
                f"delta={measurement.build_seconds - baseline['build_seconds']:+.2f}s "
                f"({_pct(measurement.build_seconds, baseline['build_seconds'])}), "
                f"budget={budget:.2f}s"
            ),
        )
    )

    present = measurement.language in measurement.languages
    results.append(
        PropertyResult(
            "primary_language_present",
            1 if present else 0,
            1,
            present,
            ""
            if present
            else (
                "primary_language_present dropped: baseline=1 measured=0 "
                f"delta=-1. {measurement.repo} is a {measurement.language} "
                f"project but the graph holds languages {measurement.languages}. "
                "The extension mapping or the grammar for that language stopped "
                "producing File nodes."
            ),
        )
    )
    return results


#: Every property the corpus compares, in the order ``compare`` emits them.
#: The canary asserts the returned set matches this exactly, so a property that
#: silently stops being evaluated fails the check instead of passing it.
PROPERTY_NAMES: tuple[str, ...] = (
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
    "primary_language_present",
)


# ---------------------------------------------------------------------------
# Baseline file
# ---------------------------------------------------------------------------


def load_baselines() -> dict:
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def failures(results: Iterable[PropertyResult]) -> list[PropertyResult]:
    return [r for r in results if not r.ok]


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record(names: list[str] | None = None) -> dict:
    """Build every repository in the corpus and return a fresh baseline dict.

    Used by ``python -m tests.real_repo_corpus --record``. Never called from a
    test: a check that rewrites its own baseline cannot fail.
    """
    existing = load_baselines() if BASELINE_PATH.exists() else {}
    repos = dict(existing.get("repos", {}))
    cache = corpus_cache_dir()
    with tempfile.TemporaryDirectory(prefix="crg-corpus-db-") as tmp:
        for spec in repo_specs():
            if names and spec.name not in names:
                continue
            print(f"[corpus] {spec.name} @ {spec.commit[:12]} ...", flush=True)
            path = ensure_clone(spec, cache)
            db = Path(tmp) / f"{spec.name}.db"
            m = measure(spec, path, db)
            repos[spec.name] = m.to_baseline()
            print(
                f"[corpus] {spec.name}: files={m.files_parsed} "
                f"nodes={m.total_nodes} edges={m.total_edges} "
                f"resolved={m.resolved_edge_share:.3f} "
                f"imports_resolved={m.imports_resolved_share:.3f} "
                f"dangling_contains={m.dangling_contains_edges} "
                f"errors={m.parse_errors} orphan_files={m.files_without_nodes} "
                f"{m.build_seconds:.1f}s",
                flush=True,
            )
    out = dict(existing)
    out["repos"] = repos
    return out


def _default_bands() -> dict:
    return {
        "_rationale": (
            "A pinned commit parsed by a fixed build is deterministic, so these "
            "bands do not model measurement noise. They model how much "
            "intentional change the project may make before the baseline has to "
            "be re-recorded. Low factors are tight because losing nodes, files "
            "or edges is always a regression; high factors are loose because "
            "adding extraction is the normal direction of travel. build_seconds "
            "is the exception: wall clock varies with the machine, so its budget "
            "is set to catch an algorithmic blow-up, not a slower runner."
        ),
        "_files_parsed": (
            "The inventory of a fixed commit only moves when the ignore rules "
            "or the supported-extension list change. -2% is 'must not lose "
            "files'; +25% leaves room for a newly supported extension."
        ),
        "_total_nodes": (
            "-8% is several hundred nodes on every repo here, far outside any "
            "legitimate tidy-up; +40% admits a whole new node kind without a "
            "forced re-record but still catches runaway duplication."
        ),
        "_total_edges": (
            "Wider than nodes in both directions because resolvers routinely "
            "trade edges for precision: -10% still means one relationship in "
            "ten stopped being recorded."
        ),
        "_resolved_edge_share": (
            "A floor in percentage points, not a ratio, so a repo that starts "
            "near 0.20 and one near 0.60 get the same absolute protection. 5pp "
            "is roughly a thousand edges on the larger repos."
        ),
        "_build_seconds": (
            "max(3x, +20s): 3x catches a quadratic resolver; the +20s term "
            "keeps the sub-2-second repos from failing on scheduler noise."
        ),
        "files_parsed_low": 0.98,
        "files_parsed_high": 1.25,
        "total_nodes_low": 0.92,
        "total_nodes_high": 1.40,
        "total_edges_low": 0.90,
        "total_edges_high": 1.50,
        "file_nodes_low": 0.98,
        "file_nodes_high": 1.25,
        "resolved_edge_share_drop": 0.05,
        "imports_resolved_share_drop": 0.05,
        "build_seconds_multiplier": 3.0,
        "build_seconds_floor": 20.0,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="re-record baselines")
    parser.add_argument("--repo", action="append", help="limit to these repo names")
    parser.add_argument(
        "--clean", action="store_true", help="delete the clone cache first"
    )
    args = parser.parse_args(argv)

    if args.clean and corpus_cache_dir().exists():
        shutil.rmtree(corpus_cache_dir())

    if not args.record:
        for spec in repo_specs():
            print(f"{spec.name:16} {spec.language:11} {spec.commit} {spec.url}")
        return 0

    data = record(args.repo)
    data.setdefault("bands", _default_bands())
    data["_generated_by"] = "python -m tests.real_repo_corpus --record"
    ordered = {
        "_generated_by": data["_generated_by"],
        "bands": data["bands"],
        "repos": data["repos"],
    }
    BASELINE_PATH.write_text(
        json.dumps(ordered, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"[corpus] wrote {BASELINE_PATH}")
    return 0


if __name__ == "__main__":  # pragma: no cover - developer entry point
    raise SystemExit(main())

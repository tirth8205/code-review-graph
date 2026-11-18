"""Reviewed dead-code corpus: compare `find_dead_code` with human verdicts.

The corpus records symbols a reviewer classified as ``live`` or ``dead``, with
the evidence and provenance that justified the label. Running it against the
real checkouts turns "the tool reports too much" into a measurable contract:

* a ``live`` symbol the engine reports is a **false positive** and fails;
* a ``dead`` symbol the engine no longer reports is a **false negative** and
  fails when it exceeds the recorded baseline or lowers recall.

CI cannot see the private checkouts, so it runs the synthetic tests in
``tests/test_dead_code_corpus.py``. The real gate is ``make eval-dead-code`` on
a host that has the repositories; roots come from ``CRG_CORPUS_ROOT`` or one
``CRG_CORPUS_<REPO>`` variable per repository.

Re-record the baseline after a reviewed, intentional change::

    python -m tests.dead_code_corpus --record
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS_PATH = Path(__file__).with_name("dead_code_corpus.json")
DEFAULT_ROOT_ENV = "CRG_CORPUS_ROOT"

#: Verdicts a corpus entry may carry.
LIVE = "live"
DEAD = "dead"


@dataclass(frozen=True)
class Entry:
    """One reviewed symbol and the evidence behind its verdict."""

    repo: str
    name: str
    kind: str
    verdict: str
    reason_code: str
    provenance: str
    qualified_name: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class Outcome:
    """One entry after comparison with the engine's report."""

    entry: Entry
    reported: bool

    @property
    def false_positive(self) -> bool:
        return self.entry.verdict == LIVE and self.reported

    @property
    def false_negative(self) -> bool:
        return self.entry.verdict == DEAD and not self.reported


@dataclass
class Report:
    """Corpus evaluation result, ready for a gate or for `--record`."""

    outcomes: list[Outcome] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)

    @property
    def false_positives(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.false_positive]

    @property
    def false_negatives(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.false_negative]

    @property
    def expected_dead(self) -> int:
        return sum(1 for o in self.outcomes if o.entry.verdict == DEAD)

    @property
    def recall(self) -> float | None:
        dead = self.expected_dead
        if not dead:
            return None
        return (dead - len(self.false_negatives)) / dead


def load_corpus(path: Path = CORPUS_PATH) -> dict[str, Any]:
    """Load the corpus manifest, validating the fields the gate depends on."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"{path}: unsupported corpus version {data.get('version')!r}")
    for raw in data.get("entries", []):
        missing = {"repo", "name", "kind", "verdict", "reason_code", "provenance"} - set(raw)
        if missing:
            raise ValueError(f"{path}: entry {raw.get('name')!r} is missing {sorted(missing)}")
        if raw["verdict"] not in (LIVE, DEAD):
            raise ValueError(f"{path}: entry {raw['name']!r} has verdict {raw['verdict']!r}")
    return data


def resolve_roots(corpus: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Path]:
    """Resolve every declared repository root, or report the ones that are absent."""
    env = environ if environ is not None else dict(os.environ)
    default_root = env.get(corpus.get("default_root_env", DEFAULT_ROOT_ENV), "")
    roots: dict[str, Path] = {}
    for repo in corpus.get("repos", {}):
        override = env.get(f"CRG_CORPUS_{repo.upper().replace('-', '_')}", "")
        candidate = Path(override or (Path(default_root) / repo if default_root else ""))
        roots[repo] = candidate
    return roots


def _reported_paths(rows: list[dict[str, Any]]) -> tuple[set[str], set[tuple[str, str]]]:
    qualified: set[str] = set()
    by_path: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("qualified_name"):
            qualified.add(row["qualified_name"])
        rel = row.get("relative_path") or row.get("file") or ""
        if rel:
            by_path.add((rel.replace("\\", "/"), row.get("name", "")))
    return qualified, by_path


def evaluate(
    corpus: dict[str, Any],
    rows_by_repo: dict[str, list[dict[str, Any]]],
    missing_roots: list[str],
) -> Report:
    """Compare corpus entries with the engine's rows; pure and testable."""
    report = Report(missing_roots=list(missing_roots))
    if not corpus.get("entries"):
        raise ValueError("the corpus is empty: a gate over no entries proves nothing")
    for raw in corpus["entries"]:
        entry = Entry(**raw)
        if entry.repo in missing_roots:
            continue
        qualified, by_path = _reported_paths(rows_by_repo.get(entry.repo, []))
        reported = bool(entry.qualified_name and entry.qualified_name in qualified)
        if not reported and entry.path:
            reported = (entry.path.replace("\\", "/"), entry.name) in by_path
        if not reported and not entry.qualified_name and not entry.path:
            raise ValueError(f"entry {entry.name!r} needs a qualified_name or a path")
        report.outcomes.append(Outcome(entry=entry, reported=reported))
    return report


def resolve_data_dirs(
    corpus: dict[str, Any], environ: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Per-repository graph database overrides, for a dev/installed skew.

    The registry graph may be built by a different analyzer version than the one
    under test (a released tool and a worktree). ``CRG_CORPUS_DATA_<REPO>``
    points the gate at the database the current code actually produced.
    """
    env = environ if environ is not None else dict(os.environ)
    found: dict[str, Path] = {}
    for repo in corpus.get("repos", {}):
        override = env.get(f"CRG_CORPUS_DATA_{repo.upper().replace('-', '_')}", "")
        if override:
            found[repo] = Path(override)
    return found


def collect_rows(
    roots: dict[str, Path],
    data_dirs: dict[str, Path] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Run `find_dead_code` over every checkout that is actually present."""
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import assert_graph_serves_root, get_db_path
    from code_review_graph.refactor import find_dead_code

    rows: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for repo, root in roots.items():
        if not root.is_dir():
            missing.append(repo)
            continue
        db_path = (data_dirs or {}).get(repo) or get_db_path(root)
        store = GraphStore(db_path, checkout=root, read_only=True)
        try:
            assert_graph_serves_root(root, store)
            rows[repo] = find_dead_code(store, root=root)
        finally:
            store.close()
    return rows, missing


def _label(entry: Entry) -> str:
    """Name an entry the way a failure message should read."""
    where = entry.qualified_name or f"{entry.path}::{entry.name}"
    return f"[{entry.repo}] {where} ({entry.kind})"


def format_report(report: Report, baseline: dict[str, Any]) -> str:
    """Render the gate result: failures first, then the measured numbers."""
    lines: list[str] = []
    for outcome in report.false_positives:
        entry = outcome.entry
        lines.append(
            f"FALSE POSITIVE {_label(entry)} "
            f"reason={entry.reason_code} provenance={entry.provenance}"
        )
    for outcome in report.false_negatives:
        entry = outcome.entry
        lines.append(
            f"FALSE NEGATIVE {_label(entry)} "
            f"reason={entry.reason_code} provenance={entry.provenance}"
        )
    for repo in report.missing_roots:
        lines.append(f"MISSING ROOT [{repo}] set CRG_CORPUS_ROOT or CRG_CORPUS_{repo.upper()}")
    recall = report.recall
    lines.append(
        f"entries={len(report.outcomes)} expected_dead={report.expected_dead} "
        f"fp={len(report.false_positives)} fn={len(report.false_negatives)} "
        f"recall={'n/a' if recall is None else f'{recall:.3f}'}"
    )
    lines.append(
        f"baseline expected_dead={baseline.get('expected_dead')} "
        f"fn={baseline.get('false_negatives')} recall={baseline.get('recall')}"
    )
    return "\n".join(lines)


def gate(report: Report, baseline: dict[str, Any]) -> int:
    """Return the process exit code: 0 only when precision and recall hold."""
    if report.missing_roots:
        return 2
    if report.false_positives:
        return 1
    if len(report.false_negatives) > int(baseline.get("false_negatives", 0)):
        return 1
    recorded = baseline.get("recall")
    if recorded is not None and report.recall is not None and report.recall < float(recorded):
        return 1
    return 0


def record(report: Report, baseline: dict[str, Any]) -> dict[str, Any]:
    """Produce the baseline a reviewed change is allowed to keep."""
    return {
        "expected_dead": report.expected_dead,
        "false_negatives": len(report.false_negatives),
        "recall": report.recall,
        "recorded_from": baseline.get("recorded_from", "reviewed corpus"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--record", action="store_true", help="rewrite the baseline in place")
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    used = {raw["repo"] for raw in corpus["entries"]}
    roots = {repo: path for repo, path in resolve_roots(corpus).items() if repo in used}
    rows, missing = collect_rows(roots, resolve_data_dirs(corpus))
    report = evaluate(corpus, rows, missing)
    print(format_report(report, corpus.get("baseline", {})))
    if args.record:
        if report.false_positives or missing:
            print("refusing to record: false positives or missing roots present")
            return 1
        corpus["baseline"] = record(report, corpus.get("baseline", {}))
        args.corpus.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
        print(f"recorded baseline in {args.corpus}")
        return 0
    return gate(report, corpus.get("baseline", {}))


if __name__ == "__main__":
    raise SystemExit(main())

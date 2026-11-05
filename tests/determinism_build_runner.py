"""Out-of-process full build used by ``tests/test_determinism.py``.

Not a test module. It exists as a standalone script because the determinism
gate has to vary things that can only be chosen *before* the interpreter
starts (``PYTHONHASHSEED``) or that are process-global once a build has run
(the parse executor, the file inventory). Driving each build through a fresh
subprocess keeps every variant honest: no state survives from the previous
one.

Usage::

    python determinism_build_runner.py <repo_root> <out_db> [--order ORDER]

``--order`` controls the order ``full_build`` receives the file inventory in:

``asis``      the inventory exactly as ``collect_all_files`` returned it
``reversed``  the same files, reversed
``rotate``    the same files, rotated by half their length

The build always writes into ``<repo_root>/.code-review-graph`` (the real
location the real pipeline uses); the finished database is then copied to
``<out_db>`` with the SQLite backup API so the copy is consistent even if a
WAL segment is still open.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path


def _apply_order(order: str) -> None:
    """Patch the file inventory so ``full_build`` sees a different order.

    The same files with the same contents, presented differently. Any
    difference in the resulting graph is a real ordering dependency, not a
    difference of input.
    """
    if order == "asis":
        return

    from code_review_graph import incremental

    original = incremental.collect_all_files

    def reordered(*args: object, **kwargs: object) -> list[str]:
        files = list(original(*args, **kwargs))  # type: ignore[arg-type]
        if not files:
            raise SystemExit("file inventory was empty; nothing to reorder")
        if order == "reversed":
            return files[::-1]
        if order == "rotate":
            half = len(files) // 2
            return files[half:] + files[:half]
        raise SystemExit(f"unknown order: {order}")

    incremental.collect_all_files = reordered  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root")
    parser.add_argument("out_db")
    parser.add_argument(
        "--order",
        default="asis",
        choices=("asis", "reversed", "rotate"),
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    data_dir = repo_root / ".code-review-graph"
    shutil.rmtree(data_dir, ignore_errors=True)

    _apply_order(args.order)

    from code_review_graph.tools.build import build_or_update_graph

    result = build_or_update_graph(
        full_rebuild=True,
        repo_root=str(repo_root),
        postprocess="full",
    )

    db_path = data_dir / "graph.db"
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path} after build")

    out_db = Path(args.out_db)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(out_db))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    # The harness reads this off stdout to assert the build did real work
    # before any comparison is attempted.
    print(json.dumps({
        "files_parsed": result.get("files_parsed"),
        "total_nodes": result.get("total_nodes"),
        "total_edges": result.get("total_edges"),
        "status": result.get("status"),
        "warnings": result.get("warnings", []),
        "errors": [e.get("file") for e in result.get("errors", [])],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

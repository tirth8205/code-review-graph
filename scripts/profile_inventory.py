"""Profile CRG's authoritative parseable-file inventory for one repository."""

from __future__ import annotations

import sys
from pathlib import Path

from code_review_graph.incremental import collect_all_files


def main() -> None:
    """Print the inventory count so profiles retain a decisive result."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: profile_inventory.py REPOSITORY")
    repository = Path(sys.argv[1]).resolve(strict=True)
    files = collect_all_files(repository)
    print(f"repository={repository}")
    print(f"parseable_files={len(files)}")


if __name__ == "__main__":
    main()

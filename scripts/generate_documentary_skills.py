#!/usr/bin/env python3
"""Write skills/build-graph, review-delta and review-pr from one source.

``DOCUMENTARY_SKILLS`` in ``code_review_graph/skills.py`` holds the text of
these three ``SKILL.md`` files. Edit it there, then run:

    uv run python scripts/generate_documentary_skills.py

With ``--check`` nothing is written; the script lists the files that differ
from the source and exits 1, so a hand edit to a generated file is caught.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from code_review_graph.skills import (
    DOCUMENTARY_SKILLS,
    generate_documentary_skills,
    render_documentary_skill_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def stale_skills(skills_dir: Path) -> list[str]:
    """Return the slugs whose ``SKILL.md`` is missing or differs from the source."""
    stale = []
    for slug in DOCUMENTARY_SKILLS:
        path = skills_dir / slug / "SKILL.md"
        if not path.is_file() or path.read_bytes() != render_documentary_skill_markdown(
            slug
        ).encode("utf-8"):
            stale.append(slug)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if a file differs",
    )
    parser.add_argument(
        "--skills-dir", type=Path, default=REPO_ROOT / "skills", help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    stale = stale_skills(args.skills_dir)
    if args.check:
        for slug in stale:
            print(f"{args.skills_dir / slug / 'SKILL.md'} differs from DOCUMENTARY_SKILLS")
        return 1 if stale else 0
    generate_documentary_skills(REPO_ROOT, skills_dir=args.skills_dir)
    for slug in stale:
        print(f"wrote {args.skills_dir / slug / 'SKILL.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

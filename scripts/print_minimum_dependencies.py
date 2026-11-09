"""Print the installed version of each package in the minimum-version file."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REQUIREMENTS = Path(__file__).parents[1] / ".github" / "requirements-minimum.txt"


def main() -> None:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        package = line.split("==", 1)[0]
        try:
            print(f"{package}=={version(package)}")
        except PackageNotFoundError:
            # A pin with a marker, such as tomli on Python 3.11 and newer.
            print(f"{package}: not installed")


if __name__ == "__main__":
    main()

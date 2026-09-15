from __future__ import annotations

from importlib.metadata import version

PACKAGES = (
    "mcp",
    "fastmcp",
    "tree-sitter",
    "tree-sitter-language-pack",
    "pyyaml",
    "networkx",
    "watchdog",
)


def main() -> None:
    for package in PACKAGES:
        print(f"{package}=={version(package)}")


if __name__ == "__main__":
    main()

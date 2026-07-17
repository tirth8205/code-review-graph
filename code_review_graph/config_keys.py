"""Shared, value-free handling for Spring configuration property keys."""

from __future__ import annotations

import re
from pathlib import Path

_SPRING_CONFIG_NAME = re.compile(
    r"^application(?:-[A-Za-z0-9_.-]+)?\.(?:properties|ya?ml)$",
    re.IGNORECASE,
)


def is_spring_config_path(path: Path) -> bool:
    """Return whether *path* uses Spring Boot's application-file convention."""
    return bool(_SPRING_CONFIG_NAME.fullmatch(path.name))


def normalize_spring_config_key(key: str) -> str:
    """Canonicalize relaxed-binding spellings without inspecting their values."""
    normalized: list[str] = []
    for segment in key.strip().split("."):
        match = re.fullmatch(r"(.*?)(\[[0-9]+\])?", segment)
        base = match.group(1) if match else segment
        index = match.group(2) or "" if match else ""
        tokens = [token for token in re.split(r"[-_]+", base) if token]
        if not tokens:
            normalized.append(index)
            continue
        head = tokens[0].lower() if base.isupper() or len(tokens) > 1 else tokens[0]
        tail = "".join(token[:1].upper() + token[1:].lower() for token in tokens[1:])
        normalized.append(f"{head}{tail}{index}")
    return ".".join(normalized)

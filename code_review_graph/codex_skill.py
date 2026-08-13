"""Install the bundled Code Review Graph skill for Codex.

The skill is kept as package data so ``code-review-graph install`` works from
both a source checkout and a wheel.  Installation is deliberately conservative:
an existing directory that was not created by CRG is never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CODEX_SKILL_NAME = "code-review-graph"
MANIFEST_NAME = ".code-review-graph-managed.json"
_RESOURCE_ROOT = Path(__file__).resolve().parent / "assets" / "code-review-graph"


def codex_home() -> Path:
    """Return the active Codex state directory.

    Codex defaults to ``~/.codex`` but accepts ``CODEX_HOME``.  Looking up the
    environment variable at call time matters for installers and tests that
    select a different Codex runtime after importing this module.
    """

    configured = os.environ.get("CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def codex_skill_dir() -> Path:
    """Return the user-level directory where Codex discovers this skill."""

    return codex_home() / "skills" / CODEX_SKILL_NAME


def bundled_skill_dir() -> Path:
    """Return the read-only skill resources shipped in the Python package."""

    return _RESOURCE_ROOT


def _iter_resource_files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"bundled Codex skill is missing: {root}")
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path
    if "SKILL.md" not in files:
        raise FileNotFoundError(f"bundled Codex skill has no SKILL.md: {root}")
    return files


def bundled_skill_hashes() -> dict[str, str]:
    """Return SHA-256 hashes for the files shipped with the skill."""

    return {
        relative: hashlib.sha256(path.read_bytes()).hexdigest()
        for relative, path in _iter_resource_files(bundled_skill_dir()).items()
    }


def _read_manifest(skill_dir: Path) -> dict[str, str] | None:
    path = skill_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and _is_safe_relative(key)
        for key, value in files.items()
    ):
        return None
    return dict(files)


def _is_safe_relative(relative: str) -> bool:
    """Return whether a manifest path stays below the skill directory."""

    path = Path(relative)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == relative
        and relative not in {"", "."}
    )


def _safe_target(root: Path, target: Path) -> bool:
    """Reject symlinked path components and paths outside ``root``."""

    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    current = target
    while current != root:
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
        current = current.parent
    return True


def _write_manifest(skill_dir: Path, hashes: dict[str, str]) -> None:
    payload = {"version": 1, "files": dict(sorted(hashes.items()))}
    (skill_dir / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def install_codex_skill() -> Path:
    """Install or update the bundled skill and return its destination.

    A manifest records files owned by CRG.  Reinstall updates only those files
    and preserves unrelated files.  If a pre-existing unmarked skill conflicts
    with a bundled file, installation is skipped instead of clobbering it.
    """

    source_files = _iter_resource_files(bundled_skill_dir())
    destination = codex_skill_dir()
    if destination.is_symlink() or not _safe_target(codex_home(), destination):
        logger.warning("Cannot install Codex skill through symlink: %s", destination)
        return destination
    destination.mkdir(parents=True, exist_ok=True)

    manifest = _read_manifest(destination)
    if (destination / MANIFEST_NAME).exists() and manifest is None:
        logger.warning(
            "Existing Codex skill manifest is invalid; leaving %s unchanged", destination
        )
        return destination

    # A manually installed copy may predate the manifest.  Adopt it only when
    # every overlapping file is byte-identical; never overwrite user content.
    if manifest is None:
        conflicts = [
            relative
            for relative, source in source_files.items()
            if (destination / relative).exists()
            and _file_hash(destination / relative)
            != hashlib.sha256(source.read_bytes()).hexdigest()
        ]
        if conflicts:
            logger.warning(
                "Existing unmarked Codex skill differs (%s); leaving it unchanged",
                ", ".join(conflicts),
            )
            return destination
        manifest = {}

    for relative in (*source_files, *manifest):
        if not _is_safe_relative(relative) or not _safe_target(
            destination, destination / relative
        ):
            logger.warning("Unsafe Codex skill path; leaving %s unchanged", destination)
            return destination

    current_hashes: dict[str, str] = {}
    preserved_files: set[str] = set()
    for relative, source in source_files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        old_hash = manifest.get(relative)
        if (
            target.exists()
            and old_hash is not None
            and _file_hash(target) not in {old_hash, source_hash}
        ):
            logger.warning("Preserving user-edited Codex skill file: %s", target)
            preserved_files.add(relative)
            continue
        shutil.copy2(source, target)
        current_hashes[relative] = source_hash

    # Remove obsolete files that the previous CRG manifest owned, but only if
    # they still match the recorded bytes.  User edits and unknown files stay.
    for relative, old_hash in manifest.items():
        if relative in source_files:
            continue
        old_path = destination / relative
        if old_path.is_file() and _file_hash(old_path) == old_hash:
            old_path.unlink()

    # Keep a user-edited file out of the next manifest so uninstall will not
    # mistake the edited bytes for installer-owned content.
    _write_manifest(
        destination,
        {
            **current_hashes,
            **{key: manifest[key] for key in preserved_files},
        },
    )
    logger.info("Installed Codex skill in %s", destination)
    return destination


def manifest_path(skill_dir: Path | None = None) -> Path:
    """Return the management manifest path for a destination skill."""

    return (skill_dir or codex_skill_dir()) / MANIFEST_NAME


def owned_skill_files(skill_dir: Path | None = None) -> list[Path]:
    """Return installed files that still match CRG-owned content.

    This lets uninstall remove a generated skill without deleting a user's
    edits or unrelated files in the shared user skill directory.
    """

    destination = skill_dir or codex_skill_dir()
    manifest = _read_manifest(destination)
    if manifest is None:
        return []
    expected = manifest
    owned: list[Path] = []
    for relative, expected_hash in expected.items():
        path = destination / relative
        if path.is_file() and _file_hash(path) == expected_hash:
            owned.append(path)
    manifest_file = manifest_path(destination)
    if manifest is not None and manifest_file.is_file():
        owned.append(manifest_file)
    return owned

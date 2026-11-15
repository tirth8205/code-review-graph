"""Content-addressed virtual store for CRG index bundles.

A bundle is keyed by the repository's tree hash (``git rev-parse HEAD^{tree}``),
which captures the full committed tree including submodule gitlinks: one hash
identifies the exact indexed content of a checkout, so a worktree sharing a
HEAD can seed its index delta-zero instead of rebuilding (epic aihub-3t7yh.3.1).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION = 1
_RECEIPT_NAME = "receipt.json"
_BUNDLE_DIRNAME = "bundle"


@dataclass(frozen=True)
class VstoreReceipt:
    """Immutable evidence for one stored bundle."""

    schema_version: int
    tree_hash: str
    head: str
    repo_path: str
    embedding_model: str | None
    created_at: str
    files: dict[str, str]

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> VstoreReceipt:
        try:
            raw_files = payload["files"]
            if not isinstance(raw_files, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in raw_files.items()
            ):
                raise TypeError("files must map str -> str")
            schema_version = payload["schema_version"]
            if not isinstance(schema_version, int):
                raise TypeError("schema_version must be int")
            embedding_model = payload.get("embedding_model")
            return cls(
                schema_version=schema_version,
                tree_hash=str(payload["tree_hash"]),
                head=str(payload["head"]),
                repo_path=str(payload["repo_path"]),
                embedding_model=str(embedding_model) if embedding_model else None,
                created_at=str(payload["created_at"]),
                files=dict(raw_files),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid vstore receipt: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tree_hash": self.tree_hash,
            "head": self.head,
            "repo_path": self.repo_path,
            "embedding_model": self.embedding_model,
            "created_at": self.created_at,
            "files": self.files,
        }


def default_store_root() -> Path:
    """Resolve the store root from ``$CRG_VSTORE``, ``$CRG_HOME`` or the default home."""
    env = os.environ
    if env.get("CRG_VSTORE"):
        return Path(env["CRG_VSTORE"]).expanduser().resolve()
    if env.get("CRG_HOME"):
        return (Path(env["CRG_HOME"]).expanduser() / "vstore").resolve()
    return (Path.home() / ".code-review-graph" / "vstore").resolve()


def tree_hash(repo_root: Path) -> str:
    """Return the committed tree hash of ``repo_root``; fail loud when absent.

    Verifies that ``repo_root`` is itself a git repository root (not merely a
    subdirectory of one) to prevent ``git rev-parse`` from traversing upward
    and silently returning a parent repository's tree hash.
    """
    repo_root = Path(repo_root).resolve()
    toplevel = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        reason = toplevel.stderr.strip() or "not a git repository"
        raise RuntimeError(f"tree hash unavailable for {repo_root}: {reason}")
    actual_root = Path(toplevel.stdout.strip()).resolve()
    if actual_root != repo_root:
        raise RuntimeError(
            f"tree hash unavailable for {repo_root}: "
            f"not a repository root (git toplevel is {actual_root})"
        )
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tree hash unavailable for {repo_root}: {result.stderr.strip()}")
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"tree hash unavailable for {repo_root}: empty output")
    return commit


def _head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"HEAD unavailable for {repo_root}: {result.stderr.strip()}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reflink_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` preferring reflinks, falling back to a byte copy."""
    result = subprocess.run(
        ["cp", "--reflink=auto", str(src), str(dst)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.copyfile(src, dst)


def bundle_files(data_dir: Path) -> dict[str, Path]:
    """Collect the regular files of an index data directory, relative-keyed."""
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"index data directory not found: {data_dir}")
    files = {
        str(path.relative_to(data_dir)): path
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    }
    if not files:
        raise RuntimeError(f"index data directory is empty: {data_dir}")
    return files


def put(
    store_root: Path,
    repo_root: Path,
    data_dir: Path,
    embedding_model: str | None = None,
) -> VstoreReceipt:
    """Store ``data_dir``'s bundle under the repo's tree hash, atomically."""
    repo_root = Path(repo_root).resolve()
    digest = tree_hash(repo_root)
    files = bundle_files(data_dir)
    file_hashes = {key: _sha256(path) for key, path in files.items()}
    receipt = VstoreReceipt(
        schema_version=_SCHEMA_VERSION,
        tree_hash=digest,
        head=_head(repo_root),
        repo_path=str(repo_root),
        embedding_model=embedding_model,
        created_at=datetime.now(timezone.utc).isoformat(),
        files=file_hashes,
    )
    entry = Path(store_root).resolve() / digest
    staging = entry.parent / f".staging-{digest[:12]}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    bundle = staging / _BUNDLE_DIRNAME
    bundle.mkdir(parents=True)
    for key, src in files.items():
        dest = bundle / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        _reflink_copy(src, dest)
        if _sha256(dest) != file_hashes[key]:
            raise RuntimeError(f"reflink copy corrupted {key}: re-run failed")
    (staging / _RECEIPT_NAME).write_text(
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if entry.exists():
        shutil.rmtree(staging)
    else:
        entry.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(entry)
    return receipt


def _load_receipt(store_root: Path, digest: str) -> tuple[VstoreReceipt, Path]:
    entry = Path(store_root).resolve() / digest
    receipt_path = entry / _RECEIPT_NAME
    if not receipt_path.is_file():
        raise FileNotFoundError(f"no vstore entry for tree hash {digest}")
    receipt = VstoreReceipt.from_dict(json.loads(receipt_path.read_text(encoding="utf-8")))
    if receipt.tree_hash != digest:
        raise ValueError(f"receipt tree hash mismatch: {receipt.tree_hash!r} != {digest!r}")
    bundle = entry / _BUNDLE_DIRNAME
    for key, expected in receipt.files.items():
        stored = bundle / key
        if not stored.is_file():
            raise ValueError(f"bundle file missing: {key}")
        if _sha256(stored) != expected:
            raise ValueError(f"bundle file tampered: {key}")
    return receipt, bundle


def get(store_root: Path, digest: str, dest_dir: Path) -> VstoreReceipt:
    """Materialize a stored bundle into ``dest_dir`` after full receipt validation."""
    receipt, bundle = _load_receipt(store_root, digest)
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key in receipt.files:
        src = bundle / key
        dest = dest_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        _reflink_copy(src, dest)
    return receipt


def list_entries(store_root: Path) -> list[VstoreReceipt]:
    """Return every valid receipt in the store, oldest first."""
    root = Path(store_root).resolve()
    if not root.is_dir():
        return []
    receipts: list[VstoreReceipt] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime):
        receipt_path = entry / _RECEIPT_NAME
        if entry.is_dir() and receipt_path.is_file():
            receipts.append(
                VstoreReceipt.from_dict(json.loads(receipt_path.read_text(encoding="utf-8")))
            )
    return receipts


def retain(
    store_root: Path,
    *,
    max_entries: int | None = None,
    ttl_days: float | None = None,
) -> list[str]:
    """Apply LRU/TTL retention; return removed tree hashes (oldest first).

    Entries are ordered oldest-first by bundle mtime. ``max_entries`` keeps
    the newest N; ``ttl_days`` drops entries older than the window. Both may
    combine; at least one must be set.
    """
    if max_entries is None and ttl_days is None:
        raise ValueError("retention requires max_entries and/or ttl_days")
    entries = list_entries(store_root)
    if not entries:
        return []
    root = Path(store_root).resolve()
    victims: list[str] = []
    if ttl_days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - ttl_days * 86400
        for receipt in entries:
            entry = root / receipt.tree_hash
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                victims.append(receipt.tree_hash)
    if max_entries is not None:
        survivors = [r.tree_hash for r in entries if r.tree_hash not in victims]
        excess = survivors[:-max_entries] if max_entries > 0 else survivors
        victims.extend(t for t in excess if t not in victims)
    for digest in victims:
        shutil.rmtree(root / digest)
    return victims

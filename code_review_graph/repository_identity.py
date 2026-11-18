"""Git identities that permit artifact reuse, independently of graph views."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .constants import GIT_TIMEOUT
from .errors import GraphStoreError


def _remote_identity(root: Path, url: str) -> str | None:
    """Return a credential-free identity for a supported Git transport."""
    if "://" not in url:
        scp = re.fullmatch(r"(?:([^@/]+)@)?([^:/]+):([^:]+)", url)
        if not scp:
            local = (root / url).resolve()
            return f"file:{os.path.normcase(str(local))}" if local.is_dir() else None
        user, host, path = scp.groups()
        scheme, port = "ssh", None
    else:
        try:
            parsed = urlsplit(url)
            host, path = parsed.hostname or "", parsed.path
            scheme, port = parsed.scheme, parsed.port
            user = parsed.username
        except ValueError:
            return None
        if scheme == "file" and host in ("", "localhost"):
            local = Path(unquote(path)).resolve()
            return f"file:{os.path.normcase(str(local))}" if local.is_dir() else None
    default_ports = {"ssh": 22, "http": 80, "https": 443, "git": 9418}
    if scheme not in default_ports or not host or not path:
        return None
    host = host.lower()
    path = path.rstrip("/")
    if port == default_ports[scheme]:
        port = None
    # GitHub's standard endpoints identify the same owner/repo across
    # transports. Other servers can expose different repos over SSH and HTTP.
    if host == "github.com" and port is None:
        name = path.strip("/").removesuffix(".git").lower()
        if name.count("/") == 1:
            return f"{host}/{name}"
    # Relative SCP paths can resolve against different SSH users' homes.
    principal = f"{user}@" if scheme == "ssh" and user else ""
    return f"{scheme}://{principal}{host}:{port or default_ports[scheme]}/{path}"


def repository_scopes(root: Path) -> tuple[str, ...]:
    """Identify direct reuse peers through the Git common dir or any remote."""
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                       "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}
    }
    env["LC_ALL"] = "C"

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(root), *args), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False,
        )

    def read(*args: str) -> str:
        result = git(*args)
        if result.returncode:
            # The URL may contain credentials; never include Git's raw output.
            raise GraphStoreError(f"Cannot read Git repository identity for {root}")
        return result.stdout

    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        if "not a git repository" not in common.stderr:
            raise GraphStoreError(f"Cannot read Git common directory for {root}")
        peers = {f"path:{os.path.normcase(str(root.resolve()))}"}
    else:
        directory = Path(common.stdout.rstrip("\n")).resolve()
        peers = {f"git:{os.path.normcase(str(directory))}"}
        for remote in read("remote").splitlines():
            for url in read("remote", "get-url", "--all", remote).splitlines():
                if key := _remote_identity(root, url):
                    peers.add(f"remote:{key}")
    return tuple(sorted(hashlib.sha256(os.fsencode(peer)).hexdigest() for peer in peers))

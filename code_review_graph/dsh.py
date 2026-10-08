"""Native DSH bundle lifecycle, driven by the declarative platform inventory."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

BUNDLE = "dsh-code-review-graph"


def home() -> Path:
    return Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))


def config_path(_root: Path, profile: str = "web") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("DSH profile must contain only letters, digits, underscores or hyphens")
    return home() / "profiles" / profile / "package.json"


def configure(path: Path, *, install: bool, dry_run: bool = False) -> bool:
    """Use the released DSH CLI; preserve user YAML and unrelated manifest fields.

    An already-installed bundle is not rewritten. Desktop installation belongs
    to Electron and must be performed through its plugin manager.
    """
    profile = path.parent.name
    if profile == "desktop":
        print("  DSH Desktop: use the application's plugin manager to add/remove " + BUNDLE)
        return False
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("profile manifest must be an object")
            deps = manifest.get("dependencies", {})
            bundles = manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
            if not isinstance(deps, dict) or not isinstance(bundles, list):
                raise ValueError("invalid dependencies or bundles")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f"  DSH: skipping malformed profile {path}: {exc}")
            return False
    else:
        deps, bundles = {}, []
    present = BUNDLE in deps or BUNDLE in bundles
    if install and BUNDLE in deps and BUNDLE in bundles:
        print(f"  DSH: already configured in {path}")
        return True
    if not install and not present:
        return False
    action = "add" if install else "remove"
    if dry_run:
        print(f"  [dry-run] DSH: plugin --profile {profile} {action} {BUNDLE}")
        return True
    executable = shutil.which("dsh")
    if not executable:
        print("  DSH: CLI unavailable; install/manage the bundle in the DSH application")
        return False
    try:
        subprocess.run([executable, "plugin", "--profile", profile, action,
                        BUNDLE + "@0.1.2" if install else BUNDLE],
                       check=True, shell=False)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"  DSH: native bundle {action} failed: {exc}")
        return False
    return True

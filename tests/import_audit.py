"""Loader for ``scripts/audit_python_imports.py``.

The audit script is the reusable, reviewable definition of "is this import
edge correct"; the tests import from it so the CI guard and the numbers in
the pull request cannot drift apart. ``scripts/`` is not an importable
package, so the module is loaded by path, the way
``tests/test_action_render.py`` loads ``scripts/render_pr_comment.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "audit_python_imports.py"

_spec = importlib.util.spec_from_file_location("audit_python_imports", SCRIPT)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

audit_calls = audit.audit_calls
audit_dangling = audit.audit_dangling
audit_imports = audit.audit_imports
exists_case_exact = audit.exists_case_exact
looks_like_a_path = audit.looks_like_a_path
missing_path_targets = audit.missing_path_targets

__all__ = [
    "REPO_ROOT",
    "audit",
    "audit_calls",
    "audit_dangling",
    "audit_imports",
    "exists_case_exact",
    "looks_like_a_path",
    "missing_path_targets",
]

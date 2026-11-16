"""Framework and plugin loaders instantiate classes that have no call site.

An Ansible action or callback plugin is found by its class name in a plugins
directory and run by the runner; its base class is from ansible.plugins. Both
look dead when only CALLS edges are counted.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

ACTION = '''\
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        return super().run(tmp, task_vars)
'''

CALLBACK = '''\
from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    def v2_runner_on_ok(self, result):
        return result
'''

ORDINARY = '''\
class Unused:
    def helper(self):
        return 1
'''


def test_ansible_plugins_are_live_and_ordinary_classes_still_reported(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "action").mkdir(parents=True)
    (root / "plugins" / "callback").mkdir(parents=True)
    (root / "plugins" / "action" / "backup_jobs.py").write_text(ACTION, encoding="utf-8")
    (root / "plugins" / "callback" / "receipt.py").write_text(CALLBACK, encoding="utf-8")
    (root / "app.py").write_text(ORDINARY, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "ActionModule" not in dead
    assert "CallbackModule" not in dead
    assert "run" not in dead
    assert "Unused" in dead

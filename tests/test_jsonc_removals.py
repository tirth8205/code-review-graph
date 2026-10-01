"""Strict JSON and comment preservation regressions for issue #1068."""

import json
from pathlib import Path

import pytest

from code_review_graph.jsonc import remove_paths


@pytest.mark.parametrize("raw,paths,expected", [
    ('{"keep": 1, "a": 2, "b": 3}', [("a",), ("b",)], {"keep": 1}),
    ('{"a": 1, "b": 2, "keep": 3}', [("a",), ("b",)], {"keep": 3}),
    ('{"items": [1, 2, 3]}', [("items", 1), ("items", 2)], {"items": [1]}),
    ('{"items": [1, 2, 3]}', [("items", 0), ("items", 1)], {"items": [3]}),
    ('{"a": 1, "b": 2}', [("a",), ("b",)], {}),
])
def test_multi_removal_preserves_strict_json(raw, paths, expected):
    assert json.loads(remove_paths(raw, paths)) == expected


def test_removal_preserves_comments_and_string_commas():
    raw = '{"keep": "literal,}", /* user comment */ "a": 2, "b": 3}'
    rewritten = remove_paths(raw, [("a",), ("b",)])
    assert '"literal,}"' in rewritten
    assert "/* user comment */" in rewritten
    assert json.loads(rewritten.replace("/* user comment */", "")) == {"keep": "literal,}"}


def test_existing_jsonc_trailing_comma_is_preserved():
    raw = '{"keep": {"value": 1, /* keep comma */}, "a": 2, "b": 3}'
    rewritten = remove_paths(raw, [("a",), ("b",)])
    assert '{"value": 1, /* keep comma */}' in rewritten


def test_install_uninstall_preserves_strict_claude_settings(tmp_path, monkeypatch):
    from code_review_graph import skills, uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {
        "permissions": {"allow": ["Bash(npm test)"], "deny": ["Bash(git push:*)"]},
        "hooks": {"PreToolUse": [{
            "matcher": "Bash", "hooks": [{"type": "command", "command": "echo MY-OWN-HOOK"}],
        }]},
    }
    settings.write_text(json.dumps(original, indent=2), encoding="utf-8")
    skills.install_hooks(repo)
    json.loads(settings.read_text(encoding="utf-8"))
    report = uninstall.run(repo=repo, keep_data=True)
    assert not report.errors
    assert json.loads(settings.read_text(encoding="utf-8")) == original

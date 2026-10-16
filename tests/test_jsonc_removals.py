"""Strict JSON and comment preservation regressions for issue #1068."""

import itertools
import json
from pathlib import Path

import pytest

from code_review_graph import jsonc
from code_review_graph.jsonc import remove_paths
from code_review_graph.skills import _strip_jsonc


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


# --- Edge cases added while hardening PR #1078 ---------------------------------

def _exposed_trailing_commas(text: str) -> list[int]:
    """Offsets of commas that sit directly before a closing bracket."""
    tokens = jsonc.tokenize(text)
    return [
        token.start
        for token, following in zip(tokens, tokens[1:])
        if token.kind == "," and following.kind in ("}", "]")
    ]


def _subset_cases():
    for size in range(1, 6):
        for removed in itertools.chain.from_iterable(
            itertools.combinations(range(size), count) for count in range(size + 1)
        ):
            yield size, removed


@pytest.mark.parametrize("indent", [None, 2])
def test_every_removal_subset_leaves_strict_json(indent):
    """Any subset of object members or array elements, adjacent or not."""
    for size, removed in _subset_cases():
        obj = {f"k{index}": index for index in range(size)}
        raw = json.dumps({"obj": obj, "tail": 0}, indent=indent)
        rewritten = remove_paths(raw, [("obj", f"k{index}") for index in removed])
        expected = {key: value for key, value in obj.items() if int(key[1:]) not in removed}
        assert json.loads(rewritten) == {"obj": expected, "tail": 0}, (size, removed)

        arr = list(range(size))
        raw = json.dumps({"arr": arr}, indent=indent)
        rewritten = remove_paths(raw, [("arr", index) for index in removed])
        expected_arr = [value for value in arr if value not in removed]
        assert json.loads(rewritten) == {"arr": expected_arr}, (size, removed)


def test_every_removal_subset_keeps_comments_and_exposes_no_comma():
    """Commented JSONC: comments survive and no new trailing comma appears."""
    for size, removed in _subset_cases():
        lines = ["{"]
        for index in range(size):
            comma = "," if index < size - 1 else ""
            lines.append(f'  "k{index}": {index}{comma} // note-{index}')
        lines.append("  /* closing note */")
        lines.append("}")
        raw = "\n".join(lines) + "\n"
        rewritten = remove_paths(raw, [(f"k{index}",) for index in removed])
        for index in range(size):
            assert f"// note-{index}" in rewritten, (size, removed)
        assert "/* closing note */" in rewritten
        assert _exposed_trailing_commas(rewritten) == [], (size, removed)
        assert json.loads(_strip_jsonc(rewritten)) == {
            f"k{index}": index for index in range(size) if index not in removed
        }


def test_no_paths_returns_the_document_unchanged():
    raw = '{\n  "a": [1, 2,],\n  // note\n  "b": {"c": 3,},\n}\n'
    assert remove_paths(raw, []) == raw


def test_surviving_member_keeps_its_own_jsonc_trailing_comma():
    raw = '{\n  "a": 1,\n  "keep": 2,\n}\n'
    rewritten = remove_paths(raw, [("a",)])
    assert '"keep": 2,\n}' in rewritten
    assert json.loads(_strip_jsonc(rewritten)) == {"keep": 2}


def test_removed_tail_with_jsonc_trailing_comma_leaves_strict_json():
    raw = '{"keep": 1, "a": 2, "b": 3,}'
    rewritten = remove_paths(raw, [("a",), ("b",)])
    assert json.loads(rewritten) == {"keep": 1}

    raw = '{"keep": 1, "b": 3,}'
    assert json.loads(remove_paths(raw, [("b",)])) == {"keep": 1}


@pytest.mark.parametrize("paths", [
    [("a",), ("a",), ("b",)],
    [("b",), ("a",), ("b",)],
    [("a",), ("b",), ("a",), ("b",)],
])
def test_duplicate_paths_are_removed_once(paths):
    raw = '{"keep": 1, "a": 2, "b": 3}'
    assert json.loads(remove_paths(raw, paths)) == {"keep": 1}


def test_parent_and_child_paths_overlap_cleanly():
    raw = json.dumps(
        {"keep": 1, "hooks": {"PostToolUse": [1], "SessionStart": [2]}}, indent=2
    )
    rewritten = remove_paths(
        raw, [("hooks", "PostToolUse"), ("hooks",), ("hooks", "SessionStart", 0)]
    )
    assert json.loads(rewritten) == {"keep": 1}


def test_removals_in_several_containers_at_once():
    raw = json.dumps(
        {"x": {"k": 1, "a": 2, "b": 3}, "y": [1, 2, 3], "z": {"n": [4, 5, 6]}, "w": 0},
        indent=2,
    )
    rewritten = remove_paths(raw, [
        ("x", "a"), ("x", "b"), ("y", 1), ("y", 2), ("z", "n", 1), ("z", "n", 2), ("w",),
    ])
    assert json.loads(rewritten) == {"x": {"k": 1}, "y": [1], "z": {"n": [4]}}


def test_nested_hook_entries_removed_at_the_end_of_a_group():
    raw = json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "Edit|Write",
        "hooks": [{"command": "user"}, {"command": "crg-1"}, {"command": "crg-2"}],
    }]}}, indent=2)
    rewritten = remove_paths(raw, [
        ("hooks", "PostToolUse", 0, "hooks", 1),
        ("hooks", "PostToolUse", 0, "hooks", 2),
    ])
    assert json.loads(rewritten) == {"hooks": {"PostToolUse": [{
        "matcher": "Edit|Write", "hooks": [{"command": "user"}],
    }]}}


def test_unicode_escapes_and_windows_paths_before_the_edit_keep_offsets_right():
    original = {
        "ключ": "値,}",
        "emoji": "😀 \",] \\",
        "path": "C:\\Users\\Ünïcode dir\\settings.json,}",
        "a": 1,
        "b": [2],
    }
    for ensure_ascii in (True, False):
        raw = json.dumps(original, indent=2, ensure_ascii=ensure_ascii)
        rewritten = remove_paths(raw, [("a",), ("b",)])
        expected = {key: value for key, value in original.items() if key not in ("a", "b")}
        assert json.loads(rewritten) == expected


def test_crlf_line_endings_survive_and_stay_strict():
    raw = json.dumps({"keep": 1, "a": 2, "b": 3}, indent=2).replace("\n", "\r\n")
    rewritten = remove_paths(raw, [("a",), ("b",)])
    assert json.loads(rewritten) == {"keep": 1}
    assert "\n" not in rewritten.replace("\r\n", "")


def test_large_document_tail_and_interleaved_removals():
    size = 300
    raw = json.dumps({f"k{index}": index for index in range(size)}, indent=2)
    rewritten = remove_paths(raw, [(f"k{index}",) for index in range(size // 2, size)])
    assert json.loads(rewritten) == {f"k{index}": index for index in range(size // 2)}

    raw = json.dumps({"a": list(range(size))})
    rewritten = remove_paths(raw, [("a", index) for index in range(1, size, 2)])
    assert json.loads(rewritten) == {"a": list(range(0, size, 2))}


@pytest.mark.parametrize("raw,paths,error", [
    ('{"a": 1}', [("missing",)], KeyError),
    ('{"a": [1]}', [("a", 3)], IndexError),
    ('{"a": [1]}', [("a", -1)], IndexError),
    ('{"a": 1}', [()], ValueError),
    ("", [("a",)], ValueError),
    ('{"a": 1, "b": 2}', [("b",), ("missing",)], KeyError),
])
def test_unresolvable_paths_still_raise(raw, paths, error):
    with pytest.raises(error):
        remove_paths(raw, paths)


# --- Audit additions for PR #1078 ----------------------------------------------

_FUZZ_SCALARS = (1, 2.5, True, None, "s,}", "x//y /* z */", 'q"],', "ü😀\\")


def _random_value(rng, depth):
    roll = rng.random()
    if depth <= 0 or roll < 0.3:
        return rng.choice(_FUZZ_SCALARS)
    size = rng.randint(0, 4)
    if roll < 0.65:
        return {f"k{index}": _random_value(rng, depth - 1) for index in range(size)}
    return [_random_value(rng, depth - 1) for _ in range(size)]


def _render_jsonc(value, rng, path, trailing_at, *, depth, compact, trailing, comments):
    """Render ``value`` as JSONC and record the containers given a trailing comma."""
    if not isinstance(value, (dict, list)) or not value:
        return json.dumps(value, ensure_ascii=False)
    is_object = isinstance(value, dict)
    items = list(value.items()) if is_object else list(enumerate(value))
    newline = "" if compact else "\n"
    pad = "" if compact else "  " * (depth + 1)
    parts = []
    for position, (key, child) in enumerate(items):
        comma = ","
        if position == len(items) - 1:
            comma = "," if trailing and rng.random() < 0.5 else ""
            if comma:
                trailing_at.add(path)
        note = ""
        if comments and rng.random() < 0.35:
            note = " /* note ] , */" if compact or rng.random() < 0.5 else " // note ,}"
        body = _render_jsonc(
            child, rng, path + (key,), trailing_at,
            depth=depth + 1, compact=compact, trailing=trailing, comments=comments,
        )
        head = f"{json.dumps(key, ensure_ascii=False)}: " if is_object else ""
        parts.append(f"{pad}{head}{body}{comma}{note}")
    opening, closing = ("{", "}") if is_object else ("[", "]")
    close_pad = "" if compact else "  " * depth
    return opening + newline + newline.join(parts) + newline + close_pad + closing


def _all_paths(value, prefix=()):
    children = (
        value.items() if isinstance(value, dict)
        else enumerate(value) if isinstance(value, list) else ()
    )
    for key, child in children:
        yield prefix + (key,)
        yield from _all_paths(child, prefix + (key,))


def _without(value, removed, prefix=()):
    if isinstance(value, dict):
        return {
            key: _without(child, removed, prefix + (key,))
            for key, child in value.items() if prefix + (key,) not in removed
        }
    if isinstance(value, list):
        return [
            _without(child, removed, prefix + (index,))
            for index, child in enumerate(value) if prefix + (index,) not in removed
        ]
    return value


def _walk(value, path):
    for key in path:
        value = value[key]
    return value


def _is_deletion_of(original: str, rewritten: str) -> bool:
    """Whether ``rewritten`` is ``original`` with characters only deleted."""
    remaining = iter(original)
    return all(char in remaining for char in rewritten)


def _comment_free(text: str) -> str:
    """Drop comments but keep every comma, as strip-json-comments would."""
    return " ".join(text[token.start:token.end] for token in jsonc.tokenize(text))


def test_random_nested_jsonc_removals_match_the_parsed_edit():
    """Seeded sweep: nested containers, comments, JSONC trailing commas, overlaps.

    For every case the splice must equal the same edit made on the parsed
    value, may only delete characters, must keep exactly the original trailing
    commas whose container and last element survive, and, for a file that had
    none, must leave strict JSON once its comments are stripped.
    """
    import random

    rng = random.Random(1078)
    for case in range(3000):
        value = {"root": _random_value(rng, 3), "tail": _random_value(rng, 2)}
        trailing = rng.random() < 0.4
        trailing_at: set = set()
        raw = _render_jsonc(
            value, rng, (), trailing_at,
            depth=0, compact=rng.random() < 0.25, trailing=trailing,
            comments=rng.random() < 0.5,
        )
        paths = list(_all_paths(value))
        chosen = rng.sample(paths, rng.randint(0, min(len(paths), 6)))
        if chosen and rng.random() < 0.1:
            chosen.append(rng.choice(chosen))
        removed = set(chosen)
        context = (case, raw, chosen)

        rewritten = remove_paths(raw, chosen)

        assert json.loads(_strip_jsonc(rewritten)) == _without(value, removed), context
        assert _is_deletion_of(raw, rewritten), context
        kept_trailing = 0
        for path in trailing_at:
            container = _walk(value, path)
            last = list(container)[-1] if isinstance(container, dict) else len(container) - 1
            gone = any(path[:size] in removed for size in range(1, len(path) + 1))
            if not gone and path + (last,) not in removed:
                kept_trailing += 1
        assert len(_exposed_trailing_commas(rewritten)) == kept_trailing, context
        if not trailing:
            assert json.loads(_comment_free(rewritten)) == _without(value, removed), context


@pytest.mark.parametrize("raw,paths,expected", [
    ("[1, 2, 3]", [(1,), (2,)], [1]),
    ("[1, 2, 3]", [(0,), (2,)], [2]),
    ('[{"a": 1, "b": 2, "c": 3}, 4, 5]', [(0, "b"), (0, "c"), (1,), (2,)], [{"a": 1}]),
])
def test_top_level_array_removals_leave_strict_json(raw, paths, expected):
    assert json.loads(remove_paths(raw, paths)) == expected


def test_comma_after_a_value_comment_is_dropped_and_the_comment_kept():
    raw = '{\n  "keep": 1 /* why */, // after\n  "a": 2,\n  "b": 3\n}\n'
    rewritten = remove_paths(raw, [("a",), ("b",)])
    assert "/* why */" in rewritten and "// after" in rewritten
    assert _exposed_trailing_commas(rewritten) == []
    assert json.loads(_comment_free(rewritten)) == {"keep": 1}

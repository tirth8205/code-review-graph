"""A Python file goes through the stdlib ``ast`` parser once per parse.

Three passes need the ``ast`` tree as well as the tree-sitter one: function
scopes, calls in dead branches, and imports in dead branches. Each used to
parse the whole file again, about 7% of the time spent parsing a Python
repository. They now share one tree, and the result is the same.
"""

import ast

from code_review_graph.parser import CodeParser

_SOURCE = '''\
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from os import path

def helper(value):
    local = value + 1
    return local

def run():
    if False:
        helper(0)
    return helper(1)
'''


def test_the_whole_file_is_parsed_once(tmp_path, monkeypatch):
    path = tmp_path / "mod.py"
    path.write_text(_SOURCE)
    whole_file = []
    real_parse = ast.parse

    def counting_parse(source, *args, **kwargs):
        if source == _SOURCE:
            whole_file.append(1)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    parser = CodeParser()
    parser.clear_caches()
    nodes, edges = parser.parse_bytes(path, _SOURCE.encode())
    assert len(whole_file) == 1

    # The passes still see what they look for.
    calls = {e.line for e in edges if e.kind == "CALLS" and e.target.endswith("helper")}
    assert calls == {13}  # the call under ``if False`` is dropped

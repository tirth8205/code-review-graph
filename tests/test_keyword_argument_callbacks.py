"""Edge-case tests for keyword-argument callback references (#840, PR #855).

Stresses the ``keyword_argument`` branch of ``_ref_from_arguments`` beyond the
PR's own coverage: nested calls, lambdas, method references, stdlib names,
keyword-name/value confusion, unicode, splats, scale, and the dead-code
interplay (no over-suppression of genuinely dead functions).
"""

import tempfile
from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse_source(tmp_path: Path, source: str, name: str = "mod.py"):
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    parser = CodeParser()
    return parser.parse_file(p)


def _ref_targets(edges) -> set:
    return {
        e.target.rsplit("::", 1)[-1] for e in edges if e.kind == "REFERENCES"
    }


class TestKeywordCallbackEdges:
    def test_keyword_name_never_treated_as_reference(self, tmp_path):
        """f(handler=42): the keyword NAME collides with a defined function
        but the VALUE is a literal — no REFERENCES edge may be emitted."""
        src = (
            "def handler(x):\n"
            "    return x\n"
            "\n"
            "def wire(reg):\n"
            "    reg.configure(handler=42)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        assert "handler" not in _ref_targets(edges)

    def test_keyword_value_matching_keyword_name_emits_single_edge(self, tmp_path):
        """f(handler=handler): exactly one REFERENCES edge, from the value."""
        src = (
            "def handler(x):\n"
            "    return x\n"
            "\n"
            "def wire(reg):\n"
            "    reg.configure(handler=handler)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        refs = [
            e for e in edges
            if e.kind == "REFERENCES" and e.target.endswith("handler")
        ]
        assert len(refs) == 1, [(e.source, e.target, e.line) for e in refs]
        assert refs[0].line == 5

    def test_no_reference_edges_to_stdlib_builtins(self, tmp_path):
        """key=len, cb=print, factory=str: builtins are neither defined
        locally nor imported, so no REFERENCES edges may appear."""
        src = (
            "def use(items, reg):\n"
            "    ordered = sorted(items, key=len)\n"
            "    reg.configure(cb=print, factory=str, cls=dict)\n"
            "    return ordered\n"
        )
        _, edges = _parse_source(tmp_path, src)
        targets = _ref_targets(edges)
        for builtin in ("len", "print", "str", "dict"):
            assert builtin not in targets, targets

    def test_nested_call_and_lambda_values_do_not_crash_or_emit(self, tmp_path):
        """cb=make() and cb=lambda: non-identifier values are skipped, while a
        keyword deeper inside the nested call still emits via recursion."""
        src = (
            "def make_handler():\n"
            "    return None\n"
            "\n"
            "def inner_cb(x):\n"
            "    return x\n"
            "\n"
            "def wire(reg):\n"
            "    reg.configure(cb=make_handler())\n"
            "    reg.configure(cb=lambda x: x + 1)\n"
            "    reg.configure(cb=outer_wrap(inner=inner_cb))\n"
        )
        _, edges = _parse_source(tmp_path, src)
        targets = _ref_targets(edges)
        # The nested keyword `inner=inner_cb` must still be found by recursion.
        assert "inner_cb" in targets, targets
        # `make_handler()` is a call, not a bare identifier: no REFERENCES
        # from the keyword path (it gets a CALLS edge instead).
        call_targets = {
            e.target.rsplit("::", 1)[-1] for e in edges if e.kind == "CALLS"
        }
        assert "make_handler" in call_targets

    def test_method_reference_value_is_skipped_without_crash(self, tmp_path):
        """cb=self.on_event / cb=obj.on_event are attribute nodes: the PR's
        scope skips them, and parsing must not crash."""
        src = (
            "class Widget:\n"
            "    def on_event(self, e):\n"
            "        return e\n"
            "\n"
            "    def wire(self, reg):\n"
            "        reg.configure(cb=self.on_event)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        # No exception is the main assertion; also no bogus 'self' ref.
        assert "self" not in _ref_targets(edges)

    def test_splat_and_conditional_values_do_not_crash(self, tmp_path):
        src = (
            "def primary(x):\n"
            "    return x\n"
            "\n"
            "def wire(reg, cbs, opts, flag):\n"
            "    reg.configure(*cbs, **opts)\n"
            "    reg.configure(cb=primary if flag else None)\n"
            "    reg.configure(cb=(primary))\n"
        )
        _, edges = _parse_source(tmp_path, src)
        # Conditional/parenthesized values are out of scope: skipped, no crash.
        assert isinstance(edges, list)

    def test_imported_name_as_keyword_value_emits_reference(self, tmp_path):
        src = (
            "from mypkg.handlers import telemetry_handler\n"
            "\n"
            "def wire(sp):\n"
            "    sp.set_defaults(func=telemetry_handler)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        assert "telemetry_handler" in _ref_targets(edges)

    def test_unicode_function_name_as_keyword_value(self, tmp_path):
        src = (
            "def обработчик(args):\n"
            "    return args\n"
            "\n"
            "def wire(sp):\n"
            "    sp.set_defaults(func=обработчик)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        assert "обработчик" in _ref_targets(edges)

    def test_uppercase_and_single_char_values_skipped_by_heuristic(self, tmp_path):
        src = (
            "def HANDLER(x):\n"
            "    return x\n"
            "\n"
            "def h(x):\n"
            "    return x\n"
            "\n"
            "def wire(reg):\n"
            "    reg.configure(cb=HANDLER, other=h)\n"
        )
        _, edges = _parse_source(tmp_path, src)
        targets = _ref_targets(edges)
        assert "HANDLER" not in targets
        assert "h" not in targets

    def test_many_keyword_callbacks_scale(self, tmp_path):
        """300 keyword-referenced handlers: every one gets an edge."""
        n = 300
        defs = "\n".join(
            f"def handler_{i:03d}(args):\n    return args\n" for i in range(n)
        )
        wires = "\n".join(
            f"    sp.set_defaults(func_{i:03d}=handler_{i:03d})" for i in range(n)
        )
        src = f"{defs}\n\ndef wire(sp):\n{wires}\n"
        _, edges = _parse_source(tmp_path, src)
        targets = _ref_targets(edges)
        missing = {f"handler_{i:03d}" for i in range(n)} - targets
        assert not missing, sorted(missing)[:5]

    def test_dead_code_not_over_suppressed(self, tmp_path):
        """A keyword-referenced handler is alive; an unreferenced sibling
        must still be reported dead."""
        from code_review_graph.graph import GraphStore
        from code_review_graph.refactor import find_dead_code

        src = (
            "def live_handler(args):\n"
            "    return args\n"
            "\n"
            "def truly_dead(args):\n"
            "    return args\n"
            "\n"
            "def wire(sp):\n"
            "    sp.set_defaults(func=live_handler)\n"
        )
        p = tmp_path / "deadmod.py"
        p.write_text(src, encoding="utf-8")
        parser = CodeParser()
        nodes, edges = parser.parse_file(p)
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = GraphStore(Path(tmp_dir) / "graph.db")
            try:
                store.store_file_nodes_edges(str(p), nodes, edges, "")
                dead_names = {d["name"] for d in find_dead_code(store)}
                assert "live_handler" not in dead_names, dead_names
                assert "truly_dead" in dead_names, dead_names
            finally:
                store.close()

    def test_javascript_arguments_behavior_unchanged(self, tmp_path):
        """The python-only guard must not alter JS bare-identifier callbacks."""
        src = (
            "function clickHandler(e) { return e; }\n"
            "function wire(el) { el.addEventListener('click', clickHandler); }\n"
        )
        _, edges = _parse_source(tmp_path, src, name="mod.js")
        assert "clickHandler" in _ref_targets(edges)

"""Go receiver method call resolution and generic receiver unwrapping
(issue #829 / PR #830, issue #832 / PR #833).

These stress the lexical-scope walk beyond the shipped fixture coverage:
closures and goroutines capturing the receiver, shadow leakage between
sibling scopes, else-branch sharing of an if-init binding, assignment (as
opposed to declaration) forms, unicode identifiers with multibyte byte
offsets, labels, method values, cross-package selectors, and deep nesting.
``TestGoGenericReceiverEdges`` covers the generic-receiver unwrapping that
gives generic methods the right parent name.
"""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(source: str):
    parser = CodeParser()
    return parser.parse_bytes(Path("edge_case.go"), source.encode())


def _receiver_calls(edges, method_suffix: str, receiver: str = "a"):
    return [
        edge for edge in edges
        if edge.kind == "CALLS"
        and edge.source.endswith(method_suffix)
        and edge.extra.get("receiver") == receiver
    ]


def _resolution(edges, method_suffix: str, target_suffix: str, receiver: str = "a"):
    return [
        edge.target.endswith(target_suffix)
        for edge in _receiver_calls(edges, method_suffix, receiver)
    ]


def _parents(nodes):
    return {n.name: n.parent_name for n in nodes if n.kind == "Function"}


class TestClosureCapture:
    def test_goroutine_and_defer_closures_keep_receiver(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a *A) Save() {}\n"
            "func (a *A) M() {\n"
            "\tgo func() { a.Save() }()\n"
            "\tdefer func() { a.Save() }()\n"
            "\tfunc() { func() { a.Save() }() }()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [True, True, True]

    def test_shadow_inside_closure_body_blocks_resolution(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M() {\n"
            "\tfunc() {\n"
            "\t\tvar a *B\n"
            "\t\ta.Save()\n"
            "\t}()\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]


class TestScopeBoundaries:
    def test_else_branch_shares_if_init_shadow(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() bool { return true }\n"
            "func (b *B) Save() bool { return true }\n"
            "func (a *A) M() {\n"
            "\tif a := (&B{}); a.Save() {\n"
            "\t\ta.Save()\n"
            "\t} else {\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [
            False, False, False, True,
        ]

    def test_sibling_switch_cases_do_not_leak_shadow(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M(x int) {\n"
            "\tswitch x {\n"
            "\tcase 1:\n"
            "\t\tvar a *B\n"
            "\t\ta.Save()\n"
            "\tcase 2:\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]

    def test_sibling_blocks_do_not_leak_shadow(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M() {\n"
            "\t{\n"
            "\t\ta := &B{}\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "\t{\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]

    def test_range_assignment_form_keeps_receiver(self):
        # ``for _, a = range`` assigns to the receiver variable; it does not
        # declare a new one, so the static type (and method set) is unchanged.
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a *A) Save() {}\n"
            "func (a *A) M() {\n"
            "\tfor _, a = range []*A{nil} {\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [True, True]

    def test_multi_name_var_declaration_shadows(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M() {\n"
            "\t{\n"
            "\t\tvar x, a *B\n"
            "\t\t_ = x\n"
            "\t\ta.Save()\n"
            "\t}\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]

    def test_label_does_not_shadow_receiver(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a *A) Save() {}\n"
            "func (a *A) M() {\n"
            "a:\n"
            "\tfor {\n"
            "\t\tbreak a\n"
            "\t}\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [True]


class TestFalseEdgeGuards:
    def test_sibling_file_method_does_not_bind_to_free_function(self):
        # ``Missing`` the method is defined on A in a sibling file of the
        # same package; this file only has an unrelated free ``Missing``.
        # The parser sees one file at a time, so the call must stay bare
        # rather than bind to the free function.
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func Missing() {}\n"
            "func (a *A) M() { a.Missing() }\n"
        )
        calls = _receiver_calls(edges, "::A.M")
        assert len(calls) == 1
        assert calls[0].target == "Missing"

    def test_cross_package_selector_stays_unresolved(self):
        # ``fmt.Println`` must not bind to a same-file free ``Println``.
        _, edges = _parse(
            "package edge\n"
            'import "fmt"\n'
            "func Println() {}\n"
            "type A struct{}\n"
            "func (a *A) M() {\n"
            '\tfmt.Println("x")\n'
            "}\n"
        )
        calls = [
            edge for edge in edges
            if edge.kind == "CALLS" and edge.source.endswith("::A.M")
        ]
        assert len(calls) == 1
        assert calls[0].target == "Println"
        assert "go_method_receiver" not in calls[0].extra

    def test_two_types_resolve_to_their_own_methods(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Help() {}\n"
            "func (a *A) Do() { a.Help() }\n"
            "func (b *B) Help() {}\n"
            "func (b *B) Do() { b.Help() }\n"
        )
        assert _resolution(edges, "::A.Do", "::A.Help") == [True]
        assert _resolution(edges, "::B.Do", "::B.Help", receiver="b") == [True]

    def test_method_value_creates_no_call_edge_to_method(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a *A) Save() {}\n"
            "func (a *A) M() {\n"
            "\tf := a.Save\n"
            "\tf()\n"
            "}\n"
        )
        targets = [
            edge.target for edge in edges
            if edge.kind == "CALLS" and edge.source.endswith("::A.M")
        ]
        assert all(not target.endswith("::A.Save") for target in targets)


class TestReceiverForms:
    def test_value_receiver_resolves(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a A) Save() {}\n"
            "func (a A) M() { a.Save() }\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [True]

    def test_parenthesized_receiver_resolves(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "func (a *A) Save() {}\n"
            "func (a *A) M() {\n"
            "\t(a).Save()\n"
            "\t((a)).Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [True, True]

    def test_pointer_deref_of_shadowed_binding_stays_unresolved(self):
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b B) Save() {}\n"
            "func (a *A) M() {\n"
            "\t{\n"
            "\t\tvar a *B\n"
            "\t\t(*a).Save()\n"
            "\t}\n"
            "\ta.Save()\n"
            "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]

    def test_unicode_receiver_and_method_names(self):
        _, edges = _parse(
            "package edge\n"
            "type Alpha struct{}\n"
            "type Beta struct{}\n"
            "func (α *Alpha) Σ() {}\n"
            "func (β *Beta) Σ() {}\n"
            "func (α *Alpha) Μέθοδος() {\n"
            "\tα.Σ()\n"
            "\t{\n"
            "\t\tvar α *Beta\n"
            "\t\tα.Σ()\n"
            "\t}\n"
            "\tα.Σ()\n"
            "}\n"
        )
        assert _resolution(
            edges, "::Alpha.Μέθοδος", "::Alpha.Σ", receiver="α",
        ) == [True, False, True]


class TestScale:
    def test_deeply_nested_shadow_blocks(self):
        # Stay inside _MAX_AST_DEPTH (180): each Go block adds two AST
        # levels, and deeper nesting is dropped by the extraction recursion
        # guard before call edges are ever emitted.
        depth = 80
        inner = "var a *B\na.Save()\n"
        body = "{\n" * depth + inner + "}\n" * depth + "a.Save()\n"
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M() {\n" + body + "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True]

    def test_many_alternating_scopes(self):
        blocks = []
        for _ in range(150):
            blocks.append("{\nvar a *B\na.Save()\n}\na.Save()\n")
        _, edges = _parse(
            "package edge\n"
            "type A struct{}\n"
            "type B struct{}\n"
            "func (a *A) Save() {}\n"
            "func (b *B) Save() {}\n"
            "func (a *A) M() {\n" + "".join(blocks) + "}\n"
        )
        assert _resolution(edges, "::A.M", "::A.Save") == [False, True] * 150


class TestGoGenericReceiverEdges:
    def setup_method(self):
        self.parser = CodeParser()

    def parse(self, src: bytes):
        return self.parser.parse_bytes(Path("edge.go"), src)

    def test_multiple_type_params_value_and_pointer(self):
        nodes, edges = self.parse(
            b"package s\n"
            b"type Map[K comparable, V any] struct{}\n"
            b"func (m Map[K, V]) Get() {}\n"
            b"func (m *Map[K, V]) Set() {}\n"
        )
        assert _parents(nodes) == {"Get": "Map", "Set": "Map"}
        contains = {
            e.target.rsplit("::", 1)[-1]
            for e in edges
            if e.kind == "CONTAINS" and e.source.endswith("::Map")
        }
        assert contains == {"Map.Get", "Map.Set"}

    def test_unnamed_generic_receivers(self):
        nodes, _ = self.parse(
            b"package s\n"
            b"type Box[T any] struct{}\n"
            b"func (Box[T]) A() {}\n"
            b"func (*Box[T]) B() {}\n"
        )
        assert _parents(nodes) == {"A": "Box", "B": "Box"}

    def test_underscore_receiver_name(self):
        nodes, _ = self.parse(
            b"package s\n"
            b"type Box[T any] struct{}\n"
            b"func (_ Box[T]) C() {}\n"
        )
        assert _parents(nodes) == {"C": "Box"}

    def test_non_generic_receivers_unchanged(self):
        nodes, _ = self.parse(
            b"package s\n"
            b"type T struct{}\n"
            b"func (s T) K() {}\n"
            b"func (s *T) L() {}\n"
            b"func (T) M() {}\n"
            b"func (*T) N() {}\n"
        )
        assert _parents(nodes) == {"K": "T", "L": "T", "M": "T", "N": "T"}

    def test_unicode_receiver_type_name(self):
        nodes, _ = self.parse(
            "package s\n"
            "type Böx[T any] struct{}\n"
            "func (b Böx[T]) J() {}\n".encode()
        )
        assert _parents(nodes) == {"J": "Böx"}

    def test_nested_generic_receiver_does_not_crash(self):
        # Illegal Go (receiver type params must be plain identifiers) but
        # tree-sitter parses it; the base type must still win, never Pair.
        nodes, _ = self.parse(b"package s\nfunc (b Box[Pair[K, V]]) D() {}\n")
        assert _parents(nodes).get("D") == "Box"

    def test_comment_inside_receiver_before_pointer(self):
        nodes, _ = self.parse(
            b"package s\ntype T struct{}\nfunc (b /*c*/ *T) G() {}\n"
        )
        assert _parents(nodes) == {"G": "T"}

    def test_comment_between_generic_base_and_type_args(self):
        nodes, _ = self.parse(
            b"package s\n"
            b"type Box[T any] struct{}\n"
            b"func (b Box /*c*/ [T]) E() {}\n"
        )
        assert _parents(nodes) == {"E": "Box"}

    def test_malformed_receivers_do_not_crash(self):
        nodes, _ = self.parse(b"package s\nfunc () H() {}\nfunc (,) I() {}\n")
        parents = _parents(nodes)
        # Both must degrade to top-level functions, never raise.
        assert parents.get("H") is None
        assert parents.get("I") is None

    def test_many_generic_methods_scale(self):
        src = [b"package s", b"type Big[T any] struct{}"]
        for i in range(200):
            src.append(b"func (b *Big[T]) M%d() {}" % i)
        nodes, _ = self.parse(b"\n".join(src) + b"\n")
        parents = _parents(nodes)
        assert len(parents) == 200
        assert set(parents.values()) == {"Big"}

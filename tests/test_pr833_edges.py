"""Edge-case tests for Go generic receiver unwrapping (PR #833 / issue #832)."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parents(nodes):
    return {n.name: n.parent_name for n in nodes if n.kind == "Function"}


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

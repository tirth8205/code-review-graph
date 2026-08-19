"""Edge-case tests for #804 / PR #808: Java method and constructor names come
from the grammar ``name`` field in ``_get_name`` (shared branch with C#).

Stresses shapes beyond the PR's own regression test: generic methods and
constructors, array and qualified-generic return types, annotations, records,
interfaces, enums, unicode identifiers, nested and anonymous classes, and
malformed input. Also guards that the now-shared branch left C# behavior
untouched.
"""

from code_review_graph.parser import CodeParser


def _java_funcs(source: str, tmp_path):
    p = tmp_path / "Edge.java"
    p.write_text(source, encoding="utf-8")
    nodes, _ = CodeParser().parse_file(p)
    return {n.name for n in nodes if n.kind == "Function"}


class TestJavaNameFieldEdges:
    def test_generic_methods_and_constructors(self, tmp_path):
        names = _java_funcs(
            "public class Box<T> {\n"
            "    public <T> Box() { }\n"
            "    public <T extends Comparable<T>> T max(T a, T b) { return a; }\n"
            "    public T[] toArray() { return null; }\n"
            "    public String[] names() { return null; }\n"
            "    public java.util.List<String> qualified() { return null; }\n"
            "}\n",
            tmp_path,
        )
        assert names == {"Box", "max", "toArray", "names", "qualified"}
        # Return types and type parameters must never leak in as names.
        assert names.isdisjoint({"T", "String", "List", "Comparable", "java"})

    def test_annotated_methods_and_constructors(self, tmp_path):
        names = _java_funcs(
            "public class Annotated {\n"
            "    @Override\n"
            '    @SuppressWarnings("unchecked")\n'
            '    public String toString() { return ""; }\n'
            "    @Deprecated public Annotated(@NonNull String s) { }\n"
            "}\n",
            tmp_path,
        )
        assert names == {"toString", "Annotated"}
        assert "Override" not in names
        assert "SuppressWarnings" not in names

    def test_record_members(self, tmp_path):
        names = _java_funcs(
            "public record Point(int x, int y) {\n"
            "    public Point(int x) { this(x, 0); }\n"
            "    public int sum() { return x + y; }\n"
            "    public static Point origin() { return new Point(0, 0); }\n"
            "}\n",
            tmp_path,
        )
        # Explicit constructor and methods inside a record body use the name
        # field like any other method_declaration/constructor_declaration.
        assert {"Point", "sum", "origin"} <= names

    def test_interface_default_static_and_abstract_methods(self, tmp_path):
        names = _java_funcs(
            "public interface Svc {\n"
            "    String name();\n"
            "    default int count() { return 0; }\n"
            "    static Svc create() { return null; }\n"
            "}\n",
            tmp_path,
        )
        assert names == {"name", "count", "create"}
        assert "Svc" not in names

    def test_enum_constructor_and_method(self, tmp_path):
        names = _java_funcs(
            "public enum Color {\n"
            "    RED, GREEN;\n"
            "    private Color() { }\n"
            "    public Color next() { return GREEN; }\n"
            "}\n",
            tmp_path,
        )
        assert names == {"Color", "next"}

    def test_unicode_identifiers(self, tmp_path):
        names = _java_funcs(
            "public class Unicodé {\n"
            "    public Unicodé() { }\n"
            '    public String grüße() { return "hallo"; }\n'
            "}\n",
            tmp_path,
        )
        assert "Unicodé" in names
        assert "grüße" in names
        assert "String" not in names

    def test_nested_and_anonymous_class_methods(self, tmp_path):
        names = _java_funcs(
            "public class Outer {\n"
            "    public class Inner {\n"
            "        public Inner() { }\n"
            "        public Outer outer() { return null; }\n"
            "    }\n"
            "    public Runnable anon() {\n"
            "        return new Runnable() {\n"
            "            public void run() { }\n"
            "        };\n"
            "    }\n"
            "}\n",
            tmp_path,
        )
        assert {"Inner", "outer", "anon", "run"} <= names
        assert "Runnable" not in names

    def test_malformed_source_does_not_crash(self, tmp_path):
        # A method with no name plus an unclosed class body: parsing must not
        # raise, and the well-formed sibling must still be extracted.
        names = _java_funcs(
            "public class Broken {\n"
            '    public String () { return ""; }\n'
            "    public int good() { return 1; }\n",
            tmp_path,
        )
        assert "good" in names


class TestCSharpBranchUntouched:
    """The Java fix merged into the C# name-field branch; C# results from the
    #791 fix must be byte-for-byte identical."""

    def test_csharp_names_unchanged(self, tmp_path):
        p = tmp_path / "Edge.cs"
        p.write_text(
            "public class Suite {\n"
            "    public async Task Should_do_thing() { }\n"
            "    public async Task<int> Returns_generic() { return 1; }\n"
            "    public Suite() { }\n"
            "    public void PlainVoid() { }\n"
            "}\n",
            encoding="utf-8",
        )
        nodes, _ = CodeParser().parse_file(p)
        names = {n.name for n in nodes if n.kind == "Function"}
        assert names == {"Should_do_thing", "Returns_generic", "Suite", "PlainVoid"}
        assert "Task" not in names

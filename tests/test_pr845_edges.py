"""Edge-case tests for dotted-stem relative import resolution (PR #845).

Stresses `CodeParser._resolve_module_to_file` beyond the PR's own tests:
multi-dot stems, dotted directory names, file-vs-directory precedence,
`.`/`..` specifiers, unicode stems, trailing slashes, the `.jsx` -> `.tsx`
NodeNext fallback, exact-extension precedence, unresolvable garbage, and an
end-to-end IMPORTS_FROM edge through `parse_file`.
"""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser


@pytest.fixture()
def parser():
    return CodeParser()


def _touch(root: Path, name: str, text: str = "export const x = 1;\n") -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestDottedStemEdges:
    def test_multi_dot_stem_with_decoys_at_every_truncation(self, tmp_path, parser):
        """`./a.b.c` must hit `a.b.c.ts`, not the `a.b.ts` / `a.ts` decoys."""
        want = _touch(tmp_path, "a.b.c.ts")
        _touch(tmp_path, "a.b.ts")
        _touch(tmp_path, "a.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./a.b.c", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_extension_priority_ts_beats_js_for_dotted_stem(self, tmp_path, parser):
        """When both `x.entity.ts` and `x.entity.js` exist, `.ts` wins (probe order)."""
        want = _touch(tmp_path, "x.entity.ts")
        _touch(tmp_path, "x.entity.js")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./x.entity", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_dotted_stem_file_beats_same_named_directory_index(self, tmp_path, parser):
        """Node semantics: `outlet.entity.ts` file wins over `outlet.entity/index.ts`."""
        want = _touch(tmp_path, "outlet.entity.ts")
        _touch(tmp_path, "outlet.entity/index.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./outlet.entity", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_dotted_directory_still_resolves_to_index(self, tmp_path, parser):
        """A dotted *directory* import (`./styles.module/` layout) falls through
        the append probes to the index-file branch instead of mis-resolving to
        a truncated sibling (`styles.ts`), which the old with_suffix probe hit.
        """
        _touch(tmp_path, "styles.ts", "export const decoy = 1;\n")
        want = _touch(tmp_path, "styles.module/index.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./styles.module", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_unicode_dotted_stem(self, tmp_path, parser):
        want = _touch(tmp_path, "café.entité.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./café.entité", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_many_dots_stem(self, tmp_path, parser):
        want = _touch(tmp_path, "a.b.c.d.e.f.g.spec.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./a.b.c.d.e.f.g.spec", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_vue_dotted_stem(self, tmp_path, parser):
        want = _touch(tmp_path, "modal.confirm.vue", "<template><div/></template>\n")
        importer = tmp_path / "app.vue"
        resolved = parser._resolve_module_to_file(
            "./modal.confirm", str(importer), "vue",
        )
        assert resolved == str(want.resolve())

    def test_unresolvable_dotted_stem_returns_none_not_truncated_decoy(
        self, tmp_path, parser,
    ):
        """`./gone.entity` with only a truncated decoy present must be None,
        never the decoy: a missing edge is recoverable, a wrong edge is not.
        """
        _touch(tmp_path, "gone.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./gone.entity", str(importer), "typescript",
        )
        assert resolved is None


class TestSpecifierShapes:
    def test_parent_directory_import_resolves_index(self, tmp_path, parser):
        """`import x from ".."` — dotty base path must not break the probes."""
        want = _touch(tmp_path, "index.ts")
        importer = tmp_path / "sub" / "main.ts"
        importer.parent.mkdir()
        resolved = parser._resolve_module_to_file(
            "..", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_current_directory_import_resolves_index(self, tmp_path, parser):
        want = _touch(tmp_path, "index.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            ".", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_trailing_slash_directory_import(self, tmp_path, parser):
        want = _touch(tmp_path, "utils/index.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./utils/", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_explicit_ts_extension_exact_match_wins(self, tmp_path, parser):
        """An import that already carries `.ts` and exists must short-circuit
        before any append probe (which would look for `foo.ts.ts`)."""
        want = _touch(tmp_path, "foo.ts")
        _touch(tmp_path, "foo.ts.ts", "export const trap = 1;\n")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./foo.ts", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())


class TestNodeNextFallback:
    def test_jsx_specifier_resolves_tsx_source(self, tmp_path, parser):
        want = _touch(tmp_path, "comp.tsx", "export const C = () => null;\n")
        importer = tmp_path / "main.tsx"
        resolved = parser._resolve_module_to_file(
            "./comp.jsx", str(importer), "tsx",
        )
        assert resolved == str(want.resolve())

    def test_cjs_specifier_resolves_ts_source(self, tmp_path, parser):
        want = _touch(tmp_path, "legacy.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./legacy.cjs", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_existing_js_file_beats_ts_substitution(self, tmp_path, parser):
        """`./foo.js` with a real `foo.js` on disk must return the JS file,
        not substitute `foo.ts` (matches Node runtime behavior)."""
        want = _touch(tmp_path, "foo.js", "module.exports = 1;\n")
        _touch(tmp_path, "foo.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./foo.js", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_dotted_stem_nodenext_specifier_resolves_ts_source(self, tmp_path, parser):
        """Both features at once: a compiled-NestJS NodeNext specifier
        `./user.service.js` must resolve the dotted-stem source
        `user.service.ts` (with_suffix in the fallback replaces only the
        final `.js`, leaving the dotted stem intact)."""
        want = _touch(tmp_path, "user.service.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./user.service.js", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())

    def test_dotted_stem_ending_in_js_segment(self, tmp_path, parser):
        """`./foo.js` where only `foo.js.ts` exists: the append probe runs
        before the NodeNext substitution, so the literal appended file wins.
        Documents probe order so a reorder is a conscious decision."""
        want = _touch(tmp_path, "foo.js.ts")
        importer = tmp_path / "main.ts"
        resolved = parser._resolve_module_to_file(
            "./foo.js", str(importer), "typescript",
        )
        assert resolved == str(want.resolve())


class TestDartEdges:
    def test_dart_multi_dot_stem_with_decoys(self, tmp_path, parser):
        want = _touch(tmp_path, "thing.model.g.dart", "class T {}\n")
        _touch(tmp_path, "thing.model.dart", "class Decoy1 {}\n")
        _touch(tmp_path, "thing.dart", "class Decoy2 {}\n")
        importer = tmp_path / "consumer.dart"
        resolved = parser._resolve_module_to_file(
            "./thing.model.g", str(importer), "dart",
        )
        assert resolved == str(want.resolve())

    def test_dart_exact_extension_still_wins(self, tmp_path, parser):
        want = _touch(tmp_path, "thing.model.dart", "class T {}\n")
        importer = tmp_path / "consumer.dart"
        resolved = parser._resolve_module_to_file(
            "./thing.model.dart", str(importer), "dart",
        )
        assert resolved == str(want.resolve())

    def test_dart_unresolvable_returns_none(self, tmp_path, parser):
        _touch(tmp_path, "thing.dart", "class Decoy {}\n")
        importer = tmp_path / "consumer.dart"
        resolved = parser._resolve_module_to_file(
            "./thing.model", str(importer), "dart",
        )
        assert resolved is None


class TestEndToEnd:
    def test_imports_from_edge_carries_resolved_dotted_target(self, tmp_path, parser):
        """Full parse: the IMPORTS_FROM edge target must be the resolved
        dotted-stem file, not the truncated decoy and not the bare module."""
        _touch(
            tmp_path, "outlet.entity.ts",
            "export class Outlet {}\n",
        )
        _touch(tmp_path, "outlet.ts", "export const decoy = 1;\n")
        svc = _touch(
            tmp_path, "outlet.service.ts",
            'import { Outlet } from "./outlet.entity";\n'
            "export class OutletService { o = new Outlet(); }\n",
        )
        nodes, edges = parser.parse_file(svc)
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert imports, "expected an IMPORTS_FROM edge"
        targets = [e.target for e in imports]
        assert any(t.endswith("outlet.entity.ts") for t in targets), targets
        assert not any(t.endswith("/outlet.ts") for t in targets), targets

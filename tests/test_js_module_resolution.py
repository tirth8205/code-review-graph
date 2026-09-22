"""JS/TS relative module resolution: dotted stems, extension probing, and
NodeNext fallbacks (PR #831, PR #845, PR #866).

Stresses `CodeParser._resolve_module_to_file` beyond the PR's own tests:
multi-dot stems, dotted directory names, file-vs-directory precedence,
`.`/`..` specifiers, unicode stems, trailing slashes, the `.jsx` -> `.tsx`
NodeNext fallback, exact-extension precedence, unresolvable garbage, and an
end-to-end IMPORTS_FROM edge through `parse_file`.

Also holds the `_do_resolve_module` probe-order cases from PR #831 and the
`.js`/`.mjs`/`.cjs` to `.jsx`/`.mts`/`.cts` source mappings from PR #866.
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


def _parse(tmp_path: Path, source: str, suffix: str = ".js", name: str = "app"):
    path = tmp_path / f"{name}{suffix}"
    path.write_text(source, encoding="utf-8")
    return path, CodeParser().parse_file(path)


def _import_targets(edges):
    return [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]


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


def test_multi_dot_stem_resolves_full_filename(tmp_path):
    # More than one dot in the stem: only appending survives all of them.
    target = tmp_path / "a.b.c.ts"
    target.write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "a.b.ts").write_text("export const wrong = 1;\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text("export const wrong = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./a.b.c');\n")

    assert _import_targets(edges) == [target.resolve().as_posix()]


def test_missing_dotted_file_does_not_produce_false_edge_to_decoy(tmp_path):
    # The dotted file does NOT exist; a truncated-name sibling does. The old
    # with_suffix code resolved to the sibling (a wrong edge). The fix must
    # leave the specifier unresolved instead of inventing a false import.
    (tmp_path / "outlet.ts").write_text("export const wrong = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./outlet.entity');\n")

    assert _import_targets(edges) == ["./outlet.entity"]


def test_dotted_file_beats_directory_index_with_same_name(tmp_path):
    # Both `mod.entity.ts` and `mod.entity/index.ts` exist. The appended-
    # extension probe runs before the directory-index probe, so the file wins
    # (matches Node's own file-before-directory resolution order).
    file_target = tmp_path / "mod.entity.ts"
    file_target.write_text("export const x = 1;\n", encoding="utf-8")
    pkg = tmp_path / "mod.entity"
    pkg.mkdir()
    (pkg / "index.ts").write_text("export const wrong = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./mod.entity');\n")

    assert _import_targets(edges) == [file_target.resolve().as_posix()]


def test_dotted_directory_name_still_resolves_via_index(tmp_path):
    # A directory whose own name contains a dot: no `v1.2.ts` file exists,
    # the ESM fallback gate (.js/.jsx/.mjs/.cjs) must not fire for `.2`,
    # and the index probe must still run.
    pkg = tmp_path / "v1.2"
    pkg.mkdir()
    index = pkg / "index.ts"
    index.write_text("export const x = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./v1.2');\n")

    assert _import_targets(edges) == [index.resolve().as_posix()]


def test_extension_priority_ts_wins_over_js_for_dotted_stem(tmp_path):
    # Both `.ts` and `.js` variants of the dotted file exist; the extensions
    # list probes `.ts` first, so it must win deterministically.
    ts_target = tmp_path / "user.service.ts"
    ts_target.write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "user.service.js").write_text("module.exports = {};\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./user.service');\n")

    assert _import_targets(edges) == [ts_target.resolve().as_posix()]


def test_exact_js_file_on_disk_beats_esm_ts_fallback(tmp_path):
    # `./helper.js` where helper.js itself exists AND helper.ts exists:
    # the exact-path probe runs first, so the .js file must win.
    js_target = tmp_path / "helper.js"
    js_target.write_text("module.exports = {};\n", encoding="utf-8")
    (tmp_path / "helper.ts").write_text("export const x = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./helper.js');\n")

    assert _import_targets(edges) == [js_target.resolve().as_posix()]


def test_appended_probe_beats_esm_fallback_for_js_suffixed_specifier(tmp_path):
    # `./helper.js` where helper.js.ts (append probe) and helper.ts (ESM
    # fallback) both exist but helper.js itself does not. The append loop
    # runs before the fallback, so helper.js.ts wins. Guards the documented
    # probe order against accidental reordering.
    appended = tmp_path / "helper.js.ts"
    appended.write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "helper.ts").write_text("export const y = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./helper.js');\n")

    assert _import_targets(edges) == [appended.resolve().as_posix()]


def test_mjs_specifier_falls_back_to_ts_source(tmp_path):
    # The ESM fallback gate includes `.mjs`.
    ts_target = tmp_path / "worker.ts"
    ts_target.write_text("export const x = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./worker.mjs');\n")

    assert _import_targets(edges) == [ts_target.resolve().as_posix()]


def test_parent_relative_dotted_stem_resolves(tmp_path):
    # `../models/user.entity` from a sibling subdirectory.
    models = tmp_path / "models"
    models.mkdir()
    target = models / "user.entity.ts"
    target.write_text("export class User {}\n", encoding="utf-8")
    services = tmp_path / "services"
    services.mkdir()

    _path, (_nodes, edges) = _parse(
        services, "const { User } = require('../models/user.entity');\n"
    )

    assert _import_targets(edges) == [target.resolve().as_posix()]


def test_unicode_dotted_stem_resolves(tmp_path):
    target = tmp_path / "café.entity.ts"
    target.write_text("export const x = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(
        tmp_path, "const m = require('./café.entity');\n"
    )

    assert _import_targets(edges) == [target.resolve().as_posix()]


def test_trailing_dot_specifier_does_not_crash_and_stays_unresolved(tmp_path):
    # Malformed specifier ending in a bare dot: must not raise, must not
    # invent an edge to anything on disk.
    (tmp_path / "weird.ts").write_text("export const x = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(tmp_path, "const m = require('./weird.');\n")

    assert _import_targets(edges) == ["./weird."]


def test_dotted_stem_from_typescript_importer(tmp_path):
    # Same fix exercised through a .ts importer (language "typescript"),
    # not just the .js/CommonJS path.
    target = tmp_path / "outlet.entity.ts"
    target.write_text("export class Outlet {}\n", encoding="utf-8")
    (tmp_path / "outlet.ts").write_text("export const wrong = 1;\n", encoding="utf-8")

    _path, (_nodes, edges) = _parse(
        tmp_path,
        "import { Outlet } from './outlet.entity';\n",
        suffix=".ts",
        name="outlet.service",
    )

    assert target.resolve().as_posix() in _import_targets(edges)


def test_js_specifier_resolves_jsx_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.js"\n', encoding="utf-8")
    (tmp_path / "foo.jsx").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.js", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.jsx").as_posix()


def test_mjs_specifier_resolves_mts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.mjs"\n', encoding="utf-8")
    (tmp_path / "foo.mts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.mjs", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.mts").as_posix()


def test_cjs_specifier_resolves_cts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.cts"
    caller.write_text('import "./foo.cjs"\n', encoding="utf-8")
    (tmp_path / "foo.cts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.cjs", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.cts").as_posix()


@pytest.mark.parametrize(("specifier", "native"), [("mjs", "mts"), ("cjs", "cts")])
def test_native_module_source_precedes_legacy_ts_fallback(tmp_path, specifier, native):
    caller = tmp_path / "app.ts"
    native_source = tmp_path / f"foo.{native}"
    native_source.write_text("export {}\n")
    (tmp_path / "foo.ts").write_text("export {}\n")
    resolved = CodeParser()._resolve_module_to_file(
        f"./foo.{specifier}",
        str(caller),
        "typescript",
    )
    assert resolved == str(native_source.resolve())

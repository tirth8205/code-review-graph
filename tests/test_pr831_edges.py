"""Edge-case tests for the dotted-stem relative-import fix (PR #831).

Stress the JS/TS relative-import resolver in ``_do_resolve_module`` beyond
the PR's own coverage: probe-order precedence, multi-dot stems, unicode,
directory-vs-file collisions, dotted directory names, extension priority,
parent-relative traversal, and the no-false-edge guarantee when the dotted
file is missing.
"""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(tmp_path: Path, source: str, suffix: str = ".js", name: str = "app"):
    path = tmp_path / f"{name}{suffix}"
    path.write_text(source, encoding="utf-8")
    return path, CodeParser().parse_file(path)


def _import_targets(edges):
    return [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]


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

"""Imports whose package name is not the on-disk layout.

``src`` layouts already resolve because ``mypkg.runner`` is a literal suffix of
``src/mypkg/runner.py``. Frameworks that assemble a package at runtime are the
gap: the import carries a prefix that exists in no directory. Odoo is the
clearest case -- ``odoo.addons.foo.bar`` is served from ``addons/<any>/foo/bar.py``
and nothing on disk is named ``odoo`` -- but Django app registries and plugin
namespaces land in the same place.
"""

from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.python_resolver import (
    _MIN_PREFIX_EVIDENCE,
    _candidates_behind_prefix,
    learn_synthetic_prefixes,
)


def _odoo_like_repo(tmp_path):
    """Two addons and their tests, addressed through a synthetic package.

    Two, not one: a prefix has to be attested by more than a single import
    before it is treated as a namespace, and a framework that assembles one
    always loads more than a single module through it.
    """
    module = tmp_path / "addons" / "partner" / "billing" / "wizard" / "invoice.py"
    test_file = tmp_path / "addons" / "partner" / "billing" / "tests" / "test_invoice.py"
    sibling = tmp_path / "addons" / "partner" / "shipping" / "wizard" / "label.py"
    sibling_test = tmp_path / "addons" / "partner" / "shipping" / "tests" / "test_label.py"
    for path in (module, test_file, sibling, sibling_test):
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "__init__.py").write_text("")
    module.write_text(
        "def prepare_totals(amount):\n"
        "    return amount * 2\n"
    )
    test_file.write_text(
        "from odoo.addons.billing.wizard.invoice import prepare_totals\n\n"
        "def test_prepare_totals_doubles():\n"
        "    assert prepare_totals(2) == 4\n"
    )
    sibling.write_text(
        "def build_label(code):\n"
        "    return code.upper()\n"
    )
    sibling_test.write_text(
        "from odoo.addons.shipping.wizard.label import build_label\n\n"
        "def test_build_label_uppercases():\n"
        "    assert build_label('a') == 'A'\n"
    )
    (tmp_path / ".git").mkdir()
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    tracked = [
        "addons/partner/billing/wizard/__init__.py",
        "addons/partner/billing/wizard/invoice.py",
        "addons/partner/billing/tests/__init__.py",
        "addons/partner/billing/tests/test_invoice.py",
        "addons/partner/shipping/wizard/__init__.py",
        "addons/partner/shipping/wizard/label.py",
        "addons/partner/shipping/tests/__init__.py",
        "addons/partner/shipping/tests/test_label.py",
    ]
    return module, test_file, graph_dir, tracked


def test_synthetic_package_import_resolves_to_the_indexed_file(tmp_path):
    module, test_file, graph_dir, tracked = _odoo_like_repo(tmp_path)
    store = GraphStore(graph_dir / "graph.db")
    try:
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=tracked,
        ):
            result = full_build(tmp_path, store)

        assert result["python_resolution"]["imports_resolved"] == 2
        assert {
            row["target_qualified"]
            for row in store._conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchall()
        } == {module.as_posix()}
    finally:
        store.close()


def test_coverage_is_visible_through_a_synthetic_package_import(tmp_path):
    """The user-visible symptom: tests_for reporting covered code as uncovered."""
    module, _test_file, graph_dir, tracked = _odoo_like_repo(tmp_path)
    store = GraphStore(graph_dir / "graph.db")
    try:
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=tracked,
        ):
            full_build(tmp_path, store)
        run_post_processing(store)

        production = f"{module.as_posix()}::prepare_totals"
        covering = store.get_transitive_tests(production, max_depth=0)

        assert {test["name"] for test in covering} == {"test_prepare_totals_doubles"}
    finally:
        store.close()


def _attested(*raw_modules):
    """A prefix only counts once several imports agree on it."""
    return list(raw_modules) * _MIN_PREFIX_EVIDENCE


def test_attested_prefix_is_learned():
    modules = {
        "billing.wizard.invoice": {"/repo/addons/partner/billing/wizard/invoice.py"},
        "shipping.wizard.label": {"/repo/addons/partner/shipping/wizard/label.py"},
    }

    prefixes = learn_synthetic_prefixes(
        modules,
        _attested(
            "odoo.addons.billing.wizard.invoice",
            "odoo.addons.shipping.wizard.label",
        ),
    )

    assert "odoo.addons" in prefixes


def test_import_of_an_unindexed_module_stays_unresolved():
    """The regression that made the first cut of this fix wrong.

    ``web`` is not in the repository -- it ships with the framework. Stripping
    one segment further finds an unrelated ``controllers/utils.py`` that happens
    to be the only one, and a confidently wrong edge is worse than none.
    """
    modules = {
        "billing.wizard.invoice": {"/repo/addons/partner/billing/wizard/invoice.py"},
        "controllers.utils": {"/repo/addons/other/renting/controllers/utils.py"},
    }
    prefixes = learn_synthetic_prefixes(
        modules, _attested("odoo.addons.billing.wizard.invoice"),
    )

    assert prefixes == {"odoo.addons"}
    assert _candidates_behind_prefix(
        modules, "odoo.addons.web.controllers.utils", prefixes,
    ) == set()


def test_ambiguous_module_is_reported_rather_than_picked():
    """Two files behind the same module must stay ambiguous.

    The caller resolves only when exactly one candidate comes back, so handing
    it both is what keeps a wrong edge from being invented.
    """
    both = {
        "/repo/addons/partner/billing/wizard/invoice.py",
        "/repo/addons/other/billing/wizard/invoice.py",
    }
    modules = {"billing.wizard.invoice": both}

    assert _candidates_behind_prefix(
        modules, "odoo.addons.billing.wizard.invoice", {"odoo.addons"},
    ) == both


def test_a_single_coincidence_does_not_establish_a_prefix():
    modules = {"controllers.utils": {"/repo/addons/other/renting/controllers/utils.py"}}

    prefixes = learn_synthetic_prefixes(modules, ["odoo.addons.web.controllers.utils"])

    assert prefixes == set()


def test_unattested_prefix_resolves_nothing():
    modules = {"billing.wizard.invoice": {"/repo/addons/partner/billing/wizard/invoice.py"}}

    assert _candidates_behind_prefix(
        modules, "odoo.addons.billing.wizard.invoice", set(),
    ) == set()


def test_single_segment_remainder_is_not_evidence():
    """``exceptions`` matches half the world; it must never resolve on its own."""
    modules = {"exceptions": {"/repo/addons/partner/billing/exceptions.py"}}

    assert learn_synthetic_prefixes(modules, _attested("odoo.exceptions")) == set()
    assert _candidates_behind_prefix(modules, "odoo.exceptions", {"odoo"}) == set()

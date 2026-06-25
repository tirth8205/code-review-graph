"""Tests for the post-build scoped/static call resolver (``Class::method``).

Languages whose grammars emit a ``::`` scope separator in call expressions
(PHP ``scoped_call_expression``, Rust ``scoped_identifier``) store a CALLS
edge whose target is the intermediate ``Class::method`` form (e.g.
``Mailer::send``).  Nodes are keyed by their dotted qualified name
(``<file>::Class.method``), so those edges dangle and ``callers_of`` /
``get_impact_radius`` silently miss the caller.

The scoped resolver rewrites resolvable ``Class::method`` targets to the
canonical node qualified name as a post-build pass, mirroring the existing
Spring/Temporal/ReScript resolvers.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.scoped_resolver import resolve_scoped_calls
from code_review_graph.tools.query import query_graph


class TestScopedCallResolver:
    def _build(self, tmp_path):
        (tmp_path / ".git").mkdir()
        src = tmp_path / "src"
        src.mkdir()

        # --- PHP: cross-file static call + self:: call ---
        (src / "Mailer.php").write_text(
            "<?php\n"
            "class Mailer {\n"
            "    public static function send($to) { return true; }\n"
            "}\n"
        )
        (src / "SignupController.php").write_text(
            "<?php\n"
            "class SignupController {\n"
            "    public function register($email) {\n"
            "        return Mailer::send($email);\n"
            "    }\n"
            "    public function reuse($email) {\n"
            "        return self::register($email);\n"
            "    }\n"
            "}\n"
        )

        # --- PHP: namespaced (PSR-4) static call ---
        (src / "Sender.php").write_text(
            "<?php\n"
            "namespace App;\n"
            "class Sender {\n"
            "    public static function dispatch($to) { return true; }\n"
            "}\n"
        )
        (src / "NsCaller.php").write_text(
            "<?php\n"
            "class NsCaller {\n"
            "    public function go($email) {\n"
            "        return \\App\\Sender::dispatch($email);\n"
            "    }\n"
            "}\n"
        )

        # --- Rust: genuinely cross-file associated-fn call ---
        (src / "notifier.rs").write_text(
            "struct Notifier;\n"
            "impl Notifier {\n"
            "    fn notify(to: &str) -> bool { true }\n"
            "}\n"
        )
        (src / "account.rs").write_text(
            "struct Account;\n"
            "impl Account {\n"
            "    fn signup(email: &str) -> bool {\n"
            "        Notifier::notify(email)\n"
            "    }\n"
            "    fn resignup(email: &str) -> bool {\n"
            "        Self::signup(email)\n"
            "    }\n"
            "    fn external(email: &str) -> bool {\n"
            "        Missing::gone(email)\n"
            "    }\n"
            "    fn deep(email: &str) -> bool {\n"
            "        a::b::run(email)\n"
            "    }\n"
            "}\n"
        )
        # A struct `b` with method `run` exists elsewhere — the multi-segment
        # path a::b::run must NOT be misattributed to it.
        (src / "widgets.rs").write_text(
            "struct b;\n"
            "impl b {\n"
            "    fn run(x: &str) -> bool { true }\n"
            "}\n"
        )

        graph_dir = tmp_path / ".code-review-graph"
        graph_dir.mkdir()
        store = GraphStore(graph_dir / "graph.db")
        result = full_build(tmp_path, store)
        return store, result

    def _targets_from(self, store, source_suffix):
        rows = store._conn.execute(
            "SELECT target_qualified FROM edges "
            "WHERE kind='CALLS' AND source_qualified LIKE ?",
            (f"%{source_suffix}",),
        ).fetchall()
        return {r["target_qualified"] for r in rows}

    def _qn(self, store, suffix):
        row = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE qualified_name LIKE ?",
            (f"%{suffix}",),
        ).fetchone()
        return row["qualified_name"] if row else None

    # ------------------------------------------------------------------
    # PHP
    # ------------------------------------------------------------------
    def test_php_cross_file_static_call_resolves_to_node(self, tmp_path):
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "SignupController.register")
        assert any(t.endswith("Mailer.php::Mailer.send") for t in targets), (
            f"static call Mailer::send not resolved to node, got {targets}"
        )
        assert "Mailer::send" not in targets

    def test_php_callers_of_finds_static_caller(self, tmp_path):
        store, _ = self._build(tmp_path)
        send_qn = self._qn(store, "Mailer.php::Mailer.send")
        try:
            result = query_graph(
                pattern="callers_of",
                target=send_qn,
                repo_root=str(tmp_path),
                detail_level="standard",
            )
        finally:
            store.close()
        assert result["status"] == "ok", result
        names = {r["name"] for r in result["results"]}
        assert "register" in names, f"register not found as caller: {result}"

    def test_php_self_call_resolved_endstate(self, tmp_path):
        # PHP strips `self`, so `self::register` arrives bare and is already
        # qualified by the same-file resolver before this pass. This is a
        # regression guard on the end state, not on the scoped resolver itself.
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "SignupController.reuse")
        assert any(
            t.endswith("SignupController.php::SignupController.register")
            for t in targets
        ), f"self::register not resolved to enclosing class, got {targets}"

    def test_php_namespaced_static_call_resolves(self, tmp_path):
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "NsCaller.go")
        assert any(t.endswith("Sender.php::Sender.dispatch") for t in targets), (
            f"namespaced App\\Sender::dispatch not resolved, got {targets}"
        )
        assert not any("\\" in t for t in targets)

    # ------------------------------------------------------------------
    # Rust
    # ------------------------------------------------------------------
    def test_rust_cross_file_associated_call_resolves(self, tmp_path):
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "account.rs::Account.signup")
        assert any(t.endswith("notifier.rs::Notifier.notify") for t in targets), (
            f"Notifier::notify not resolved to node, got {targets}"
        )
        assert "Notifier::notify" not in targets

    def test_rust_self_call_resolves_to_enclosing_type(self, tmp_path):
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "account.rs::Account.resignup")
        assert any(t.endswith("account.rs::Account.signup") for t in targets), (
            f"Self::signup not resolved to enclosing type, got {targets}"
        )

    # ------------------------------------------------------------------
    # Safety: never invent edges for unresolvable / multi-segment targets
    # ------------------------------------------------------------------
    def test_external_scoped_target_left_untouched(self, tmp_path):
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "account.rs::Account.external")
        assert targets == {"Missing::gone"}, (
            f"external target should be untouched, got {targets}"
        )

    def test_multisegment_target_not_falsely_resolved(self, tmp_path):
        # a::b::run is a module path, NOT Class::method. It must never be
        # misattributed to the unrelated struct `b` with method `run`.
        store, _ = self._build(tmp_path)
        targets = self._targets_from(store, "account.rs::Account.deep")
        assert not any(t.endswith("widgets.rs::b.run") for t in targets), (
            f"multi-segment path falsely resolved to unrelated node: {targets}"
        )

    # ------------------------------------------------------------------
    # Telemetry + idempotency
    # ------------------------------------------------------------------
    def test_stats_reported_in_build_result(self, tmp_path):
        _, result = self._build(tmp_path)
        stats = result["scoped_resolution"]
        # Mailer::send, App\Sender::dispatch, Notifier::notify, Self::signup.
        # (PHP self::register arrives bare; a::b::run and Missing::gone are
        # intentionally not resolved.)
        assert stats["calls_resolved"] >= 4, stats

    def test_resolver_is_idempotent(self, tmp_path):
        store, _ = self._build(tmp_path)
        second = resolve_scoped_calls(store)
        assert second["calls_resolved"] == 0, second

    def test_resolved_edges_marked_inferred(self, tmp_path):
        store, _ = self._build(tmp_path)
        row = store._conn.execute(
            "SELECT confidence_tier FROM edges "
            "WHERE kind='CALLS' AND target_qualified LIKE '%Mailer.php::Mailer.send'"
        ).fetchone()
        assert row["confidence_tier"] == "INFERRED"

    def test_incremental_reparse_reresolves_scoped_call(self, tmp_path):
        # On incremental re-parse a file's edges are deleted and re-created in
        # the raw `Class::method` form; the resolver must re-run and re-resolve.
        store, _ = self._build(tmp_path)
        caller = tmp_path / "src" / "SignupController.php"
        caller.write_text(caller.read_text().replace(
            "return Mailer::send($email);",
            "$x = 1;\n        return Mailer::send($email);",
        ))
        result = incremental_update(
            tmp_path, store, changed_files=["src/SignupController.php"],
        )
        assert result["scoped_resolution"] is not None
        targets = self._targets_from(store, "SignupController.register")
        assert any(t.endswith("Mailer.php::Mailer.send") for t in targets), (
            f"scoped call not re-resolved after incremental update: {targets}"
        )
        assert "Mailer::send" not in targets


class TestScopedResolverDisambiguation:
    """Seeded tests for same-name class collisions (two ``Box.open`` defs)."""

    def _seed(self, tmp_path, with_import: bool):
        store = GraphStore(tmp_path / "graph.db")
        for box_file in ("boxa.php", "boxb.php"):
            store.upsert_node(NodeInfo(
                kind="Function", name="open", file_path=box_file,
                line_start=1, line_end=1, language="php", parent_name="Box",
            ))
        store.upsert_node(NodeInfo(
            kind="Function", name="use_box", file_path="user.php",
            line_start=1, line_end=1, language="php", parent_name="User",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source="user.php::User.use_box",
            target="Box::open", file_path="user.php", line=1,
        ))
        if with_import:
            store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM", source="user.php",
                target="boxa.php", file_path="user.php", line=1,
            ))
        store.commit()
        return store

    def _call_target(self, store):
        return store._conn.execute(
            "SELECT target_qualified FROM edges "
            "WHERE kind='CALLS' AND source_qualified='user.php::User.use_box'"
        ).fetchone()["target_qualified"]

    def test_collision_disambiguated_by_import(self, tmp_path):
        store = self._seed(tmp_path, with_import=True)
        try:
            stats = resolve_scoped_calls(store)
            assert stats["calls_resolved"] == 1
            assert self._call_target(store) == "boxa.php::Box.open"
        finally:
            store.close()

    def test_collision_without_import_left_unresolved(self, tmp_path):
        store = self._seed(tmp_path, with_import=False)
        try:
            stats = resolve_scoped_calls(store)
            assert stats["calls_resolved"] == 0
            # ambiguous (two Box.open, no import) -> left untouched, no false edge
            assert self._call_target(store) == "Box::open"
        finally:
            store.close()

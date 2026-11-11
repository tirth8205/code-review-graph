"""A scoped call binds only when the caller's file backs the class it names.

``Mailer::send()`` used to bind to the one ``Mailer.send`` in the graph even
when the calling file had no ``use`` for it and sat in another namespace or
module. Being the only match in the graph is no evidence on its own. A same
file, an import, the caller's own PHP namespace, or ``self``/``Self`` is.
See: crg-ekp
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build

MAILER_PHP = (
    "<?php\n"
    "namespace App\\Mail;\n"
    "class Mailer {\n"
    "    public static function send($x) { return $x; }\n"
    "}\n"
)

WIDGET_RS = (
    "pub struct Widget;\n"
    "impl Widget {\n"
    "    pub fn build() -> Widget { Widget }\n"
    "}\n"
)


def _targets(tmp_path: Path, files: dict[str, str]) -> dict[str, str]:
    """Caller file (relative) -> the target of its one call."""
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        full_build(root, store)
        rows = store._conn.execute(
            "SELECT file_path, target_qualified FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    finally:
        store.close()
    return {
        Path(row["file_path"]).relative_to(root).as_posix(): row["target_qualified"]
        for row in rows
    }


def test_php_call_from_another_namespace_without_use_stays_unbound(tmp_path):
    targets = _targets(tmp_path, {
        "src/Mail/Mailer.php": MAILER_PHP,
        "src/Loose.php": (
            "<?php\n"
            "namespace Vendor\\Thing;\n"
            "class Loose {\n"
            "    public function go($x) { return Mailer::send($x); }\n"
            "}\n"
        ),
    })

    assert targets["src/Loose.php"] == "Mailer::send"


def test_php_call_with_use_or_same_namespace_still_binds(tmp_path):
    targets = _targets(tmp_path, {
        "src/Mail/Mailer.php": MAILER_PHP,
        "src/Other.php": (
            "<?php\n"
            "namespace App;\n"
            "use App\\Mail\\Mailer;\n"
            "class Other {\n"
            "    public function go($x) { return Mailer::send($x); }\n"
            "}\n"
        ),
        "src/Mail/Notifier.php": (
            "<?php\n"
            "namespace App\\Mail;\n"
            "class Notifier {\n"
            "    public function notify($x) { return Mailer::send($x); }\n"
            "}\n"
        ),
    })

    assert targets["src/Other.php"].endswith("src/Mail/Mailer.php::Mailer.send")
    assert targets["src/Mail/Notifier.php"].endswith("src/Mail/Mailer.php::Mailer.send")


def test_rust_call_without_use_stays_unbound(tmp_path):
    targets = _targets(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": "pub mod widget;\npub mod loose;\n",
        "src/widget.rs": WIDGET_RS,
        "src/loose.rs": "pub fn make() { Widget::build(); }\n",
    })

    assert targets["src/loose.rs"] == "Widget::build"


def test_rust_call_through_a_crate_import_still_binds(tmp_path):
    targets = _targets(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": "pub mod widget;\n",
        "src/widget.rs": WIDGET_RS,
        "tests/it.rs": (
            "use demo::widget::Widget;\n"
            "#[test]\n"
            "fn builds() { Widget::build(); }\n"
        ),
    })

    assert targets["tests/it.rs"].endswith("src/widget.rs::Widget.build")


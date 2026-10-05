"""An ignored C++ file that never made it into the graph has nothing to migrate."""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    CPP_IDENTITY_VERSION,
    full_build,
    incremental_update,
)


def test_ignoring_a_never_stored_pending_file_clears_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    from code_review_graph.parser import CodeParser

    original = CodeParser.parse_bytes

    def flaky(self, path, source):
        if Path(path).name == "broken.cpp":
            raise RuntimeError("persistent parse failure")
        return original(self, path, source)

    monkeypatch.setattr(CodeParser, "parse_bytes", flaky)

    repo = tmp_path / "repo"
    (repo / "third_party").mkdir(parents=True)
    (repo / "healthy.py").write_text("def healthy():\n    return 1\n")
    (repo / "third_party" / "broken.cpp").write_text("void run(int v) {}\n")

    with GraphStore(tmp_path / "graph.db") as store:
        built = full_build(repo, store)
        assert [e["file"] for e in built["errors"]] == ["third_party/broken.cpp"]

        (repo / ".code-review-graphignore").write_text("third_party/\n")
        reconciled = incremental_update(repo, store, changed_files=[])
        assert reconciled["errors"] == []
        pending = json.loads(store.get_metadata("cpp_identity_pending"))
        assert pending["files"] == []
        assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION

        batch = incremental_update(repo, store, changed_files=[], reconcile_stale=False)
        assert batch["errors"] == []

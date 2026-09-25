"""The Codex fallback only forwards supported, read-only CLI commands."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_fallback_routes_query_and_rejects_mutation(tmp_path):
    wrapper = (
        Path(__file__).parents[1]
        / "code_review_graph/assets/code-review-graph/scripts/crg_readonly.py"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    captured = tmp_path / "argv.json"
    fake_cli = tmp_path / "fake crg"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE_ARGV'], 'w') as out:\n"
        "    json.dump(sys.argv[1:], out)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    env = {**os.environ, "CRG_BIN": str(fake_cli), "CAPTURE_ARGV": str(captured)}

    result = subprocess.run(
        [sys.executable, str(wrapper), "query", "callers_of", "symbol", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()) == [
        "query", "callers_of", "symbol", "--repo", str(repo)
    ]

    captured.unlink()
    result = subprocess.run(
        [sys.executable, str(wrapper), "build", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not captured.exists()

    not_repo = tmp_path / "not-repo"
    not_repo.mkdir()
    result = subprocess.run(
        [sys.executable, str(wrapper), "status", "--repo", str(not_repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 127
    assert "No repository marker" in result.stderr
    assert not captured.exists()

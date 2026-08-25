"""#912 — empty integer environment variables must not kill the process at import."""
import os
import subprocess
import sys
from unittest.mock import patch

from code_review_graph.constants import _int_env


class TestIntEnv:
    def test_unset_returns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_TEST_INT", None)
            assert _int_env("CRG_TEST_INT", 42) == 42

    def test_empty_string_returns_default(self):
        with patch.dict(os.environ, {"CRG_TEST_INT": ""}):
            assert _int_env("CRG_TEST_INT", 42) == 42

    def test_whitespace_returns_default(self):
        with patch.dict(os.environ, {"CRG_TEST_INT": "   "}):
            assert _int_env("CRG_TEST_INT", 42) == 42

    def test_valid_integer(self):
        with patch.dict(os.environ, {"CRG_TEST_INT": "100"}):
            assert _int_env("CRG_TEST_INT", 42) == 100

    def test_invalid_integer_warns_and_returns_default(self, caplog):
        with patch.dict(os.environ, {"CRG_TEST_INT": "not-a-number"}):
            import logging
            with caplog.at_level(logging.WARNING):
                assert _int_env("CRG_TEST_INT", 42) == 42
            assert "CRG_TEST_INT" in caplog.text


class TestImportWithEmptyEnv:
    """The original bug: CRG_GIT_TIMEOUT='' crashed every subcommand at import."""

    def test_empty_git_timeout_does_not_crash_import(self, tmp_path):
        env = os.environ.copy()
        env["CRG_GIT_TIMEOUT"] = ""
        result = subprocess.run(
            [sys.executable, "-c", "import code_review_graph.incremental; print('ok')"],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "ok" in result.stdout

    def test_empty_max_impact_nodes_does_not_crash_import(self, tmp_path):
        env = os.environ.copy()
        env["CRG_MAX_IMPACT_NODES"] = ""
        result = subprocess.run(
            [sys.executable, "-c", "import code_review_graph.constants; print('ok')"],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "ok" in result.stdout

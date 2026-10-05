"""End-to-end coverage for the composite GitHub Action (``action.yml``).

``tests/test_action_render.py`` covers the markdown renderer in isolation.
Nothing covered the Action as a whole: the ordered steps in ``action.yml``,
the two workflows that consume it, the report those steps actually produce
on a real repository with a real branch and a real diff, or the security
properties ``docs/GITHUB_ACTION.md`` claims.

This module drives the Action's own shell, extracted verbatim from
``action.yml`` by a YAML parser, against scratch Git repositories built in
temporary directories. The Actions runner contract is emulated faithfully
enough for the scripts to behave as they do in CI: ``GITHUB_ENV`` is read
back between steps (that is how ``CRG_BASE`` reaches the later steps),
``GITHUB_OUTPUT`` captures ``comment-file``, ``RUNNER_TEMP`` and
``GITHUB_WORKSPACE`` are real directories, and ``if:`` expressions are
evaluated the way Actions evaluates them.

Every external binary the scripts call is resolved through a shim directory
that records each invocation. That log is the canary: a test that asserts
against a report can only pass if the log proves the Action's own steps
produced it, and the log is also the evidence for "no source code leaves the
runner" and "no third-party network call".

Run only this module::

    uv run --python 3.13 python -m pytest tests/test_action_e2e.py -m action_e2e -q

Run everything except it::

    uv run --python 3.13 python -m pytest tests/ -m "not action_e2e" -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from code_review_graph.migrations import LATEST_VERSION
from tests.test_action_render import report_rendering_to_exactly

pytestmark = [
    pytest.mark.action_e2e,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is required"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required"),
]

RUN_COMMAND = (
    "uv run --python 3.13 python -m pytest tests/test_action_e2e.py -m action_e2e -q"
)


@pytest.fixture(scope="module", autouse=True)
def opt_in(request: pytest.FixtureRequest) -> None:
    """Skip unless ``-m action_e2e`` was asked for.

    The marker alone would not keep these out of the ordinary suite: CI's
    coverage job runs ``pytest -m "not browser"``, which still selects them.
    This module builds real graphs over real scratch repositories a dozen
    times, so it is opt-in. Module scope, and requested by every heavy
    fixture below, so nothing expensive is built before the skip.
    """
    markexpr = str(request.config.getoption("-m", default="") or "")
    if "action_e2e" not in markexpr:
        pytest.skip(f"opt-in suite; run: {RUN_COMMAND}")

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_YML = REPO_ROOT / "action.yml"
UNPRIVILEGED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-review.yml"
PRIVILEGED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-review-comment.yml"
ACTION_DOC = REPO_ROOT / "docs" / "GITHUB_ACTION.md"
RENDERER = REPO_ROOT / "scripts" / "render_pr_comment.py"

MARKER = "<!-- code-review-graph-report -->"
HEADING = "## code-review-graph review"

# Step names in action.yml. Keeping them as constants makes a rename of a
# step a loud KeyError here rather than a silently skipped check.
STEP_RESOLVE_BASE = "Resolve diff base"
STEP_BUILD = "Build or update the graph"
STEP_ANALYZE = "Run risk-scored change analysis"
STEP_RENDER = "Render markdown report"
STEP_COMMENT = "Upsert sticky PR comment"
# Unconditional, and separate from the risk gate on purpose: "no analysis
# happened" is not a risk level, so `fail-on-risk: none` cannot switch it off.
STEP_ANALYSIS_RAN = "Fail when the analysis did not run"
STEP_GATE = "Enforce risk gate"

# Caps the privileged workflow enforces on the artifact it downloads.
_WORKFLOW_MAX_REPORT_BYTES = 60_000


# ---------------------------------------------------------------------------
# action.yml / workflow parsing
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a YAML mapping"
    return data


def action_doc() -> dict[str, Any]:
    return _load_yaml(ACTION_YML)


def action_steps() -> dict[str, dict[str, Any]]:
    """Map step name -> step mapping, in ``action.yml`` order."""
    steps = action_doc()["runs"]["steps"]
    named = {}
    for step in steps:
        name = step.get("name")
        assert name, f"every action.yml step needs a name: {step}"
        named[name] = step
    return named


def workflow_jobs(path: Path) -> dict[str, Any]:
    doc = _load_yaml(path)
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path} has no jobs"
    return jobs


def all_run_scripts(path: Path) -> list[tuple[str, str]]:
    """Every ``run:`` script in *path*, as ``(step name, script)`` pairs."""
    found: list[tuple[str, str]] = []

    def walk(node: Any, label: str) -> None:
        if isinstance(node, dict):
            name = node.get("name", label)
            if isinstance(node.get("run"), str):
                found.append((str(name), node["run"]))
            for key, value in node.items():
                if key != "run":
                    walk(value, str(name))
        elif isinstance(node, list):
            for item in node:
                walk(item, label)

    walk(_load_yaml(path), path.name)
    return found


# ---------------------------------------------------------------------------
# Actions runner emulation
# ---------------------------------------------------------------------------

_EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


class Context:
    """The subset of the Actions expression context these steps read."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def resolve(self, template: str) -> str:
        """Substitute ``${{ expr }}`` occurrences using the context values."""

        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in self.values:
                raise KeyError(f"no value supplied for Actions expression {key!r}")
            return self.values[key]

        return _EXPR.sub(sub, template)

    def evaluate_if(self, expression: str) -> bool:
        """Evaluate the small ``if:`` grammar action.yml actually uses.

        Supports ``a == 'x'``, ``a != 'x'`` and ``&&`` of those. Anything
        richer raises, so a future condition cannot be silently treated as
        true by this harness.
        """
        text = expression.strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
        result = True
        for clause in text.split("&&"):
            clause = clause.strip()
            match = re.fullmatch(r"([A-Za-z0-9_.\-]+)\s*(==|!=)\s*'([^']*)'", clause)
            if match is None:
                raise AssertionError(f"unsupported if: clause {clause!r}")
            key, op, literal = match.groups()
            if key not in self.values:
                raise KeyError(f"no value supplied for Actions expression {key!r}")
            actual = self.values[key]
            result = result and ((actual == literal) if op == "==" else (actual != literal))
        return result


@dataclass
class StepResult:
    name: str
    returncode: int
    stdout: str
    stderr: str
    skipped: bool = False


@dataclass
class Runner:
    """A directory layout and environment that stands in for a GitHub runner."""

    workspace: Path
    runner_temp: Path
    home: Path
    crg_home: Path
    shim_dir: Path
    call_log: Path
    github_env: Path
    github_output: Path
    context: Context
    env_overlay: dict[str, str] = field(default_factory=dict)
    steps_run: list[StepResult] = field(default_factory=list)

    # -- environment ------------------------------------------------------
    def base_env(self) -> dict[str, str]:
        env = {
            "PATH": f"{self.shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "CRG_HOME": str(self.crg_home),
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_OUTPUT": str(self.github_output),
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_REPOSITORY": self.context.values.get(
                "github.repository", "example/scratch"
            ),
            "CRG_CALL_LOG": str(self.call_log),
            "CRG_PARSE_EXECUTOR": "thread",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            # Keep Git deterministic and free of the developer's own config.
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "crg-test",
            "GIT_AUTHOR_EMAIL": "crg-test@example.invalid",
            "GIT_COMMITTER_NAME": "crg-test",
            "GIT_COMMITTER_EMAIL": "crg-test@example.invalid",
        }
        env.update(self.env_overlay)
        return env

    # -- step execution ---------------------------------------------------
    def run_step(
        self,
        step: dict[str, Any],
        *,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> StepResult:
        """Execute one ``action.yml`` step's own shell in this runner."""
        name = str(step["name"])
        condition = step.get("if")
        if condition is not None and not self.context.evaluate_if(str(condition)):
            result = StepResult(name, 0, "", "", skipped=True)
            self.steps_run.append(result)
            return result

        script = step["run"]
        # The Action must never interpolate an expression into shell text.
        assert "${{" not in script, f"step {name!r} interpolates into its shell"
        assert step.get("shell") == "bash", f"step {name!r} must declare shell: bash"

        env = self.base_env()
        for key, template in (step.get("env") or {}).items():
            env[str(key)] = self.context.resolve(str(template))
        if extra_env:
            env.update(extra_env)

        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=str(self.workspace),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
        self._absorb_github_env()
        result = StepResult(name, completed.returncode, completed.stdout, completed.stderr)
        self.steps_run.append(result)
        if check and completed.returncode != 0:
            raise AssertionError(
                f"action step {name!r} failed (rc={completed.returncode})\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return result

    def _absorb_github_env(self) -> None:
        """Propagate ``GITHUB_ENV`` writes to later steps, as Actions does."""
        if not self.github_env.exists():
            return
        for line in self.github_env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key:
                self.env_overlay[key] = value

    def outputs(self) -> dict[str, str]:
        if not self.github_output.exists():
            return {}
        out: dict[str, str] = {}
        for line in self.github_output.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key:
                out[key] = value
        return out

    def calls(self) -> list[list[str]]:
        """Every shimmed binary invocation, as argv lists."""
        if not self.call_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.call_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def calls_to(self, binary: str) -> list[list[str]]:
        return [argv for argv in self.calls() if Path(argv[0]).name == binary]


_SHIM = """#!/usr/bin/env bash
# Records the invocation, then delegates to the real binary. Present so the
# test can prove which commands the Action's own steps ran.
"{python}" - "$0" "$@" <<'PY'
import json, os, sys
log = os.environ.get("CRG_CALL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
PY
exec {target} "$@"
"""


def _write_shim(shim_dir: Path, name: str, target: str) -> None:
    path = shim_dir / name
    path.write_text(
        _SHIM.format(python=sys.executable, target=target), encoding="utf-8"
    )
    path.chmod(0o755)


def _make_shims(shim_dir: Path) -> None:
    """Shim every binary the Action's steps invoke.

    ``code-review-graph`` and ``python`` are pointed at this checkout so the
    test can never accidentally exercise a PyPI-installed copy. ``git`` and
    ``gh`` delegate to the real binary when one exists; ``gh`` is
    deliberately left as a recording stub that fails loudly, because a test
    run must never reach the GitHub API.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    quoted = f'"{sys.executable}"'
    _write_shim(shim_dir, "code-review-graph", f"{quoted} -m code_review_graph")
    _write_shim(shim_dir, "python", quoted)
    _write_shim(shim_dir, "python3", quoted)
    git = shutil.which("git") or "/usr/bin/git"
    _write_shim(shim_dir, "git", f'"{git}"')
    gh_stub = shim_dir / "gh"
    gh_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${CRG_CALL_LOG:-}" ]; then\n'
        '  printf \'["gh"\' >> "${CRG_CALL_LOG}"\n'
        '  for arg in "$@"; do printf \', "%s"\' "${arg}" >> "${CRG_CALL_LOG}"; done\n'
        '  printf \']\\n\' >> "${CRG_CALL_LOG}"\n'
        "fi\n"
        'echo "gh must not be reached from a test run" >&2\n'
        "exit 97\n",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)


# ---------------------------------------------------------------------------
# Scratch repositories
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    """Git environment with the developer's own config fully excluded.

    ``GIT_CONFIG_GLOBAL=os.devnull`` and ``GIT_CONFIG_NOSYSTEM`` mean these
    repositories cannot pick up (or write) anything on the real machine.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "crg-test",
            "GIT_AUTHOR_EMAIL": "crg-test@example.invalid",
            "GIT_COMMITTER_NAME": "crg-test",
            "GIT_COMMITTER_EMAIL": "crg-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


BASE_AUTH = '''"""Authentication helpers."""


def validate_token(token):
    if not token:
        return False
    return len(token) > 10


def login(user, password):
    if not validate_token(password):
        return None
    return {"user": user}


def logout(session):
    return None
'''

FEATURE_AUTH = '''"""Authentication helpers."""

SHARED_SECRET_SENTINEL = "kumquat-parrot-lodestone"


def validate_token(token):
    if not token:
        return False
    if token.startswith("bad"):
        return False
    return len(token) > 10


def login(user, password):
    if not validate_token(password):
        return None
    return {"user": user}


def logout(session):
    return None


def refresh_token(token, secret):
    if not validate_token(token):
        return None
    return token + secret + SHARED_SECRET_SENTINEL
'''

BASE_API = """from src.auth import login, logout


def handle_request(path, body):
    if path == "/login":
        return login(body.get("user"), body.get("password"))
    if path == "/logout":
        return logout(body.get("session"))
    return None


def main():
    return handle_request("/login", {})
"""

BASE_TEST = """from src.auth import validate_token


def test_validate_token():
    assert validate_token("x" * 20)
"""

# A literal that only ever exists inside a source file. If it reaches the
# rendered comment, source content left the runner.
SOURCE_SENTINEL = "kumquat-parrot-lodestone"


def _make_upstream(root: Path) -> Path:
    """A bare ``origin`` for *root*, so the Action's base fetch has a remote.

    ``actions/checkout`` always leaves an ``origin`` behind; without one the
    Action's ``git fetch origin`` fails and the whole pipeline silently
    degrades to "no changes", which would make every assertion here vacuous.
    """
    upstream = root.parent / f"{root.name}-upstream.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(upstream)],
        check=True,
        capture_output=True,
        env=_git_env(),
        timeout=120,
    )
    _git(root, "remote", "add", "origin", str(upstream))
    _git(root, "push", "-q", "origin", "main", "feature")
    _git(root, "fetch", "-q", "origin")
    return upstream


def make_scratch_repo(root: Path) -> Path:
    """A two-commit repo: ``main`` with a base tree, ``feature`` with a diff.

    Returns the bare upstream repository that serves as ``origin``.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    _git(root, "init", "-q", "-b", "main", ".")
    (root / "src" / "auth.py").write_text(BASE_AUTH, encoding="utf-8")
    (root / "src" / "api.py").write_text(BASE_API, encoding="utf-8")
    (root / "tests" / "test_auth.py").write_text(BASE_TEST, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "src" / "auth.py").write_text(FEATURE_AUTH, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feature")
    return _make_upstream(root)


def make_runner(
    tmp_path_factory: pytest.TempPathFactory,
    slug: str,
    *,
    base_ref: str = "main",
    event_name: str = "pull_request",
    comment: str = "true",
    fail_on_risk: str = "none",
) -> Runner:
    root = tmp_path_factory.mktemp(slug)
    workspace = root / "workspace"
    runner_temp = root / "runner-temp"
    runner_temp.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    crg_home = root / "crg-home"
    crg_home.mkdir(parents=True, exist_ok=True)
    shim_dir = root / "shims"
    _make_shims(shim_dir)
    return Runner(
        workspace=workspace,
        runner_temp=runner_temp,
        home=home,
        crg_home=crg_home,
        shim_dir=shim_dir,
        call_log=root / "calls.jsonl",
        github_env=runner_temp / "github_env",
        github_output=runner_temp / "github_output",
        context=Context(
            **{
                "github.base_ref": base_ref,
                "github.event_name": event_name,
                "github.repository": "example/scratch",
                "github.event.pull_request.number": "42",
                "inputs.comment": comment,
                "inputs.fail-on-risk": fail_on_risk,
                "inputs.github-token": "ghs_not_a_real_token",
            }
        ),
    )


def run_action(runner: Runner, *, check: bool = True) -> Runner:
    """Run the composite Action's shell steps in ``action.yml`` order."""
    steps = action_steps()
    for name in (STEP_RESOLVE_BASE, STEP_BUILD, STEP_ANALYZE, STEP_RENDER):
        runner.run_step(steps[name], check=check)
    runner.run_step(steps[STEP_COMMENT], check=check)
    runner.run_step(steps[STEP_GATE], check=check)
    return runner


@dataclass
class ActionRun:
    runner: Runner
    report: dict[str, Any] | None
    comment: str
    report_path: Path
    comment_path: Path


def execute(runner: Runner, *, check: bool = True) -> ActionRun:
    run_action(runner, check=check)
    report_path = runner.runner_temp / "crg-report.json"
    comment_path = runner.runner_temp / "crg-comment.md"
    raw = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None
    body = comment_path.read_text(encoding="utf-8") if comment_path.exists() else ""
    return ActionRun(runner, parsed, body, report_path, comment_path)


# ---------------------------------------------------------------------------
# Module-scoped happy-path run
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def happy(opt_in: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ActionRun]:
    runner = make_runner(tmp_path_factory, "crg-action-happy", comment="false")
    make_scratch_repo(runner.workspace)
    yield execute(runner)


@pytest.fixture(scope="module")
def diff_files(happy: ActionRun) -> list[str]:
    out = _git(happy.runner.workspace, "diff", "--name-only", "main", "HEAD")
    return sorted(p for p in out.stdout.splitlines() if p.strip())


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def markdown_tables(body: str) -> list[list[str]]:
    """Group the body's pipe-table lines into tables."""
    tables: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            current.append(line.strip())
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def table_cells(row: str) -> list[str]:
    """Split one GFM table row into cells, honouring ``\\|`` escapes."""
    assert row.startswith("|") and row.endswith("|")
    inner = row[1:-1]
    cells: list[str] = []
    buf: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner):
            buf.append(inner[index : index + 2])
            index += 2
            continue
        if char == "|":
            cells.append("".join(buf).strip())
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    cells.append("".join(buf).strip())
    return cells


def has_control_chars(text: str) -> bool:
    return any(
        unicodedata.category(ch) == "Cc" and ch not in "\n\r\t" for ch in text
    )


# The privileged workflow's validator, lifted from pr-review-comment.yml so
# the test exercises the real consumer rather than a paraphrase of it.
def _validator_source() -> str:
    for _name, script in all_run_scripts(PRIVILEGED_WORKFLOW):
        if "Report has an unexpected heading" not in script:
            continue
        marker = "python - <<'PY'\n"
        start = script.index(marker) + len(marker)
        body = script[start:]
        lines: list[str] = []
        for line in body.splitlines():
            if line.strip() == "PY":
                break
            lines.append(line)
        indent = min(
            (len(line) - len(line.lstrip(" ")) for line in lines if line.strip()),
            default=0,
        )
        return "\n".join(line[indent:] if line.strip() else "" for line in lines)
    raise AssertionError("could not locate the validator in the privileged workflow")


def run_trusted_validator(tmp_path: Path, comment_body: str, pr_number: str = "42") -> str:
    """Run the privileged workflow's validator; return the wrapped body.

    Raises ``AssertionError`` carrying the validator's own message when the
    workflow would reject the artifact (and therefore turn the run red).
    """
    download = tmp_path / "crg-report-download"
    download.mkdir(parents=True, exist_ok=True)
    (download / "crg-comment.md").write_text(comment_body, encoding="utf-8")
    (download / "pr-number.txt").write_text(pr_number + "\n", encoding="utf-8")
    out_body = tmp_path / "crg-comment-body.md"
    script = tmp_path / "validator.py"
    script.write_text(_validator_source(), encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "DOWNLOAD_DIR": str(download),
            "COMMENT_BODY": str(out_body),
            "VALIDATED_PR_NUMBER": str(tmp_path / "crg-pr-number.txt"),
            "MAX_BODY_BYTES": "65000",
            "MAX_PR_NUMBER_BYTES": "12",
            "MAX_REPORT_BYTES": str(_WORKFLOW_MAX_REPORT_BYTES),
            "TRUSTED_MARKER": MARKER,
        }
    )
    completed = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "privileged workflow rejected the report: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return out_body.read_text(encoding="utf-8")


def render_report(tmp_path: Path, report: Any, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the renderer exactly as the Action's render step does."""
    source = tmp_path / "report.json"
    if isinstance(report, str):
        source.write_text(report, encoding="utf-8")
    else:
        source.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out.md"),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


# ===========================================================================
# 0. Harness canaries: prove the checks really ran and really compared
# ===========================================================================


def test_canary_action_steps_are_the_real_ones():
    """The harness drives action.yml's own steps, in action.yml's own order."""
    names = list(action_steps())
    assert names == [
        "Set up Python",
        "Install code-review-graph",
        "Cache knowledge graph",
        STEP_RESOLVE_BASE,
        STEP_BUILD,
        STEP_ANALYZE,
        STEP_RENDER,
        STEP_COMMENT,
        STEP_ANALYSIS_RAN,
        STEP_GATE,
    ], f"action.yml step list changed: {names}"
    build = action_steps()[STEP_BUILD]["run"]
    assert "code-review-graph update --base" in build
    assert "code-review-graph build" in build


def test_canary_every_step_actually_executed(happy: ActionRun):
    """The run log proves the Action's shell invoked the real binaries."""
    executed = [s.name for s in happy.runner.steps_run if not s.skipped]
    assert STEP_RESOLVE_BASE in executed
    assert STEP_BUILD in executed
    assert STEP_ANALYZE in executed
    assert STEP_RENDER in executed

    crg_calls = happy.runner.calls_to("code-review-graph")
    subcommands = [argv[1] for argv in crg_calls if len(argv) > 1]
    assert "build" in subcommands, f"no build was run: {crg_calls}"
    assert "detect-changes" in subcommands, f"no analysis was run: {crg_calls}"

    detect = next(argv for argv in crg_calls if len(argv) > 1 and argv[1] == "detect-changes")
    assert "--base" in detect, detect
    assert detect[detect.index("--base") + 1] == "origin/main"

    python_calls = happy.runner.calls_to("python")
    assert any(
        "render_pr_comment.py" in " ".join(argv) for argv in python_calls
    ), f"the renderer was never invoked: {python_calls}"


def test_canary_report_is_compared_against_the_real_diff(happy: ActionRun, diff_files):
    """A real report, a real diff, and a symbol that must not appear."""
    assert happy.report is not None, "the Action produced no JSON report"
    assert diff_files == ["src/auth.py"], diff_files
    names = {f.get("name") for f in happy.report["changed_functions"]}
    assert "refresh_token" in names, names
    # Proof the comparison has teeth: a symbol the diff never touched.
    assert "handle_request" not in names, names
    assert "no_such_symbol_anywhere" not in names


def test_canary_a_wrong_expectation_fails(happy: ActionRun):
    """The assertion style used throughout this module can actually fail."""
    with pytest.raises(AssertionError):
        assert "definitely_not_a_changed_symbol" in happy.comment


# ===========================================================================
# 1. The report the Action's own steps produce is correct
# ===========================================================================


def test_resolve_diff_base_sets_crg_base_from_base_ref(happy: ActionRun):
    assert happy.runner.env_overlay.get("CRG_BASE") == "origin/main"


def test_resolve_diff_base_falls_back_when_not_a_pull_request(
    tmp_path_factory: pytest.TempPathFactory,
):
    runner = make_runner(
        tmp_path_factory, "crg-action-push", base_ref="", event_name="push"
    )
    make_scratch_repo(runner.workspace)
    runner.run_step(action_steps()[STEP_RESOLVE_BASE])
    assert runner.env_overlay.get("CRG_BASE") == "HEAD~1"


def test_changed_files_are_exactly_the_diff(happy: ActionRun, diff_files):
    report = happy.report
    assert report is not None
    reported = set()
    for entry in report["changed_functions"] + report["review_priorities"]:
        path = str(entry["file_path"])
        reported.add(str(Path(path).relative_to(happy.runner.workspace)))
    assert reported == set(diff_files), (reported, diff_files)


def test_risk_scores_are_present_and_in_range(happy: ActionRun):
    report = happy.report
    assert report is not None
    assert 0.0 <= float(report["risk_score"]) <= 1.0
    assert report["changed_functions"], "no changed functions were scored"
    for entry in report["changed_functions"]:
        score = entry.get("risk_score")
        assert score is not None, entry
        assert 0.0 <= float(score) <= 1.0, entry
    assert float(report["risk_score"]) == max(
        float(e["risk_score"]) for e in report["changed_functions"]
    )


def test_test_gaps_are_right(happy: ActionRun):
    """The untested new function is a gap; the tested one is not."""
    report = happy.report
    assert report is not None
    gap_names = {g.get("name") for g in report["test_gaps"]}
    assert "refresh_token" in gap_names, gap_names
    assert "validate_token" not in gap_names, gap_names


def test_risk_table_rows_match_the_report(happy: ActionRun):
    tables = markdown_tables(happy.comment)
    assert tables, happy.comment
    header, divider, *rows = tables[0]
    assert table_cells(header) == [
        "Risk",
        "Level",
        "Symbol",
        "Location",
        "Tested",
    ]
    assert set("".join(table_cells(divider))) <= set(":- ")
    report = happy.report
    assert report is not None
    priorities = report["review_priorities"] or report["changed_functions"]
    assert len(rows) == min(len(priorities), 10)
    for row, entry in zip(rows, priorities):
        cells = table_cells(row)
        assert cells[0] == f"{float(entry['risk_score']):.2f}"
        assert entry["name"] in cells[2].replace("\\", "")
        assert str(entry["line_start"]) in cells[3]


def test_tested_column_agrees_with_test_gaps(happy: ActionRun):
    report = happy.report
    assert report is not None
    gap_names = {
        str(g.get("qualified_name") or g.get("name")) for g in report["test_gaps"]
    }
    rows = markdown_tables(happy.comment)[0][2:]
    for row, entry in zip(rows, report["review_priorities"]):
        tested = table_cells(row)[4]
        expected = "no" if str(entry["qualified_name"]) in gap_names else "yes"
        if entry.get("is_test"):
            expected = "(test)"
        assert tested == expected, (row, entry["name"])


def test_flows_section_matches_the_report(happy: ActionRun):
    report = happy.report
    assert report is not None
    flows = report["affected_flows"]
    if not flows:
        pytest.skip("no affected flows in this report")
    assert "### Affected execution flows" in happy.comment
    for flow in flows[:5]:
        assert str(flow["name"]).replace("_", "\\_") in happy.comment


def test_runner_absolute_paths_do_not_leak_into_the_comment(happy: ActionRun):
    workspace = str(happy.runner.workspace)
    assert workspace not in happy.comment
    assert "/home/runner/work" not in happy.comment
    assert "src/auth.py" in happy.comment


# ---------------------------------------------------------------------------
# Token-savings honesty
# ---------------------------------------------------------------------------


def _savings_line(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("**Token savings:**"):
            return line
    return None


def test_token_savings_panel_is_internally_honest(happy: ActionRun, diff_files):
    """The claim must be reproducible from the same inputs, and conservative."""
    from code_review_graph.context_savings import estimate_file_tokens, estimate_tokens

    report = happy.report
    assert report is not None
    savings = report["context_savings"]
    assert savings["estimated"] is True, "the estimate must be labelled an estimate"

    baseline = estimate_file_tokens(happy.runner.workspace, diff_files)
    assert baseline > 0
    saved = int(savings["saved_tokens"])
    percent = int(savings["saved_percent"])
    assert 0 <= saved <= baseline, (saved, baseline)
    assert 0 <= percent <= 100, percent
    assert percent == round(saved / baseline * 100)

    # The claim is "fewer tokens than reading every changed file in full", so
    # it may never exceed what reading those files would have cost.
    returned = estimate_tokens({k: v for k, v in report.items() if k != "context_savings"})
    assert saved <= max(0, baseline - returned) + 1, (saved, baseline, returned)


def test_token_savings_line_matches_the_report_numbers(happy: ActionRun):
    report = happy.report
    assert report is not None
    saved = int(report["context_savings"]["saved_tokens"])
    percent = int(report["context_savings"]["saved_percent"])
    line = _savings_line(happy.comment)
    if not saved:
        assert line is None, "a zero saving must not be advertised"
        return
    assert line is not None
    assert f"~{saved:,} " in line, line
    assert f"(~{percent}%)" in line, line
    assert "estimated" in line, "the panel must say the figure is an estimate"


def test_token_savings_panel_never_claims_a_saving_it_did_not_make(tmp_path: Path):
    """A report whose own JSON is larger than the files must claim nothing."""
    report = {
        "risk_score": 0.1,
        "changed_functions": [],
        "review_priorities": [],
        "test_gaps": [],
        "affected_flows": [],
        "context_savings": {"estimated": True, "saved_tokens": 0, "saved_percent": 0},
    }
    assert render_report(tmp_path, report).returncode == 0
    body = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert _savings_line(body) is None


# ---------------------------------------------------------------------------
# No source code leaves the runner
# ---------------------------------------------------------------------------


def test_no_source_code_leaves_the_runner(happy: ActionRun):
    """The comment carries names, paths and numbers; never file contents."""
    source = (happy.runner.workspace / "src" / "auth.py").read_text(encoding="utf-8")
    assert SOURCE_SENTINEL in source, "the fixture lost its sentinel"
    assert SOURCE_SENTINEL not in happy.comment
    assert SOURCE_SENTINEL not in happy.report_path.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if len(stripped) > 12 and not stripped.startswith(("def ", '"""')):
            assert stripped not in happy.comment, stripped


def test_action_makes_no_third_party_network_calls(happy: ActionRun):
    """Only git (origin) and the local CLI ran; nothing else was invoked."""
    invoked = {Path(argv[0]).name for argv in happy.runner.calls()}
    assert invoked <= {"git", "code-review-graph", "python", "python3", "gh"}, invoked
    for name, script in all_run_scripts(ACTION_YML):
        for banned in ("curl", "wget", "nc ", "openssl s_client"):
            assert banned not in script, f"{name} shells out to {banned!r}"


# ===========================================================================
# 2. The rendered comment as a consumer sees it
# ===========================================================================


def test_sticky_marker_present_exactly_once(happy: ActionRun):
    assert happy.comment.count(MARKER) == 1
    assert happy.comment.startswith(MARKER + "\n")


def test_comment_is_well_formed_markdown(happy: ActionRun):
    body = happy.comment
    assert body.splitlines()[2] == HEADING
    assert body.rstrip().endswith("*")
    assert "*Powered by [code-review-graph]" in body
    # Headings are ATX with a space, and no heading is left empty.
    for line in body.splitlines():
        if line.startswith("#"):
            assert re.fullmatch(r"#{1,6} \S.*", line), repr(line)
    # Fenced code blocks must balance (there should be none at all).
    assert body.count("```") % 2 == 0


def test_tables_are_well_formed(happy: ActionRun):
    for table in markdown_tables(happy.comment):
        assert len(table) >= 2, table
        width = len(table_cells(table[0]))
        assert width >= 2
        divider = table_cells(table[1])
        assert len(divider) == width, table[1]
        for cell in divider:
            assert re.fullmatch(r":?-{3,}:?", cell), cell
        for row in table[2:]:
            assert len(table_cells(row)) == width, (
                f"row has {len(table_cells(row))} cells, header has {width}: {row}"
            )


def test_no_unescaped_pipe_breaks_a_row(happy: ActionRun):
    for table in markdown_tables(happy.comment):
        for row in table:
            inner = row[1:-1]
            unescaped = re.sub(r"\\.", "", inner).count("|")
            assert unescaped == len(table_cells(row)) - 1, row


def test_comment_has_no_control_characters(happy: ActionRun):
    assert not has_control_chars(happy.comment)
    assert "\x7f" not in happy.comment
    assert "\x1b" not in happy.comment


def test_links_resolve(happy: ActionRun):
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", happy.comment)
    assert links, "the footer link is missing"
    for url in links:
        assert url.startswith("https://github.com/"), url
        assert " " not in url


def test_trusted_workflow_accepts_the_real_rendered_comment(
    happy: ActionRun, tmp_path: Path
):
    """The privileged consumer must accept what the Action actually renders."""
    wrapped = run_trusted_validator(tmp_path, happy.comment)
    assert wrapped.count(MARKER) == 1
    assert wrapped.startswith(MARKER)
    assert HEADING in wrapped
    assert "@" not in wrapped.replace("&#64;", ""), "mentions must be neutralised"


# ---------------------------------------------------------------------------
# Hostile content
# ---------------------------------------------------------------------------

HOSTILE_SYMBOL = (
    "evil|name`rm -rf /`[click](https://attacker.invalid)"
    "<img src=x onerror=alert(1)>*bold*_em_"
)
HOSTILE_REPORT = {
    "risk_score": 0.91,
    "changed_functions": [
        {
            "name": HOSTILE_SYMBOL,
            "qualified_name": f"src/x|y.py::{HOSTILE_SYMBOL}",
            "file_path": "src/x|y`z.py",
            "line_start": 7,
            "risk_score": 0.91,
            "is_test": False,
        }
    ],
    "review_priorities": [
        {
            "name": HOSTILE_SYMBOL,
            "qualified_name": f"src/x|y.py::{HOSTILE_SYMBOL}",
            "file_path": "src/x|y`z.py",
            "line_start": 7,
            "risk_score": 0.91,
            "is_test": False,
        }
    ],
    "test_gaps": [
        {
            "name": HOSTILE_SYMBOL,
            "qualified_name": f"src/x|y.py::{HOSTILE_SYMBOL}",
            "file_path": "src/x|y`z.py",
            "line_start": 7,
        }
    ],
    "affected_flows": [
        {
            "name": f"{HOSTILE_SYMBOL}\n{MARKER}\x1b[31m",
            "criticality": 0.5,
            "node_count": 2,
            "file_count": 1,
        }
    ],
    "context_savings": {"estimated": True, "saved_tokens": 100, "saved_percent": 40},
}


@pytest.fixture(scope="module")
def hostile_comment(opt_in: None, tmp_path_factory: pytest.TempPathFactory) -> str:
    tmp = tmp_path_factory.mktemp("crg-hostile")
    result = render_report(tmp, HOSTILE_REPORT)
    assert result.returncode == 0, result.stderr
    return (tmp / "out.md").read_text(encoding="utf-8")


def test_hostile_symbol_keeps_the_table_well_formed(hostile_comment: str):
    for table in markdown_tables(hostile_comment):
        width = len(table_cells(table[0]))
        for row in table:
            assert len(table_cells(row)) == width, row


def test_hostile_symbol_is_neutralised(hostile_comment: str):
    # No raw HTML tag survives: every "<" from report content is backslashed.
    # The Action's own sticky marker is the single legitimate HTML comment.
    without_marker = hostile_comment.replace(MARKER, "")
    raw_tag = re.search(r"(?<!\\)<[A-Za-z/!]", without_marker)
    assert raw_tag is None, without_marker[
        max(0, raw_tag.start() - 40) : raw_tag.end() + 40
    ]
    assert "\\<img src=x onerror=alert(1)\\>" in hostile_comment, "content is kept"
    # No markdown link survives either: the brackets are backslashed, so the
    # payload renders as inert text with the URL still legible. Asserting the
    # whole escaped span pins both halves of that at once, the escaping and
    # the surviving text, and the unescaped span is asserted absent so the
    # pair cannot both pass on a renderer that emits the link twice. Neither
    # literal is a bare URL on its own, which is CodeQL's
    # py/incomplete-url-substring-sanitization shape;
    # tests/test_codeql_url_substring.py keeps that shape out of this
    # repository.
    assert "[click](https://attacker.invalid)" not in hostile_comment
    assert "\\[click\\](https://attacker.invalid)" in hostile_comment, "kept, inert"
    links = re.findall(r"(?<!\\)\[[^\]\\]+\]\(([^)]+)\)", hostile_comment)
    assert links == ["https://github.com/tirth8205/code-review-graph"], links
    # Backticks are escaped, so the injected command never becomes code.
    assert "`rm -rf /`" not in hostile_comment
    assert "\\`rm -rf /\\`" in hostile_comment


def test_hostile_content_cannot_forge_a_second_marker(hostile_comment: str):
    assert hostile_comment.count(MARKER) == 1
    assert hostile_comment.startswith(MARKER + "\n")


def test_hostile_content_has_no_control_characters(hostile_comment: str):
    assert not has_control_chars(hostile_comment)
    assert "\x1b" not in hostile_comment


def test_hostile_comment_survives_the_trusted_validator(
    hostile_comment: str, tmp_path: Path
):
    wrapped = run_trusted_validator(tmp_path, hostile_comment)
    assert wrapped.count(MARKER) == 1


def test_hostile_comment_stays_readable(hostile_comment: str):
    """Escaping must not destroy the information a reviewer needs."""
    assert "evil" in hostile_comment
    assert "0.91" in hostile_comment
    assert "critical" in hostile_comment
    assert "src/x" in hostile_comment
    assert ":7" in hostile_comment


# ===========================================================================
# 3. Inputs that change behaviour
# ===========================================================================


@pytest.mark.parametrize(
    ("risk", "level", "expected"),
    [
        (0.10, "none", 0),
        (0.10, "high", 0),
        (0.10, "critical", 0),
        (0.69, "high", 0),
        (0.70, "high", 3),
        (0.84, "high", 3),
        (0.84, "critical", 0),
        (0.85, "critical", 3),
        (0.99, "critical", 3),
        (0.99, "none", 0),
    ],
)
def test_fail_on_risk_exit_codes(tmp_path: Path, risk: float, level: str, expected: int):
    """Each level sets the documented exit code for a given report."""
    report = {
        "risk_score": risk,
        "changed_functions": [],
        "review_priorities": [],
        "test_gaps": [],
        "affected_flows": [],
    }
    args = ["--quiet"] if level == "none" else ["--fail-on-risk", level, "--quiet"]
    assert render_report(tmp_path, report, *args).returncode == expected


def test_risk_gate_step_runs_the_real_action_shell(
    tmp_path_factory: pytest.TempPathFactory,
):
    """``fail-on-risk: high`` fails the Action's own gate step on a high report."""
    runner = make_runner(
        tmp_path_factory, "crg-action-gate", comment="false", fail_on_risk="high"
    )
    make_scratch_repo(runner.workspace)
    run = execute(runner, check=False)
    assert run.report is not None
    gate = next(s for s in runner.steps_run if s.name == STEP_GATE)
    assert not gate.skipped, "the gate must run when fail-on-risk is not none"
    breached = float(run.report["risk_score"]) >= 0.70
    assert (gate.returncode == 3) is breached, (gate.returncode, run.report["risk_score"])
    if breached:
        assert "Risk gate breached" in gate.stderr


def test_risk_gate_step_is_skipped_when_fail_on_risk_is_none(happy: ActionRun):
    gate = next(s for s in happy.runner.steps_run if s.name == STEP_GATE)
    assert gate.skipped


def test_comment_false_produces_the_artifact_without_posting(happy: ActionRun):
    """``comment: false`` renders the file, runs no gh call, posts nothing."""
    comment_step = next(s for s in happy.runner.steps_run if s.name == STEP_COMMENT)
    assert comment_step.skipped, "comment: false must not reach the GitHub API"
    assert happy.runner.calls_to("gh") == []
    outputs = happy.runner.outputs()
    assert "comment-file" in outputs
    produced = Path(outputs["comment-file"])
    assert produced.is_file() and produced.stat().st_size > 0
    assert produced.read_text(encoding="utf-8") == happy.comment


def test_comment_true_on_a_non_pull_request_event_does_not_post(
    tmp_path_factory: pytest.TempPathFactory,
):
    runner = make_runner(
        tmp_path_factory, "crg-action-push-nocomment", comment="true", event_name="push"
    )
    steps = action_steps()
    assert runner.context.evaluate_if(str(steps[STEP_COMMENT]["if"])) is False


def test_comment_true_on_a_pull_request_would_post(
    tmp_path_factory: pytest.TempPathFactory,
):
    """Guard the negative above: the condition is true in the real case."""
    runner = make_runner(tmp_path_factory, "crg-action-post", comment="true")
    steps = action_steps()
    assert runner.context.evaluate_if(str(steps[STEP_COMMENT]["if"])) is True


def test_action_output_points_at_the_rendered_file(happy: ActionRun):
    doc = action_doc()
    value = doc["outputs"]["comment-file"]["value"]
    assert value.strip() == "${{ steps.render.outputs.comment-file }}"
    assert action_steps()[STEP_RENDER].get("id") == "render"


# ---------------------------------------------------------------------------
# Cache key / schema version
# ---------------------------------------------------------------------------


def _cache_step() -> dict[str, Any]:
    return action_steps()["Cache knowledge graph"]


def cache_schema_segments() -> list[str]:
    """Every ``schema<N>`` token in the cache step's key and restore keys."""
    step = _cache_step()
    text = str(step["with"]["key"]) + "\n" + str(step["with"]["restore-keys"])
    return re.findall(r"schema(\d+)", text)


def test_cache_key_tracks_the_schema_version():
    """The cache key and the database schema version cannot drift apart."""
    segments = cache_schema_segments()
    assert segments, "the cache key lost its schema segment"
    assert set(segments) == {str(LATEST_VERSION)}, (
        f"action.yml caches schema{set(segments)} but migrations.LATEST_VERSION "
        f"is {LATEST_VERSION}; bump the cache key or the caches are stale"
    )


def test_cache_key_and_restore_keys_agree():
    step = _cache_step()
    key = str(step["with"]["key"])
    restore = str(step["with"]["restore-keys"]).strip()
    prefix = f"code-review-graph-schema{LATEST_VERSION}-"
    assert key.startswith(prefix), key
    for line in restore.splitlines():
        assert line.strip().startswith(prefix), line


def test_cache_drift_canary_detects_a_moved_schema_version():
    """Prove the drift check has teeth: a bumped version must fail it."""
    segments = cache_schema_segments()
    pretend_next = str(LATEST_VERSION + 1)
    assert set(segments) != {pretend_next}, "canary precondition"
    with pytest.raises(AssertionError):
        assert set(segments) == {pretend_next}


def test_docs_cache_key_matches_action_yml():
    doc = ACTION_DOC.read_text(encoding="utf-8")
    assert f"code-review-graph-schema{LATEST_VERSION}-" in doc
    assert f"`schema{LATEST_VERSION}`" in doc
    stale = {
        found
        for found in re.findall(r"schema(\d+)", doc)
        if found != str(LATEST_VERSION)
    }
    assert not stale, f"docs/GITHUB_ACTION.md still mentions schema{stale}"


def test_action_yml_comment_points_at_migrations():
    text = ACTION_YML.read_text(encoding="utf-8")
    assert "LATEST_VERSION in code_review_graph/migrations.py" in text
    assert f"schema{LATEST_VERSION}" in text


def test_cache_path_is_the_graph_directory():
    assert str(_cache_step()["with"]["path"]).strip() == ".code-review-graph"


# ===========================================================================
# 4. Failure paths: degrade usefully, never a red run with a traceback
# ===========================================================================


def test_shallow_clone_without_a_merge_base_still_reports(
    tmp_path_factory: pytest.TempPathFactory,
):
    """``actions/checkout`` depth 1 plus the Action's depth-1 base fetch."""
    origin_root = tmp_path_factory.mktemp("crg-shallow-origin")
    upstream = make_scratch_repo(origin_root / "origin")
    runner = make_runner(tmp_path_factory, "crg-shallow", comment="false")
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--branch",
            "feature",
            f"file://{upstream}",
            str(runner.workspace),
        ],
        check=True,
        capture_output=True,
        env=_git_env(),
        timeout=120,
    )
    run = execute(runner)

    merge_base = subprocess.run(
        ["git", "merge-base", "origin/main", "HEAD"],
        cwd=str(runner.workspace),
        capture_output=True,
        text=True,
        env=_git_env(),
        timeout=60,
    )
    assert merge_base.returncode != 0, "this fixture must have no local merge base"

    assert run.report is not None, "a shallow clone must still produce a report"
    assert run.comment.count(MARKER) == 1
    assert HEADING in run.comment
    names = {f["name"] for f in run.report["changed_functions"]}
    assert "refresh_token" in names, names
    for step in runner.steps_run:
        assert "Traceback (most recent call last)" not in step.stderr, step.name


def test_pull_request_with_no_changed_files_degrades_cleanly(
    tmp_path_factory: pytest.TempPathFactory,
):
    runner = make_runner(tmp_path_factory, "crg-nochanges", comment="false")
    make_scratch_repo(runner.workspace)
    # A PR whose head is its own base: a real "no changed files" pull request.
    _git(runner.workspace, "push", "-qf", "origin", "HEAD:main")
    run = execute(runner)
    assert run.report is None, "detect-changes should not emit a JSON report"
    assert run.comment.count(MARKER) == 1
    assert "No analyzable code changes detected" in run.comment
    assert "*Powered by [code-review-graph]" in run.comment
    for step in runner.steps_run:
        assert step.returncode == 0, (step.name, step.stderr)


def test_first_run_with_no_cache_builds_from_scratch(happy: ActionRun):
    """The happy-path fixture starts with no ``.code-review-graph`` at all."""
    crg_calls = happy.runner.calls_to("code-review-graph")
    subcommands = [argv[1] for argv in crg_calls if len(argv) > 1]
    assert subcommands[0] == "build", subcommands
    assert "update" not in subcommands, "a cold run must not take the update path"
    assert (happy.runner.workspace / ".code-review-graph" / "graph.db").is_file()


def test_warm_cache_takes_the_update_path(tmp_path_factory: pytest.TempPathFactory):
    """A restored, healthy cache re-parses incrementally instead of rebuilding."""
    runner = make_runner(tmp_path_factory, "crg-warm", comment="false")
    make_scratch_repo(runner.workspace)
    execute(runner)
    runner.call_log.unlink(missing_ok=True)
    runner.steps_run.clear()
    execute(runner)
    subcommands = [
        argv[1] for argv in runner.calls_to("code-review-graph") if len(argv) > 1
    ]
    assert subcommands[0] == "update", subcommands


def test_invalid_json_report_degrades_to_the_no_changes_comment(tmp_path: Path):
    result = render_report(tmp_path, "No changes detected.")
    assert result.returncode == 0
    body = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "No analyzable code changes detected" in body
    assert body.count(MARKER) == 1


def test_missing_report_file_exits_two_without_a_traceback():
    result = subprocess.run(
        [sys.executable, str(RENDERER), "--input", "/nonexistent/crg-report.json"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# Known-bad failure paths (xfail: the expectation below is the correct one)
# ---------------------------------------------------------------------------


def test_corrupt_cached_graph_falls_back_to_a_full_build(
    tmp_path_factory: pytest.TempPathFactory,
):
    runner = make_runner(tmp_path_factory, "crg-corrupt", comment="false")
    make_scratch_repo(runner.workspace)
    cache = runner.workspace / ".code-review-graph"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "graph.db").write_bytes(b"this is not a sqlite database")

    steps = action_steps()
    runner.run_step(steps[STEP_RESOLVE_BASE])
    build = runner.run_step(steps[STEP_BUILD], check=False)
    assert "Traceback (most recent call last)" not in build.stderr
    assert build.returncode == 0, build.stderr[-2000:]
    # Recovered, not merely survived: the bad file is gone and a usable
    # graph stands in its place.
    rebuilt = cache / "graph.db"
    assert rebuilt.read_bytes()[:16] == b"SQLite format 3\x00"
    runner.run_step(steps[STEP_ANALYZE])
    report = json.loads((runner.runner_temp / "crg-report.json").read_text())
    assert report.get("changed_functions") is not None, report


def _nodes_table_columns(db: Path) -> set[str]:
    """Column names of the graph's ``nodes`` table, read straight from SQLite.

    PRAGMA takes no placeholders, so the table name is a literal here rather
    than interpolated.
    """
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()


def test_cached_graph_with_foreign_tables_falls_back_to_a_full_build(
    tmp_path_factory: pytest.TempPathFactory,
):
    """A valid SQLite cache whose tables are the wrong shape must recover too.

    ``actions/cache`` restores whatever it last saved, and a database saved
    by a different schema (or by something else entirely) is a perfectly
    readable SQLite file. ``CREATE TABLE IF NOT EXISTS`` will not replace it,
    so the build fails on the first statement that names a column it does not
    have -- and, because ``update || build`` fails the same way twice, every
    run stays red until someone clears the cache by hand. That is the failure
    the corrupt-cache recovery exists to end, reached by a different route.
    """
    runner = make_runner(tmp_path_factory, "crg-foreign-schema", comment="false")
    make_scratch_repo(runner.workspace)
    cache = runner.workspace / ".code-review-graph"
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / "graph.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, junk TEXT)")
    conn.commit()
    conn.close()
    assert db.read_bytes()[:16] == b"SQLite format 3\x00", "fixture must be SQLite"

    steps = action_steps()
    runner.run_step(steps[STEP_RESOLVE_BASE])
    build = runner.run_step(steps[STEP_BUILD], check=False)
    assert "Traceback (most recent call last)" not in build.stderr
    assert build.returncode == 0, build.stderr[-2000:]
    # Recovered, not merely survived: the foreign table is gone and this
    # project's own schema stands in its place.
    columns = _nodes_table_columns(db)
    assert "junk" not in columns, columns
    assert {"qualified_name", "file_path"} <= columns, columns
    runner.run_step(steps[STEP_ANALYZE])
    report = json.loads((runner.runner_temp / "crg-report.json").read_text())
    assert report.get("changed_functions") is not None, report


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: render_pr_comment.py raises an uncaught ValueError/TypeError on a "
        "non-numeric risk_score or criticality, so a malformed report turns the "
        "render step red with a traceback instead of degrading."
    ),
)
@pytest.mark.parametrize("bad", ["not-a-number", {"a": 1}])
def test_malformed_risk_score_degrades_instead_of_crashing(tmp_path: Path, bad: Any):
    report = {
        "risk_score": bad,
        "changed_functions": [],
        "review_priorities": [],
        "test_gaps": [],
        "affected_flows": [],
    }
    result = render_report(tmp_path, report)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode in (0, 2), result.stderr


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: JSON Infinity/NaN reach the risk gate unchecked. The comment "
        "renders 'Overall risk: inf (CRITICAL)' and --fail-on-risk fails the "
        "job on a value that is not a score."
    ),
)
@pytest.mark.parametrize("literal", ["Infinity", "NaN"])
def test_non_finite_risk_score_is_rejected(tmp_path: Path, literal: str):
    raw = (
        '{"risk_score": %s, "changed_functions": [], "review_priorities": [], '
        '"test_gaps": [], "affected_flows": []}' % literal
    )
    result = render_report(tmp_path, raw)
    body = (tmp_path / "out.md").read_text(encoding="utf-8") if result.returncode == 0 else ""
    assert "inf" not in body.lower() and "nan" not in body.lower(), body


def test_truncated_report_is_accepted_by_the_trusted_workflow(tmp_path: Path):
    entries = [
        {
            "name": f"function_number_{i}",
            "qualified_name": f"src/module_{i % 40}.py::function_number_{i}",
            "file_path": f"src/module_{i % 40}.py",
            "line_start": i,
            "risk_score": 0.5,
        }
        for i in range(4000)
    ]
    report = {
        "risk_score": 0.5,
        "changed_functions": entries,
        "review_priorities": entries,
        "test_gaps": [],
        "affected_flows": [],
    }
    result = render_report(tmp_path, report, "--max-functions", "4000")
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "*Report truncated.*" in body, "this fixture must trigger truncation"
    # The notice and footer are inside the budget the consumer enforces, not
    # bolted on after it.
    assert len(body.encode("utf-8")) <= _WORKFLOW_MAX_REPORT_BYTES, len(body)
    run_trusted_validator(tmp_path, body)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_report_at_the_byte_cap_is_accepted_by_the_trusted_workflow(
    tmp_path: Path, offset: int
):
    """The budget is enforced on the artifact, which is what is stat'ed.

    ``_fit_to_budget`` measures the body it returns, but the render step
    writes that body followed by a newline and the privileged workflow's
    validator stats the file. A body sized at exactly the cap is therefore a
    60,001-byte artifact and the workflow rejects it -- the same "largest
    pull requests get no comment" failure the budget exists to prevent, one
    byte later. One byte under, exactly on, and one byte over.
    """
    report = report_rendering_to_exactly(_WORKFLOW_MAX_REPORT_BYTES + offset)
    result = render_report(tmp_path, report, "--max-functions", "100000")
    assert result.returncode == 0, result.stderr
    artifact = tmp_path / "out.md"
    assert artifact.stat().st_size <= _WORKFLOW_MAX_REPORT_BYTES, artifact.stat().st_size
    wrapped = run_trusted_validator(
        tmp_path, artifact.read_text(encoding="utf-8")
    )
    assert HEADING in wrapped


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _location() interpolates line_start into a table cell without "
        "md_escape, so a non-integer line number adds columns and breaks the "
        "row. It is the only table field that skips escaping."
    ),
)
def test_line_start_is_escaped_like_every_other_cell(tmp_path: Path):
    entry = {
        "name": "f",
        "qualified_name": "src/a.py::f",
        "file_path": "src/a.py",
        "line_start": "1 | injected | 9",
        "risk_score": 0.2,
    }
    report = {
        "risk_score": 0.2,
        "changed_functions": [entry],
        "review_priorities": [entry],
        "test_gaps": [],
        "affected_flows": [],
    }
    assert render_report(tmp_path, report).returncode == 0
    body = (tmp_path / "out.md").read_text(encoding="utf-8")
    for table in markdown_tables(body):
        width = len(table_cells(table[0]))
        for row in table:
            assert len(table_cells(row)) == width, row


@pytest.mark.parametrize("odd_name", ["my module.py", "café.py"])
def test_paths_git_quotes_still_report_their_changed_symbols(
    tmp_path_factory: pytest.TempPathFactory, odd_name: str
):
    runner = make_runner(tmp_path_factory, "crg-quoted-path", comment="false")
    root = runner.workspace
    (root / "src").mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main", ".")
    (root / "src" / "seed.py").write_text("def seed():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    body = "from src.seed import seed\n\n\ndef odd_handler(y):\n    return seed() + y\n"
    (root / "src" / odd_name).write_text(body, encoding="utf-8")
    (root / "src" / "plain.py").write_text(
        "from src.seed import seed\n\n\ndef plain_handler(y):\n    return seed() - y\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add")
    _make_upstream(root)

    run = execute(runner)
    assert run.report is not None
    names = {f["name"] for f in run.report["changed_functions"]}
    assert "plain_handler" in names, f"control symbol missing: {names}"
    assert "odd_handler" in names, (
        f"a changed symbol in {odd_name!r} never reached the report: {names}"
    )


# ===========================================================================
# 5. Security properties the docs claim
# ===========================================================================


def test_unprivileged_workflow_does_not_post():
    jobs = workflow_jobs(UNPRIVILEGED_WORKFLOW)
    job = jobs["review"]
    action_step = next(s for s in job["steps"] if s.get("uses") == "./")
    assert str(action_step["with"]["comment"]).strip('"') == "false"
    for name, script in all_run_scripts(UNPRIVILEGED_WORKFLOW):
        assert "gh api" not in script, f"{name} talks to the GitHub API"
        assert "gh pr" not in script, f"{name} talks to the GitHub API"


def test_unprivileged_workflow_permissions_are_read_only():
    doc = _load_yaml(UNPRIVILEGED_WORKFLOW)
    permissions = doc["permissions"]
    assert permissions == {"contents": "read"}, permissions
    assert "pull-requests" not in permissions


def test_unprivileged_workflow_runs_on_pull_request_only():
    doc = _load_yaml(UNPRIVILEGED_WORKFLOW)
    triggers = doc[True] if True in doc else doc["on"]
    assert set(triggers) == {"pull_request"}, triggers
    assert "pull_request_target" not in triggers


def test_privileged_workflow_verifies_the_source_event():
    doc = _load_yaml(PRIVILEGED_WORKFLOW)
    triggers = doc[True] if True in doc else doc["on"]
    assert set(triggers) == {"workflow_run"}, triggers
    assert triggers["workflow_run"]["workflows"] == ["PR Review"]
    condition = str(doc["jobs"]["comment"]["if"])
    assert "github.event.workflow_run.event == 'pull_request'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition


def test_privileged_workflow_verifies_the_analysed_commit():
    scripts = dict(all_run_scripts(PRIVILEGED_WORKFLOW))
    upsert = " ".join(scripts["Upsert sticky PR comment"].split())
    assert '"repos/${GITHUB_REPOSITORY}/pulls/${pr_number}"' in upsert
    assert "--jq '.head.sha'" in upsert
    assert '[ "${actual_sha}" != "${HEAD_SHA}" ]' in upsert
    assert "refusing to comment" in upsert
    # The compared value must come from the workflow_run payload, not the PR.
    steps = workflow_jobs(PRIVILEGED_WORKFLOW)["comment"]["steps"]
    env = next(s for s in steps if s.get("name") == "Upsert sticky PR comment")["env"]
    assert env["HEAD_SHA"].strip() == "${{ github.event.workflow_run.head_sha }}"


def test_privileged_workflow_never_checks_out_pr_code():
    steps = workflow_jobs(PRIVILEGED_WORKFLOW)["comment"]["steps"]
    for step in steps:
        uses = str(step.get("uses", ""))
        assert not uses.startswith("actions/checkout"), uses
        assert uses != "./", "the privileged workflow must not run the action"
    doc = _load_yaml(PRIVILEGED_WORKFLOW)
    assert doc["permissions"] == {"actions": "read", "pull-requests": "write"}


def test_privileged_workflow_caps_and_validates_the_artifact():
    scripts = dict(all_run_scripts(PRIVILEGED_WORKFLOW))
    locate = scripts["Locate one bounded report artifact"]
    assert "MAX_ARCHIVE_BYTES" in locate
    assert 'Expected exactly one non-expired crg-report artifact' in locate
    validator = _validator_source()
    for guard in (
        "Artifact extraction root is not a safe directory",
        "Artifact must contain exactly the expected files",
        "Artifact entries must be regular files",
        "Report contains disallowed control characters",
        "Report has an unexpected heading",
        "Report is missing its expected footer",
    ):
        assert guard in validator, guard


def test_privileged_workflow_extracts_only_under_runner_temp():
    steps = workflow_jobs(PRIVILEGED_WORKFLOW)["comment"]["steps"]
    download = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact")
    )
    assert str(download["with"]["path"]).startswith("${{ runner.temp }}")


@pytest.mark.parametrize(
    "path",
    [ACTION_YML, UNPRIVILEGED_WORKFLOW, PRIVILEGED_WORKFLOW],
    ids=lambda p: p.name,
)
def test_nothing_is_interpolated_into_a_shell_command(path: Path):
    """Every dynamic value must reach a script through ``env:``, not ``${{ }}``."""
    for name, script in all_run_scripts(path):
        assert "${{" not in script, (
            f"{path.name}: step {name!r} interpolates an Actions expression "
            "into shell text"
        )


@pytest.mark.parametrize(
    "path",
    [ACTION_YML, UNPRIVILEGED_WORKFLOW, PRIVILEGED_WORKFLOW],
    ids=lambda p: p.name,
)
def test_interpolation_canary_would_catch_a_regression(path: Path):
    """Prove the interpolation check can fail on the pattern it looks for."""
    scripts = all_run_scripts(path)
    assert scripts, f"{path.name} has no run: scripts to check"
    poisoned = scripts[0][1] + "\necho ${{ github.event.pull_request.title }}\n"
    with pytest.raises(AssertionError):
        assert "${{" not in poisoned, "canary"


def test_action_steps_pin_third_party_actions_to_a_major_version():
    for step in action_doc()["runs"]["steps"]:
        uses = step.get("uses")
        if uses:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", uses), uses


def test_docs_security_claims_match_the_workflows():
    doc = " ".join(ACTION_DOC.read_text(encoding="utf-8").split())
    assert "no code leaves the CI runner" in RENDERER.read_text(encoding="utf-8")
    assert "never by interpolation into shell commands" in doc
    assert "pull_request_target" in doc, "the docs must warn about pwn requests"
    assert "verify the source event and analysed commit" in doc
    action_prose = " ".join(
        ACTION_YML.read_text(encoding="utf-8").replace("#", " ").split()
    )
    assert "no source code is sent to any external service" in action_prose


def test_sticky_upsert_only_targets_its_own_comment():
    script = action_steps()[STEP_COMMENT]["run"]
    assert "user.login" in script, (
        "the comment lookup must filter by author before PATCHing"
    )
    # A marker anywhere in the body is not enough: the marker is published
    # in docs/GITHUB_ACTION.md, so a participant can put it in a comment of
    # their own and have it adopted.
    assert "contains(" not in script, script
    assert "startswith(" in script, script


# ---------------------------------------------------------------------------
# The sticky comment, against the three token kinds that can drive the Action
# ---------------------------------------------------------------------------
#
# `gh api user` answers a personal access token with its own login and is
# unavailable to an installation token. Assuming github-actions[bot] whenever
# it fails is right for the default GITHUB_TOKEN and wrong for a GitHub App,
# whose comments are authored by <app-slug>[bot]: the App never finds its own
# comment and posts a new one on every push. These tests drive the Action's
# own shell against a stub GitHub API so the behaviour is observed, not
# inferred from the script's text.

_GH_API_STUB = '''#!/usr/bin/env python3
"""Stub of the subset of `gh api` the sticky-comment step calls."""
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
log = os.environ["CRG_GH_LOG"]


def record(entry):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if argv[:1] != ["api"]:
    print("stub only implements `gh api`", file=sys.stderr)
    sys.exit(97)

method, jq_filter, endpoint = "GET", None, None
rest = argv[1:]
index = 0
while index < len(rest):
    token = rest[index]
    if token == "--method":
        method, index = rest[index + 1], index + 2
    elif token == "--jq":
        jq_filter, index = rest[index + 1], index + 2
    elif token == "-F":
        index += 2
    elif token in ("--paginate", "--silent"):
        index += 1
    else:
        endpoint, index = token, index + 1

record([method, endpoint])

if endpoint == "user":
    whoami = os.environ.get("CRG_FAKE_WHOAMI", "")
    if not whoami:
        # What an installation token really gets from GET /user.
        print("gh: Resource not accessible by integration (HTTP 403)",
              file=sys.stderr)
        sys.exit(1)
    print(whoami)
    sys.exit(0)

if method != "GET":
    sys.exit(0)

payload = open(os.environ["CRG_FAKE_COMMENTS"], encoding="utf-8").read()
if jq_filter is None:
    sys.stdout.write(payload)
    sys.exit(0)
# Real `gh api --jq` runs the filter in raw-output mode.
done = subprocess.run(
    ["jq", "-r", jq_filter], input=payload, capture_output=True, text=True,
)
sys.stderr.write(done.stderr)
sys.stdout.write(done.stdout)
sys.exit(done.returncode)
'''


def _comment(comment_id: int, login: str, user_type: str, *, marked: bool) -> dict:
    """One issue comment as the GitHub API returns it."""
    body = (MARKER + "\n\n" + HEADING) if marked else "just a normal comment"
    return {"id": comment_id, "user": {"login": login, "type": user_type}, "body": body}


def _run_sticky_step(
    tmp_path_factory: pytest.TempPathFactory,
    slug: str,
    *,
    whoami: str,
    comments: list[dict],
) -> list[list[str]]:
    """Run the real upsert step against a stub API; return its API calls."""
    runner = make_runner(tmp_path_factory, slug, comment="true")
    runner.workspace.mkdir(parents=True, exist_ok=True)
    (runner.runner_temp / "crg-comment.md").write_text(
        f"{MARKER}\n\n{HEADING}\n", encoding="utf-8"
    )
    stub = runner.shim_dir / "gh"
    stub.write_text(_GH_API_STUB, encoding="utf-8")
    stub.chmod(0o755)
    payload = runner.runner_temp / "comments.json"
    payload.write_text(json.dumps(comments), encoding="utf-8")
    gh_log = runner.runner_temp / "gh-calls.jsonl"
    runner.run_step(
        action_steps()[STEP_COMMENT],
        extra_env={
            "CRG_GH_LOG": str(gh_log),
            "CRG_FAKE_COMMENTS": str(payload),
            "CRG_FAKE_WHOAMI": whoami,
        },
    )
    return [
        json.loads(line)
        for line in gh_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


_needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="the stub GitHub API runs the step's own jq filter through jq",
)


@_needs_jq
def test_github_app_token_updates_its_own_sticky_comment(
    tmp_path_factory: pytest.TempPathFactory,
):
    """A GitHub App's installation token must not post a duplicate.

    `gh api user` is unavailable to it, and its comments are authored by
    <app-slug>[bot], so falling back to github-actions[bot] finds nothing and
    POSTs again on every run -- one more comment per push, forever.
    """
    calls = _run_sticky_step(
        tmp_path_factory,
        "crg-sticky-app",
        whoami="",
        comments=[
            _comment(1, "someone", "User", marked=False),
            _comment(2, "my-review-app[bot]", "Bot", marked=True),
        ],
    )
    methods = [method for method, _ in calls]
    assert "POST" not in methods, f"the App posted a duplicate: {calls}"
    assert "PATCH" in methods, calls
    patched = [endpoint for method, endpoint in calls if method == "PATCH"]
    assert patched == ["repos/example/scratch/issues/comments/2"], patched


@_needs_jq
def test_default_github_token_still_updates_its_own_comment(
    tmp_path_factory: pytest.TempPathFactory,
):
    """The documented default keeps working: no regression for GITHUB_TOKEN."""
    calls = _run_sticky_step(
        tmp_path_factory,
        "crg-sticky-default",
        whoami="",
        comments=[_comment(7, "github-actions[bot]", "Bot", marked=True)],
    )
    assert [m for m, _ in calls if m == "PATCH"] == ["PATCH"], calls
    assert "POST" not in [m for m, _ in calls], calls


@_needs_jq
def test_personal_access_token_matches_its_own_login_exactly(
    tmp_path_factory: pytest.TempPathFactory,
):
    """A PAT knows its login, so it must adopt that comment and no other."""
    calls = _run_sticky_step(
        tmp_path_factory,
        "crg-sticky-pat",
        whoami="octo-pat",
        comments=[
            _comment(3, "other-app[bot]", "Bot", marked=True),
            _comment(4, "octo-pat", "User", marked=True),
        ],
    )
    patched = [endpoint for method, endpoint in calls if method == "PATCH"]
    assert patched == ["repos/example/scratch/issues/comments/4"], patched


@_needs_jq
def test_a_participants_marker_comment_is_never_adopted(
    tmp_path_factory: pytest.TempPathFactory,
):
    """Teeth for the fallback: widening it to bots must not widen it to people.

    The marker is published in docs/GITHUB_ACTION.md, so anyone can post a
    comment carrying it. A human participant is a User, never a Bot, and must
    be left alone -- the Action posts its own comment instead.
    """
    calls = _run_sticky_step(
        tmp_path_factory,
        "crg-sticky-forged",
        whoami="",
        comments=[_comment(9, "mallory", "User", marked=True)],
    )
    methods = [method for method, _ in calls]
    assert "PATCH" not in methods, f"a participant's comment was adopted: {calls}"
    assert "POST" in methods, calls

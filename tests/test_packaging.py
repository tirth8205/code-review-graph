"""Distribution gate: prove the artefact users install actually works.

CI installs this project with ``pip install -e .``, which reads every file
straight out of the checkout. That never exercises the build backend, so
nothing in the normal suite can catch a file that fails to make it into the
wheel. It has already happened: the shipped skills were missing from the
wheel (#909) and no test noticed.

This module closes that hole. It builds both artefacts with ``python -m
build``, installs each into its own throwaway virtual environment, and then
drives the installed program from a working directory that has nothing to do
with this checkout:

* ``code-review-graph --version``
* ``build`` over a small real repository assembled from this project's own
  source files
* ``status --json`` and ``detect-changes``
* ``visualize`` (the only path that reads the vendored D3 asset)
* the MCP server over stdio, spoken to with the real ``mcp`` client:
  ``initialize``, ``tools/list``, ``prompts/list`` and a ``tools/call`` that
  reads the packaged LLM reference document

Every smoke test first asserts that the installed ``code_review_graph``
resolves inside the environment's ``site-packages`` and nowhere near
``REPO_ROOT``, so a pass can never come from the source tree leaking in.

The content assertions do not trust ``pyproject.toml``. The list of data
files the wheel must carry is derived by parsing every packaged module and
statically evaluating the path expressions it builds out of
``importlib.resources.files("code_review_graph")`` and
``Path(__file__).parent`` -- that is, by looking at what the code actually
opens. ``test_discovery_finds_the_known_data_surface`` is the canary for
that derivation: if the evaluator silently stops finding anything, the
inventory test would pass vacuously, so the canary fails loudly instead.

These checks are slow (three virtual environments, several ``pip install``
runs, a full ``python -m build``) and need network access. They are not for
every pull request. The normal suite skips them; run them on demand with::

    uv run --python 3.13 python -m pytest tests/test_packaging.py -m packaging

``tests/conftest.py`` does the skipping, keyed on the ``packaging`` marker.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "code_review_graph"
PKG_NAME = "code_review_graph"

# Generous: a cold interpreter, a pip resolve over the network and a full
# tree-sitter build on a loaded runner are all slow, but nothing here should
# take a quarter of an hour.
SUBPROCESS_TIMEOUT = 900

pytestmark = [
    pytest.mark.packaging,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is required to build"),
    pytest.mark.skipif(
        not (REPO_ROOT / ".git").exists(),
        reason="needs a git checkout of this repository to build a distribution",
    ),
]


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _clean_env(**extra: str) -> dict[str, str]:
    """A child environment with nothing pointing back at this checkout.

    ``PYTHONPATH``/``PYTHONHOME``/``VIRTUAL_ENV`` are the three ways the
    developer's (or CI's) editable install can sneak onto the child's
    ``sys.path`` and make a broken wheel look fine.
    """
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with no shell, failing loudly with both streams attached."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else _clean_env(),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def _bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _make_venv(base_python: str, dest: Path) -> Path:
    """Create a virtual environment and return its executable directory."""
    _run([base_python, "-m", "venv", str(dest)])
    bindir = _bin_dir(dest)
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    if not python.exists():
        raise AssertionError(f"venv at {dest} has no interpreter at {python}")
    return bindir


@dataclass(frozen=True)
class InstalledEnv:
    """One throwaway environment with the distribution installed into it."""

    kind: str
    root: Path
    python: Path
    script: Path
    daemon_script: Path


def _install_into_fresh_venv(kind: str, artefact: Path, dest: Path) -> InstalledEnv:
    bindir = _make_venv(sys.executable, dest)
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    _run([str(python), "-m", "pip", "install", "--quiet", str(artefact)])
    suffix = ".exe" if os.name == "nt" else ""
    return InstalledEnv(
        kind=kind,
        root=dest,
        python=python,
        script=bindir / f"code-review-graph{suffix}",
        daemon_script=bindir / f"crg-daemon{suffix}",
    )


# ---------------------------------------------------------------------------
# Building the artefacts
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def builder_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An interpreter with ``build`` available, isolated from the test env."""
    venv = tmp_path_factory.mktemp("crg-builder")
    bindir = _make_venv(sys.executable, venv)
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    _run([str(python), "-m", "pip", "install", "--quiet", "build"])
    return python


@dataclass(frozen=True)
class Artefacts:
    wheel: Path
    sdist: Path


@pytest.fixture(scope="session")
def artefacts(builder_python: Path, tmp_path_factory: pytest.TempPathFactory) -> Artefacts:
    """``python -m build`` over this checkout, into a temp directory.

    ``--outdir`` keeps the checkout's own ``dist/`` untouched.
    """
    outdir = tmp_path_factory.mktemp("crg-dist")
    _run([str(builder_python), "-m", "build", "--outdir", str(outdir)], cwd=REPO_ROOT)
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected exactly one wheel in {outdir}, got {wheels}"
    assert len(sdists) == 1, f"expected exactly one sdist in {outdir}, got {sdists}"
    return Artefacts(wheel=wheels[0], sdist=sdists[0])


@pytest.fixture(scope="session")
def wheel_names(artefacts: Artefacts) -> list[str]:
    with zipfile.ZipFile(artefacts.wheel) as zf:
        return sorted(zf.namelist())


@pytest.fixture(scope="session")
def sdist_names(artefacts: Artefacts) -> list[str]:
    """Sdist member paths with the ``<name>-<version>/`` prefix stripped."""
    with tarfile.open(artefacts.sdist) as tf:
        members = [m.name for m in tf.getmembers() if m.isfile()]
    stripped = []
    for name in members:
        parts = PurePosixPath(name).parts
        stripped.append(str(PurePosixPath(*parts[1:])) if len(parts) > 1 else name)
    return sorted(stripped)


@pytest.fixture(scope="session")
def wheel_env(artefacts: Artefacts, tmp_path_factory: pytest.TempPathFactory) -> InstalledEnv:
    return _install_into_fresh_venv(
        "wheel", artefacts.wheel, tmp_path_factory.mktemp("crg-env-wheel")
    )


@pytest.fixture(scope="session")
def sdist_env(artefacts: Artefacts, tmp_path_factory: pytest.TempPathFactory) -> InstalledEnv:
    return _install_into_fresh_venv(
        "sdist", artefacts.sdist, tmp_path_factory.mktemp("crg-env-sdist")
    )


# ---------------------------------------------------------------------------
# Deriving the runtime data surface from the code, not from pyproject.toml
# ---------------------------------------------------------------------------


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so ``/ D3_LOCAL_FILENAME`` resolves."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and isinstance(node.target, ast.Name):
                constants[node.target.id] = node.value.value
    return constants


def _static_str(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _static_path(node: ast.AST, module_path: Path, constants: dict[str, str]) -> Path | None:
    """Statically evaluate a path expression, or return ``None``.

    Understands exactly the shapes this package uses to reach its own data
    files::

        importlib.resources.files("code_review_graph")
        Path(__file__)  /  .resolve()  /  .parent
        <base> / "literal"   or   <base> / MODULE_LEVEL_CONSTANT
        <base>.joinpath("literal", ...)

    Anything with a runtime-variable component evaluates to ``None``, which
    is the safe answer: it simply is not claimed as a required data file.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "files" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == PKG_NAME:
                    return PKG_DIR
                return None
            if func.attr == "resolve":
                return _static_path(func.value, module_path, constants)
            if func.attr == "joinpath":
                base = _static_path(func.value, module_path, constants)
                if base is None:
                    return None
                for arg in node.args:
                    piece = _static_str(arg, constants)
                    if piece is None:
                        return None
                    base = base / piece
                return base
            return None
        if isinstance(func, ast.Name) and func.id == "Path" and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id == "__file__":
                return module_path
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _static_path(node.value, module_path, constants)
        return base.parent if base is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _static_path(node.left, module_path, constants)
        if base is None:
            return None
        piece = _static_str(node.right, constants)
        return base / piece if piece is not None else None
    return None


def discover_package_data_paths() -> dict[str, list[str]]:
    """Return ``{package-relative path: [modules that build it]}``.

    Walks every ``.py`` file that ships in the package and evaluates each
    path expression it contains. Only results that land *inside* the package
    directory are kept; the source-tree fallbacks that reach out to the
    checkout root are deliberately dropped, because a wheel has no checkout.
    """
    found: dict[str, list[str]] = {}
    for module in sorted(PKG_DIR.rglob("*.py")):
        if "__pycache__" in module.parts:
            continue
        tree = ast.parse(module.read_bytes(), filename=str(module))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.BinOp, ast.Call)):
                continue
            resolved = _static_path(node, module, constants)
            if resolved is None:
                continue
            try:
                rel = resolved.relative_to(PKG_DIR)
            except ValueError:
                continue
            if rel == Path("."):
                continue
            if rel.suffix == ".py":
                continue
            key = rel.as_posix()
            found.setdefault(key, [])
            name = module.relative_to(REPO_ROOT).as_posix()
            if name not in found[key]:
                found[key].append(name)
    return found


def required_wheel_files() -> tuple[set[str], set[str]]:
    """Split the discovered surface into concrete files and directory prefixes.

    A discovered path that exists in the checkout is expanded to the concrete
    non-``.py`` files under it. A discovered path that does *not* exist in the
    checkout (``_bundled_skills`` is injected by the build backend) becomes a
    prefix the wheel must populate.
    """
    files: set[str] = set()
    prefixes: set[str] = set()
    for rel in discover_package_data_paths():
        on_disk = PKG_DIR / rel
        if on_disk.is_file():
            files.add(f"{PKG_NAME}/{rel}")
        elif on_disk.is_dir():
            for child in sorted(on_disk.rglob("*")):
                if not child.is_file() or "__pycache__" in child.parts:
                    continue
                if child.suffix == ".py":
                    continue
                files.add(f"{PKG_NAME}/{child.relative_to(PKG_DIR).as_posix()}")
        else:
            prefixes.add(f"{PKG_NAME}/{rel}")
    return files, prefixes


def test_discovery_finds_the_known_data_surface() -> None:
    """Canary for the derivation the inventory test depends on.

    Without this, a broken evaluator would make
    ``test_wheel_contains_every_runtime_data_file`` assert nothing at all and
    still go green. Every anchor below is a path some packaged module builds
    and then opens.
    """
    discovered = discover_package_data_paths()
    for anchor in (
        "assets/d3.v7.min.js",
        "docs/LLM-OPTIMIZED-REFERENCE.md",
        "eval/configs",
        "_bundled_skills",
    ):
        assert anchor in discovered, (
            f"static path evaluation no longer finds {anchor!r}; "
            f"it found {sorted(discovered)}. The inventory check below is "
            "only as good as this derivation, so fix the evaluator rather "
            "than deleting this assertion."
        )

    files, prefixes = required_wheel_files()
    assert f"{PKG_NAME}/assets/d3.v7.min.js" in files
    assert f"{PKG_NAME}/docs/LLM-OPTIMIZED-REFERENCE.md" in files
    assert f"{PKG_NAME}/_bundled_skills" in prefixes
    config_files = {f for f in files if f.startswith(f"{PKG_NAME}/eval/configs/")}
    assert len(config_files) >= 5, f"expected the benchmark configs, got {config_files}"
    assert len(files) >= 7, f"suspiciously small data surface: {sorted(files)}"


def test_wheel_contains_every_runtime_data_file(wheel_names: list[str]) -> None:
    """Every file the packaged code opens must be inside the wheel."""
    files, prefixes = required_wheel_files()
    present = set(wheel_names)

    missing = sorted(f for f in files if f not in present)
    assert not missing, (
        "the wheel is missing data files the packaged code reads at runtime: "
        f"{missing}. These were found by evaluating path expressions in the "
        "source, so each one has a module that opens it."
    )

    for prefix in sorted(prefixes):
        under = [n for n in wheel_names if n.startswith(prefix + "/")]
        assert under, (
            f"the wheel has nothing under {prefix!r}, but packaged code "
            "resolves that directory as a package resource"
        )


def test_wheel_bundles_every_shipped_skill(wheel_names: list[str]) -> None:
    """The bundled skill resource must equal the checkout's ``skills/`` tree.

    ``skills.py`` treats ``code_review_graph/_bundled_skills`` and the
    checkout's top-level ``skills/`` as the same content: the first is the
    installed resource, the second the editable-install fallback. Regression
    #909 was exactly this set coming back empty.
    """
    source_skills = REPO_ROOT / "skills"
    expected = {
        p.relative_to(source_skills).as_posix()
        for p in sorted(source_skills.rglob("*"))
        if p.is_file()
    }
    assert expected, "the checkout has no skills/ tree; this check would be vacuous"

    prefix = f"{PKG_NAME}/_bundled_skills/"
    shipped = {n[len(prefix) :] for n in wheel_names if n.startswith(prefix)}

    assert shipped == expected, (
        "bundled skills in the wheel do not match the checkout's skills/ tree.\n"
        f"missing from wheel: {sorted(expected - shipped)}\n"
        f"unexpected in wheel: {sorted(shipped - expected)}"
    )
    assert all(name.endswith("/SKILL.md") for name in shipped), (
        f"every bundled skill must be a SKILL.md; got {sorted(shipped)}"
    )


def test_bundled_skill_files_match_the_generated_ones(wheel_names: list[str]) -> None:
    """The file form and the inline form of a skill must not drift apart.

    ``generate_skills`` renders skills from a dict of Python strings, while
    ``install_qoder_skills`` copies the bundled ``SKILL.md`` files. Where a
    skill exists in both, the bytes must agree, or two platforms ship
    different instructions under one name.
    """
    from code_review_graph.skills import _SKILLS

    assert _SKILLS, "no inline skills defined; this check would be vacuous"
    compared = 0
    for filename, skill in _SKILLS.items():
        name = filename.removesuffix(".md")
        bundled = REPO_ROOT / "skills" / name / "SKILL.md"
        if not bundled.is_file():
            continue
        rendered = (
            "---\n"
            f"name: {skill['name']}\n"
            f"description: {skill['description']}\n"
            "---\n\n"
            f"{skill['body']}\n"
        )
        assert bundled.read_text(encoding="utf-8") == rendered, (
            f"skills/{name}/SKILL.md has drifted from the inline _SKILLS entry; "
            "Claude Code and Qoder would install different text under one name"
        )
        compared += 1
    assert compared >= 4, f"only compared {compared} skills; expected at least 4"


def test_no_package_module_reads_a_hook_template(sdist_names: list[str]) -> None:
    """Hook configs are generated in code, so no template needs shipping.

    Recorded here so the claim is checked rather than assumed: if someone
    later makes the installer read ``hooks/hooks.json`` from disk, the
    discovery evaluator will surface it and this test fails, pointing at a
    file the wheel does not carry.
    """
    discovered = discover_package_data_paths()
    hookish = sorted(k for k in discovered if "hook" in k.lower())
    assert not hookish, (
        f"packaged code now resolves hook templates as package data: {hookish}. "
        "The wheel does not ship the checkout's hooks/ directory, so these "
        "must be added to the wheel before this assertion is relaxed."
    )
    # The checkout's templates still travel in the sdist, which is where the
    # GitHub Action and manual installs pick them up.
    assert "hooks/hooks.json" in sdist_names
    assert "hooks/session-start.sh" in sdist_names
    shipped = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert set(shipped) == {"SessionStart", "PostToolUse"}, (
        f"shipped hook template declares unexpected events: {sorted(shipped)}"
    )


# ---------------------------------------------------------------------------
# What must NOT be in the artefacts
# ---------------------------------------------------------------------------

_FORBIDDEN_TOP_LEVEL = {
    "tests",
    "scratch",
    "evaluate",
    "diagrams",
    "scripts",
    "code-review-graph-vscode",
    ".beads",
    ".github",
    ".claude",
    ".code-review-graph",
    "node_modules",
}

_FORBIDDEN_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QODER.md",
    "CODEBUDDY.md",
    "CHANGELOG.md",
    ".mcp.json",
    "uv.lock",
    "conftest.py",
    "beads.db",
}

_FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".pyc", ".vsix", ".so")


def _forbidden_hits(names: list[str], *, allow_dot: set[str]) -> list[str]:
    hits = []
    for name in names:
        parts = PurePosixPath(name).parts
        if not parts:
            continue
        if parts[0] in _FORBIDDEN_TOP_LEVEL:
            hits.append(name)
            continue
        if parts[-1] in _FORBIDDEN_NAMES or parts[-1].startswith("test_"):
            hits.append(name)
            continue
        if name.endswith(_FORBIDDEN_SUFFIXES):
            hits.append(name)
            continue
        dotted = [p for p in parts if p.startswith(".")]
        if dotted and name not in allow_dot:
            hits.append(name)
    return sorted(set(hits))


def test_wheel_ships_nothing_it_should_not(wheel_names: list[str]) -> None:
    # Canary: a listing that came back empty or tiny must not pass silently.
    assert len(wheel_names) > 50, f"only {len(wheel_names)} entries in the wheel"
    assert f"{PKG_NAME}/cli.py" in wheel_names, "the wheel does not even contain the CLI"

    hits = _forbidden_hits(wheel_names, allow_dot=set())
    assert not hits, f"the wheel ships files that must never reach a user: {hits}"

    strays = sorted(
        n
        for n in wheel_names
        if not n.startswith(f"{PKG_NAME}/") and ".dist-info/" not in n
    )
    assert not strays, f"wheel has top-level entries outside the package: {strays}"


def test_sdist_ships_nothing_it_should_not(sdist_names: list[str]) -> None:
    assert len(sdist_names) > 50, f"only {len(sdist_names)} entries in the sdist"
    assert "pyproject.toml" in sdist_names

    # Hatchling always emits .gitignore and cannot be told not to; every other
    # dot-path is a leak. Pinning it as the single exception means a new one
    # fails this test instead of hiding behind a blanket allowance.
    hits = _forbidden_hits(sdist_names, allow_dot={".gitignore"})
    assert not hits, f"the sdist ships files that must never be published: {hits}"

    dotted = sorted(
        n for n in sdist_names if any(p.startswith(".") for p in PurePosixPath(n).parts)
    )
    assert dotted == [".gitignore"], f"unexpected dot-paths in the sdist: {dotted}"


def test_artefact_size_and_file_count(artefacts: Artefacts, wheel_names: list[str]) -> None:
    """Record the numbers, and fail if either moves by an order of magnitude.

    A wheel that suddenly triples has picked something up; one that halves has
    dropped something. Both are worth a human look.
    """
    wheel_bytes = artefacts.wheel.stat().st_size
    sdist_bytes = artefacts.sdist.stat().st_size
    print(
        f"\nwheel: {artefacts.wheel.name} {wheel_bytes} bytes, "
        f"{len(wheel_names)} files\n"
        f"sdist: {artefacts.sdist.name} {sdist_bytes} bytes"
    )
    assert 200_000 < wheel_bytes < 5_000_000, f"wheel size {wheel_bytes} is out of band"
    assert 200_000 < sdist_bytes < 8_000_000, f"sdist size {sdist_bytes} is out of band"
    assert 60 <= len(wheel_names) <= 400, f"wheel file count {len(wheel_names)} is out of band"


# ---------------------------------------------------------------------------
# Metadata a user sees
# ---------------------------------------------------------------------------


def _pyproject_text() -> str:
    return (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _metadata_field(blob: str, field: str) -> str | None:
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", blob, re.MULTILINE)
    return match.group(1).strip() if match else None


@pytest.fixture(scope="session")
def wheel_metadata(artefacts: Artefacts) -> str:
    with zipfile.ZipFile(artefacts.wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return zf.read(name).decode("utf-8")


def test_version_is_the_same_everywhere(wheel_metadata: str, artefacts: Artefacts) -> None:
    init_match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', (PKG_DIR / "__init__.py").read_text(), re.MULTILINE
    )
    assert init_match, "code_review_graph/__init__.py has no __version__"
    init_version = init_match.group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", init_version), f"odd version {init_version!r}"

    project_match = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject_text(), re.MULTILINE)
    assert project_match, "pyproject.toml has no [project] version"

    assert project_match.group(1) == init_version, (
        f"pyproject version {project_match.group(1)} != __init__ {init_version}"
    )
    assert _metadata_field(wheel_metadata, "Version") == init_version
    assert init_version in artefacts.wheel.name
    assert init_version in artefacts.sdist.name


def test_declared_python_floor_matches_the_code(wheel_metadata: str) -> None:
    """The declared floor must both match pyproject and actually parse."""
    declared = _metadata_field(wheel_metadata, "Requires-Python")
    assert declared, "wheel metadata declares no Requires-Python"
    from_pyproject = re.search(r'^requires-python\s*=\s*"([^"]+)"', _pyproject_text(), re.M)
    assert from_pyproject and from_pyproject.group(1) == declared

    floor = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared)
    assert floor, f"unexpected Requires-Python spelling: {declared!r}"
    feature_version = (int(floor.group(1)), int(floor.group(2)))

    parsed = 0
    offenders: list[str] = []
    for module in sorted(PKG_DIR.rglob("*.py")):
        if "__pycache__" in module.parts:
            continue
        parsed += 1
        try:
            ast.parse(module.read_bytes(), filename=str(module), feature_version=feature_version)
        except SyntaxError as exc:
            offenders.append(f"{module.relative_to(REPO_ROOT)}:{exc.lineno}: {exc.msg}")
    assert parsed >= 40, f"only parsed {parsed} modules; the walk is broken"
    assert not offenders, (
        f"packaged code uses syntax newer than the declared floor {declared}: {offenders}"
    )


def test_console_scripts_resolve_in_the_installed_env(
    wheel_env: InstalledEnv, artefacts: Artefacts
) -> None:
    """Every declared entry point must import and be callable after install."""
    with zipfile.ZipFile(artefacts.wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
        entry_points = zf.read(name).decode("utf-8")
    pairs = re.findall(r"^([\w-]+)\s*=\s*([\w.]+):(\w+)$", entry_points, re.M)
    assert len(pairs) >= 2, f"expected at least two console scripts, got {entry_points!r}"
    declared = {script: (mod, attr) for script, mod, attr in pairs}
    assert "code-review-graph" in declared and "crg-daemon" in declared

    probe = (
        "import importlib, json, sys\n"
        "spec = json.loads(sys.argv[1])\n"
        "for script, (mod, attr) in spec.items():\n"
        "    target = getattr(importlib.import_module(mod), attr)\n"
        "    assert callable(target), (script, mod, attr)\n"
        "print('ok')\n"
    )
    out = _run(
        [str(wheel_env.python), "-c", probe, json.dumps(declared)],
        cwd=wheel_env.root,
    ).stdout
    assert "ok" in out

    for script in (wheel_env.script, wheel_env.daemon_script):
        assert script.exists(), f"console script {script.name} was not installed"
    assert wheel_env.daemon_script.name.startswith("crg-daemon")
    help_out = _run([str(wheel_env.daemon_script), "--help"], cwd=wheel_env.root).stdout
    assert "crg-daemon" in help_out


def test_declared_dependencies_are_sufficient(wheel_env: InstalledEnv) -> None:
    """The core path must run on the declared dependency closure alone.

    The environment was built with ``pip install <wheel>`` into an empty
    venv, so it holds exactly the declared dependencies and their transitive
    closure. If any optional-extra package had leaked in, a module that
    quietly needs it would look fine here, so the extras are asserted absent
    before the import sweep runs.
    """
    extras = [
        "numpy",
        "sentence-transformers",
        "igraph",
        "jedi",
        "matplotlib",
        "ollama",
        "google-genai",
        "tiktoken",
        "playwright",
    ]
    probe = (
        "import importlib, importlib.metadata as md, json, pkgutil, sys\n"
        "installed = {d.metadata['Name'].lower().replace('_','-') "
        "for d in md.distributions() if d.metadata['Name']}\n"
        "extras = json.loads(sys.argv[1])\n"
        "leaked = sorted(e for e in extras if e in installed)\n"
        "import code_review_graph\n"
        "failed = []\n"
        "for m in pkgutil.walk_packages(code_review_graph.__path__, 'code_review_graph.'):\n"
        "    if m.name.endswith('.__main__'):\n"
        "        continue\n"
        "    try:\n"
        "        importlib.import_module(m.name)\n"
        "    except Exception as exc:\n"
        "        failed.append(f'{m.name}: {type(exc).__name__}: {exc}')\n"
        "core = sorted(md.requires('code-review-graph') or [])\n"
        "print(json.dumps({'leaked': leaked, 'failed': failed, 'requires': core,\n"
        "                  'n_installed': len(installed)}))\n"
    )
    out = _run(
        [str(wheel_env.python), "-c", probe, json.dumps(extras)],
        cwd=wheel_env.root,
    ).stdout
    payload = json.loads(out.strip().splitlines()[-1])

    assert payload["n_installed"] > 10, "the probe saw almost nothing installed"
    assert not payload["leaked"], (
        "optional-extra packages are present in the core install, so this "
        f"environment cannot prove the declared set is enough: {payload['leaked']}"
    )
    assert not payload["failed"], (
        "modules fail to import with only the declared dependencies installed: "
        f"{payload['failed']}"
    )

    unconditional = {
        re.split(r"[<>=!;\s]", spec, maxsplit=1)[0].lower()
        for spec in payload["requires"]
        if "extra ==" not in spec
    }
    for name in ("mcp", "fastmcp", "tree-sitter", "networkx", "pyyaml", "watchdog"):
        assert name in unconditional, f"{name} is not a declared runtime dependency"


def test_wheel_installs_and_runs_on_the_declared_python_floor(
    artefacts: Artefacts, wheel_metadata: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Install on the floor interpreter itself, not just parse for it."""
    declared = _metadata_field(wheel_metadata, "Requires-Python") or ""
    floor = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared)
    assert floor, f"unexpected Requires-Python spelling: {declared!r}"
    label = f"{floor.group(1)}.{floor.group(2)}"

    base = shutil.which(f"python{label}")
    if base is None and shutil.which("uv"):
        found = _run(["uv", "python", "find", label], check=False)
        if found.returncode == 0 and found.stdout.strip():
            base = found.stdout.strip()
    if base is None:
        pytest.skip(f"no python{label} interpreter available to test the declared floor")

    venv = tmp_path_factory.mktemp("crg-floor")
    bindir = _make_venv(base, venv)
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    _run([str(python), "-m", "pip", "install", "--quiet", str(artefacts.wheel)])

    version = _run(
        [str(bindir / ("code-review-graph.exe" if os.name == "nt" else "code-review-graph")),
         "--version"],
        cwd=venv,
    ).stdout
    assert _metadata_field(wheel_metadata, "Version") in version

    probe = (
        "import importlib, json, pkgutil, sys\n"
        "assert sys.version_info[:2] == tuple(json.loads(sys.argv[1])), sys.version\n"
        "import code_review_graph\n"
        "failed = []\n"
        "for m in pkgutil.walk_packages(code_review_graph.__path__, 'code_review_graph.'):\n"
        "    if m.name.endswith('.__main__'):\n"
        "        continue\n"
        "    try:\n"
        "        importlib.import_module(m.name)\n"
        "    except Exception as exc:\n"
        "        failed.append(f'{m.name}: {type(exc).__name__}: {exc}')\n"
        "print(json.dumps(failed))\n"
    )
    out = _run(
        [str(python), "-c", probe, json.dumps([int(floor.group(1)), int(floor.group(2))])],
        cwd=venv,
    ).stdout
    failed = json.loads(out.strip().splitlines()[-1])
    assert not failed, f"packaged modules do not import on the declared floor {label}: {failed}"


# ---------------------------------------------------------------------------
# The smoke test: drive the installed program with the checkout out of reach
# ---------------------------------------------------------------------------

# Real source files, copied out of this checkout into a standalone git
# repository. Real code, real cross-file imports, real call edges -- and none
# of it importable as ``code_review_graph``, so a build can never reach back
# into the package under test.
_CORPUS_PY = (
    "code_review_graph/config_keys.py",
    "code_review_graph/constants.py",
    "code_review_graph/memory.py",
    "code_review_graph/graph_diff.py",
    "code_review_graph/python_resolver.py",
    "code_review_graph/hcl_resolver.py",
    "code_review_graph/postprocessing.py",
    "scripts/render_pr_comment.py",
)
_CORPUS_TS = (
    "code-review-graph-vscode/src/backend/cli.ts",
    "code-review-graph-vscode/src/backend/watcher.ts",
)

_MCP_PROBE = '''
import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    command, repo, crg_home = sys.argv[1], sys.argv[2], sys.argv[3]
    env = dict(os.environ)
    env["CRG_HOME"] = crg_home
    env.pop("PYTHONPATH", None)
    params = StdioServerParameters(
        command=command, args=["serve", "--repo", repo], env=env, cwd=repo
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=180)
            tools = await asyncio.wait_for(session.list_tools(), timeout=180)
            prompts = await asyncio.wait_for(session.list_prompts(), timeout=180)
            called = await asyncio.wait_for(
                session.call_tool(
                    "get_docs_section_tool",
                    {"section_name": "usage", "repo_root": repo},
                ),
                timeout=180,
            )
            text = called.content[0].text if called.content else ""
            print(
                "CRG_PROBE "
                + json.dumps(
                    {
                        "tools": sorted(t.name for t in tools.tools),
                        "prompts": sorted(p.name for p in prompts.prompts),
                        "docs_section": text,
                    }
                )
            )


asyncio.run(main())
'''

_REQUIRED_TOOLS = {
    "build_or_update_graph_tool",
    "detect_changes_tool",
    "get_docs_section_tool",
    "get_impact_radius_tool",
    "get_minimal_context_tool",
    "get_review_context_tool",
    "list_graph_stats_tool",
    "query_graph_tool",
    "semantic_search_nodes_tool",
}
_REQUIRED_PROMPTS = {
    "architecture_map",
    "debug_issue",
    "onboard_developer",
    "pre_merge_check",
    "review_changes",
}


def _make_sample_repo(dest: Path) -> Path:
    """Assemble and commit a small real repository, twice, so HEAD~1 exists."""
    project = dest / "sample_project"
    project.mkdir(parents=True)
    copied = 0
    for rel in _CORPUS_PY + _CORPUS_TS:
        source = REPO_ROOT / rel
        if not source.is_file():
            continue
        shutil.copy2(source, project / Path(rel).name)
        copied += 1
    assert copied >= 6, f"only copied {copied} corpus files from {REPO_ROOT}"

    git = ["git", "-C", str(dest)]
    _run(["git", "init", "--quiet", str(dest)])
    _run(git + ["config", "user.email", "packaging-gate@example.invalid"])
    _run(git + ["config", "user.name", "packaging gate"])
    _run(git + ["add", "-A"])
    _run(git + ["commit", "--quiet", "-m", "initial import"])

    target = project / "memory.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n\ndef packaging_gate_probe(value):\n    return normalise(value)\n",
        encoding="utf-8",
    )
    _run(git + ["add", "-A"])
    _run(git + ["commit", "--quiet", "-m", "add a probe function"])
    return dest


def _json_tail(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, f"no JSON object in output:\n{text}"
    return json.loads(text[start:])


@pytest.mark.parametrize("env_fixture", ["wheel_env", "sdist_env"])
def test_installed_distribution_smoke(
    env_fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Drive the installed program end to end with no source tree in reach."""
    env: InstalledEnv = request.getfixturevalue(env_fixture)
    repo = _make_sample_repo(tmp_path / "sample-repo")
    crg_home = tmp_path / "crg-home"
    fake_home = tmp_path / "home"
    crg_home.mkdir()
    fake_home.mkdir()
    child = _clean_env(CRG_HOME=str(crg_home), HOME=str(fake_home))

    # --- the load-bearing precondition ----------------------------------
    located = _run(
        [
            str(env.python),
            "-c",
            "import code_review_graph, sys; print(code_review_graph.__file__); "
            "print(sys.prefix)",
        ],
        cwd=repo,
        env=child,
    ).stdout.splitlines()
    module_file = Path(located[0].strip()).resolve()
    assert env.root.resolve() in module_file.parents, (
        f"{env.kind} install resolves to {module_file}, outside {env.root}"
    )
    assert REPO_ROOT not in module_file.parents, (
        f"{env.kind} smoke test is reading the checkout at {module_file}; "
        "every assertion below would be meaningless"
    )

    # --- --version -------------------------------------------------------
    version_out = _run([str(env.script), "--version"], cwd=repo, env=child).stdout
    init_version = re.search(
        r'__version__\s*=\s*"([^"]+)"', (PKG_DIR / "__init__.py").read_text()
    ).group(1)
    assert version_out.strip().endswith(init_version), version_out

    # --- build -----------------------------------------------------------
    build_out = _run([str(env.script), "build"], cwd=repo, env=child)
    assert "Full build" in build_out.stdout, build_out.stdout
    assert (repo / ".code-review-graph" / "graph.db").is_file()

    # --- status ----------------------------------------------------------
    status = _json_tail(_run([str(env.script), "status", "--json"], cwd=repo, env=child).stdout)
    assert status["files"] >= 6, status
    assert status["nodes"] > 10, status
    assert status["edges"] > 10, status
    assert "python" in status["languages"], status

    # --- detect-changes --------------------------------------------------
    changes = _json_tail(
        _run(
            [str(env.script), "detect-changes", "--base", "HEAD~1"], cwd=repo, env=child
        ).stdout
    )
    blob = json.dumps(changes)
    assert "packaging_gate_probe" in blob, (
        "detect-changes did not surface the function added in the second "
        f"commit; payload keys: {sorted(changes)}"
    )
    assert "memory.py" in blob

    # --- visualize: the only reader of the vendored D3 asset -------------
    _run([str(env.script), "visualize"], cwd=repo, env=child)
    html = repo / ".code-review-graph" / "graph.html"
    d3 = repo / ".code-review-graph" / "d3.v7.min.js"
    assert html.is_file() and html.stat().st_size > 10_000
    assert d3.is_file(), (
        "the vendored D3 asset did not reach the generated page, so the wheel "
        "either omits code_review_graph/assets/d3.v7.min.js or ships it corrupt"
    )
    assert d3.read_bytes() == (PKG_DIR / "assets" / "d3.v7.min.js").read_bytes()

    # --- packaged benchmark configs --------------------------------------
    configs = _run(
        [
            str(env.python),
            "-c",
            "from code_review_graph.eval.runner import load_all_configs;"
            "print(len(load_all_configs()))",
        ],
        cwd=repo,
        env=child,
    ).stdout
    assert int(configs.strip().splitlines()[-1]) >= 5, configs

    # --- MCP over stdio --------------------------------------------------
    probe = tmp_path / f"mcp_probe_{env.kind}.py"
    probe.write_text(_MCP_PROBE, encoding="utf-8")
    probe_out = _run(
        [str(env.python), str(probe), str(env.script), str(repo), str(crg_home)],
        cwd=repo,
        env=child,
    ).stdout
    line = next(line for line in probe_out.splitlines() if line.startswith("CRG_PROBE "))
    payload = json.loads(line[len("CRG_PROBE ") :])

    assert len(payload["tools"]) >= 25, payload["tools"]
    missing = sorted(_REQUIRED_TOOLS - set(payload["tools"]))
    assert not missing, f"MCP server does not expose {missing}"
    assert set(payload["prompts"]) == _REQUIRED_PROMPTS, payload["prompts"]

    section = payload["docs_section"]
    assert "not_found" not in section, (
        "get_docs_section_tool could not read the packaged LLM reference "
        f"document over the real transport: {section[:400]}"
    )
    assert len(section) > 200, section


def test_install_command_works_from_the_installed_wheel(
    wheel_env: InstalledEnv, tmp_path: Path
) -> None:
    """``install`` must write skills and hooks without any source tree.

    This is the exact shape of #909: the installer succeeded but wrote
    nothing, because the files it copies were not in the wheel.
    """
    project = tmp_path / "target"
    project.mkdir()
    _run(["git", "init", "--quiet", str(project)])
    (project / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    child = _clean_env(CRG_HOME=str(tmp_path / "crg-home"), HOME=str(fake_home))

    out = _run(
        [str(wheel_env.script), "install", "--platform", "claude-code", "-y"],
        cwd=project,
        env=child,
    ).stdout
    assert "Generated Claude Code skills" in out, out

    written = sorted(
        p.relative_to(project).as_posix()
        for p in (project / ".claude" / "skills").rglob("SKILL.md")
    )
    assert len(written) >= 4, f"install wrote only {written}"
    for path in written:
        body = (project / path).read_text(encoding="utf-8")
        assert body.startswith("---\nname: "), f"{path} has no frontmatter"
        assert len(body) > 200, f"{path} is suspiciously short"

    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"SessionStart", "PostToolUse"}, settings

    # Qoder is the platform that copies the bundled SKILL.md resources rather
    # than rendering them from Python strings, so it is the direct test of
    # the wheel's _bundled_skills payload.
    qoder_out = _run(
        [
            str(wheel_env.python),
            "-c",
            "from pathlib import Path;"
            "from code_review_graph.skills import install_qoder_skills;"
            "print(install_qoder_skills(Path(__import__('sys').argv[1])))",
            str(project),
        ],
        cwd=project,
        env=child,
    ).stdout
    assert ".qoder" in qoder_out, qoder_out
    bundled = sorted(
        p.relative_to(project / ".qoder" / "skills").as_posix()
        for p in (project / ".qoder" / "skills").rglob("SKILL.md")
    )
    expected = sorted(
        p.relative_to(REPO_ROOT / "skills").as_posix()
        for p in (REPO_ROOT / "skills").rglob("SKILL.md")
    )
    assert bundled == expected, (
        f"Qoder install copied {bundled} but the checkout ships {expected}"
    )

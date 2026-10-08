"""Native DSH bundle install/reinstall/uninstall ownership in isolated profiles."""
import json

import pytest

from code_review_graph import dsh, skills, uninstall


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    path = dsh.config_path(tmp_path, "headless")
    path.parent.mkdir(parents=True)
    manifest = {"dependencies": {"other": "1"},
                "dsh": {"profile": {"bundles": ["other"]}}, "custom": {"keep": True}}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    calls = []

    def native_cli(args, **kwargs):
        assert args[:4] == ["fake-dsh", "plugin", "--profile", "headless"]
        assert kwargs == {"check": True, "shell": False}
        calls.append(args)
        data = json.loads(path.read_text())
        if args[4] == "add":
            assert args[5] == dsh.BUNDLE + "@0.1.2"
            data["dependencies"][dsh.BUNDLE] = "0.1.2"
            data["dsh"]["profile"]["bundles"].append(dsh.BUNDLE)
        else:
            data["dependencies"].pop(dsh.BUNDLE, None)
            data["dsh"]["profile"]["bundles"].remove(dsh.BUNDLE)
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(dsh.shutil, "which", lambda _: "fake-dsh")
    monkeypatch.setattr(dsh.subprocess, "run", native_cli)
    return path, calls


def test_native_install_reinstall_and_scoped_uninstall(profile, tmp_path):
    path, calls = profile
    result = skills.install_platform_configs(tmp_path, target="dsh", dsh_profile="headless")
    assert result and len(calls) == 1
    before = path.read_bytes()
    skills.install_platform_configs(tmp_path, target="dsh", dsh_profile="headless")
    assert path.read_bytes() == before and len(calls) == 1
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    report = uninstall.run(repo=repo, platforms=["dsh"], dsh_profile="headless")
    assert not report.errors and len(calls) == 2
    data = json.loads(path.read_text())
    assert data["dependencies"] == {"other": "1"}
    assert data["dsh"]["profile"]["bundles"] == ["other"]
    assert data["custom"] == {"keep": True}
    assert not dsh.configure(path, install=False)
    assert len(calls) == 2


def test_dry_run_and_desktop_ownership(profile, tmp_path):
    path, calls = profile
    assert dsh.configure(path, install=True, dry_run=True)
    assert not dsh.configure(dsh.config_path(tmp_path, "desktop"), install=True)
    assert not calls


@pytest.mark.parametrize("bad", ["../desktop", "x/y", "x;echo", "", ".."])
def test_invalid_profile_is_rejected(bad, tmp_path):
    with pytest.raises(ValueError):
        dsh.config_path(tmp_path, bad)


def test_malformed_profile_is_preserved(profile):
    path, calls = profile
    path.write_text("{invalid", encoding="utf-8")
    assert not dsh.configure(path, install=True)
    assert path.read_text() == "{invalid" and not calls

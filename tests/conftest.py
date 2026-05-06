import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAKE_GH_DIR = Path(__file__).resolve().parent / "fake_gh"


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@pytest.fixture
def git_sandbox(tmp_path, monkeypatch):
    """Bare remote + clone with branches main -> a -> b -> c."""
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)

    (clone / "f").write_text("0\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "0")
    _git(clone, "push", "-u", "origin", "main")

    for name in ("a", "b", "c"):
        _git(clone, "checkout", "-b", name)
        (clone / "f").write_text(f"{name}\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-m", name)
        _git(clone, "push", "-u", "origin", name)

    _git(clone, "checkout", "main")
    monkeypatch.chdir(clone)
    return clone


@pytest.fixture
def fake_gh_on_path(monkeypatch, tmp_path):
    fixture_dir = tmp_path / "ghfix"
    fixture_dir.mkdir()
    monkeypatch.setenv("PATH", f"{FAKE_GH_DIR}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PR_TEST_FIXTURE_DIR", str(fixture_dir))
    monkeypatch.setenv("PR_TEST_REPO", "test/repo")
    monkeypatch.setenv("PR_TEST_DEFAULT_BRANCH", "main")
    return fixture_dir


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    p = tmp_path / "pr.json"
    monkeypatch.setattr("pr.STATE_PATH", p)
    return p

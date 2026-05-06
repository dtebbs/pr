import os
import subprocess

import pytest

import pr


def _commit_on(cwd, branch, filename, content):
    subprocess.run(["git", "checkout", branch], cwd=cwd, check=True, capture_output=True)
    (cwd / filename).write_text(content)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "."], cwd=cwd, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "x"], cwd=cwd, check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "origin", branch], cwd=cwd, check=True, capture_output=True, env=env)


def test_needs_rebase_detects_advanced_dep(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "feat"])
    _commit_on(git_sandbox, "feat", "feat.txt", "feat\n")

    assert pr.needs_rebase("feat", "main", "main") == "ok"

    _commit_on(git_sandbox, "main", "main2.txt", "main2\n")

    assert pr.needs_rebase("feat", "main", "main") == "needs-rebase"


def test_pr_rebase_invokes_git_rebase(git_sandbox, fake_gh_on_path, isolated_state, monkeypatch):
    pr.main(["branch", "feat"])
    _commit_on(git_sandbox, "feat", "feat.txt", "feat\n")
    _commit_on(git_sandbox, "main", "main2.txt", "main2\n")

    subprocess.run(["git", "checkout", "feat"], cwd=git_sandbox, check=True, capture_output=True)
    monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "true")
    monkeypatch.setenv("GIT_EDITOR", "true")

    pr.main(["rebase"])

    assert pr.needs_rebase("feat", "main", "main") == "ok"


def test_pr_rebase_exits_cleanly_when_git_rebase_fails(
    git_sandbox, fake_gh_on_path, isolated_state, monkeypatch
):
    pr.main(["branch", "feat"])
    _commit_on(git_sandbox, "feat", "feat.txt", "feat\n")
    _commit_on(git_sandbox, "main", "main2.txt", "main2\n")

    subprocess.run(["git", "checkout", "feat"], cwd=git_sandbox, check=True, capture_output=True)
    (git_sandbox / "feat.txt").write_text("dirty unstaged change\n")
    monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "true")
    monkeypatch.setenv("GIT_EDITOR", "true")

    with pytest.raises(SystemExit) as exc:
        pr.main(["rebase"])

    assert exc.value.code not in (0, None)

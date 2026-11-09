import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import pr

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _key(*argv):
    return hashlib.sha256(json.dumps(list(argv)).encode()).hexdigest()[:16]


def _fixture(fixture_dir, argv, stdout="", exit_code=0):
    path = fixture_dir / f"{_key(*argv)}.json"
    path.write_text(json.dumps({"stdout": stdout, "stderr": "", "exit": exit_code}))


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


def _branches(state_path):
    return json.loads(state_path.read_text())["trees"][pr.current_tree()]["branches"]


def _entry(pr_num, dep=None, status="open"):
    return {"pr": pr_num, "depends_on": dep, "status": status, "closed_at": None}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def _commit(cwd, name):
    (cwd / name).write_text(name + "\n")
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", f"add {name}")
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def stacked(git_sandbox, fake_gh_on_path, isolated_state):
    """On branch b (PR #7, dep a with PR #5) with two extra commits: b2 then b3."""
    _git(git_sandbox, "checkout", "b")
    mid = _commit(git_sandbox, "b2")
    _commit(git_sandbox, "b3")
    _seed(isolated_state, {"a": _entry(5), "b": _entry(7, dep="a")})
    return SimpleNamespace(cwd=git_sandbox, fixtures=fake_gh_on_path, state=isolated_state, mid=mid)


def _register_split_fixtures(fixtures, title, draft=True, base="a", old_title="[dep #5] B title"):
    create = ["pr", "create", "--head", "b-part1", "--base", base, "--title", title, "--body", ""]
    if draft:
        create.append("--draft")
    _fixture(fixtures, create, stdout="https://github.com/test/repo/pull/9\n")
    _fixture(fixtures, ["pr", "edit", "7", "--base", "b-part1"])
    _fixture(fixtures, ["pr", "view", "7", "--json", "title", "-q", ".title"], stdout=old_title + "\n")
    _fixture(fixtures, ["pr", "edit", "7", "--title", "[dep #9] B title"])


def test_split_creates_branch_pr_and_rewires_deps(stacked, capsys):
    _register_split_fixtures(stacked.fixtures, "[dep #5] add b2")

    pr.main(["split", stacked.mid[:8], "b-part1"])

    assert _git(stacked.cwd, "rev-parse", "b-part1") == stacked.mid
    assert _git(stacked.cwd, "rev-parse", "origin/b-part1") == stacked.mid
    assert _git(stacked.cwd, "symbolic-ref", "--short", "HEAD") == "b"
    branches = _branches(stacked.state)
    assert branches["b-part1"] == {"pr": 9, "depends_on": "a", "status": "open", "draft": True}
    assert branches["b"]["depends_on"] == "b-part1" and branches["b"]["pr"] == 7
    out = capsys.readouterr().out
    assert "created PR #9 for b-part1: [dep #5] add b2" in out
    assert "PR #7 now depends on b-part1 (#9)" in out


def test_split_with_message_and_ready(stacked):
    _register_split_fixtures(stacked.fixtures, "[dep #5] First half", draft=False)

    pr.main(["split", "HEAD~1", "b-part1", "-m", "First half", "--ready"])

    assert _branches(stacked.state)["b-part1"]["draft"] is False


def test_split_on_default_branch_dep_has_no_prefix(stacked):
    _seed(stacked.state, {"b": _entry(7)})
    _register_split_fixtures(stacked.fixtures, "add b2", base="main", old_title="B title")

    pr.main(["split", stacked.mid, "b-part1"])

    assert _branches(stacked.state)["b-part1"]["depends_on"] is None


@pytest.mark.parametrize("commit, reason", [
    ("a", "already in a"),
    ("c", "not on branch b"),
    ("HEAD", "tip of b"),
    ("nope", "unknown commit"),
])
def test_split_rejects_commits_outside_the_pr(stacked, capsys, commit, reason):
    with pytest.raises(SystemExit):
        pr.main(["split", commit, "b-part1"])

    assert reason in capsys.readouterr().err
    assert subprocess.run(["git", "rev-parse", "--verify", "--quiet", "b-part1"], cwd=stacked.cwd).returncode != 0


def test_split_requires_an_open_pr(stacked, capsys):
    _seed(stacked.state, {"a": _entry(5), "b": _entry(None, dep="a")})

    with pytest.raises(SystemExit):
        pr.main(["split", stacked.mid, "b-part1"])

    assert "has no PR" in capsys.readouterr().err


def test_split_refuses_tracked_or_existing_branch_name(stacked, capsys):
    _seed(stacked.state, {"a": _entry(5), "b": _entry(7, dep="a"), "b-part1": _entry(None)})
    with pytest.raises(SystemExit):
        pr.main(["split", stacked.mid, "b-part1"])
    assert "already tracked" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        pr.main(["split", stacked.mid, "c"])
    assert "already exists" in capsys.readouterr().err


def test_split_requires_dep_with_open_pr(stacked, capsys):
    _seed(stacked.state, {"a": _entry(5, status="merged"), "b": _entry(7, dep="a")})

    with pytest.raises(SystemExit):
        pr.main(["split", stacked.mid, "b-part1"])

    assert "has no open PR" in capsys.readouterr().err

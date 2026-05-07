import hashlib
import json
import subprocess

import pytest

import pr


def _checkout(cwd, branch):
    subprocess.run(["git", "checkout", branch], cwd=cwd, check=True, capture_output=True)


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


def _entry(pr_num, dep=None, status="open"):
    return {"pr": pr_num, "depends_on": dep, "status": status, "closed_at": None}


def test_target_sets_dep_on_branch_without_pr(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "wip"])  # entry exists but pr is None
    _seed(isolated_state, {
        "wip": _entry(None),
        "a": _entry(5),
    })

    pr.main(["target", "a"])  # no gh fixtures: must not call gh

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["wip"]["depends_on"] == "a"
    assert branches["wip"]["pr"] is None


def test_target_main_clears_dep_on_branch_without_pr(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "wip"])
    _seed(isolated_state, {"wip": _entry(None, dep="a"), "a": _entry(5)})

    pr.main(["target", "--main"])

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["wip"]["depends_on"] is None


def test_target_creates_entry_when_branch_has_no_state(git_sandbox, fake_gh_on_path, isolated_state):
    subprocess.run(["git", "checkout", "-b", "raw"], cwd=git_sandbox, check=True, capture_output=True)
    _seed(isolated_state, {"a": _entry(5)})

    pr.main(["target", "a"])

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["raw"] == {"pr": None, "depends_on": "a", "status": "no-pr", "closed_at": None}


def test_target_dies_if_dep_branch_has_no_open_pr(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "wip"])
    _seed(isolated_state, {"wip": _entry(None), "a": _entry(5, status="merged")})

    with pytest.raises(SystemExit):
        pr.main(["target", "a"])


def test_target_dies_if_current_pr_is_closed(git_sandbox, fake_gh_on_path, isolated_state):
    _checkout(git_sandbox, "a")
    _seed(isolated_state, {
        "a": _entry(7, status="closed"),
        "b": _entry(5),
    })

    with pytest.raises(SystemExit):
        pr.main(["target", "b"])


def test_target_default_branch_by_name_clears_dep(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "wip"])  # currently checked out on `wip`
    _seed(isolated_state, {"wip": _entry(None, dep="a"), "a": _entry(5)})

    pr.main(["target", "main"])  # `main` is the default branch — should clear dep

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["wip"]["depends_on"] is None

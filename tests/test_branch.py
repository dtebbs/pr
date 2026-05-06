import json
import subprocess

import pytest

import pr


def _branches(cwd):
    out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                         cwd=cwd, check=True, capture_output=True, text=True).stdout
    return out.split()


def test_pr_branch_creates_local_and_state(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "foo"])

    assert "foo" in _branches(git_sandbox)
    state = json.loads(isolated_state.read_text())
    tree_key = pr.current_tree()
    entry = state["trees"][tree_key]["branches"]["foo"]
    assert entry == {"pr": None, "depends_on": "main", "status": "no-pr", "closed_at": None}


def test_pr_branch_main_clears_dep(git_sandbox, fake_gh_on_path, isolated_state):
    subprocess.run(["git", "checkout", "a"], cwd=git_sandbox, check=True, capture_output=True)
    pr.main(["branch", "foo", "--main"])

    state = json.loads(isolated_state.read_text())
    tree_key = pr.current_tree()
    entry = state["trees"][tree_key]["branches"]["foo"]
    assert entry["depends_on"] is None


def test_pr_branch_chains_from_current(git_sandbox, fake_gh_on_path, isolated_state):
    subprocess.run(["git", "checkout", "b"], cwd=git_sandbox, check=True, capture_output=True)
    pr.main(["branch", "d"])

    state = json.loads(isolated_state.read_text())
    tree_key = pr.current_tree()
    assert state["trees"][tree_key]["branches"]["d"]["depends_on"] == "b"
    assert "d" in _branches(git_sandbox)


def test_pr_branch_refuses_existing_state_entry(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "foo"])
    with pytest.raises(SystemExit):
        pr.main(["branch", "foo"])

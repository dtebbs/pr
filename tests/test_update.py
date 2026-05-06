import hashlib
import json
import subprocess

import pytest

import pr


def _key(*argv):
    return hashlib.sha256(json.dumps(list(argv)).encode()).hexdigest()[:16]


def _fixture(fixture_dir, argv, stdout="", exit_code=0):
    path = fixture_dir / f"{_key(*argv)}.json"
    path.write_text(json.dumps({"stdout": stdout, "stderr": "", "exit": exit_code}))


def _view_title_argv(pr_num):
    return ["pr", "view", str(pr_num), "--json", "title", "-q", ".title"]


def _edit_title_argv(pr_num, title):
    return ["pr", "edit", str(pr_num), "--title", title]


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


def _entry(pr_num, dep=None, status="open"):
    return {"pr": pr_num, "depends_on": dep, "status": status, "closed_at": None}


def _checkout(cwd, branch):
    subprocess.run(["git", "checkout", branch], cwd=cwd, check=True, capture_output=True)


def test_pr_update_adds_prefix_for_stacked_pr(git_sandbox, fake_gh_on_path, isolated_state):
    _checkout(git_sandbox, "b")
    _seed(isolated_state, {
        "a": _entry(5),
        "b": _entry(6, dep="a"),
    })
    _fixture(fake_gh_on_path, _view_title_argv(6), stdout="feat: foo\n")
    _fixture(fake_gh_on_path, _edit_title_argv(6, "[dep #5] feat: foo"))

    pr.main(["update"])  # missing edit fixture would crash fake_gh


def test_pr_update_strips_prefix_when_targeting_default(git_sandbox, fake_gh_on_path, isolated_state):
    _checkout(git_sandbox, "a")
    _seed(isolated_state, {"a": _entry(6, dep=None)})
    _fixture(fake_gh_on_path, _view_title_argv(6), stdout="[dep #5] feat: foo\n")
    _fixture(fake_gh_on_path, _edit_title_argv(6, "feat: foo"))

    pr.main(["update"])


def test_pr_update_replaces_stale_prefix(git_sandbox, fake_gh_on_path, isolated_state):
    _checkout(git_sandbox, "b")
    _seed(isolated_state, {
        "a": _entry(7),
        "b": _entry(6, dep="a"),
    })
    _fixture(fake_gh_on_path, _view_title_argv(6), stdout="[dep #5] feat: foo\n")
    _fixture(fake_gh_on_path, _edit_title_argv(6, "[dep #7] feat: foo"))

    pr.main(["update"])


def test_pr_update_noop_when_title_correct(git_sandbox, fake_gh_on_path, isolated_state, capsys):
    _checkout(git_sandbox, "b")
    _seed(isolated_state, {
        "a": _entry(5),
        "b": _entry(6, dep="a"),
    })
    _fixture(fake_gh_on_path, _view_title_argv(6), stdout="[dep #5] feat: foo\n")
    # Deliberately no edit fixture — fake_gh would fail if edit were called.

    pr.main(["update"])

    assert "already correct" in capsys.readouterr().out


def test_pr_update_dies_when_branch_has_no_pr(git_sandbox, fake_gh_on_path, isolated_state):
    pr.main(["branch", "feat"])  # creates entry with pr=None
    with pytest.raises(SystemExit):
        pr.main(["update"])


def test_pr_update_dies_when_no_state_entry(git_sandbox, fake_gh_on_path, isolated_state):
    # On main, no state entry recorded.
    with pytest.raises(SystemExit):
        pr.main(["update"])


def test_pr_update_dies_when_dep_has_no_open_pr(git_sandbox, fake_gh_on_path, isolated_state):
    _checkout(git_sandbox, "b")
    _seed(isolated_state, {
        "a": _entry(5, status="merged"),
        "b": _entry(6, dep="a"),
    })
    with pytest.raises(SystemExit):
        pr.main(["update"])

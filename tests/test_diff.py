import json
import subprocess

import pytest

import pr


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


@pytest.fixture
def recorded_git_diff(monkeypatch):
    """Record `git diff` invocations while still running them for real."""
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
            calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(pr.subprocess, "run", spy)
    return calls


def test_diff_uses_tracked_dep_as_base(git_sandbox, isolated_state, recorded_git_diff):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})
    subprocess.run(["git", "checkout", "b"], cwd=git_sandbox, check=True, capture_output=True)

    pr.main(["diff"])

    assert recorded_git_diff == [["git", "diff", "--no-ext-diff", "origin/a...b"]]


def test_diff_untracked_branch_falls_back_to_default(
    git_sandbox, fake_gh_on_path, isolated_state, recorded_git_diff
):
    subprocess.run(["git", "checkout", "c"], cwd=git_sandbox, check=True, capture_output=True)

    pr.main(["diff"])

    assert recorded_git_diff == [["git", "diff", "--no-ext-diff", "origin/main...c"]]


def test_diff_named_branch_and_passthrough_flags(git_sandbox, isolated_state, recorded_git_diff):
    _seed(isolated_state, {"c": {"pr": 9, "depends_on": "b", "status": "open"}})

    pr.main(["diff", "c", "--", "--stat", "--", "f"])

    assert recorded_git_diff == [["git", "diff", "--no-ext-diff", "origin/b...c", "--stat", "--", "f"]]


def test_diff_propagates_git_failure(git_sandbox, isolated_state, recorded_git_diff, capfd):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})

    with pytest.raises(SystemExit) as exc:
        pr.main(["diff", "b", "--", "--no-such-flag"])

    assert exc.value.code not in (0, None)


def test_passthrough_rejected_for_other_commands(git_sandbox, isolated_state, capsys):
    with pytest.raises(SystemExit) as exc:
        pr.main(["show", "--", "--stat"])

    assert exc.value.code not in (0, None)
    assert "only supported by `pr diff`" in capsys.readouterr().err

import json
import os
import subprocess

import pytest

import pr


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


def _short(cwd, ref):
    return subprocess.run(
        ["git", "rev-parse", "--short", ref], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd, subject):
    (cwd / "f").write_text(subject + "\n")
    subprocess.run(["git", "commit", "-am", subject], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def fake_pbcopy(monkeypatch, tmp_path):
    """Put a `pbcopy` on PATH that writes its stdin to a file; return that file."""
    clip = tmp_path / "clipboard"
    bindir = tmp_path / "pbbin"
    bindir.mkdir()
    shim = bindir / "pbcopy"
    shim.write_text('#!/bin/sh\ncat > "$PR_TEST_CLIPBOARD"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PR_TEST_CLIPBOARD", str(clip))
    return clip


def test_commits_copies_branch_commits_to_clipboard(git_sandbox, isolated_state, fake_pbcopy, capfd):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})
    subprocess.run(["git", "checkout", "b"], cwd=git_sandbox, check=True, capture_output=True)
    _commit(git_sandbox, "feat[x]: second change")

    pr.main(["commits"])

    out, err = capfd.readouterr()
    expected = (
        f"   {_short(git_sandbox, 'b')} feat[x]: second change\n"
        f"   {_short(git_sandbox, 'b~1')} b\n"
    )
    assert out == expected
    assert err.rstrip().endswith("(copied to clipboard)")
    assert fake_pbcopy.read_text() == expected


def test_commits_no_copy_lists_without_touching_clipboard(git_sandbox, isolated_state, fake_pbcopy, capfd):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})

    pr.main(["commits", "--no-copy", "b"])

    out, err = capfd.readouterr()
    assert out == f"   {_short(git_sandbox, 'b')} b\n"
    assert "clipboard" not in err
    assert not fake_pbcopy.exists()


def test_commits_accepts_branch_name_and_pr_number(git_sandbox, isolated_state, capfd):
    _seed(isolated_state, {
        "a": {"pr": 5, "depends_on": None, "status": "open"},
        "b": {"pr": 7, "depends_on": "a", "status": "open"},
    })

    pr.main(["commits", "--no-copy", "b"])
    by_name = capfd.readouterr().out
    pr.main(["commits", "--no-copy", "7"])
    by_number = capfd.readouterr().out

    assert by_name == by_number == f"   {_short(git_sandbox, 'b')} b\n"


def test_commits_untracked_branch_falls_back_to_default(git_sandbox, fake_gh_on_path, isolated_state, capfd):
    pr.main(["commits", "--no-copy", "c"])

    assert capfd.readouterr().out == (
        f"   {_short(git_sandbox, 'c')} c\n"
        f"   {_short(git_sandbox, 'b')} b\n"
        f"   {_short(git_sandbox, 'a')} a\n"
    )


def test_commits_unknown_pr_number_dies(git_sandbox, isolated_state, capsys):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})

    with pytest.raises(SystemExit) as exc:
        pr.main(["commits", "42"])

    assert exc.value.code not in (0, None)
    assert "no tracked branch for PR #42" in capsys.readouterr().err


def test_commits_unknown_branch_dies(git_sandbox, isolated_state, capsys):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})

    with pytest.raises(SystemExit) as exc:
        pr.main(["commits", "--no-copy", "nope"])

    assert exc.value.code not in (0, None)


def test_commits_missing_pbcopy_dies(git_sandbox, isolated_state, monkeypatch, capsys):
    _seed(isolated_state, {"b": {"pr": 7, "depends_on": "a", "status": "open"}})
    real = subprocess.run

    def no_pbcopy(cmd, *a, **kw):
        if cmd == ["pbcopy"]:
            raise FileNotFoundError("pbcopy")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(pr.subprocess, "run", no_pbcopy)

    with pytest.raises(SystemExit) as exc:
        pr.main(["commits", "b"])

    assert exc.value.code not in (0, None)
    out, err = capsys.readouterr()
    assert out == f"   {_short(git_sandbox, 'b')} b\n"
    assert "pbcopy not found; pass --no-copy" in err

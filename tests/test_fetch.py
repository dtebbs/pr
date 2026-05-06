import hashlib
import json

import pr


LIST_FIELDS = "number,headRefName,baseRefName,state,closedAt,author,title,statusCheckRollup"
VIEW_FIELDS = "state,baseRefName,closedAt,title,statusCheckRollup"


def _key(*argv):
    return hashlib.sha256(json.dumps(list(argv)).encode()).hexdigest()[:16]


def _write_json_fixture(fixture_dir, argv, payload):
    path = fixture_dir / f"{_key(*argv)}.json"
    path.write_text(json.dumps({"stdout": json.dumps(payload), "stderr": "", "exit": 0}))


def _write_text_fixture(fixture_dir, argv, text):
    path = fixture_dir / f"{_key(*argv)}.json"
    path.write_text(json.dumps({"stdout": text, "stderr": "", "exit": 0}))


def _list_argv():
    return ["pr", "list", "--state", "open", "--limit", "1000", "--json", LIST_FIELDS]


def _view_argv(pr_num):
    return ["pr", "view", str(pr_num), "--json", VIEW_FIELDS]


def _user_argv():
    return ["api", "user", "--jq", ".login"]


def _set_login(fixture_dir, login):
    _write_text_fixture(fixture_dir, _user_argv(), login + "\n")


def _pr(*, number, head, base="main", author="someone", state="OPEN", closed_at=None, title="t", checks=None):
    return {
        "number": number,
        "headRefName": head,
        "baseRefName": base,
        "state": state,
        "closedAt": closed_at,
        "author": {"login": author},
        "title": title,
        "statusCheckRollup": checks if checks is not None else [],
    }


def _view_payload(*, base="main", state="OPEN", closed_at=None, title="t", checks=None):
    return {
        "state": state,
        "baseRefName": base,
        "closedAt": closed_at,
        "title": title,
        "statusCheckRollup": checks if checks is not None else [],
    }


def _wire_views(fixture_dir, prs):
    for p in prs:
        _write_json_fixture(
            fixture_dir, _view_argv(p["number"]),
            _view_payload(base=p["baseRefName"], title=p["title"], checks=p["statusCheckRollup"]),
        )


def test_fetch_discovers_all_open_prs(git_sandbox, fake_gh_on_path, isolated_state):
    _set_login(fake_gh_on_path, "alice")
    prs = [
        _pr(number=1, head="alice/foo", author="alice"),
        _pr(number=2, head="bob/bar", author="bob"),
        _pr(number=3, head="carol/baz", author="carol", base="bob/bar"),
    ]
    _write_json_fixture(fake_gh_on_path, _list_argv(), prs)
    _wire_views(fake_gh_on_path, prs)

    pr.main(["fetch"])

    state = json.loads(isolated_state.read_text())
    branches = state["trees"][pr.current_tree()]["branches"]
    assert set(branches.keys()) == {"alice/foo", "bob/bar", "carol/baz"}
    assert branches["alice/foo"]["depends_on"] is None
    assert branches["bob/bar"]["depends_on"] is None
    assert branches["carol/baz"]["depends_on"] == "bob/bar"


def test_fetch_marks_external_correctly(git_sandbox, fake_gh_on_path, isolated_state):
    _set_login(fake_gh_on_path, "alice")
    prs = [
        _pr(number=10, head="mine", author="alice"),
        _pr(number=11, head="theirs", author="bob"),
    ]
    _write_json_fixture(fake_gh_on_path, _list_argv(), prs)
    _wire_views(fake_gh_on_path, prs)

    pr.main(["fetch"])

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["mine"]["external"] is False
    assert branches["theirs"]["external"] is True


def test_fetch_preserves_local_no_pr_entries(git_sandbox, fake_gh_on_path, isolated_state):
    _set_login(fake_gh_on_path, "alice")
    pr.main(["branch", "wip"])
    _write_json_fixture(fake_gh_on_path, _list_argv(), [])

    pr.main(["fetch"])

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert "wip" in branches
    assert branches["wip"]["pr"] is None
    assert branches["wip"]["status"] == "no-pr"


def test_fetch_persists_ci_status(git_sandbox, fake_gh_on_path, isolated_state):
    _set_login(fake_gh_on_path, "alice")
    prs = [
        _pr(number=20, head="green", author="alice",
            checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}]),
        _pr(number=21, head="broken", author="alice",
            checks=[{"status": "COMPLETED", "conclusion": "FAILURE"}]),
        _pr(number=22, head="running", author="alice",
            checks=[{"status": "IN_PROGRESS", "conclusion": None}]),
        _pr(number=23, head="empty", author="alice", checks=[]),
    ]
    _write_json_fixture(fake_gh_on_path, _list_argv(), prs)
    _wire_views(fake_gh_on_path, prs)

    pr.main(["fetch"])

    branches = json.loads(isolated_state.read_text())["trees"][pr.current_tree()]["branches"]
    assert branches["green"]["ci"] == "pass"
    assert branches["broken"]["ci"] == "fail"
    assert branches["running"]["ci"] == "pending"
    assert branches["empty"]["ci"] == "none"

import pytest

import pr


def _view_response(*, state="OPEN", merge_state="CLEAN", checks=None):
    return {
        "state": state,
        "statusCheckRollup": checks if checks is not None else [],
        "mergeStateStatus": merge_state,
    }


def _passing():
    return [{"status": "COMPLETED", "conclusion": "SUCCESS"}]


def _failing():
    return [{"status": "COMPLETED", "conclusion": "FAILURE"}]


def _pending():
    return [{"status": "IN_PROGRESS", "conclusion": None}]


def _make_gh_json(view_responses, *, list_response=None):
    """Return a fake gh_json that yields list_response for `pr list` calls,
    and successive view_responses for `pr view` calls."""
    list_response = list_response if list_response is not None else [{"number": 42}]
    counter = {"view": 0, "list": 0}

    def fake(argv, fields):
        if argv[:2] == ["pr", "list"]:
            counter["list"] += 1
            return list_response
        if argv[:2] == ["pr", "view"]:
            idx = counter["view"]
            counter["view"] += 1
            return view_responses[idx]
        raise AssertionError(f"unexpected gh call: {argv!r}")

    return fake, counter


def test_automerge_merges_when_ci_passes(monkeypatch):
    fake, counter = _make_gh_json([_view_response(checks=_passing())])
    gh_calls = []
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: gh_calls.append(a) or "")
    monkeypatch.setattr(pr.time, "sleep", lambda s: pytest.fail("should not sleep when CI already passes"))

    pr.main(["automerge", "feat"])

    assert any("merge" in c and "--merge" in c for c in gh_calls)
    assert counter["list"] == 1


def test_automerge_merges_when_no_checks(monkeypatch):
    fake, _ = _make_gh_json([_view_response(checks=[])])
    gh_calls = []
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: gh_calls.append(a) or "")
    monkeypatch.setattr(pr.time, "sleep", lambda s: pytest.fail("should not sleep when no checks"))

    pr.main(["automerge", "feat"])

    assert any("merge" in c for c in gh_calls)


def test_automerge_errors_when_ci_failing(monkeypatch):
    fake, _ = _make_gh_json([_view_response(checks=_failing())])
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: pytest.fail("should not merge on failing CI"))

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_errors_when_behind(monkeypatch):
    fake, _ = _make_gh_json([_view_response(merge_state="BEHIND", checks=_passing())])
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: pytest.fail("should not merge when behind"))

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_errors_when_dirty(monkeypatch):
    fake, _ = _make_gh_json([_view_response(merge_state="DIRTY", checks=_passing())])
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: pytest.fail("should not merge on conflicts"))

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_errors_when_no_pr_for_branch(monkeypatch):
    fake, _ = _make_gh_json([], list_response=[])
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: pytest.fail("should not merge"))

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_loops_until_ci_passes(monkeypatch):
    views = [
        _view_response(checks=_pending()),
        _view_response(checks=_pending()),
        _view_response(checks=_passing()),
    ]
    fake, counter = _make_gh_json(views)
    sleeps = []
    gh_calls = []
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: gh_calls.append(a) or "")
    monkeypatch.setattr(pr.time, "sleep", lambda s: sleeps.append(s))

    pr.main(["automerge", "feat"])

    assert counter["list"] == 1, "should only resolve PR once at startup"
    assert counter["view"] == 3
    assert sleeps == [pr._AUTOMERGE_POLL_SECONDS, pr._AUTOMERGE_POLL_SECONDS]
    assert any("merge" in c for c in gh_calls)


def test_automerge_errors_when_pr_not_open(monkeypatch):
    fake, _ = _make_gh_json([_view_response(state="CLOSED", checks=_passing())])
    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: pytest.fail("should not merge closed PR"))

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_defaults_to_current_branch(monkeypatch):
    seen_branches = []

    def fake(argv, fields):
        if argv[:2] == ["pr", "list"]:
            head_idx = argv.index("--head") + 1
            seen_branches.append(argv[head_idx])
            return [{"number": 42}]
        return _view_response(checks=_passing())

    monkeypatch.setattr(pr, "gh_json", fake)
    monkeypatch.setattr(pr, "gh", lambda *a, **kw: "")
    monkeypatch.setattr(pr, "current_branch", lambda: "my-current")

    pr.main(["automerge"])

    assert seen_branches == ["my-current"]

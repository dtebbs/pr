import json

import pytest

import pr


def _seed(state_path, branches):
    state_path.write_text(json.dumps({
        "version": pr.STATE_VERSION,
        "trees": {pr.current_tree(): {"branches": branches}},
    }))


def _branches(state_path):
    return json.loads(state_path.read_text())["trees"][pr.current_tree()]["branches"]


REVIEW = "- f:1 something is wrong\n\nChecked and clean: everything else"

EVENTS = [
    {"type": "system", "subtype": "init", "model": "claude-test", "permissionMode": "plan"},
    {"type": "assistant", "message": {
        "id": "m1",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000, "output_tokens": 5},
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                     "input": {"command": "git diff --no-ext-diff origin/a...origin/b"}}],
    }},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "diff"}]}},
    {"type": "assistant", "message": {
        "id": "m2",
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50, "output_tokens": 7},
        "content": [{"type": "text", "text": REVIEW}],
    }},
]

RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "result": REVIEW, "total_cost_usd": 0.46,
    "modelUsage": {"claude-test": {"inputTokens": 30, "outputTokens": 811, "cacheReadInputTokens": 1000,
                                   "cacheCreationInputTokens": 1050, "costUSD": 0.46}},
    "permission_denials": [],
}


@pytest.fixture
def tracked_b(git_sandbox, isolated_state):
    _seed(isolated_state, {"b": {"pr": None, "depends_on": "a", "status": "no-pr"}})
    return isolated_state


def test_review_streams_progress_and_stores_review(tracked_b, fake_claude_on_path, capfd):
    log = fake_claude_on_path(EVENTS + [RESULT])

    pr.main(["review", "b"])

    out, err = capfd.readouterr()
    assert out.strip() == REVIEW
    assert "reviewing branch `b` vs base `origin/a`" in err
    assert "review done in " in err and "2 api calls, 1 tool calls, 1 agents" in err and "cost $0.46" in err
    assert "out 811" in err
    assert _branches(tracked_b)["b"]["last_review"] == REVIEW
    recorded = json.loads(log.read_text())
    argv = recorded["argv"]
    assert argv[:2] == ["-p", "--output-format"] and "stream-json" in argv and "--verbose" in argv
    assert "--permission-mode" in argv and "plan" in argv
    assert recorded["prompt"].splitlines()[0] == "/code-review medium origin/b --max-findings all"
    assert "git diff --no-ext-diff origin/a...origin/b" in recorded["prompt"]


def test_review_verbose_logs_events(tracked_b, fake_claude_on_path, capfd):
    fake_claude_on_path(EVENTS + [RESULT])

    pr.main(["review", "-v", "b"])

    out, err = capfd.readouterr()
    assert out.strip() == REVIEW
    assert err.count("tool#1  Bash: git diff --no-ext-diff origin/a...origin/b") == 1
    assert "agent start  general-purpose  (1 running)" in err
    assert "api request #1" in err and "model responding  (first byte after 0.1s)" in err
    assert "api#2  ctx 1.1k  in 2.1k" in err
    assert "result success  cost $0.46" in err


def test_review_level_is_passed_to_skill(tracked_b, fake_claude_on_path):
    log = fake_claude_on_path(EVENTS + [RESULT])

    pr.main(["review", "--level", "low", "b"])

    assert json.loads(log.read_text())["prompt"].startswith("/code-review low origin/b ")


def test_review_nonzero_exit_is_propagated_and_nothing_stored(tracked_b, fake_claude_on_path, capfd):
    fake_claude_on_path(EVENTS + [RESULT], exit_code=3)

    with pytest.raises(SystemExit) as exc:
        pr.main(["review", "b"])

    assert exc.value.code == 3
    assert "claude exited with code 3" in capfd.readouterr().err
    assert "last_review" not in _branches(tracked_b)["b"]


def test_review_error_result_is_not_stored(tracked_b, fake_claude_on_path, capfd):
    fake_claude_on_path(EVENTS + [{**RESULT, "is_error": True, "result": "budget exceeded"}])

    with pytest.raises(SystemExit) as exc:
        pr.main(["review", "b"])

    assert exc.value.code not in (0, None)
    assert "claude review failed: is_error: budget exceeded" in capfd.readouterr().err
    assert "last_review" not in _branches(tracked_b)["b"]


def test_review_without_result_event_fails(tracked_b, fake_claude_on_path, capfd):
    fake_claude_on_path(EVENTS)

    with pytest.raises(SystemExit) as exc:
        pr.main(["review", "b"])

    assert exc.value.code not in (0, None)
    err = capfd.readouterr().err
    assert "review ended in " in err and "without a result" in err
    assert "stream ended without a result event" in err
    assert "last_review" not in _branches(tracked_b)["b"]

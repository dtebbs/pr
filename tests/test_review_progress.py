import io
import json
import os
import shlex
import subprocess

import pytest

import pr


def _assistant(msg_id, usage=None, content=None):
    return {"type": "assistant", "message": {"id": msg_id, "usage": usage or {}, "content": content or []}}


def _bash(command):
    return {"type": "tool_use", "name": "Bash", "input": {"command": command, "description": "run it"}}


USAGE = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200, "output_tokens": 7}


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_fmt_duration():
    assert pr._fmt_duration(0) == "0:00"
    assert pr._fmt_duration(83) == "1:23"
    assert pr._fmt_duration(3725) == "1:02:05"


def test_fmt_tokens():
    assert pr._fmt_tokens(842) == "842"
    assert pr._fmt_tokens(12345) == "12.3k"
    assert pr._fmt_tokens(1_234_567) == "1.2M"


def test_tool_label_prefers_command_and_truncates():
    assert pr._tool_label(_bash("gh pr diff 1234")) == "Bash: gh pr diff 1234"
    label = pr._tool_label(_bash("x" * 200))
    assert label.startswith("Bash: ") and len(label) == 48 and label.endswith("…")
    assert pr._tool_label({"name": "Read", "input": {"file_path": "pr.py"}}) == "Read: pr.py"
    assert pr._tool_label({"name": "Task", "input": {"description": "review  hunk\n3"}}) == "Task: review hunk 3"
    assert pr._tool_label({"name": "Glob", "input": {}}) == "Glob"


def test_decode_event_skips_garbage():
    assert pr._decode_event("{bad") is None
    assert pr._decode_event("") is None
    assert pr._decode_event("[1, 2]") is None
    assert pr._decode_event('{"type": "x"}\n') == {"type": "x"}


def test_assistant_event_counts_call_and_tokens():
    p = pr.ReviewProgress(clock=_Clock())
    lines = p.handle({"type": "system", "subtype": "init", "model": "m", "permissionMode": "plan"})
    assert lines == ["0:00  init  model=m  mode=plan"]
    lines = p.handle(_assistant("m1", USAGE, [_bash("gh pr diff 1")]))
    assert p.api_calls == 1
    assert p.tokens_in == 1210
    assert p.context_tokens == 1210
    assert p.output_tokens == 7
    assert p.tool_calls == 1
    assert p.last_tool == "Bash: gh pr diff 1"
    assert lines == ["0:00  api#1  ctx 1.2k  in 1.2k", "0:00  tool#1  Bash: gh pr diff 1"]


def test_same_message_id_counted_once_but_tools_still_counted():
    p = pr.ReviewProgress(clock=_Clock())
    p.handle(_assistant("m1", USAGE, [{"type": "text", "text": "hi"}]))
    p.handle(_assistant("m1", USAGE, [_bash("ls")]))
    assert p.api_calls == 1
    assert p.tokens_in == 1210
    assert p.tool_calls == 1


def test_zero_usage_event_is_not_an_api_call():
    p = pr.ReviewProgress(clock=_Clock())
    p.handle(_assistant("synthetic", {"input_tokens": 0, "output_tokens": 0}, [{"type": "text", "text": "done"}]))
    assert p.api_calls == 0


def test_task_events_only_describe():
    p = pr.ReviewProgress(clock=_Clock())
    lines = p.handle({"type": "system", "subtype": "task_started", "task_id": "t1", "description": "/code-review",
                      "subagent_type": "general-purpose", "task_type": "local_agent"})
    assert lines == ["0:00  task start  /code-review (general-purpose)"]
    assert p.status_line(200) == "0:00  starting 0:00  api 0  tools 0  agents 0  log 0  /code-review"
    assert p.handle({"type": "system", "subtype": "task_started", "task_id": "b1", "task_type": "local_bash"}) == []
    assert p.handle({"type": "system", "subtype": "task_notification", "task_id": "b1", "status": "stopped"}) == []
    lines = p.handle({"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "stopped",
                      "summary": "Reviewed"})
    assert lines == ["0:00  task stopped  /code-review: Reviewed"]
    assert p.status_line(200) == "0:00  starting 0:00  api 0  tools 0  agents 0  log 0  /code-review stopped"
    assert p.updates == 2


def test_hook_events_count_tools_and_agents_live():
    p = pr.ReviewProgress(clock=_Clock())
    lines = p.handle({"hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "general-purpose"})
    assert lines == ["0:00  agent start  general-purpose  (1 running)"]
    assert p.status_line(200) == "0:00  starting 0:00  api 0  tools 0  agents 1  log 0  agent general-purpose"
    lines = p.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_1",
                      "tool_input": {"command": "gh pr diff 42"}})
    assert lines == ["0:00  tool#1  Bash: gh pr diff 42"]
    assert p.status_line(200) == "0:00  running Bash 0:00  api 0  tools 1  agents 1  log 0  Bash: gh pr diff 42"
    # The same call replayed later in the stream is not counted twice.
    replay = _assistant("m1", USAGE, [{**_bash("gh pr diff 42"), "id": "toolu_1"}])
    assert p.handle(replay) == ["0:00  api#1  ctx 1.2k  in 1.2k"]
    assert p.tool_calls == 1
    # And a hook for a call already seen in the stream is skipped too.
    p.handle(_assistant("m2", USAGE, [{**_bash("ls"), "id": "toolu_2"}]))
    assert p.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_2",
                     "tool_input": {"command": "ls"}}) == []
    assert p.tool_calls == 2
    lines = p.handle({"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "general-purpose"})
    assert lines == ["0:00  agent stop  general-purpose  (0 running)"]
    assert p.running_agents == {} and p.agents_started == 1
    assert p.handle({"hook_event_name": "Mystery"}) == []


def test_hook_settings_install_hooks_writing_to_path(tmp_path):
    settings = json.loads(pr._hook_settings(str(tmp_path / "h.jsonl")))
    assert set(settings["hooks"]) == {"PreToolUse", "SubagentStart", "SubagentStop"}
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == ""
    argv = shlex.split(command)
    assert argv[-1] == str(tmp_path / "h.jsonl") and argv[1] == "-c"
    # The hook script itself runs and writes one trimmed line per event.
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t", "session_id": "secret",
               "tool_input": {"command": "x" * 300, "timeout": 5}}
    subprocess.run(argv, input=json.dumps(payload), text=True, check=True)
    (record,) = [json.loads(l) for l in (tmp_path / "h.jsonl").read_text().splitlines()]
    assert record["tool_name"] == "Bash" and "session_id" not in record
    assert record["tool_input"] == {"command": "x" * 200, "timeout": 5}


def test_log_lines_track_api_requests_and_phase():
    clock = _Clock()
    p = pr.ReviewProgress(clock=clock)
    assert p.handle_log_line("2026-11-01T19:53:40.890Z [DEBUG] MDM settings load completed in 0ms\n") == []
    clock.t = 2.0
    line = "2026-11-01T19:53:46.266Z [DEBUG] [API REQUEST] /v1/messages x-client-request-id=abc source=agent:custom\n"
    assert p.handle_log_line(line) == ["0:02  api request #1"]
    assert p.api_requests == 1 and p.phase == "waiting for model"
    clock.t = 3.5
    assert p.phase_text() == "waiting for model 0:01"
    line = "2026-11-01T19:53:47.491Z [DEBUG] [API:timing] first byte after 1225ms\n"
    assert p.handle_log_line(line) == ["0:03  model responding  (first byte after 1.2s)"]
    assert p.status_line(200).startswith("0:03  model responding 0:00  api 1  tools 0  agents 0  log 3")
    line = "2026-11-01T19:53:46.232Z [INFO] [Stall] tool_dispatch_end tool=Bash toolUseId=toolu_1 outcome=ok durationMs=1215\n"
    assert p.handle_log_line(line) == ["0:03  tool done  Bash in 1.2s"]
    assert p.phase == "thinking" and p.log_lines == 4 and p.updates == 3
    p.handle({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "t2", "tool_input": {"file_path": "x"}})
    assert p.phase == "running Read"
    assert p.summary().startswith("review ended in 0:03 without a result: 1 api calls, 1 tool calls")


def test_drain_lines_returns_only_complete_lines(tmp_path):
    path = tmp_path / "d.log"
    path.write_bytes(b"one\ntw")
    with open(path, "rb") as fh:
        assert pr._drain_lines(fh) == ["one\n"]
        with open(path, "ab") as w:
            w.write(b"o\n")
        assert pr._drain_lines(fh) == ["two\n"]
        assert pr._drain_lines(fh) == []


def test_drain_hook_file_returns_only_complete_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2')
    with open(path, "rb") as fh:
        assert pr._drain_hook_file(fh) == [{"a": 1}]
        with open(path, "ab") as w:
            w.write(b"}\n")
        assert pr._drain_hook_file(fh) == [{"b": 2}]
        assert pr._drain_hook_file(fh) == []


def test_tool_error_logged():
    p = pr.ReviewProgress(clock=_Clock())
    event = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}}
    assert p.handle(event) == ["0:00  tool error  boom"]
    ok = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "fine"}]}}
    assert p.handle(ok) == []


def test_unknown_and_malformed_events_ignored():
    p = pr.ReviewProgress(clock=_Clock())
    for event in ({"type": "rate_limit_event"}, {"type": "system", "subtype": "background_tasks_changed"},
                  {"type": "zzz"}, {}, {"type": "assistant", "message": None},
                  {"type": "assistant", "message": {"usage": "nope", "content": "nope"}}):
        assert p.handle(event) == []
    assert (p.api_calls, p.tool_calls, p.agents_started) == (0, 0, 0)


def test_status_line_format_and_width():
    clock = _Clock(83.0)
    p = pr.ReviewProgress(clock=clock)
    clock.t = 166.0
    p.handle(_assistant("m1", USAGE, [_bash("gh pr diff 1234")]))
    p.handle(_assistant("m2", USAGE))
    p.handle({"hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "general-purpose"})
    line = p.status_line(200)
    assert line == "1:23  starting 1:23  api 2  tools 1  agents 1  log 0  ctx 1.2k  in 2.4k  Bash: gh pr diff 1234"
    assert len(p.status_line(20)) == 20


def test_summary_sums_model_usage_and_lists_denials():
    p = pr.ReviewProgress(clock=_Clock())
    p.handle(_assistant("m1", USAGE, [_bash("ls")]))
    result = {
        "type": "result", "subtype": "success", "is_error": False, "result": "the review",
        "total_cost_usd": 0.46,
        "modelUsage": {
            "a": {"inputTokens": 100, "outputTokens": 800, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 50, "costUSD": 0.4},
            "b": {"inputTokens": 20, "outputTokens": 11, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.06},
        },
        "permission_denials": [{"tool_name": "Edit", "tool_input": {"file_path": "pr.py"}}],
    }
    assert p.handle(result) == ["0:00  result success  cost $0.46"]
    assert p.summary() == (
        "review done in 0:00: 1 api calls, 1 tool calls, 0 agents; "
        "tokens in 120, out 811, cache read 1.0k, cache write 50; cost $0.46; "
        "1 permission denial: Edit: pr.py"
    )
    assert p.review_text() == "the review"
    assert p.failure() is None


def test_summary_without_model_usage_falls_back_to_events():
    p = pr.ReviewProgress(clock=_Clock())
    p.handle(_assistant("m1", USAGE))
    assert p.summary() == (
        "review ended in 0:00 without a result: 1 api calls, 0 tool calls, 0 agents; "
        "tokens (from events) in 1.2k, out 7; cost n/a"
    )
    p.handle({"type": "result", "subtype": "success", "result": "r", "total_cost_usd": 1.5})
    assert p.summary().startswith("review done in 0:00: 1 api calls")
    assert p.summary().endswith("tokens (from events) in 1.2k, out 7; cost $1.50")


def test_failure_reasons():
    p = pr.ReviewProgress(clock=_Clock())
    assert p.failure() == "stream ended without a result event"
    p.handle({"type": "result", "subtype": "error_max_turns", "result": ""})
    assert p.failure() == "error_max_turns"
    p.handle({"type": "result", "subtype": "success", "is_error": True, "result": "budget exceeded"})
    assert p.failure() == "is_error: budget exceeded"
    assert p.review_text() == "budget exceeded"
    p.handle({"type": "result", "subtype": "success", "result": None})
    assert p.failure() is None and p.review_text() == ""


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_display_tty_rewrites_in_place():
    clock = _Clock()
    out = _FakeTTY()
    d = pr.ReviewDisplay(out=out, clock=clock)
    p = pr.ReviewProgress(clock=clock)
    d.tick(p)
    assert out.getvalue() == "\r\033[2K" + p.status_line(99)
    d.tick(p)
    assert out.getvalue().count("\r") == 1
    clock.t = 1.0
    d.tick(p)
    assert out.getvalue().count("\r") == 2
    d.finish()
    assert out.getvalue().endswith("\r\033[2K")


def test_display_non_tty_prints_on_new_event_or_timeout():
    clock = _Clock()
    out = io.StringIO()
    d = pr.ReviewDisplay(out=out, clock=clock)
    p = pr.ReviewProgress(clock=clock)
    d.tick(p)
    assert out.getvalue() == ""
    p.handle({"type": "system", "subtype": "task_started", "task_id": "t1", "description": "/code-review"})
    d.tick(p)
    assert out.getvalue() == "0:00  starting 0:00  api 0  tools 0  agents 0  log 0  /code-review\n"
    p.handle(_assistant("m1", USAGE, [_bash("ls")]))
    d.tick(p)
    assert out.getvalue().endswith(p.status_line(200) + "\n")
    out.truncate(0)
    out.seek(0)
    clock.t = 10.0
    d.tick(p)
    assert out.getvalue() == ""
    clock.t = 40.0
    d.tick(p)
    assert out.getvalue() == "0:40  starting 0:40  api 1  tools 1  agents 0  log 0  ctx 1.2k  in 1.2k  Bash: ls\n"
    d.finish()
    assert "\r" not in out.getvalue()


def test_display_verbose_logs_lines_and_heartbeats_when_quiet():
    clock = _Clock()
    out = io.StringIO()
    d = pr.ReviewDisplay(out=out, verbose=True, clock=clock)
    p = pr.ReviewProgress(clock=clock)
    d.tick(p)
    d.log(["a", "b"])
    d.log([])
    assert out.getvalue() == "a\nb\n"
    clock.t = 10.0
    d.tick(p)
    assert out.getvalue() == "a\nb\n"
    clock.t = 16.0
    d.tick(p)
    assert out.getvalue() == "a\nb\n0:16  starting 0:16  api 0  tools 0  agents 0  log 0\n"
    assert "\r" not in out.getvalue()


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.stdin = io.StringIO()
        self.returncode = returncode
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def _hook_path(cmd):
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    return shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1]


def _debug_path(cmd):
    return cmd[cmd.index("--debug-file") + 1]


def test_run_claude_stream_feeds_stream_and_hook_events(monkeypatch, tmp_path):
    lines = ['{"type":"system","subtype":"init","model":"m"}\n', "garbage\n",
             '{"type":"assistant","message":{"id":"m1","usage":{"input_tokens":5,"output_tokens":1},'
             '"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls"}}]}}\n',
             '{"type":"result","subtype":"success","result":"REVIEW"}\n']
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        hook_path = _hook_path(cmd)

        def stdout():
            with open(hook_path, "a") as f:
                f.write('{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_use_id":"toolu_1",'
                        '"tool_input":{"command":"ls"}}\n')
            with open(_debug_path(cmd), "a") as f:
                f.write("x [DEBUG] [API REQUEST] /v1/messages id=1\nx [DEBUG] noise\n")
            yield from lines

        proc = _FakeProc([], returncode=3)
        proc.stdout = stdout()
        seen["proc"] = proc
        return proc

    monkeypatch.setattr(pr.subprocess, "Popen", fake_popen)
    transcript = tmp_path / "t.jsonl"
    monkeypatch.setenv("PR_CLAUDE_TRANSCRIPT", str(transcript))
    out = io.StringIO()

    rc, progress = pr._run_claude_stream(["claude"], "the prompt", str(tmp_path), pr.ReviewDisplay(out=out, verbose=True))

    assert rc == 3
    assert seen["cmd"][:2] == ["claude", "--settings"] and seen["cmd"][3] == "--debug-file"
    assert not os.path.exists(_hook_path(seen["cmd"])) and not os.path.exists(_debug_path(seen["cmd"]))
    assert progress.model == "m" and progress.review_text() == "REVIEW"
    assert progress.tool_calls == 1 and progress.api_calls == 1
    assert progress.api_requests == 1 and progress.log_lines == 2
    assert "api request #1" in out.getvalue()
    assert seen["proc"].stdin.closed
    assert transcript.read_text() == "".join(lines)
    assert out.getvalue().count("tool#1  Bash: ls") == 1
    assert "init  model=m" in out.getvalue() and "result success" in out.getvalue()


def test_run_claude_stream_interrupt_terminates_child(monkeypatch, tmp_path):
    proc = _FakeProc(['{"type":"system","subtype":"init"}\n', '{"type":"result"}\n'])
    monkeypatch.setattr(pr.subprocess, "Popen", lambda *a, **k: proc)
    calls = []
    real = pr._decode_event

    def interrupting(line):
        calls.append(line)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(line)

    monkeypatch.setattr(pr, "_decode_event", interrupting)

    with pytest.raises(SystemExit) as exc:
        pr._run_claude_stream(["claude"], "p", str(tmp_path), pr.ReviewDisplay(out=io.StringIO()))

    assert exc.value.code == 130
    assert proc.terminated


def test_run_claude_stream_missing_binary(monkeypatch, tmp_path):
    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(pr.subprocess, "Popen", missing)
    with pytest.raises(SystemExit):
        pr._run_claude_stream(["claude"], "p", str(tmp_path), pr.ReviewDisplay(out=io.StringIO()))

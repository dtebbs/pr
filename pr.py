#!/usr/bin/env python3
"""pr — stacked-PR manager. See README in this dir."""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_PATH = Path.home() / ".pr.json"
STATE_VERSION = 1
DEP_PREFIX_RE = re.compile(r"^\[dep #\d+\] ?")


class CmdError(RuntimeError):
    pass


def die(msg: str, code: int = 1):
    print(f"pr: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip() if capture else ""
        raise CmdError(f"{' '.join(cmd)} failed (exit {proc.returncode}){': ' + stderr if stderr else ''}")
    return proc


def git(*args: str, check: bool = True, capture: bool = True) -> str:
    proc = run(["git", *args], check=check, capture=capture)
    return proc.stdout.rstrip() if capture else ""


def gh(*args: str, check: bool = True, capture: bool = True) -> str:
    proc = run(["gh", *args], check=check, capture=capture)
    return proc.stdout.rstrip() if capture else ""


def gh_json(args: list[str], fields: list[str]) -> object:
    raw = gh(*args, "--json", ",".join(fields))
    return json.loads(raw or "null")


def require_keys(obj: dict, keys: list[str], context: str):
    missing = [k for k in keys if k not in obj]
    extra = [k for k in obj if k not in keys]
    if missing or extra:
        raise CmdError(f"gh JSON shape mismatch [{context}]: missing={missing} extra={extra}")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": STATE_VERSION, "trees": {}}
    data = json.loads(STATE_PATH.read_text())
    if data.get("version") != STATE_VERSION:
        die(f"state file {STATE_PATH} has unknown version {data.get('version')}")
    return data


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_PATH)


def tree_state(state: dict, tree: str) -> dict:
    return state["trees"].setdefault(tree, {"branches": {}})


def current_branch() -> str:
    return git("symbolic-ref", "--short", "HEAD")


def current_tree() -> str:
    return git("rev-parse", "--show-toplevel")


def current_user_login() -> str:
    return gh("api", "user", "--jq", ".login")


def default_branch() -> str:
    try:
        ref = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except CmdError:
        return gh("repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")
    return ref[len("origin/"):] if ref.startswith("origin/") else ref


def needs_rebase(branch: str, dep: str | None, db: str) -> str:
    target = dep if dep is not None else db
    try:
        dep_tip = git("rev-parse", f"origin/{target}")
    except CmdError:
        return "?"
    try:
        branch_tip = git("rev-parse", f"refs/heads/{branch}")
    except CmdError:
        try:
            branch_tip = git("rev-parse", f"origin/{branch}")
        except CmdError:
            return "(no local)"
    try:
        mb = git("merge-base", dep_tip, branch_tip)
    except CmdError:
        return "?"
    return "ok" if mb == dep_tip else "rebase"


def fmt_pr(entry: dict) -> str:
    return f"#{entry['pr']}" if entry["pr"] is not None else "-"


_CI_FAIL_CONCLUSIONS = {"FAILURE", "CANCELLED", "ACTION_REQUIRED", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
_CI_PENDING_STATUSES = {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}


def _summarize_checks(rollup) -> str:
    if not rollup:
        return "none"
    has_fail = False
    has_pending = False
    for c in rollup:
        if not isinstance(c, dict):
            continue
        # CheckRun: has status + conclusion. StatusContext: has state.
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        state = (c.get("state") or "").upper()
        if conclusion in _CI_FAIL_CONCLUSIONS or state in {"FAILURE", "ERROR"}:
            has_fail = True
        elif status in _CI_PENDING_STATUSES or state in {"PENDING", "EXPECTED"}:
            has_pending = True
    if has_fail:
        return "fail"
    if has_pending:
        return "pending"
    return "pass"


_ANSI_RED = "31"
_ANSI_YELLOW = "33"
_ANSI_GREEN = "32"
_ANSI_ORANGE = "38;5;208"
_ANSI_WHITE = "37"
_ANSI_GREY = "90"


def _ansi(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _color_rebase(rebase: str) -> str:
    if rebase == "ok":
        return _ansi(rebase, _ANSI_GREEN)
    if rebase == "rebase":
        return _ansi(rebase, _ANSI_ORANGE)
    return rebase


def render_tree(visible: dict, db: str, rebase_fn, current: str | None = None) -> list[str]:
    if not visible:
        return ["no open tracked branches"]
    children: dict[str, list[str]] = {}
    for name, e in visible.items():
        parent = db if e["depends_on"] is None else e["depends_on"]
        children.setdefault(parent, []).append(name)
    for v in children.values():
        v.sort()

    def _walk(rows_out: list, name: str, prefix: str, is_last: bool):
        entry = visible[name]
        rebase = rebase_fn(name, entry["depends_on"])
        rows_out.append({
            "lead": prefix + ("└─ " if is_last else "├─ "),
            "name": f"[{name}]",
            "external": bool(entry.get("external")),
            "current": name == current,
            "draft": bool(entry.get("draft")),
            "pr": fmt_pr(entry),
            "ci": entry.get("ci") or "none",
            "rebase": rebase,
            "title": DEP_PREFIX_RE.sub("", entry.get("title") or ""),
        })
        kids = children.get(name, [])
        new_prefix = prefix + ("   " if is_last else "│  ")
        for i, child in enumerate(kids):
            _walk(rows_out, child, new_prefix, i == len(kids) - 1)

    groups: list[tuple[str, list[dict]]] = []

    db_rows: list[dict] = []
    db_kids = children.get(db, [])
    for i, name in enumerate(db_kids):
        _walk(db_rows, name, "", i == len(db_kids) - 1)
    db_color = f"1;{_ANSI_RED}" if db == current else _ANSI_RED
    groups.append((_ansi(f"[{db}]", db_color), db_rows))

    externals = sorted(set(children) - {db} - set(visible))
    for ext_name in externals:
        ext_rows: list[dict] = []
        kids = children[ext_name]
        for i, name in enumerate(kids):
            _walk(ext_rows, name, "", i == len(kids) - 1)
        groups.append((f"<external: {ext_name}>", ext_rows))

    all_rows = [r for _, rs in groups for r in rs]
    max_lead_name = max((len(r["lead"]) + len(r["name"]) for r in all_rows), default=0)
    widths = {c: max((len(r[c]) for r in all_rows), default=0) for c in ("pr", "rebase")}

    ci_glyphs = {"pass": "✓", "fail": "✗", "pending": "?"}
    ci_colors = {"pass": _ANSI_GREEN, "fail": _ANSI_RED, "pending": _ANSI_YELLOW}

    def _ci_icon(ci: str) -> str:
        glyph = ci_glyphs.get(ci, " ")
        color = ci_colors.get(ci)
        return _ansi(glyph, color) if color else glyph

    def _format(r: dict) -> str:
        pad_name = " " * (max_lead_name - len(r["lead"]) - len(r["name"]))
        pr_pad = " " * (widths["pr"] - len(r["pr"]))
        rebase_pad = " " * (widths["rebase"] - len(r["rebase"]))
        if r["draft"]:
            # Draft PRs render as one uniformly greyed line, dropping the
            # per-cell colors so the whole row reads as muted.
            segs = [
                r["lead"], r["name"], pad_name,
                "  ", r["pr"], pr_pad,
                "  ", ci_glyphs.get(r["ci"], " "),
                "  ", r["rebase"], rebase_pad,
            ]
            if r["title"]:
                segs.append("  " + r["title"])
            return _ansi("".join(segs).rstrip(), _ANSI_GREY)
        name_color = _ANSI_WHITE if r["external"] else _ANSI_RED
        if r["current"]:
            name_color = f"1;{name_color}"
        parts = [
            r["lead"],
            _ansi(r["name"], name_color),
            pad_name,
        ]
        parts.append("  " + _ansi(r["pr"], _ANSI_YELLOW) + pr_pad)
        parts.append("  " + _ci_icon(r["ci"]))
        parts.append("  " + _color_rebase(r["rebase"]) + rebase_pad)
        if r["title"]:
            parts.append("  " + r["title"])
        return "".join(parts).rstrip()

    lines: list[str] = []
    for header, rs in groups:
        lines.append(header)
        for r in rs:
            lines.append(_format(r))
    return lines


def cmd_show(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    if args.sync:
        _do_fetch(state, rs)
        save_state(state)
    if not rs["branches"]:
        print("no tracked branches")
        return
    db = default_branch()
    visible = {
        n: e for n, e in rs["branches"].items()
        if args.all or e.get("status") not in ("merged", "closed")
    }
    if args.org:
        for name in visible:
            print(f"** TODO [{name}]")
        return
    try:
        cur = current_branch()
    except CmdError:
        cur = None
    rb = lambda name, dep: needs_rebase(name, dep, db)
    for line in render_tree(visible, db, rb, current=cur):
        print(line)


def cmd_branch(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    if args.name in rs["branches"]:
        die(f"branch {args.name} already tracked in state")
    cur = current_branch()
    db = default_branch()
    dep = None if args.main or cur == db else cur
    git("checkout", "-b", args.name, capture=False)
    rs["branches"][args.name] = {
        "pr": None,
        "depends_on": dep,
        "status": "no-pr",
    }
    save_state(state)
    print(f"branch {args.name} tracked (dep: {dep or '<default>'})")


def _do_fetch(state: dict, rs: dict):
    print("fetching git refs…", file=sys.stderr)
    git("fetch", "--all", "--prune", capture=False)
    db = default_branch()

    print("listing open PRs…", file=sys.stderr)
    list_fields = ["number", "headRefName", "baseRefName", "state", "author", "title", "statusCheckRollup", "isDraft"]
    discovered = gh_json(
        ["pr", "list", "--state", "open", "--limit", "1000"],
        list_fields,
    )
    if not isinstance(discovered, list):
        raise CmdError(f"gh pr list returned non-list: {discovered!r}")
    me = current_user_login()
    for pr in discovered:
        require_keys(pr, list_fields, "gh pr list")
        head = pr["headRefName"]
        existing = rs["branches"].get(head)
        if existing is None or existing.get("pr") != pr["number"]:
            base = pr["baseRefName"]
            author = pr["author"] or {}
            login = author.get("login") if isinstance(author, dict) else None
            rs["branches"][head] = {
                "pr": pr["number"],
                "depends_on": None if base == db else base,
                "status": pr["state"].lower(),
                "external": login != me,
                "title": pr["title"],
                "ci": _summarize_checks(pr["statusCheckRollup"]),
                "draft": bool(pr["isDraft"]),
            }

    view_fields = ["state", "baseRefName", "title", "statusCheckRollup", "isDraft"]
    # Already-terminal PRs are dropped below, so there's nothing to refresh.
    tracked = [(n, e) for n, e in rs["branches"].items()
               if e["pr"] is not None and e.get("status") not in ("merged", "closed")]
    if tracked:
        print(f"refreshing {len(tracked)} PR(s)…", file=sys.stderr)
    done = 0
    with ThreadPoolExecutor(max_workers=min(8, len(tracked) or 1)) as ex:
        futures = {ex.submit(gh_json, ["pr", "view", str(e["pr"])], view_fields): (n, e) for n, e in tracked}
        for fut in as_completed(futures):
            name, entry = futures[fut]
            done += 1
            try:
                data = fut.result()
            except CmdError as e:
                print(f"pr: warning: gh pr view {entry['pr']}: {e}", file=sys.stderr)
                continue
            if not isinstance(data, dict):
                raise CmdError(f"gh pr view returned non-dict: {data!r}")
            require_keys(data, view_fields, "gh pr view")
            st = data["state"].lower()
            entry["status"] = st
            base = data["baseRefName"]
            entry["depends_on"] = None if base == db else base
            entry["title"] = data["title"]
            entry["ci"] = _summarize_checks(data["statusCheckRollup"])
            entry["draft"] = bool(data["isDraft"])
            print(f"refreshed PR #{entry['pr']} [{name}] ({done}/{len(tracked)})", file=sys.stderr)

    for entry in rs["branches"].values():
        if entry.get("depends_on") == db:
            entry["depends_on"] = None

    refs_out = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin")
    known_refs = set(refs_out.splitlines()) if refs_out else set()
    for name in list(rs["branches"]):
        if f"refs/heads/{name}" in known_refs:
            continue
        if f"refs/remotes/origin/{name}" in known_refs:
            continue
        del rs["branches"][name]

    # A merged/closed PR is done — drop it immediately, no grace window. If
    # it's later reopened, `pr list --state open` above re-discovers it.
    for name in [n for n, e in rs["branches"].items() if e.get("status") in ("merged", "closed")]:
        del rs["branches"][name]


def cmd_fetch(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    _do_fetch(state, rs)
    save_state(state)


def _format_title(message: str, dep_pr: int | None) -> str:
    return f"[dep #{dep_pr}] {message}" if dep_pr is not None else message


def _apply_title_prefix(pr_num: int, dep_pr: int | None) -> bool:
    cur_title = gh("pr", "view", str(pr_num), "--json", "title", "-q", ".title")
    stripped = DEP_PREFIX_RE.sub("", cur_title)
    new_title = _format_title(stripped, dep_pr)
    if new_title == cur_title:
        return False
    gh("pr", "edit", str(pr_num), "--title", new_title)
    return True


def cmd_create(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    _do_fetch(state, rs)

    cur = current_branch()
    existing = rs["branches"].get(cur)
    if existing and existing["pr"] is not None:
        die(f"current branch {cur} already has PR #{existing['pr']}")

    if args.dep is not None:
        dep = args.dep
    elif existing:
        dep = existing["depends_on"]
    else:
        dep = None

    dep_pr = None
    if dep is not None:
        dep_entry = rs["branches"].get(dep)
        if not dep_entry or dep_entry.get("pr") is None or dep_entry.get("status") != "open":
            die(f"dep branch {dep!r} has no open PR — create its PR first")
        dep_pr = dep_entry["pr"]

    title = _format_title(args.message, dep_pr)

    git("push", "-u", "origin", cur, capture=False)

    db = default_branch()
    base = dep if dep is not None else db

    create_args = ["pr", "create", "--base", base, "--title", title, "--body", ""]
    if not args.ready:
        create_args.append("--draft")
    out = gh(*create_args)
    m = re.search(r"/pull/(\d+)", out)
    if not m:
        die(f"could not parse PR number from gh pr create output: {out!r}")
    pr_num = int(m.group(1))

    rs["branches"][cur] = {
        "pr": pr_num,
        "depends_on": dep,
        "status": "open",
        "draft": not args.ready,
    }
    save_state(state)
    print(f"created PR #{pr_num}: {title}")


def cmd_target(args):
    if not args.main and not args.branch:
        die("specify a branch name or --main")
    if args.main and args.branch:
        die("--main and a branch name are mutually exclusive")

    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    cur = current_branch()
    entry = rs["branches"].get(cur)

    if entry and entry.get("pr") is not None and entry.get("status") != "open":
        die(f"PR #{entry['pr']} is not open (status={entry['status']})")

    db = default_branch()
    new_dep = None if args.main or args.branch == db else args.branch
    new_dep_pr = None
    if new_dep is not None:
        dep_entry = rs["branches"].get(new_dep)
        if not dep_entry or dep_entry.get("pr") is None or dep_entry.get("status") != "open":
            die(f"target branch {new_dep!r} has no open PR")
        new_dep_pr = dep_entry["pr"]

    if entry and entry.get("pr") is not None:
        new_base = new_dep if new_dep is not None else db
        gh("pr", "edit", str(entry["pr"]), "--base", new_base)
        _apply_title_prefix(entry["pr"], new_dep_pr)

    if entry is None:
        rs["branches"][cur] = {
            "pr": None,
            "depends_on": new_dep,
            "status": "no-pr",
        }
    else:
        entry["depends_on"] = new_dep
    save_state(state)

    if needs_rebase(cur, new_dep, db) == "rebase":
        print("note: branch needs rebase onto new dep — run `pr rebase`")


def cmd_rebase(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    cur = current_branch()
    entry = rs["branches"].get(cur)
    if not entry:
        die(f"no state entry for {cur} — use `pr branch` or `pr create` first")
    db = default_branch()
    target = entry["depends_on"] if entry["depends_on"] is not None else db
    rc = subprocess.run(["git", "rebase", "-i", f"origin/{target}"]).returncode
    if rc != 0:
        sys.exit(rc)


def cmd_diff(args):
    branch = args.branch or current_branch()
    state = load_state()
    rs = tree_state(state, current_tree())
    entry = rs["branches"].get(branch)
    parent = (entry or {}).get("depends_on") or default_branch()
    # Triple-dot: only the branch's own commits, not the ones its base gained
    # since they diverged.
    cmd = ["git", "diff", "--no-ext-diff", f"origin/{parent}...{branch}", *args.git_args]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(rc)


def cmd_checkout(args):
    target = args.target
    if target.isdigit():
        fields = ["headRefName"]
        info = gh_json(["pr", "view", target], fields)
        require_keys(info, fields, "gh pr view")
        branch = info["headRefName"]
    else:
        branch = target
    git("checkout", branch, capture=False)


def cmd_update(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    cur = current_branch()
    entry = rs["branches"].get(cur)
    if not entry or entry.get("pr") is None:
        die(f"no open PR for current branch {cur}")
    if entry.get("status") != "open":
        die(f"PR #{entry['pr']} is not open (status={entry['status']})")

    dep = entry["depends_on"]
    dep_pr = None
    if dep is not None:
        dep_entry = rs["branches"].get(dep)
        if not dep_entry or dep_entry.get("pr") is None or dep_entry.get("status") != "open":
            die(f"dep branch {dep!r} has no open PR")
        dep_pr = dep_entry["pr"]

    if _apply_title_prefix(entry["pr"], dep_pr):
        print(f"updated PR #{entry['pr']} title")
    else:
        print(f"PR #{entry['pr']} title already correct")


def cmd_review(args):
    branches = args.branches or [current_branch()]
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    for i, branch in enumerate(branches):
        if i:
            print()
        if len(branches) > 1:
            print(f"=== {branch} ===")
        _review_branch(state, rs, tree, branch, args)


def repo_instructions(tree: str) -> str:
    """Contents of the checkout's CLAUDE.md, plus AGENTS.md when CLAUDE.md refers to it."""
    root = Path(tree)
    names = ["CLAUDE.md"]
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file() and "AGENTS.md" in claude_md.read_text():
        names.append("AGENTS.md")
    blocks = []
    for name in names:
        path = root / name
        if path.is_file():
            text = path.read_text().strip()
            if text:
                blocks.append(f"Contents of {name}:\n{text}\n")
    return "\n".join(blocks)


def _fmt_duration(seconds: float) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _fmt_tokens(n) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _num(value):
    """A JSON number, or 0 for anything else (missing keys, strings, bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _tool_label(block: dict, limit: int = 48) -> str:
    """`name: detail` for a tool_use block, e.g. `Bash: gh pr diff 42`."""
    inp = _dict(block.get("input"))
    name = str(block.get("name") or "?")
    for key in ("command", "description", "file_path", "pattern", "prompt"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(f"{name}: {value}", limit)
    return _truncate(name, limit)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _decode_event(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _model_usage_totals(result: dict | None) -> dict | None:
    """Sum the result event's per-model usage, or None when it is absent."""
    usage = (result or {}).get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    keys = {
        "input": "inputTokens",
        "output": "outputTokens",
        "cache_read": "cacheReadInputTokens",
        "cache_write": "cacheCreationInputTokens",
        "cost": "costUSD",
    }
    totals = {name: 0 for name in keys}
    for per_model in usage.values():
        if not isinstance(per_model, dict):
            continue
        for name, key in keys.items():
            totals[name] += _num(per_model.get(key))
    return totals


# Claude buffers everything a forked slash command (the review skill) does and
# replays it when the fork ends, so the event stream shows nothing live. Hooks
# fire in real time, including inside forks and subagents: these append one
# trimmed JSON line per event to a file that the stream loop polls.
_HOOK_EVENTS = ("PreToolUse", "SubagentStart", "SubagentStop")
_HOOK_SCRIPT = (
    "import json, sys\n"
    "d = json.load(sys.stdin)\n"
    "i = d.get('tool_input')\n"
    "o = {k: d.get(k) for k in ('hook_event_name', 'tool_name', 'tool_use_id', 'agent_type', 'agent_id')}\n"
    "o['tool_input'] = {k: v[:200] if isinstance(v, str) else v for k, v in i.items()} if isinstance(i, dict) else {}\n"
    "with open(sys.argv[1], 'a') as f:\n"
    "    f.write(json.dumps(o) + '\\n')\n"
)


def _hook_settings(path: str) -> str:
    """Settings JSON installing hooks that append one JSON line per event to `path`."""
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_HOOK_SCRIPT)} {shlex.quote(path)}"
    hooks = {name: [{"hooks": [{"type": "command", "command": command}]}] for name in _HOOK_EVENTS}
    hooks["PreToolUse"][0]["matcher"] = ""
    return json.dumps({"hooks": hooks})


def _drain_lines(fh) -> list[str]:
    """Complete lines appended to a file since the last call."""
    lines = []
    while True:
        pos = fh.tell()
        line = fh.readline()
        if not line:
            return lines
        if not line.endswith(b"\n"):
            fh.seek(pos)
            return lines
        lines.append(line.decode("utf-8", "replace"))


# Milestones in claude's debug log. Unlike the event stream, the log is written
# as things happen, so it shows what a forked review is doing right now.
_LOG_API_REQUEST = re.compile(r"\[API REQUEST\] /v1/messages")
_LOG_FIRST_BYTE = re.compile(r"\[API:timing\] first byte after (\d+)ms")
_LOG_TOOL_DONE = re.compile(r"tool_dispatch_end tool=(\S+) .*durationMs=(\d+)")


def _drain_hook_file(fh) -> list[dict]:
    """Complete JSON records appended to the hook file since the last call."""
    events = [_decode_event(line) for line in _drain_lines(fh)]
    return [e for e in events if e is not None]


def _denial_labels(result: dict | None) -> list[str]:
    labels = []
    for denial in (result or {}).get("permission_denials") or []:
        if isinstance(denial, dict):
            labels.append(_tool_label({"name": denial.get("tool_name"), "input": denial.get("tool_input")}))
    return labels


class ReviewProgress:
    """Counters and rendering for one claude review run, fed stream-json events."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self.api_calls = 0
        self.tool_calls = 0
        # Input-side tokens (input + cache read + cache write) summed over calls,
        # and the same figure for the latest call. Output tokens from events are
        # only a lower bound; the result event's modelUsage has the real totals.
        # Api and token events from a forked slash command only arrive when it
        # finishes; tool calls and agents are counted live from hook events.
        self.tokens_in = 0
        self.output_tokens = 0
        self.context_tokens = 0
        self.running_agents: dict[str, str] = {}
        self.agents_started = 0
        self.last_tool = ""
        self.last_task = ""
        self.model = ""
        self.result: dict | None = None
        self.updates = 0
        # Live figures from the debug log: every line written, each API request
        # started, and the current phase with the time it began.
        self.log_lines = 0
        self.api_requests = 0
        self.phase = "starting"
        self._phase_since = self._start
        self._seen_message_ids: set[str] = set()
        self._seen_tool_ids: set[str] = set()
        self._task_descriptions: dict[str, str] = {}

    def elapsed(self) -> float:
        return self._clock() - self._start

    def _stamp(self, text: str) -> str:
        return f"{_fmt_duration(self.elapsed())}  {text}"

    def _set_phase(self, phase: str):
        self.phase = phase
        self._phase_since = self._clock()

    def phase_text(self) -> str:
        return f"{self.phase} {_fmt_duration(self._clock() - self._phase_since)}"

    def handle_log_line(self, line: str) -> list[str]:
        """Update the live counters from one debug-log line; return verbose lines for milestones."""
        self.log_lines += 1
        if _LOG_API_REQUEST.search(line):
            self.api_requests += 1
            self._set_phase("waiting for model")
            self.updates += 1
            return [self._stamp(f"api request #{self.api_requests}")]
        m = _LOG_FIRST_BYTE.search(line)
        if m:
            self._set_phase("model responding")
            self.updates += 1
            return [self._stamp(f"model responding  (first byte after {int(m.group(1)) / 1000:.1f}s)")]
        m = _LOG_TOOL_DONE.search(line)
        if m:
            self._set_phase("thinking")
            self.updates += 1
            return [self._stamp(f"tool done  {m.group(1)} in {int(m.group(2)) / 1000:.1f}s")]
        return []

    def handle(self, event: dict) -> list[str]:
        """Update counters from one event; return the lines for verbose output."""
        kind = event.get("type")
        if "hook_event_name" in event:
            lines = self._handle_hook(event)
        elif kind == "assistant":
            lines = self._handle_assistant(_dict(event.get("message")))
        elif kind == "user":
            lines = self._handle_user(_dict(event.get("message")))
        elif kind == "system":
            lines = self._handle_system(event)
        elif kind == "result":
            self.result = event
            line = f"result {event.get('subtype') or '?'}"
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                line += f"  cost ${cost:.2f}"
            lines = [self._stamp(line)]
        else:
            lines = []
        if lines:
            self.updates += 1
        return lines

    def _handle_assistant(self, message: dict) -> list[str]:
        lines = []
        msg_id = message.get("id")
        if not msg_id or msg_id not in self._seen_message_ids:
            if msg_id:
                self._seen_message_ids.add(msg_id)
            usage = _dict(message.get("usage"))
            context = sum(
                _num(usage.get(k))
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            output = _num(usage.get("output_tokens"))
            # Synthetic events (e.g. the forwarded final answer) carry zero usage.
            if context or output:
                self.api_calls += 1
                self.tokens_in += context
                self.output_tokens += output
                self.context_tokens = context
                lines.append(
                    self._stamp(f"api#{self.api_calls}  ctx {_fmt_tokens(context)}  in {_fmt_tokens(self.tokens_in)}")
                )
        for block in _blocks(message):
            if block.get("type") == "tool_use":
                lines += self._tool_call(block, block.get("id"))
        return lines

    def _tool_call(self, block: dict, tool_id) -> list[str]:
        """Count a tool call once, whether it arrives as a hook or a replayed message."""
        if tool_id:
            if tool_id in self._seen_tool_ids:
                return []
            self._seen_tool_ids.add(str(tool_id))
        self.tool_calls += 1
        self.last_tool = _tool_label(block)
        return [self._stamp(f"tool#{self.tool_calls}  {self.last_tool}")]

    def _handle_hook(self, event: dict) -> list[str]:
        name = event.get("hook_event_name")
        agent_id = str(event.get("agent_id") or "")
        agent = str(event.get("agent_type") or "agent")
        if name == "PreToolUse":
            block = {"name": event.get("tool_name"), "input": event.get("tool_input")}
            self._set_phase(f"running {event.get('tool_name') or 'tool'}")
            return self._tool_call(block, event.get("tool_use_id"))
        if name == "SubagentStart":
            self.running_agents[agent_id] = agent
            self.agents_started += 1
            self.last_task = f"agent {agent}"
            return [self._stamp(f"agent start  {agent}  ({len(self.running_agents)} running)")]
        if name == "SubagentStop":
            self.running_agents.pop(agent_id, None)
            self.last_task = f"agent {agent} done"
            return [self._stamp(f"agent stop  {agent}  ({len(self.running_agents)} running)")]
        return []

    def _handle_user(self, message: dict) -> list[str]:
        lines = []
        for block in _blocks(message):
            if block.get("type") == "tool_result" and block.get("is_error"):
                lines.append(self._stamp(f"tool error  {_truncate(_result_text(block.get('content')), 80)}"))
        return lines

    def _handle_system(self, event: dict) -> list[str]:
        subtype = event.get("subtype")
        if subtype == "init":
            self.model = str(event.get("model") or "")
            return [self._stamp(f"init  model={self.model or '?'}  mode={event.get('permissionMode') or '?'}")]
        if subtype == "task_started":
            if event.get("task_type", "local_agent") != "local_agent":
                return []
            task_id = str(event.get("task_id") or "")
            desc = _truncate(str(event.get("description") or "task"), 60)
            self._task_descriptions[task_id] = desc
            self.last_task = desc
            agent = event.get("subagent_type")
            return [self._stamp(f"task start  {desc}" + (f" ({agent})" if agent else ""))]
        if subtype == "task_notification":
            desc = self._task_descriptions.pop(str(event.get("task_id") or ""), None)
            if desc is None:
                return []
            summary = _truncate(str(event.get("summary") or ""), 60)
            status = event.get("status") or "stopped"
            if summary == desc:
                summary = ""
            self.last_task = f"{desc} {status}"
            return [self._stamp(f"task {status}  {desc}" + (f": {summary}" if summary else ""))]
        return []

    def status_line(self, width: int) -> str:
        parts = [
            _fmt_duration(self.elapsed()),
            self.phase_text(),
            f"api {max(self.api_requests, self.api_calls)}",
            f"tools {self.tool_calls}",
            f"agents {len(self.running_agents)}",
            f"log {_fmt_tokens(self.log_lines)}",
        ]
        if self.api_calls:
            parts += [f"ctx {_fmt_tokens(self.context_tokens)}", f"in {_fmt_tokens(self.tokens_in)}"]
        if self.last_tool or self.last_task:
            parts.append(self.last_tool or self.last_task)
        return "  ".join(parts)[: max(width, 0)]

    def summary(self) -> str:
        duration = _fmt_duration(self.elapsed())
        api = max(self.api_requests, self.api_calls)
        counts = f"{api} api calls, {self.tool_calls} tool calls, {self.agents_started} agents"
        totals = _model_usage_totals(self.result)
        if totals is not None:
            tokens = (
                f"tokens in {_fmt_tokens(totals['input'])}, out {_fmt_tokens(totals['output'])}, "
                f"cache read {_fmt_tokens(totals['cache_read'])}, cache write {_fmt_tokens(totals['cache_write'])}"
            )
            cost = totals["cost"]
        else:
            tokens = f"tokens (from events) in {_fmt_tokens(self.tokens_in)}, out {_fmt_tokens(self.output_tokens)}"
            cost = (self.result or {}).get("total_cost_usd")
        cost_text = f"cost ${cost:.2f}" if isinstance(cost, (int, float)) else "cost n/a"
        if self.result is None:
            return f"review ended in {duration} without a result: {counts}; {tokens}; {cost_text}"
        line = f"review done in {duration}: {counts}; {tokens}; {cost_text}"
        denials = _denial_labels(self.result)
        if denials:
            noun = "permission denial" if len(denials) == 1 else "permission denials"
            line += f"; {len(denials)} {noun}: {', '.join(denials)}"
        return line

    def review_text(self) -> str:
        text = (self.result or {}).get("result")
        return text if isinstance(text, str) else ""

    def failure(self) -> str | None:
        """Why the run produced no usable review, or None if it succeeded."""
        if self.result is None:
            return "stream ended without a result event"
        subtype = self.result.get("subtype") or "unknown"
        if self.result.get("is_error"):
            return f"is_error: {_truncate(self.review_text() or subtype, 200)}"
        if subtype != "success":
            return subtype
        return None


class ReviewDisplay:
    """Progress on stderr: a status line redrawn in place, or a line per event when verbose."""

    def __init__(self, out=None, verbose: bool = False, clock=time.monotonic):
        self._out = out or sys.stderr
        self._verbose = verbose
        self._tty = self._out.isatty()
        self._clock = clock
        self._last_draw = clock()
        self._last_updates = 0
        self._status_shown = False

    def _width(self) -> int:
        try:
            columns = os.get_terminal_size(self._out.fileno()).columns
        except (OSError, ValueError, AttributeError):
            columns = 0
        return columns - 1 if columns > 1 else 99

    def log(self, lines: list[str]):
        if not self._verbose or not lines:
            return
        for line in lines:
            print(line, file=self._out)
        self._out.flush()
        self._last_draw = self._clock()

    def tick(self, progress: ReviewProgress):
        now = self._clock()
        if self._verbose:
            # A heartbeat line every 15s of silence, so a quiet model turn is
            # distinguishable from a hang.
            if now - self._last_draw >= 15:
                print(progress.status_line(200), file=self._out)
                self._out.flush()
                self._last_draw = now
            return
        if self._tty:
            if now - self._last_draw < 0.2 and self._status_shown:
                return
            self._out.write("\r\033[2K" + progress.status_line(self._width()))
            self._status_shown = True
        else:
            # Without a terminal, emit a plain line whenever something happened
            # or every 30s, so a redirected stderr stays readable.
            if progress.updates == self._last_updates and now - self._last_draw < 30:
                return
            print(progress.status_line(200), file=self._out)
            self._last_updates = progress.updates
        self._out.flush()
        self._last_draw = now

    def finish(self):
        if self._status_shown:
            self._out.write("\r\033[2K")
            self._out.flush()
            self._status_shown = False


def _run_claude_stream(cmd: list[str], prompt: str, cwd: str, display: ReviewDisplay) -> tuple[int, ReviewProgress]:
    """Run claude with stream-json output and live hooks, feeding every event to the display."""
    progress = ReviewProgress()
    hook_fd, hook_path = tempfile.mkstemp(prefix="pr-review-hooks-", suffix=".jsonl")
    os.close(hook_fd)
    log_fd, log_path = tempfile.mkstemp(prefix="pr-review-debug-", suffix=".log")
    os.close(log_fd)
    cmd = [*cmd, "--settings", _hook_settings(hook_path), "--debug-file", log_path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, cwd=cwd)
    except FileNotFoundError:
        os.unlink(hook_path)
        os.unlink(log_path)
        die(f"{cmd[0]} not found on PATH")
    lines: queue.Queue = queue.Queue()

    # A reader thread lets the main loop wake up once a second to advance the
    # clock while claude is silent (long tool calls, thinking subagents).
    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    transcript_path = os.environ.get("PR_CLAUDE_TRANSCRIPT")
    transcript = open(transcript_path, "a") if transcript_path else None
    hook_fh = open(hook_path, "rb")
    log_fh = open(log_path, "rb")

    def feed(event: dict):
        display.log(progress.handle(event))
        display.tick(progress)

    def poll_files():
        for raw in _drain_hook_file(hook_fh):
            feed(raw)
        for raw in _drain_lines(log_fh):
            display.log(progress.handle_log_line(raw))
        display.tick(progress)

    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        while True:
            poll_files()
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                continue
            if line is None:
                break
            if transcript:
                transcript.write(line)
            event = _decode_event(line)
            if event is not None:
                feed(event)
        rc = proc.wait()
        poll_files()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        display.finish()
        die("review interrupted", code=130)
    finally:
        if transcript:
            transcript.close()
        hook_fh.close()
        log_fh.close()
        for path in (hook_path, log_path):
            try:
                os.unlink(path)
            except OSError:
                pass
    display.finish()
    return rc, progress


def _review_branch(state: dict, rs: dict, tree: str, branch: str, args):
    entry = rs["branches"].get(branch)
    parent = (entry or {}).get("depends_on") or default_branch()
    pr_num = (entry or {}).get("pr")

    if args.show:
        review = (entry or {}).get("last_review")
        if review and review.strip():
            print(review.strip())
        else:
            print(f"no stored review for branch {branch}")
        return

    # Pull the diff from the server so the review is independent of the local
    # checkout — you can switch branches while it runs. The review skill fetches
    # it again with the same command; this copy only serves to skip empty diffs.
    if pr_num is not None:
        diff = gh("pr", "diff", str(pr_num))
        # Report the PR's actual base on the server, which may differ from the
        # locally-tracked parent.
        fields = ["baseRefName", "headRefName"]
        info = gh_json(["pr", "view", str(pr_num)], fields)
        require_keys(info, fields, "gh pr view")
        source = f"PR #{pr_num} (`{info['headRefName']}` vs base `{info['baseRefName']}`)"
        target = str(pr_num)
        diff_cmd = f"gh pr diff {pr_num}"
    else:
        # No PR tracked: diff the remote refs directly (still not local HEAD).
        git("fetch", "--quiet", "origin", branch, parent, capture=False)
        diff = git("diff", "--no-ext-diff", f"origin/{parent}...origin/{branch}")
        source = f"branch `{branch}` vs base `origin/{parent}`"
        target = f"origin/{branch}"
        diff_cmd = f"git diff --no-ext-diff origin/{parent}...origin/{branch}"

    if not diff.strip():
        print(f"no diff for {source}")
        return

    print(f"reviewing {source}", file=sys.stderr)

    prior_review = (entry or {}).get("last_review")
    context_block = ""
    if prior_review and prior_review.strip():
        context_block = (
            "A review of an earlier version of this branch is included below FOR "
            "CONTEXT ONLY. Do not respond to it, repeat it, or assume its findings "
            "still apply — review the current diff on its own merits.\n\n"
            f"Previous review (context only):\n{prior_review.strip()}\n\n"
        )

    extra_block = ""
    if args.prompt and args.prompt.strip():
        extra_block = f"Additional instructions for this review:\n{args.prompt.strip()}\n\n"

    instructions_block = ""
    instructions = repo_instructions(tree)
    if instructions:
        instructions_block = (
            "Check every hunk against the conventions in the repository's "
            "instruction files, included below, and report each violation as a "
            "finding.\n\n"
            f"{instructions}\n"
        )

    # The first line invokes claude's bundled code-review skill; the rest of the
    # prompt reaches the model as ordinary instructions alongside it.
    prompt = (
        f"/code-review {args.level} {target} --max-findings all\n\n"
        f"Target: {source}. Obtain the diff with `{diff_cmd}` and review exactly "
        "that diff. Do not derive the base from the local checkout, which may be "
        "on a different branch.\n\n"
        "Be thorough rather than conservative. Examine every hunk, and read the "
        "surrounding code and callers whenever a change's correctness depends on "
        "them. Report a finding whenever you can describe a concrete way it "
        "fails, even if you are not certain the path is reachable, and state "
        "your confidence rather than withholding it. Regressions, silent error "
        "handling, and missing or inadequate tests all count.\n\n"
        f"{extra_block}"
        f"{instructions_block}"
        f"{context_block}"
        "Write the complete review as your final message, in markdown suitable "
        "for posting as a PR comment: one bullet per finding giving file:line, "
        "what is wrong, how it fails, and your confidence, followed by a short "
        "list of what you checked and found clean. Do not reply with only a "
        "one-line verdict.\n"
    )

    # Run from the repo root so claude also picks up CLAUDE.md on its own,
    # regardless of which subdirectory `pr review` was invoked from.
    # Plan mode keeps the session read-only while still allowing the git and
    # gh commands the skill needs; anything that would prompt is denied.
    # stream-json on stdout drives the progress display and carries the final
    # review; claude's own stderr still streams to the terminal.
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "plan",
        "--permission-prompts",
        "none",
        "--allowedTools",
        "Bash(gh pr diff:*),Bash(gh pr view:*)",
        "--strict-mcp-config",
    ]
    rc, progress = _run_claude_stream(cmd, prompt, tree, ReviewDisplay(verbose=args.verbose))
    print(progress.summary(), file=sys.stderr)
    if rc != 0:
        die(f"claude exited with code {rc}", code=rc)
    reason = progress.failure()
    if reason:
        die(f"claude review failed: {reason}")
    review_text = progress.review_text().strip()
    if not review_text:
        die("claude returned an empty review")
    print(review_text)

    if entry is not None:
        entry["last_review"] = review_text
        save_state(state)
    else:
        print(
            f"pr: warning: branch {branch} is not tracked; review not stored "
            "(run `pr branch` or `pr create` first)",
            file=sys.stderr,
        )


def cmd_post_review(args):
    branch = args.branch or current_branch()
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    entry = rs["branches"].get(branch)
    if entry is None:
        die(f"branch {branch} is not tracked")
    pr_num = entry.get("pr")
    if pr_num is None:
        die(f"branch {branch} has no PR to comment on")
    review = entry.get("last_review")
    if not review or not review.strip():
        die(f"no stored review for branch {branch}; run `pr review` first")
    body = f"🤖 Automated review generated by Claude:\n\n{review.strip()}"
    gh("pr", "comment", str(pr_num), "--body", body, capture=False)
    print(f"posted stored review as a comment on PR #{pr_num}")


def _resolve_tracked_pr(branch: str):
    """Return (state, rs, entry, pr_num) for a tracked branch with an open PR,
    or die with a helpful message."""
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    entry = rs["branches"].get(branch)
    if entry is None:
        die(f"branch {branch} is not tracked")
    pr_num = entry.get("pr")
    if pr_num is None:
        die(f"branch {branch} has no PR")
    return state, rs, entry, pr_num


def cmd_ready(args):
    branch = args.branch or current_branch()
    state, _rs, entry, pr_num = _resolve_tracked_pr(branch)
    gh("pr", "ready", str(pr_num), capture=False)
    entry["draft"] = False
    save_state(state)
    print(f"marked PR #{pr_num} ready for review")


def cmd_draft(args):
    branch = args.branch or current_branch()
    state, _rs, entry, pr_num = _resolve_tracked_pr(branch)
    gh("pr", "ready", str(pr_num), "--undo", capture=False)
    entry["draft"] = True
    save_state(state)
    print(f"converted PR #{pr_num} to draft")


_AUTOMERGE_POLL_SECONDS = 20
# An empty statusCheckRollup is ambiguous: it can mean "no CI is configured" or
# "CI hasn't registered yet" (common in the seconds after a PR opens or is
# rebased). Only merge a PR as no-CI once the rollup has stayed empty this long.
_AUTOMERGE_NO_CI_GRACE_SECONDS = 90


def _resolve_merge_chain(branch: str, db: str) -> list[int]:
    """Walk PRs upstream from `branch` until reaching the default branch.
    Returns PR numbers in merge order (ancestor-adjacent-to-db first)."""
    chain: list[int] = []
    cur_head = branch
    seen: set[str] = set()
    while True:
        if cur_head in seen:
            die(f"cycle detected at branch {cur_head!r}")
        seen.add(cur_head)
        listed = gh_json(
            ["pr", "list", "--head", cur_head, "--state", "open"],
            ["number", "baseRefName"],
        )
        if not isinstance(listed, list):
            raise CmdError(f"gh pr list returned non-list: {listed!r}")
        if not listed:
            if not chain:
                die(f"no open PR for branch {cur_head!r}")
            die(f"ancestor branch {cur_head!r} has no open PR — chain broken")
        if len(listed) > 1:
            nums = ", ".join(f"#{p['number']}" for p in listed)
            die(f"multiple open PRs for branch {cur_head!r}: {nums}")
        pr_data = listed[0]
        chain.append(pr_data["number"])
        base = pr_data["baseRefName"]
        if base == db:
            break
        cur_head = base
    return list(reversed(chain))


def _is_behind(base: str, head: str) -> bool:
    """True iff `origin/base` has commits not in `origin/head` — i.e., the
    branch is missing recent base commits and needs a rebase.

    Independent of branch protection (mergeStateStatus only reports BEHIND
    when 'Require branches to be up to date' is enabled)."""
    git("fetch", "--quiet", "origin", base, head, capture=False)
    proc = run(
        ["git", "merge-base", "--is-ancestor", f"origin/{base}", f"origin/{head}"],
        check=False,
    )
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise CmdError(
        f"git merge-base --is-ancestor origin/{base} origin/{head} exited "
        f"{proc.returncode}: {(proc.stderr or '').strip()}"
    )


def _strip_dep_prefix_on_dependents(merged_head: str):
    listed = gh_json(
        ["pr", "list", "--base", merged_head, "--state", "open"],
        ["number", "title"],
    )
    if not isinstance(listed, list):
        raise CmdError(f"gh pr list returned non-list: {listed!r}")
    for item in listed:
        require_keys(item, ["number", "title"], "gh pr list")
        new_title = DEP_PREFIX_RE.sub("", item["title"])
        if new_title != item["title"]:
            print(f"updating dependent PR #{item['number']} title")
            gh("pr", "edit", str(item["number"]), "--title", new_title)


def _automerge_one(pr_num: int, db: str):
    view_fields = ["state", "statusCheckRollup", "mergeStateStatus", "baseRefName", "title", "headRefName"]

    snapshot = gh_json(["pr", "view", str(pr_num)], view_fields)
    if not isinstance(snapshot, dict):
        raise CmdError(f"gh pr view returned non-dict: {snapshot!r}")
    require_keys(snapshot, view_fields, "gh pr view")
    if (snapshot["state"] or "").upper() != "OPEN":
        die(f"PR #{pr_num} is not open (state={snapshot['state']})")

    if snapshot["baseRefName"] != db:
        print(f"retargeting PR #{pr_num} base to {db}")
        gh("pr", "edit", str(pr_num), "--base", db)

    rebased = False
    no_ci_since = None
    while True:
        data = gh_json(["pr", "view", str(pr_num)], view_fields)
        if not isinstance(data, dict):
            raise CmdError(f"gh pr view returned non-dict: {data!r}")
        require_keys(data, view_fields, "gh pr view")

        state = (data["state"] or "").upper()
        merge_state = (data["mergeStateStatus"] or "").upper()
        ci = _summarize_checks(data["statusCheckRollup"])

        if state != "OPEN":
            die(f"PR #{pr_num} is no longer open (state={state})")
        if merge_state == "DIRTY":
            die(f"PR #{pr_num} has merge conflicts — can't rebase")
        if merge_state == "BLOCKED":
            die(f"PR #{pr_num} is blocked (required reviews or branch protection)")
        if ci == "fail":
            die(f"PR #{pr_num} has failing CI")

        behind = merge_state == "BEHIND"
        if not behind:
            behind = _is_behind(data["baseRefName"], data["headRefName"])
        if behind:
            if rebased:
                die(f"PR #{pr_num} still behind after rebase attempt")
            print(f"pr: warning: PR #{pr_num} is behind {db} — rebasing on server", file=sys.stderr)
            try:
                gh("pr", "update-branch", str(pr_num), "--rebase", capture=False)
            except CmdError as e:
                die(f"failed to rebase PR #{pr_num}: {e}")
            rebased = True
            no_ci_since = None  # rebase re-triggers CI; restart the no-CI grace clock
            time.sleep(_AUTOMERGE_POLL_SECONDS)
            continue

        mergeable_state = merge_state in ("CLEAN", "UNSTABLE", "HAS_HOOKS")
        should_merge = False
        if mergeable_state and ci == "pass":
            should_merge = True
        elif mergeable_state and ci == "none":
            # Empty rollup: either no CI is configured, or CI hasn't registered
            # yet. Only merge once it has stayed empty past the grace window;
            # otherwise keep polling for checks to appear.
            if no_ci_since is None:
                no_ci_since = time.monotonic()
            waited = time.monotonic() - no_ci_since
            if waited >= _AUTOMERGE_NO_CI_GRACE_SECONDS:
                print(
                    f"pr: warning: PR #{pr_num} still has no CI checks after "
                    f"{int(waited)}s — treating as no-CI and merging",
                    file=sys.stderr,
                )
                should_merge = True
            else:
                print(
                    f"PR #{pr_num} has no CI checks yet; waiting up to "
                    f"{_AUTOMERGE_NO_CI_GRACE_SECONDS}s for them to register…"
                )
                time.sleep(_AUTOMERGE_POLL_SECONDS)
                continue
        else:
            # Checks are present but not yet passing, or the merge state isn't
            # ready; the empty-rollup grace clock doesn't apply.
            no_ci_since = None

        if should_merge:
            cur_title = data["title"]
            stripped = DEP_PREFIX_RE.sub("", cur_title)
            if stripped != cur_title:
                print(f"stripping dep prefix from PR #{pr_num}")
                gh("pr", "edit", str(pr_num), "--title", stripped)
            print(f"merging PR #{pr_num}…")
            gh("pr", "merge", str(pr_num), "--merge", capture=False)
            print(f"merged PR #{pr_num}")
            _strip_dep_prefix_on_dependents(data["headRefName"])
            return

        print(f"PR #{pr_num} CI: {ci}, merge: {merge_state}; sleeping {_AUTOMERGE_POLL_SECONDS}s…")
        time.sleep(_AUTOMERGE_POLL_SECONDS)


def cmd_automerge(args):
    branch = args.branch or current_branch()
    db = default_branch()

    if args.single:
        listed = gh_json(
            ["pr", "list", "--head", branch, "--state", "open"],
            ["number", "baseRefName"],
        )
        if not isinstance(listed, list):
            raise CmdError(f"gh pr list returned non-list: {listed!r}")
        if not listed:
            die(f"no open PR for branch {branch!r}")
        if len(listed) > 1:
            nums = ", ".join(f"#{p['number']}" for p in listed)
            die(f"multiple open PRs for branch {branch!r}: {nums}")
        pr_num = listed[0]["number"]
        base = listed[0]["baseRefName"]
        print(f"merging PR #{pr_num} into {base} (no recurse)")
        _automerge_one(pr_num, base)
        return

    print(f"resolving merge chain for {branch}…")
    chain = _resolve_merge_chain(branch, db)
    print("chain (ancestor-first): " + ", ".join(f"#{n}" for n in chain))

    for pr_num in chain:
        _automerge_one(pr_num, db)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr", description="stacked-PR manager")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("show", help="display the PR tree (default)")
    s.add_argument("--all", action="store_true", help="include merged/closed PRs")
    s.add_argument("--org", action="store_true", help="Print the list of PRs as org-mode titles")
    s.add_argument("--sync", action="store_true", help="refresh state from GitHub before rendering")

    b = sub.add_parser("branch", help="create a tracked branch with a dep")
    b.add_argument("name")
    b.add_argument("--main", action="store_true", help="depend on default branch instead of current")

    c = sub.add_parser("create", help="open a PR for the current branch")
    c.add_argument("-m", "--message", required=True, help="PR title")
    c.add_argument("--dep", help="explicit dep branch (overrides state)")
    c.add_argument("--ready", action="store_true", help="create as ready (default is draft)")

    sub.add_parser("fetch", help="refresh PR state from GitHub")

    t = sub.add_parser("target", help="retarget current branch's PR to a different dep")
    t.add_argument("branch", nargs="?")
    t.add_argument("--main", action="store_true")

    sub.add_parser("rebase", help="rebase current branch onto its dep")

    d = sub.add_parser("diff", help="git diff a branch against its base (flags after `--` go to git diff)")
    d.add_argument("branch", nargs="?", help="branch to diff (defaults to current branch)")

    co = sub.add_parser("checkout", help="check out a branch or PR number")
    co.add_argument("target", help="branch name or PR number")

    sub.add_parser("update", help="sync current PR's title prefix with its dep state")

    r = sub.add_parser("review", help="run claude's code-review skill (read-only) over each branch's server diff vs its parent")
    r.add_argument("branches", nargs="*", metavar="branch", help="branches to review (defaults to current branch)")
    r.add_argument("--show", action="store_true", help="print the last stored review instead of running a new one")
    r.add_argument("-p", "--prompt", help="extra instructions to append to the review prompt")
    r.add_argument(
        "--level",
        choices=["low", "medium", "high", "xhigh", "max"],
        default="medium",
        help="code-review effort: higher levels report more findings but run more reviewer agents and take "
        "much longer (default: medium)",
    )
    r.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every claude event (tool calls, agents, token usage) to stderr instead of the status line",
    )

    pr_post = sub.add_parser("post-review", help="post a branch's stored review as a PR comment")
    pr_post.add_argument("branch", nargs="?", help="branch whose stored review to post (defaults to current branch)")

    rdy = sub.add_parser("ready", help="mark a branch's PR as ready for review")
    rdy.add_argument("branch", nargs="?", help="branch whose PR to mark ready (defaults to current branch)")

    drf = sub.add_parser("draft", help="convert a branch's PR back to draft")
    drf.add_argument("branch", nargs="?", help="branch whose PR to convert to draft (defaults to current branch)")

    am = sub.add_parser("automerge", help="poll a branch's PR and merge when CI passes")
    am.add_argument("branch", nargs="?", help="branch to merge (defaults to current branch)")
    am.add_argument("--single", action="store_true", help="merge only the target PR into its current base, without recursing into ancestor PRs")

    return p


def main(argv: list[str] | None = None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["show"] + list(argv)
    git_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, git_args = argv[:i], argv[i + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["show"])
    if git_args and args.cmd != "diff":
        die(f"`--` passthrough args are only supported by `pr diff`, not `pr {args.cmd}`")
    args.git_args = git_args
    handlers = {
        "show": cmd_show,
        "branch": cmd_branch,
        "create": cmd_create,
        "fetch": cmd_fetch,
        "target": cmd_target,
        "rebase": cmd_rebase,
        "diff": cmd_diff,
        "checkout": cmd_checkout,
        "update": cmd_update,
        "review": cmd_review,
        "post-review": cmd_post_review,
        "ready": cmd_ready,
        "draft": cmd_draft,
        "automerge": cmd_automerge,
    }
    try:
        handlers[args.cmd](args)
    except CmdError as e:
        die(str(e))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""pr — stacked-PR manager. See README in this dir."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATE_PATH = Path.home() / ".pr.json"
CONFIG_PATH = Path.home() / ".config" / "pr" / "config.json"
DEFAULT_RETENTION_HOURS = 24
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


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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


def load_config() -> dict:
    cfg = {"closed_retention_hours": DEFAULT_RETENTION_HOURS}
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            print(f"pr: warning: {CONFIG_PATH} is malformed; using defaults", file=sys.stderr)
            return cfg
        if isinstance(user, dict):
            cfg.update(user)
    return cfg


def current_branch() -> str:
    return git("symbolic-ref", "--short", "HEAD")


def current_tree() -> str:
    return git("rev-parse", "--show-toplevel")


def current_user_login() -> str:
    return gh("api", "user", "--jq", ".login")


def default_branch() -> str:
    return gh("repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")


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

    def _ci_icon(ci: str) -> str:
        if ci == "pass":
            return _ansi("✓", _ANSI_GREEN)
        if ci == "fail":
            return _ansi("✗", _ANSI_RED)
        if ci == "pending":
            return _ansi("?", _ANSI_YELLOW)
        return " "

    def _format(r: dict) -> str:
        name_color = _ANSI_WHITE if r["external"] else _ANSI_RED
        if r["current"]:
            name_color = f"1;{name_color}"
        parts = [
            r["lead"],
            _ansi(r["name"], name_color),
            " " * (max_lead_name - len(r["lead"]) - len(r["name"])),
        ]
        parts.append("  " + _ansi(r["pr"], _ANSI_YELLOW) + " " * (widths["pr"] - len(r["pr"])))
        parts.append("  " + _ci_icon(r["ci"]))
        parts.append("  " + _color_rebase(r["rebase"]) + " " * (widths["rebase"] - len(r["rebase"])))
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
        "closed_at": None,
    }
    save_state(state)
    print(f"branch {args.name} tracked (dep: {dep or '<default>'})")


def _do_fetch(state: dict, rs: dict, cfg: dict):
    git("fetch", "--all", "--prune", capture=False)
    db = default_branch()

    list_fields = ["number", "headRefName", "baseRefName", "state", "closedAt", "author", "title", "statusCheckRollup"]
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
        if head not in rs["branches"]:
            base = pr["baseRefName"]
            author = pr["author"] or {}
            login = author.get("login") if isinstance(author, dict) else None
            rs["branches"][head] = {
                "pr": pr["number"],
                "depends_on": None if base == db else base,
                "status": pr["state"].lower(),
                "closed_at": pr["closedAt"],
                "external": login != me,
                "title": pr["title"],
                "ci": _summarize_checks(pr["statusCheckRollup"]),
            }

    view_fields = ["state", "baseRefName", "closedAt", "title", "statusCheckRollup"]
    for name, entry in list(rs["branches"].items()):
        if entry["pr"] is None:
            continue
        try:
            data = gh_json(["pr", "view", str(entry["pr"])], view_fields)
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
        if st in ("merged", "closed"):
            entry["closed_at"] = data["closedAt"]
        else:
            entry["closed_at"] = None

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

    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["closed_retention_hours"])
    to_drop: list[str] = []
    for name, entry in rs["branches"].items():
        if entry["pr"] is None:
            continue
        if entry["status"] in ("merged", "closed") and entry["closed_at"]:
            if parse_iso(entry["closed_at"]) <= cutoff:
                to_drop.append(name)
    for name in to_drop:
        del rs["branches"][name]


def cmd_fetch(args):
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    cfg = load_config()
    _do_fetch(state, rs, cfg)
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
    cfg = load_config()
    _do_fetch(state, rs, cfg)

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
        "closed_at": None,
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
            "closed_at": None,
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
    cur = current_branch()
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
    entry = rs["branches"].get(cur)
    parent = (entry or {}).get("depends_on") or default_branch()

    diff = git("diff", "--no-ext-diff", f"origin/{parent}..HEAD")
    if not diff.strip():
        print(f"no diff between {cur} and origin/{parent}")
        return

    prompt = (
        f"Review this git diff (branch `{cur}` vs parent `origin/{parent}`). "
        "If there are no real issues, respond with exactly: No issues.\n"
        "Only flag actual bugs, correctness problems, or security issues. "
        "Do not suggest stylistic improvements or speculative concerns. "
        "You may read files in this repository for context.\n\n"
        f"Diff:\n{diff}\n"
    )

    proc = subprocess.run(
        ["claude", "-p", "--tools", "Read,Glob,Grep"],
        input=prompt,
        text=True,
    )
    if proc.returncode != 0:
        die(f"claude exited with code {proc.returncode}", code=proc.returncode)


_AUTOMERGE_POLL_SECONDS = 20


def cmd_automerge(args):
    branch = args.branch or current_branch()

    listed = gh_json(
        ["pr", "list", "--head", branch, "--state", "open"],
        ["number"],
    )
    if not isinstance(listed, list):
        raise CmdError(f"gh pr list returned non-list: {listed!r}")
    if not listed:
        die(f"no open PR for branch {branch!r}")
    if len(listed) > 1:
        nums = ", ".join(f"#{p['number']}" for p in listed)
        die(f"multiple open PRs for branch {branch!r}: {nums}")
    pr_num = listed[0]["number"]

    view_fields = ["state", "statusCheckRollup", "mergeStateStatus"]
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
        if ci == "fail":
            die(f"PR #{pr_num} has failing CI")
        if merge_state == "BEHIND":
            die(f"PR #{pr_num} is behind its base — needs rebase")
        if merge_state == "DIRTY":
            die(f"PR #{pr_num} has merge conflicts")
        if merge_state == "BLOCKED":
            die(f"PR #{pr_num} is blocked (required reviews or branch protection)")

        if ci in ("pass", "none"):
            print(f"merging PR #{pr_num}…")
            gh("pr", "merge", str(pr_num), "--merge", capture=False)
            print(f"merged PR #{pr_num}")
            return

        print(f"PR #{pr_num} CI: {ci}; sleeping {_AUTOMERGE_POLL_SECONDS}s…")
        time.sleep(_AUTOMERGE_POLL_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr", description="stacked-PR manager")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("show", help="display the PR tree (default)")
    s.add_argument("--all", action="store_true", help="include merged/closed PRs")
    s.add_argument("--org", action="store_true", help="Print the list of PRs as org-mode titles")

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

    sub.add_parser("update", help="sync current PR's title prefix with its dep state")

    sub.add_parser("review", help="run claude (read-only) over the diff vs the branch's parent")

    am = sub.add_parser("automerge", help="poll a branch's PR and merge when CI passes")
    am.add_argument("branch", nargs="?", help="branch to merge (defaults to current branch)")

    return p


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["show"])
    handlers = {
        "show": cmd_show,
        "branch": cmd_branch,
        "create": cmd_create,
        "fetch": cmd_fetch,
        "target": cmd_target,
        "rebase": cmd_rebase,
        "update": cmd_update,
        "review": cmd_review,
        "automerge": cmd_automerge,
    }
    try:
        handlers[args.cmd](args)
    except CmdError as e:
        die(str(e))


if __name__ == "__main__":
    main()

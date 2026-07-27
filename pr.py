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
    list_fields = ["number", "headRefName", "baseRefName", "state", "author", "title", "statusCheckRollup"]
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
            }

    view_fields = ["state", "baseRefName", "title", "statusCheckRollup"]
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
    branch = args.branch or current_branch()
    state = load_state()
    tree = current_tree()
    rs = tree_state(state, tree)
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
    # checkout — you can switch branches while it runs.
    if pr_num is not None:
        diff = gh("pr", "diff", str(pr_num))
        # Report the PR's actual base on the server, which may differ from the
        # locally-tracked parent.
        fields = ["baseRefName", "headRefName"]
        info = gh_json(["pr", "view", str(pr_num)], fields)
        require_keys(info, fields, "gh pr view")
        source = f"PR #{pr_num} (`{info['headRefName']}` vs base `{info['baseRefName']}`)"
    else:
        # No PR tracked: diff the remote refs directly (still not local HEAD).
        git("fetch", "--quiet", "origin", branch, parent, capture=False)
        diff = git("diff", "--no-ext-diff", f"origin/{parent}...origin/{branch}")
        source = f"branch `{branch}` vs base `origin/{parent}`"

    if not diff.strip():
        print(f"no diff for {source}")
        return

    print(f"reviewing {source}")

    prior_review = (entry or {}).get("last_review")
    context_block = ""
    if prior_review and prior_review.strip():
        context_block = (
            "A review of an earlier version of this branch is included below FOR "
            "CONTEXT ONLY. Do not respond to it, repeat it, or assume its findings "
            "still apply — review the current diff on its own merits.\n\n"
            f"Previous review (context only):\n{prior_review.strip()}\n\n"
        )

    prompt = (
        f"Review this git diff ({source}). "
        "If there are no real issues, respond with exactly: No issues.\n"
        "Only flag actual bugs, correctness problems, or security issues. "
        "Do not suggest stylistic improvements or speculative concerns. "
        "You may read files in this repository for context.\n\n"
        f"{context_block}"
        f"Diff:\n{diff}\n"
    )

    # Capture stdout so the review can be persisted; stderr still streams.
    proc = subprocess.run(
        ["claude", "-p", "--tools", "Read,Glob,Grep"],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
    )
    if proc.returncode != 0:
        die(f"claude exited with code {proc.returncode}", code=proc.returncode)

    review_text = (proc.stdout or "").strip()
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


_AUTOMERGE_POLL_SECONDS = 20


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
            time.sleep(_AUTOMERGE_POLL_SECONDS)
            continue

        if ci in ("pass", "none") and merge_state in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
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

    sub.add_parser("update", help="sync current PR's title prefix with its dep state")

    r = sub.add_parser("review", help="run claude (read-only) over a branch's server diff vs its parent")
    r.add_argument("branch", nargs="?", help="branch to review (defaults to current branch)")
    r.add_argument("--show", action="store_true", help="print the last stored review instead of running a new one")

    pr_post = sub.add_parser("post-review", help="post a branch's stored review as a PR comment")
    pr_post.add_argument("branch", nargs="?", help="branch whose stored review to post (defaults to current branch)")

    am = sub.add_parser("automerge", help="poll a branch's PR and merge when CI passes")
    am.add_argument("branch", nargs="?", help="branch to merge (defaults to current branch)")
    am.add_argument("--single", action="store_true", help="merge only the target PR into its current base, without recursing into ancestor PRs")

    return p


def main(argv: list[str] | None = None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["show"] + list(argv)
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
        "post-review": cmd_post_review,
        "automerge": cmd_automerge,
    }
    try:
        handlers[args.cmd](args)
    except CmdError as e:
        die(str(e))


if __name__ == "__main__":
    main()

import pytest

import pr


class FakePR:
    def __init__(self, *, number, head, base="main", title="t",
                 state="OPEN", merge_state="CLEAN", ci="pass"):
        self.number = number
        self.head = head
        self.base = base
        self.title = title
        self.state = state
        self.merge_state = merge_state
        self.ci = ci  # "pass" | "fail" | "pending" | "none"

    def rollup(self):
        if self.ci == "pass":
            return [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
        if self.ci == "fail":
            return [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        if self.ci == "pending":
            return [{"status": "IN_PROGRESS", "conclusion": None}]
        return []


class FakeServer:
    def __init__(self, prs, default_branch="main"):
        self.prs = {p.number: p for p in prs}
        self.by_head = {p.head: p for p in prs}
        self.default_branch = default_branch
        self.list_calls: list[tuple] = []
        self.view_calls: list[int] = []
        self.gh_calls: list[tuple] = []
        # When the test wants update-branch to fail, set this hook.
        self.update_branch_raises: Exception | None = None

    def gh_json(self, argv, fields):
        if argv[:2] == ["pr", "list"]:
            self.list_calls.append((tuple(argv), tuple(fields)))
            if "--head" in argv:
                head = argv[argv.index("--head") + 1]
                matcher = lambda p: p.head == head
            elif "--base" in argv:
                base = argv[argv.index("--base") + 1]
                matcher = lambda p: p.base == base
            else:
                matcher = lambda p: True
            out = []
            for p in self.prs.values():
                if matcher(p) and p.state == "OPEN":
                    full = {
                        "number": p.number,
                        "baseRefName": p.base,
                        "headRefName": p.head,
                        "title": p.title,
                    }
                    out.append({k: full[k] for k in fields})
            return out
        if argv[:2] == ["pr", "view"]:
            pr_num = int(argv[2])
            self.view_calls.append(pr_num)
            p = self.prs[pr_num]
            full = {
                "state": p.state,
                "statusCheckRollup": p.rollup(),
                "mergeStateStatus": p.merge_state,
                "baseRefName": p.base,
                "title": p.title,
                "headRefName": p.head,
            }
            return {k: full[k] for k in fields}
        raise AssertionError(f"unexpected gh_json: {argv!r}")

    def gh(self, *argv, **kwargs):
        self.gh_calls.append(argv)
        if argv[:2] == ("pr", "edit"):
            pr_num = int(argv[2])
            p = self.prs[pr_num]
            if "--base" in argv:
                p.base = argv[argv.index("--base") + 1]
            if "--title" in argv:
                p.title = argv[argv.index("--title") + 1]
        elif argv[:2] == ("pr", "update-branch"):
            if self.update_branch_raises is not None:
                raise self.update_branch_raises
            pr_num = int(argv[2])
            self.prs[pr_num].merge_state = "CLEAN"
        elif argv[:2] == ("pr", "merge"):
            pr_num = int(argv[2])
            self.prs[pr_num].state = "MERGED"
        return ""


def install_server(monkeypatch, server, current_branch_name="my-branch"):
    monkeypatch.setattr(pr, "gh_json", server.gh_json)
    monkeypatch.setattr(pr, "gh", server.gh)
    monkeypatch.setattr(pr, "current_branch", lambda: current_branch_name)
    monkeypatch.setattr(pr, "default_branch", lambda: server.default_branch)
    monkeypatch.setattr(pr.time, "sleep", lambda s: None)


def _merge_calls(server):
    return [c for c in server.gh_calls if c[:2] == ("pr", "merge")]


def test_automerge_single_branch_merges_when_ci_passes(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat")])
    install_server(monkeypatch, s)

    pr.main(["automerge", "feat"])

    assert _merge_calls(s) == [("pr", "merge", "42", "--merge")]
    assert s.prs[42].state == "MERGED"


def test_automerge_defaults_to_current_branch(monkeypatch):
    s = FakeServer([FakePR(number=42, head="my-branch")])
    install_server(monkeypatch, s, current_branch_name="my-branch")

    pr.main(["automerge"])

    first_argv = s.list_calls[0][0]
    head_arg = first_argv[first_argv.index("--head") + 1]
    assert head_arg == "my-branch"


def test_automerge_dies_when_no_pr(monkeypatch):
    s = FakeServer([])
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "missing"])
    assert _merge_calls(s) == []


def test_automerge_dies_on_failing_ci(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat", ci="fail")])
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])
    assert _merge_calls(s) == []


def test_automerge_dies_on_dirty(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat", merge_state="DIRTY")])
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_dies_on_blocked(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat", merge_state="BLOCKED")])
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])


def test_automerge_rebases_when_behind(monkeypatch):
    # BEHIND on first view; FakeServer.gh on update-branch flips it to CLEAN.
    s = FakeServer([FakePR(number=42, head="feat", merge_state="BEHIND")])
    install_server(monkeypatch, s)

    pr.main(["automerge", "feat"])

    update_calls = [c for c in s.gh_calls if c[:2] == ("pr", "update-branch")]
    assert update_calls == [("pr", "update-branch", "42", "--rebase")]
    assert _merge_calls(s) == [("pr", "merge", "42", "--merge")]


def test_automerge_dies_when_update_branch_fails(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat", merge_state="BEHIND")])
    s.update_branch_raises = pr.CmdError("rebase conflict")
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "feat"])
    assert _merge_calls(s) == []


def test_automerge_strips_dep_prefix_before_merge(monkeypatch):
    s = FakeServer([FakePR(number=42, head="feat", title="[dep #99] feat: foo")])
    install_server(monkeypatch, s)

    pr.main(["automerge", "feat"])

    edits = [c for c in s.gh_calls if c[:2] == ("pr", "edit") and "--title" in c]
    assert edits == [("pr", "edit", "42", "--title", "feat: foo")]
    assert s.prs[42].state == "MERGED"


def test_automerge_chain_merges_ancestor_first(monkeypatch):
    # main <- branch_a (#1) <- branch_b (#2)
    s = FakeServer([
        FakePR(number=1, head="branch_a", base="main", title="A"),
        FakePR(number=2, head="branch_b", base="branch_a", title="[dep #1] B"),
    ])
    install_server(monkeypatch, s)

    pr.main(["automerge", "branch_b"])

    merges = _merge_calls(s)
    assert merges == [
        ("pr", "merge", "1", "--merge"),
        ("pr", "merge", "2", "--merge"),
    ]


def test_automerge_chain_retargets_then_rebases(monkeypatch):
    # branch_b points at branch_a; after #1 merges, #2 still has base=branch_a
    # (no auto-retarget). We retarget to main; tests verify edit --base call.
    pr_a = FakePR(number=1, head="branch_a", base="main", title="A")
    pr_b = FakePR(number=2, head="branch_b", base="branch_a",
                  title="[dep #1] B", merge_state="BEHIND")
    s = FakeServer([pr_a, pr_b])
    install_server(monkeypatch, s)

    pr.main(["automerge", "branch_b"])

    base_edits = [c for c in s.gh_calls
                  if c[:2] == ("pr", "edit") and "--base" in c and c[2] == "2"]
    assert base_edits == [("pr", "edit", "2", "--base", "main")]
    update_calls = [c for c in s.gh_calls if c[:2] == ("pr", "update-branch")]
    assert update_calls == [("pr", "update-branch", "2", "--rebase")]
    title_edits = [c for c in s.gh_calls
                   if c[:2] == ("pr", "edit") and "--title" in c and c[2] == "2"]
    assert title_edits == [("pr", "edit", "2", "--title", "B")]


def test_automerge_chain_dies_when_ancestor_has_no_pr(monkeypatch):
    # branch_b points at branch_a, but branch_a has no open PR.
    s = FakeServer([
        FakePR(number=2, head="branch_b", base="branch_a"),
    ])
    install_server(monkeypatch, s)

    with pytest.raises(SystemExit):
        pr.main(["automerge", "branch_b"])
    assert _merge_calls(s) == []


def test_automerge_strips_dep_prefix_on_siblings_after_merge(monkeypatch):
    # main <- #1 (head=a); #2 and #3 both target #1 but aren't in our chain.
    s = FakeServer([
        FakePR(number=1, head="a", base="main", title="A"),
        FakePR(number=2, head="b", base="a", title="[dep #1] B"),
        FakePR(number=3, head="c", base="a", title="[dep #1] C"),
    ])
    install_server(monkeypatch, s)

    pr.main(["automerge", "a"])

    assert s.prs[1].state == "MERGED"
    assert s.prs[2].title == "B"
    assert s.prs[3].title == "C"
    # #2 and #3 should each have been edited (title) but not merged.
    title_edits = [c for c in s.gh_calls
                   if c[:2] == ("pr", "edit") and "--title" in c
                   and c[2] in ("2", "3")]
    assert sorted(title_edits) == sorted([
        ("pr", "edit", "2", "--title", "B"),
        ("pr", "edit", "3", "--title", "C"),
    ])


def test_automerge_dependent_title_update_each_chain_step(monkeypatch):
    # main <- #1 (head=a) <- #2 (head=b); #3 (head=c) hangs off #2 as a sibling.
    # Automerging #2 should: merge #1, update #2/#3 title, retarget+merge #2.
    s = FakeServer([
        FakePR(number=1, head="a", base="main", title="A"),
        FakePR(number=2, head="b", base="a", title="[dep #1] B"),
        FakePR(number=3, head="c", base="b", title="[dep #2] C"),
    ])
    install_server(monkeypatch, s)

    pr.main(["automerge", "b"])

    assert s.prs[1].state == "MERGED"
    assert s.prs[2].state == "MERGED"
    assert s.prs[3].state == "OPEN"
    assert s.prs[3].title == "C"


def test_automerge_polls_until_ci_passes(monkeypatch):
    sleeps = []
    s = FakeServer([FakePR(number=42, head="feat", ci="pending")])

    # After 2 sleeps, flip CI to pass.
    def sleep_then_flip(_):
        sleeps.append(1)
        if len(sleeps) >= 2:
            s.prs[42].ci = "pass"

    monkeypatch.setattr(pr, "gh_json", s.gh_json)
    monkeypatch.setattr(pr, "gh", s.gh)
    monkeypatch.setattr(pr, "current_branch", lambda: "feat")
    monkeypatch.setattr(pr, "default_branch", lambda: "main")
    monkeypatch.setattr(pr.time, "sleep", sleep_then_flip)

    pr.main(["automerge", "feat"])

    assert len(sleeps) == 2
    assert _merge_calls(s) == [("pr", "merge", "42", "--merge")]

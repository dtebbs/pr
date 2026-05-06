import pr


def make_entry(pr_num=None, dep=None, status="open"):
    return {"pr": pr_num, "depends_on": dep, "status": status, "closed_at": None}


def fake_rebase(name, dep):
    return "ok"


def test_empty_visible():
    lines = pr.render_tree({}, "main", fake_rebase)
    assert lines == ["no open tracked branches"]


def test_chain_under_default():
    visible = {
        "a": make_entry(pr_num=101, dep=None),
        "b": make_entry(pr_num=102, dep="a"),
        "c": make_entry(pr_num=None, dep="b", status="no-pr"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert lines[0] == "[main]"
    assert "[a]" in lines[1] and "#101" in lines[1] and "ok" in lines[1]
    assert lines[1].startswith("└─ ")
    assert "[b]" in lines[2] and "#102" in lines[2]
    assert "[c]" in lines[3] and "-" in lines[3] and "ok" in lines[3]


def test_two_roots_use_branch_glyphs():
    visible = {
        "a": make_entry(pr_num=1, dep=None),
        "b": make_entry(pr_num=2, dep=None),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert lines[0] == "[main]"
    assert lines[1].startswith("├─ ")
    assert lines[2].startswith("└─ ")


def test_external_dep_synthetic_header():
    visible = {
        "feature": make_entry(pr_num=10, dep="someones-branch"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert "<external: someones-branch>" in lines
    assert any("[feature]" in l and "#10" in l for l in lines)


def test_tracked_external_renders_inline_with_ext_tag():
    visible = {
        "someones-branch": {"pr": 10, "depends_on": None, "status": "open", "closed_at": None, "external": True},
        "feature": make_entry(pr_num=11, dep="someones-branch"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert not any("<external:" in l for l in lines)
    sb = next(l for l in lines if "[someones-branch]" in l)
    feat = next(l for l in lines if "[feature]" in l)
    assert "(ext)" in sb
    assert "(ext)" not in feat


def test_ext_tag_appears_after_pr_number():
    visible = {
        "x": {"pr": 10, "depends_on": None, "status": "open", "closed_at": None, "external": True},
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    row = lines[1]
    assert row.index("#10") < row.index("(ext)") < row.index("ok")


def test_columns_are_aligned():
    visible = {
        "short": make_entry(pr_num=1, dep=None),
        "much-longer-branch-name": make_entry(pr_num=12345, dep=None),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    rows = [l for l in lines if l.startswith(("├─ ", "└─ "))]
    pr_columns = [l.index("#") for l in rows]
    assert len(set(pr_columns)) == 1


def test_columns_align_across_tree_depth():
    visible = {
        "a": make_entry(pr_num=1, dep=None),
        "deep-name": make_entry(pr_num=22, dep="a"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    rows = [l for l in lines if "#" in l]
    pr_columns = [l.index("#") for l in rows]
    assert len(set(pr_columns)) == 1


def test_rebase_status_propagates():
    visible = {"a": make_entry(pr_num=1, dep=None)}
    def rb(name, dep):
        return "rebase"
    lines = pr.render_tree(visible, "main", rb)
    assert "rebase" in lines[1]


def test_render_appends_title():
    visible = {"a": {**make_entry(pr_num=1, dep=None), "title": "feat: do the thing"}}
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert lines[1].endswith("feat: do the thing")


def test_render_strips_dep_prefix_from_title():
    visible = {
        "a": {**make_entry(pr_num=1, dep=None), "title": "base"},
        "b": {**make_entry(pr_num=2, dep="a"), "title": "[dep #1] feat: stacked"},
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    b_line = next(l for l in lines if "[b]" in l and "#2" in l)
    assert b_line.endswith("feat: stacked")
    assert "[dep #1]" not in b_line


def test_render_omits_title_when_missing():
    visible = {"a": make_entry(pr_num=1, dep=None)}
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert lines[1].rstrip().endswith("ok")


def test_render_does_not_show_open_status():
    visible = {"a": make_entry(pr_num=1, dep=None)}
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert "OPEN" not in lines[1]
    assert "NO-PR" not in lines[1]

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
    assert lines[0] == "main"
    assert "a  #101  OPEN  ok" in lines[1]
    assert lines[1].startswith("└── ")  # only root child
    assert "b  #102  OPEN  ok" in lines[2]
    assert "c  -  NO-PR  ok" in lines[3]


def test_two_roots_use_branch_glyphs():
    visible = {
        "a": make_entry(pr_num=1, dep=None),
        "b": make_entry(pr_num=2, dep=None),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert lines[0] == "main"
    assert lines[1].startswith("├── ")
    assert lines[2].startswith("└── ")


def test_external_dep_synthetic_header():
    visible = {
        "feature": make_entry(pr_num=10, dep="someones-branch"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert "<external: someones-branch>" in lines
    assert any("feature  #10" in l for l in lines)


def test_tracked_external_renders_inline():
    visible = {
        "someones-branch": {"pr": 10, "depends_on": None, "status": "open", "closed_at": None, "external": True},
        "feature": make_entry(pr_num=11, dep="someones-branch"),
    }
    lines = pr.render_tree(visible, "main", fake_rebase)
    assert not any("<external:" in l for l in lines)
    assert any("someones-branch  #10" in l and "(external)" in l for l in lines)
    assert any("feature  #11" in l for l in lines)
    assert not any("feature  #11" in l and "(external)" in l for l in lines)


def test_rebase_status_propagates():
    visible = {"a": make_entry(pr_num=1, dep=None)}
    def rb(name, dep):
        return "needs-rebase"
    lines = pr.render_tree(visible, "main", rb)
    assert "needs-rebase" in lines[1]

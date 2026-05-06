import json
from pathlib import Path

import pytest

import pr


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    p = tmp_path / "pr.json"
    monkeypatch.setattr(pr, "STATE_PATH", p)
    return p


def test_load_state_empty(tmp_state):
    s = pr.load_state()
    assert s == {"version": pr.STATE_VERSION, "trees": {}}


def test_save_load_roundtrip(tmp_state):
    s = pr.load_state()
    rs = pr.tree_state(s, "/work/foo")
    rs["branches"]["a"] = {"pr": 1, "depends_on": None, "status": "open", "closed_at": None}
    pr.save_state(s)

    again = pr.load_state()
    assert again["trees"]["/work/foo"]["branches"]["a"] == s["trees"]["/work/foo"]["branches"]["a"]


def test_save_atomic_no_tmp_left(tmp_state):
    s = pr.load_state()
    pr.tree_state(s, "/work/foo")["branches"]["a"] = {
        "pr": None, "depends_on": None, "status": "no-pr", "closed_at": None
    }
    pr.save_state(s)
    siblings = list(tmp_state.parent.iterdir())
    assert tmp_state in siblings
    assert not any(p.suffix == ".tmp" for p in siblings)


def test_load_state_version_mismatch(tmp_state):
    tmp_state.write_text(json.dumps({"version": 999, "trees": {}}))
    with pytest.raises(SystemExit):
        pr.load_state()


def test_tree_state_isolates_trees(tmp_state):
    s = pr.load_state()
    pr.tree_state(s, "/work/x")["branches"]["foo"] = {
        "pr": 1, "depends_on": None, "status": "open", "closed_at": None
    }
    pr.tree_state(s, "/work/y")["branches"]["bar"] = {
        "pr": 2, "depends_on": None, "status": "open", "closed_at": None
    }
    pr.save_state(s)
    again = pr.load_state()
    assert "foo" in again["trees"]["/work/x"]["branches"]
    assert "bar" in again["trees"]["/work/y"]["branches"]
    assert "foo" not in again["trees"]["/work/y"]["branches"]

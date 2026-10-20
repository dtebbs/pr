import hashlib
import json

import pr


def _key(*argv):
    return hashlib.sha256(json.dumps(list(argv)).encode()).hexdigest()[:16]


def _write_json_fixture(fixture_dir, argv, payload):
    path = fixture_dir / f"{_key(*argv)}.json"
    path.write_text(json.dumps({"stdout": json.dumps(payload), "stderr": "", "exit": 0}))


def test_checkout_branch_name(git_sandbox):
    pr.main(["checkout", "a"])

    assert pr.current_branch() == "a"


def test_checkout_pr_number_resolves_head_branch(git_sandbox, fake_gh_on_path):
    _write_json_fixture(
        fake_gh_on_path,
        ["pr", "view", "7", "--json", "headRefName"],
        {"headRefName": "a"},
    )

    pr.main(["checkout", "7"])

    assert pr.current_branch() == "a"

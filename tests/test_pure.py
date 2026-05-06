from datetime import datetime, timezone

import pr


def test_dep_prefix_strips():
    assert pr.DEP_PREFIX_RE.sub("", "[dep #42] feat: x") == "feat: x"
    assert pr.DEP_PREFIX_RE.sub("", "[dep #1] x") == "x"


def test_dep_prefix_does_not_strip_non_match():
    assert pr.DEP_PREFIX_RE.sub("", "feat: x") == "feat: x"
    assert pr.DEP_PREFIX_RE.sub("", "[dep] x") == "[dep] x"
    assert pr.DEP_PREFIX_RE.sub("", "[dep #abc] x") == "[dep #abc] x"


def test_parse_iso_z_suffix():
    got = pr.parse_iso("2026-04-30T12:34:56Z")
    assert got == datetime(2026, 4, 30, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_iso_offset():
    got = pr.parse_iso("2026-04-30T12:34:56+00:00")
    assert got.tzinfo is not None


def test_fmt_pr():
    assert pr.fmt_pr({"pr": 42}) == "#42"
    assert pr.fmt_pr({"pr": None}) == "-"



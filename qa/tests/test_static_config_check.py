#!/usr/bin/env python3
"""
Unit tests for static_config_check.py.

These tests use made-up configs only — no database. The URL-shape helper is
tested directly. The frozen-enumeration logic is tested on synthetic configs
covering each shape the checker must handle: a fixed list of item URLs, a
generated-URL list of fixed ids, and the healthy shapes (follow links, sweep a
page range, or use a sitemap) that must NOT be flagged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import static_config_check as cl  # noqa: E402


# ---- URL shape helper ------------------------------------------------------
def test_job_detail_urls_detected():
    assert cl._looks_like_detail("https://example.com/global/en/job/142749/sales")
    assert cl._looks_like_detail("https://example.com/empleos/junior-seller-1118394925")
    assert cl._looks_like_detail("https://example.com/positions/12345")


def test_listing_urls_not_detail():
    assert not cl._looks_like_detail("https://example.com/global/en")
    assert not cl._looks_like_detail("https://example.com/careers")
    assert not cl._looks_like_detail("https://example.com/")


def test_parse_json_tolerates_string_and_object():
    assert cl._parse_json('[{"a": 1}]') == [{"a": 1}]
    assert cl._parse_json([{"a": 1}]) == [{"a": 1}]
    assert cl._parse_json(None) is None
    assert cl._parse_json("not json") is None


# ---- frozen-enumeration decision, tested through the real analyze_config ----
# analyze_config takes already-normalized inputs (start_urls, settings,
# has_follow_rule, extract_fields) and returns finding dicts. We pass a full
# extract_fields set so these cases isolate the discovery/enumeration logic and
# do not also trip NO_REQUIRED_SRC.
_ALL_FIELDS = {"title", "description", "employer", "location"}


def _codes(issues):
    return {f["code"] for f in issues}


def test_hardcoded_urls_no_follow_is_frozen():
    # A fixed list of item-page URLs with no follow rule: cannot find new items.
    issues = cl.analyze_config(
        "x", ["https://x.com/job/1", "https://x.com/job/2"],
        settings={}, has_follow_rule=False, extract_fields=_ALL_FIELDS)
    assert "FROZEN_ENUMERATION" in _codes(issues)


def test_hardcoded_id_list_is_frozen_even_with_follow_true():
    # A generated-URL list of fixed ids. The entry's own follow=true must NOT
    # rescue it, because it still enumerates a frozen set of ids.
    gen = [{"template": "https://x.com/{id}.md",
            "vars": {"id": {"type": "list", "values": ["A", "B", "C"]}},
            "follow": True}]
    issues = cl.analyze_config(
        "x", [], settings={"GENERATED_URLS": gen},
        has_follow_rule=False, extract_fields=_ALL_FIELDS)
    assert "FROZEN_ENUMERATION" in _codes(issues)


def test_range_generation_is_healthy():
    # Sweeping a page-number range discovers new items as pages fill up.
    gen = [{"template": "https://x.com/?p={p}",
            "vars": {"p": {"type": "range", "from": 1, "to": 485}}, "follow": True}]
    issues = cl.analyze_config(
        "x", [], settings={"GENERATED_URLS": gen},
        has_follow_rule=False, extract_fields=_ALL_FIELDS)
    assert "FROZEN_ENUMERATION" not in _codes(issues)


def test_follow_links_is_healthy():
    issues = cl.analyze_config(
        "x", ["https://x.com/jobs"], settings={},
        has_follow_rule=True, extract_fields=_ALL_FIELDS)
    assert "FROZEN_ENUMERATION" not in _codes(issues)


def test_sitemap_is_healthy():
    issues = cl.analyze_config(
        "x", [], settings={"USE_SITEMAP": "true"},
        has_follow_rule=False, extract_fields=_ALL_FIELDS)
    assert "FROZEN_ENUMERATION" not in _codes(issues)


# ---- real analyze_config() + lint_files() (the CI entry point) -------------


def test_analyze_config_frozen_url_list():
    issues = cl.analyze_config(
        "x", ["https://x.com/job/1", "https://x.com/job/2"],
        settings={}, has_follow_rule=False, extract_fields={"title", "description", "employer", "location"})
    assert "FROZEN_ENUMERATION" in _codes(issues)


def test_analyze_config_missing_required_field():
    issues = cl.analyze_config(
        "x", ["https://x.com/jobs"], settings={}, has_follow_rule=True,
        extract_fields={"title", "description"})  # employer + location missing
    assert "NO_REQUIRED_SRC" in _codes(issues)


def test_lint_files_on_sections_config(tmp_path):
    # a frozen sections config written to disk — the CI path
    import json as _json
    cfg = {
        "name": "frozen_demo",
        "start_urls": ["https://x.com/job/111", "https://x.com/job/222"],
        "sections": [{"match": ["/job/.*"], "follow": False,
                      "extract": {"title": {"css": "h1"}, "description": {"css": "div"},
                                  "employer": {"css": "a"}, "location": {"css": "span"}}}],
        "settings": {},
    }
    p = tmp_path / "frozen_demo.json"
    p.write_text(_json.dumps(cfg))
    findings = cl.lint_files([str(p)])
    assert any(f["code"] == "FROZEN_ENUMERATION" for f in findings)


def test_lint_files_healthy_follow_config(tmp_path):
    import json as _json
    cfg = {
        "name": "healthy_demo",
        "start_urls": ["https://x.com/jobs"],
        "sections": [
            {"match": [".*"], "follow": True},
            {"match": ["/job/.*"], "follow": False,
             "extract": {"title": {"css": "h1"}, "description": {"css": "d"},
                         "employer": {"css": "a"}, "location": {"css": "s"}}}],
        "settings": {},
    }
    p = tmp_path / "healthy_demo.json"
    p.write_text(_json.dumps(cfg))
    assert cl.lint_files([str(p)]) == []


def test_lint_files_unreadable(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{ not valid json")
    findings = cl.lint_files([str(p)])
    assert any(f["code"] == "UNREADABLE" for f in findings)


def _main():
    import tempfile
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for name, fn in fns:
        try:
            # supply a tmp dir to tests that take one
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                import pathlib
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
                fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {e!r}")
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()

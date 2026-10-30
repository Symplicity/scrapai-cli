#!/usr/bin/env python3
"""
Unit tests for static_quality_check.py.

These tests use made-up field values only — no database, no network. They check
each rule twice:
  1. a bad value IS flagged, and
  2. a good value is NOT flagged (so the checks do not raise false alarms).

Run:
    python -m pytest qa/tests/ -q
    # or without pytest:
    python qa/tests/test_static_quality_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import static_quality_check as dq  # noqa: E402


def codes(issues):
    return {i["code"] for i in issues}


# ---------------------------------------------------------------------------
# load_meta — metadata_json may arrive as a JSON string (SQLite) or an
# already-parsed dict (PostgreSQL json/jsonb). Both must work.
# ---------------------------------------------------------------------------
def test_load_meta_json_string():
    meta, ok = dq.load_meta('{"description": "x", "location": "NY"}')
    assert ok and meta == {"description": "x", "location": "NY"}


def test_load_meta_already_dict():
    d = {"description": "x"}
    meta, ok = dq.load_meta(d)
    assert ok and meta is d


def test_load_meta_empty_is_ok():
    assert dq.load_meta(None) == ({}, True)
    assert dq.load_meta("") == ({}, True)


def test_load_meta_bad_string_is_not_ok():
    meta, ok = dq.load_meta("{ not json")
    assert meta == {} and ok is False


def test_load_meta_non_object_json_is_ok_but_empty():
    # valid JSON that is not an object (e.g. a list) yields no fields
    assert dq.load_meta("[1, 2]") == ({}, True)


# ---------------------------------------------------------------------------
# TITLE
# ---------------------------------------------------------------------------
def test_title_good():
    assert dq.check_title("Senior Software Engineer") == []


def test_title_empty_is_critical():
    issues = dq.check_title("")
    assert issues[0]["sev"] == "crit" and issues[0]["code"] == "title_empty"


def test_title_none_is_critical():
    assert "title_empty" in codes(dq.check_title(None))


def test_title_junk_label():
    assert "title_junk" in codes(dq.check_title("Apply Now"))


def test_title_too_long_is_sentence():
    assert "title_long" in codes(dq.check_title("x" * 200))


def test_title_nonlatin_flagged():
    # mostly non-Latin script
    assert "title_nonlatin" in codes(dq.check_title("エンジニア募集 シニア"))


def test_title_international_but_latin_ok():
    # Accented Latin text is still Latin script — must NOT be flagged.
    assert dq.check_title("Contrôleur de Gestion France - CDD 9 mois (H/F)") == []


# ---------------------------------------------------------------------------
# DESCRIPTION
# ---------------------------------------------------------------------------
def test_description_good_long():
    assert dq.check_description("A real job description. " * 50) == []


def test_description_empty_is_critical():
    assert "desc_empty" in codes(dq.check_description(""))


def test_description_too_short_is_critical():
    assert "desc_too_short" in codes(dq.check_description("Great job!"))


def test_description_stub_is_warn():
    # between DESC_MIN_LEN (120) and DESC_STUB_LEN (250) => warn, not critical
    text = ("We are hiring a registered nurse for the day shift at our downtown "
            "clinic. Apply today to join a supportive and growing care team here.")
    assert 120 <= len(text) < 250, len(text)
    assert "desc_stub" in codes(dq.check_description(text))


def test_description_boilerplate_opening_flagged():
    text = "We use cookies to improve your experience. " + ("filler " * 60)
    assert "desc_boilerplate" in codes(dq.check_description(text))


def test_description_privacy_in_footer_not_flagged():
    # A long, real description that only mentions "privacy policy" at the very
    # end must stay quiet — the boilerplate rule looks at the opening, not a
    # stray phrase in a footer.
    text = ("About the role. " * 100) + " See our privacy policy for details."
    assert "desc_boilerplate" not in codes(dq.check_description(text))


# ---------------------------------------------------------------------------
# EMPLOYER
# ---------------------------------------------------------------------------
def test_employer_good():
    assert dq.check_employer("Acme Corporation", "example_spider") == []


def test_employer_empty_is_critical():
    assert "employer_empty" in codes(dq.check_employer(None, "x"))


def test_employer_sentence_flagged():
    sentence = "We are a leading global provider of innovative solutions for enterprises"
    assert "employer_sentence" in codes(dq.check_employer(sentence, "x"))


def test_employer_acronym_vs_expansion_is_info_not_warn():
    # Full name stored, initials expected (same company). A name that does not
    # match the expectation must be reported at most as info, never warn/crit.
    issues = dq.check_employer("Acme Data Systems", "example_spider", "ADS")
    sev_for_mismatch = [i["sev"] for i in issues if i["code"] == "employer_mismatch"]
    # either no mismatch raised, or raised only at info level (never warn/crit)
    assert all(s == "info" for s in sev_for_mismatch)


# ---------------------------------------------------------------------------
# LOCATION
# ---------------------------------------------------------------------------
def test_location_good_city_state():
    assert dq.check_location("Chester, New York") == []


def test_location_empty_is_critical():
    assert "location_empty" in codes(dq.check_location(""))


def test_location_workplace_flag_flagged():
    assert "location_is_flag" in codes(dq.check_location("Hybrid"))
    assert "location_is_flag" in codes(dq.check_location("Remote"))


def test_location_street_address_flagged():
    # A street address (number + street-type word) means the selector grabbed a
    # building address instead of a city/region.
    assert "location_street_address" in codes(
        dq.check_location("1080 Third Crossing Way, Mebane, NC 27302"))
    assert "location_street_address" in codes(
        dq.check_location("Main Clinic - 2320 E 93rd St"))


def test_location_region_hybrid_combo_not_street():
    # 'United States - Remote' / 'Virginia - Hybrid' are region+flag, not a
    # street address — must not raise the street-address signal.
    assert "location_street_address" not in codes(
        dq.check_location("United States - Remote"))


def test_location_plain_city_not_street():
    assert "location_street_address" not in codes(dq.check_location("London"))


# ---------------------------------------------------------------------------
# SCORING / TRIAGE
# ---------------------------------------------------------------------------
def _make_bucket(**overrides):
    """Build a per-spider bucket with the full shape analyze() produces, so a
    test stays valid even if score_spider starts reading more keys."""
    from collections import Counter
    bucket = {
        "rows": 0, "row_issues": [], "code_counts": Counter(),
        "crit_rows": 0, "warn_rows": 0, "desc_lens": [], "clean_rows": 0,
    }
    bucket.update(overrides)
    return bucket


def test_scoring_critical_when_many_bad_rows():
    # more than a quarter of rows have a critical problem => CRITICAL
    bucket = _make_bucket(rows=15, crit_rows=8)
    _, verdict = dq.score_spider(bucket)
    assert verdict == "CRITICAL"


def test_scoring_review_when_some_warn_rows():
    # 2 of 4 rows warn => weighted density (2*3)/4 = 1.5 => REVIEW exactly.
    bucket = _make_bucket(rows=4, warn_rows=2)
    _, verdict = dq.score_spider(bucket)
    assert verdict == "REVIEW"


def test_scoring_ok_when_clean():
    bucket = _make_bucket(rows=10)
    _, verdict = dq.score_spider(bucket)
    assert verdict == "OK"


# ---------------------------------------------------------------------------
# tiny runner so it works without pytest installed
# ---------------------------------------------------------------------------
def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()

#!/usr/bin/env python3
"""
Flags scraped items whose fields look wrong.

Reads saved items (never writing) and flags values that look wrong: empty, a
leftover button label instead of real text, a cookie banner saved as a
description, a work-style tag like "Remote" where a location should be, and so
on. Items are grouped by spider (one spider = one source website) and each
spider gets a verdict, so a human only looks at the suspicious ones:
    CRITICAL  many items are unusable
    REVIEW    at least one unusable item, or a lot of suspicious ones
    WATCH     only mild/odd values, probably fine
    OK        nothing flagged

Field layout: title is a plain column; description, employer and location are
keys inside a JSON column named metadata_json; published_date is optional and
ignored here.

Checks (severity in brackets — crit makes a row unusable, warn is suspicious,
info is a minor note):

  title
    [crit] title_empty       title is null or blank
    [crit] title_junk        title is a UI label, not a real title
                             e.g. "Apply Now", "Save Job"
    [warn] title_short       title shorter than 3 chars
    [warn] title_long        title longer than 160 chars — likely a whole
                             sentence captured instead of a title
    [warn] title_nonlatin    title is mostly non-Latin script, which usually
                             means the wrong language/text was captured
                             e.g. "エンジニア募集" when English was expected

  description
    [crit] desc_empty        description is null or blank
    [crit] desc_too_short    shorter than 120 chars — too short to be a real
                             description, e.g. "Great job!"
    [warn] desc_stub         120–250 chars — present but suspiciously thin
    [warn] desc_boilerplate  the text is (or opens with) a cookie/consent or
                             browser notice rather than the job body
                             e.g. "We use cookies to improve your experience…"
    [crit] desc_junk         description is a UI label, e.g. "Read more"

  employer
    [crit] employer_empty    employer is null or blank
    [crit] employer_junk     employer is a UI label
    [warn] employer_sentence looks like a sentence, not a company name (over 80
                             chars or more than 8 words) — e.g. "We are a
                             leading global provider of…" captured as employer
    [info] employer_mismatch stored employer does not resemble the expected one
                             passed via --tracking (only when that is provided)

  location
    [crit] location_empty    location is null or blank
    [crit] location_junk     location is a UI label
    [warn] location_is_flag  value is a work-style tag, not a place
                             e.g. "Remote", "Hybrid", "On-site"
    [warn] location_long     longer than 120 chars — likely the wrong field
    [warn] location_street_address
                             looks like a building address, not a city/region
                             e.g. "123 Main St" instead of "Austin, TX"
    [info] location_shape    none of the above but an unusual shape for a place

  whole-item / per-spider
    meta_unparseable         a row's metadata_json could not be read as an object
    desc_len_outlier         within one spider, a description whose length is a
                             statistical outlier vs the others — catches one
                             truncated/overlong row hiding among good ones
                             (needs at least 5 rows to judge)

Usage:
    python static_quality_check.py                      # table summary, worst first
    python static_quality_check.py --project <name>     # restrict to one project
    python static_quality_check.py --spider <name> --rows   # list a spider's bad items
    python static_quality_check.py --json report.json   # machine-readable, for CI
    python static_quality_check.py --db postgresql://user:pass@host/dbname

Exit status: 0 if nothing is flagged REVIEW/CRITICAL, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

from _db import connect_ro

# Tokens that should never *be* a field value (they mean the selector grabbed a
# button/label/boilerplate instead of real content). Matched case-insensitively
# against the whole trimmed value.
JUNK_VALUES = {
    "apply", "apply now", "apply here", "save job", "save", "share",
    "see more", "read more", "learn more", "view job", "back", "next",
    "n/a", "na", "none", "null", "-", "--", "tbd", "unknown",
    "accept all cookies", "cookie policy", "sign in", "log in",
}

# A location that is really a workplace-type flag, not a place.
WORKPLACE_FLAGS = {"remote", "hybrid", "on site", "on-site", "onsite", "virtual",
                   "flexible", "work from home", "wfh"}

# Cookie/consent/boilerplate fingerprints that sometimes land in `description`.
BOILERPLATE_HINTS = (
    "we use cookies", "this website uses cookies", "cookie preferences",
    "enable javascript", "please enable javascript", "your browser",
    "accept all", "privacy policy", "terms of service",
)

# Tunable thresholds. Adjust these if the checks are too strict or too loose.
DESC_MIN_LEN = 120          # a real job description is longer than this
DESC_STUB_LEN = 250         # below this is suspicious even if non-empty
TITLE_MIN_LEN = 3
TITLE_MAX_LEN = 160         # longer than this => probably a sentence, not a title
EMPLOYER_MAX_LEN = 80       # longer => probably a description sentence leaked in
EMPLOYER_MAX_WORDS = 8
LOCATION_MAX_LEN = 120
OUTLIER_Z = 3.0             # per-spider description-length outlier cutoff
MIN_ROWS_FOR_STATS = 5      # need at least this many rows to trust distributions

# Fraction of letters in a value that are non-ASCII. A high fraction means the
# text is mostly non-Latin script, which usually signals the wrong language was
# captured or the text is garbled. The content here is expected to be English.
NON_LATIN_MAX_FRAC = 0.30


def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def load_meta(value):
    """Return the metadata_json value as a dict.

    SQLite stores this column as a JSON string; PostgreSQL's json/jsonb type is
    returned already parsed as a dict. Accept either. Returns (meta, ok) where
    ok is False only when a non-empty value could not be interpreted."""
    if value is None or value == "":
        return {}, True
    if isinstance(value, dict):
        return value, True
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}, False
        return (parsed, True) if isinstance(parsed, dict) else ({}, True)
    return {}, False


def non_ascii_frac(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    non_ascii = sum(1 for c in letters if ord(c) > 127)
    return non_ascii / len(letters)


def looks_like_location(v: str) -> bool:
    """Cheap positive signal: 'City, ST' / 'City, Country' / has a comma or a
    multi-word place. We don't require it, we only use its absence as a weak
    negative when combined with other signals."""
    v = v.strip()
    if "," in v:
        return True
    # single word that is a known place-ish token is still ok (e.g. 'London')
    return bool(re.match(r"^[A-Z][\w .'-]{1,}$", v))


# ---- per-row checks: return a list of (severity, code, note) ----------------
# ---- per-row checks: each returns a list of issue dicts --------------------
# Each issue is {"sev": ..., "code": ..., "note": ...} where sev is:
#   'crit' (row is unusable) | 'warn' (suspicious) | 'info' (minor)

def issue(sev, code, note):
    """Build one issue record."""
    return {"sev": sev, "code": code, "note": note}


def check_title(title):
    out = []
    if is_empty(title):
        out.append(issue("crit", "title_empty", "title is null/empty"))
        return out
    t = title.strip()
    low = t.lower()
    if low in JUNK_VALUES:
        out.append(issue("crit", "title_junk", f"title is a UI label: {t!r}"))
    if len(t) < TITLE_MIN_LEN:
        out.append(issue("warn", "title_short", f"title too short: {t!r}"))
    if len(t) > TITLE_MAX_LEN:
        out.append(issue("warn", "title_long", f"title {len(t)} chars — likely a sentence"))
    if non_ascii_frac(t) > NON_LATIN_MAX_FRAC:
        out.append(issue("warn", "title_nonlatin", "title mostly non-Latin — wrong language?"))
    return out


def check_description(desc):
    out = []
    if is_empty(desc):
        out.append(issue("crit", "desc_empty", "description is null/empty"))
        return out
    d = desc.strip()
    low = d.lower()
    if len(d) < DESC_MIN_LEN:
        out.append(issue("crit", "desc_too_short", f"description only {len(d)} chars"))
    elif len(d) < DESC_STUB_LEN:
        out.append(issue("warn", "desc_stub", f"description short ({len(d)} chars) — maybe a stub"))
    # Boilerplate only matters when it *dominates* the text. A long, real
    # description that merely has 'privacy policy' in a footer is fine; a short
    # value that is mostly a cookie banner is not. Fire only when the text is
    # short OR a boilerplate hint sits in the opening of the value.
    hit = next((h for h in BOILERPLATE_HINTS if h in low), None)
    if hit:
        opening = low[:200]
        if len(d) < DESC_STUB_LEN or hit in opening:
            out.append(issue("warn", "desc_boilerplate",
                             f"description opens with/consists of boilerplate: {hit!r}"))
    if low in JUNK_VALUES:
        out.append(issue("crit", "desc_junk", f"description is a UI label: {d[:40]!r}"))
    return out


def check_employer(employer, spider_name, expected_employer=None):
    out = []
    if is_empty(employer):
        out.append(issue("crit", "employer_empty", "employer is null/empty"))
        return out
    e = employer.strip()
    low = e.lower()
    if low in JUNK_VALUES:
        out.append(issue("crit", "employer_junk", f"employer is a UI label: {e!r}"))
    if len(e) > EMPLOYER_MAX_LEN or len(e.split()) > EMPLOYER_MAX_WORDS:
        out.append(issue("warn", "employer_sentence", f"employer looks like a sentence: {e[:50]!r}"))
    # Optional cross-check: if an expected employer name was supplied (see
    # --tracking), warn when the stored employer does not resemble it.
    if expected_employer:
        if not _soft_match(e, expected_employer):
            out.append(issue("info", "employer_mismatch",
                             f"employer {e!r} != expected {expected_employer!r} (verify)"))
    return out


def check_location(location):
    out = []
    if is_empty(location):
        out.append(issue("crit", "location_empty", "location is null/empty"))
        return out
    loc = location.strip()
    low = loc.lower()
    if low in JUNK_VALUES:
        out.append(issue("crit", "location_junk", f"location is a UI label: {loc!r}"))
    if low in WORKPLACE_FLAGS:
        out.append(issue("warn", "location_is_flag",
                         f"location is a workplace-type flag, not a place: {loc!r}"))
    if len(loc) > LOCATION_MAX_LEN:
        out.append(issue("warn", "location_long", f"location {len(loc)} chars — likely wrong field"))
    # A street address (a number plus a street-type word, e.g. "123 Main St")
    # means the selector grabbed a building address instead of a city/region.
    elif _looks_like_street_address(loc):
        out.append(issue("warn", "location_street_address",
                         f"location looks like a street address, not a city: {loc!r}"))
    elif not looks_like_location(loc) and low not in WORKPLACE_FLAGS:
        out.append(issue("info", "location_shape", f"location has unusual shape: {loc!r}"))
    return out


_STREET_TYPES = (
    "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road", "dr",
    "drive", "ln", "lane", "way", "ct", "court", "pkwy", "parkway", "hwy",
    "highway", "ste", "suite", "fl", "floor",
)


def _looks_like_street_address(loc: str) -> bool:
    """A number followed somewhere by a street-type token => street address."""
    low = loc.lower()
    if not re.search(r"\b\d{1,6}\b", low):
        return False
    toks = set(re.findall(r"[a-z]+", low))
    return bool(toks & set(_STREET_TYPES))


def _soft_match(a: str, b: str) -> bool:
    """True if two company names share at least one meaningful word, after
    dropping common legal/filler words. This treats name variants as a match,
    e.g. "Acme" vs "Acme Inc", or "Acme Corporation" vs "Acme Group"."""
    def toks(s):
        return set(re.findall(r"[a-z0-9]+", s.lower())) - {
            "inc", "llc", "ltd", "ag", "sa", "corp", "co", "group", "the",
            "international", "holdings", "plc", "gmbh",
        }
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return True  # can't judge
    return len(ta & tb) > 0


def load_expected_employers(tracking_csv):
    """Read a CSV of expected employer names into {spider_name: employer}.

    The CSV must have 'spider_name' and 'employer' columns. It is optional; if
    the path is missing, this returns an empty map and the employer cross-check
    is simply skipped."""
    out = {}
    if not tracking_csv or not os.path.exists(tracking_csv):
        return out
    import csv
    with open(tracking_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sp = (row.get("spider_name") or "").strip()
            emp = (row.get("employer") or "").strip()
            if sp:
                out[sp] = emp
    return out


SEV_WEIGHT = {"crit": 10, "warn": 3, "info": 1}


def analyze(db_path, project, tracking_csv, only_spider=None):
    conn = connect_ro(db_path)
    expected = load_expected_employers(tracking_csv)

    q = """
        SELECT s.name AS spider, i.id, i.url, i.title, i.published_date,
               i.metadata_json
        FROM scraped_items i JOIN spiders s ON i.spider_id = s.id
        WHERE s.project = ?
    """
    params = [project]
    if only_spider:
        q += " AND s.name = ?"
        params.append(only_spider)

    per_spider = defaultdict(lambda: {
        "rows": 0, "row_issues": [], "code_counts": Counter(),
        "crit_rows": 0, "warn_rows": 0, "desc_lens": [], "clean_rows": 0,
    })

    for r in conn.execute(q, params):
        sp = r["spider"]
        bucket = per_spider[sp]
        bucket["rows"] += 1
        meta, ok = load_meta(r["metadata_json"])
        if not ok:
            bucket["code_counts"]["meta_unparseable"] += 1

        desc = meta.get("description")
        if isinstance(desc, str):
            bucket["desc_lens"].append(len(desc.strip()))

        issues = []
        issues += check_title(r["title"])
        issues += check_description(desc)
        issues += check_employer(meta.get("employer"), sp, expected.get(sp))
        issues += check_location(meta.get("location"))

        sevset = {i["sev"] for i in issues}
        if "crit" in sevset:
            bucket["crit_rows"] += 1
        elif "warn" in sevset:
            bucket["warn_rows"] += 1
        if not issues:
            bucket["clean_rows"] += 1

        for i in issues:
            bucket["code_counts"][i["code"]] += 1
        if issues:
            bucket["row_issues"].append({
                "id": r["id"], "url": r["url"],
                "issues": issues,
            })

    # statistical pass: per-spider description-length outliers
    for sp, b in per_spider.items():
        lens = b["desc_lens"]
        if len(lens) >= MIN_ROWS_FOR_STATS:
            mean = statistics.mean(lens)
            stdev = statistics.pstdev(lens)
            if stdev > 0:
                outliers = [l for l in lens if abs(l - mean) / stdev > OUTLIER_Z]
                if outliers:
                    b["code_counts"]["desc_len_outlier"] += len(outliers)
            b["desc_len_mean"] = round(mean, 1)
            b["desc_len_stdev"] = round(stdev, 1)

    conn.close()
    return per_spider, expected


def score_spider(b):
    """Return (score, verdict). Score is a weighted issue density per row."""
    rows = max(b["rows"], 1)
    weighted = (
        b["crit_rows"] * SEV_WEIGHT["crit"]
        + b["warn_rows"] * SEV_WEIGHT["warn"]
    )
    density = weighted / rows
    crit_frac = b["crit_rows"] / rows
    if crit_frac >= 0.25 or density >= 5:
        verdict = "CRITICAL"
    elif b["crit_rows"] > 0 or density >= 1.5:
        verdict = "REVIEW"
    elif b["warn_rows"] > 0:
        verdict = "WATCH"
    else:
        verdict = "OK"
    return round(density, 2), verdict


def main():
    ap = argparse.ArgumentParser(
        description="Check the quality of scraped rows stored in the database.")
    ap.add_argument("--db", default=None,
                    help="database URL or sqlite path; "
                         "defaults to $DATABASE_URL, then local scrapai.db")
    ap.add_argument("--project", default="jobfeed",
                    help="only check spiders in this project")
    ap.add_argument("--tracking", default="data/jobfeed/tracking.csv",
                    help="optional CSV of expected employer names "
                         "(columns: spider_name, employer); skipped if missing")
    ap.add_argument("--spider", default=None, help="restrict to one spider")
    ap.add_argument("--rows", action="store_true", help="print flagged rows")
    ap.add_argument("--json", metavar="PATH", help="write machine-readable report")
    ap.add_argument("--all", action="store_true", help="show OK spiders too")
    args = ap.parse_args()

    per_spider, _ = analyze(args.db, args.project, args.tracking, args.spider)

    results = []
    for sp, b in per_spider.items():
        density, verdict = score_spider(b)
        results.append((verdict, density, sp, b))

    order = {"CRITICAL": 0, "REVIEW": 1, "WATCH": 2, "OK": 3}
    results.sort(key=lambda t: (order[t[0]], -t[1]))

    flagged = [r for r in results if r[0] in ("CRITICAL", "REVIEW")]

    print(f"\nData-quality report — project={args.project}  "
          f"spiders={len(results)}  flagged={len(flagged)}\n")
    print(f"{'VERDICT':9} {'SCORE':>6} {'ROWS':>5} {'CRIT':>5} {'WARN':>5}  SPIDER / top issues")
    print("-" * 92)
    for verdict, density, sp, b in results:
        if verdict == "OK" and not args.all:
            continue
        top = ", ".join(f"{c}×{n}" for c, n in b["code_counts"].most_common(3))
        print(f"{verdict:9} {density:>6} {b['rows']:>5} {b['crit_rows']:>5} "
              f"{b['warn_rows']:>5}  {sp}")
        if top:
            print(f"{'':29}   {top}")

    if args.rows:
        for verdict, density, sp, b in results:
            if not b["row_issues"]:
                continue
            print(f"\n=== {sp} ({verdict}) ===")
            for ri in b["row_issues"][:20]:
                codes = ", ".join(f"[{i['sev']}]{i['code']}" for i in ri["issues"])
                print(f"  #{ri['id']} {codes}")
                print(f"      {ri['url']}")

    if args.json:
        payload = {
            "project": args.project,
            "spiders": [
                {
                    "spider": sp, "verdict": v, "score": d,
                    "rows": b["rows"], "crit_rows": b["crit_rows"],
                    "warn_rows": b["warn_rows"], "clean_rows": b["clean_rows"],
                    "codes": dict(b["code_counts"]),
                    "desc_len_mean": b.get("desc_len_mean"),
                    "desc_len_stdev": b.get("desc_len_stdev"),
                }
                for v, d, sp, b in results
            ],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.json}")

    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()

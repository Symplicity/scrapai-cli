#!/usr/bin/env python3
"""
Flags spider configs that will stop finding new content over time.

Inspects the config, not the scraped data. A config that lists specific pages
to scrape (instead of discovering them) quietly returns nothing once those
pages go away:
    Bad     start URLs are a fixed list of item pages ("/jobs/1001",
            "/jobs/1002") with no link-following, page numbering, or sitemap.
    Healthy starts on a listing and follows links, OR sweeps page numbers /
            dates, OR reads a sitemap — so new items appear on their own.

Findings:
  FROZEN_ENUMERATION — the config names specific pages and has no way to find
                       new ones, so it decays as those pages expire. Triggered
                       by either shape:
                         * start URLs are item pages with no follow rule
                           e.g. start_urls = ["/jobs/1001", "/jobs/1002"]
                         * a GENERATED_URLS "list" variable of fixed ids
                           e.g. vars: {id: {type: "list", values: ["A","B"]}}
  NO_DISCOVERY       — no follow rule, no generated URLs, no sitemap, and at
                       most one start URL: it only ever scrapes a single fixed
                       page, often a leftover from testing.
  NO_REQUIRED_SRC    — a required field (description / employer / location) has
                       no selector anywhere in the config, so the spider can
                       never populate it.
  UNREADABLE         — (file mode only) the config file is not valid JSON.

A config is considered HEALTHY (no FROZEN_ENUMERATION) when it can discover new
pages on its own: a follow rule, a GENERATED_URLS "range"/"date" variable or a
POST sweep, a sitemap (USE_SITEMAP), or PAGINATED_LISTINGS.
    e.g. vars: {page: {type: "range", from: 1, to: 500}}  -> healthy

Two ways to run (same checks):
  DATABASE mode — read spider configs from the database:
      python static_config_check.py --project <name>
  FILE mode — lint config JSON files directly, no database (useful as a CI
      check before a config is deployed):
      python static_config_check.py path/to/config1.json path/to/config2.json

Exit status: 1 if any FROZEN_ENUMERATION / NO_REQUIRED_SRC / UNREADABLE, else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from _db import connect_ro

# A start URL that looks like a specific job detail page (has an id-ish segment)
# rather than a listing/section landing page.
JOB_DETAIL_HINTS = re.compile(
    r"/job/|/jobs/|/empleos/|/positions/|/career[s]?/.+/|-\d{5,}", re.I)


def _looks_like_detail(url):
    return bool(JOB_DETAIL_HINTS.search(url or ""))


def _parse_json(val):
    """Settings/JSON columns may be stored as a JSON string or already parsed."""
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def finding(code, note):
    """Build one finding record."""
    return {"code": code, "note": note}


def analyze_config(name, start_urls, settings, has_follow_rule, extract_fields):
    """Core shape analysis, independent of where the config came from.

    Args (already normalized by a loader):
      name              spider name (for reporting)
      start_urls        list[str]
      settings          dict (GENERATED_URLS / USE_SITEMAP / PAGINATED_LISTINGS)
      has_follow_rule   bool — does any rule/section follow links?
      extract_fields    set[str] — field names the config extracts (for coverage)

    Returns a list of finding dicts, each {"code": ..., "note": ...}.
    """
    start_urls = start_urls or []
    settings = settings or {}
    n_detail_starts = sum(1 for u in start_urls if _looks_like_detail(u))

    gen = _parse_json(settings.get("GENERATED_URLS"))
    gen_list = gen_dynamic = False
    if gen:
        for entry in (gen if isinstance(gen, list) else [gen]):
            for v in (entry.get("vars") or {}).values():
                if isinstance(v, dict) and v.get("type") == "list":
                    gen_list = True
                if isinstance(v, dict) and v.get("type") in ("range", "date"):
                    gen_dynamic = True
        if any((e.get("method") or "").upper() == "POST" for e in
               (gen if isinstance(gen, list) else [gen])):
            gen_dynamic = True

    sitemap = str(settings.get("USE_SITEMAP", "")).lower() in ("true", "1")
    paginated = bool(settings.get("PAGINATED_LISTINGS"))

    has_discovery = bool(has_follow_rule) or gen_dynamic or sitemap or paginated
    enumerates_identities = (n_detail_starts >= 1 and not has_follow_rule) or gen_list

    issues = []
    if enumerates_identities and not has_discovery:
        how = []
        if gen_list:
            how.append("GENERATED_URLS `list` of hard-coded ids")
        if n_detail_starts and not has_follow_rule:
            how.append(f"{n_detail_starts} hard-coded job URL(s)")
        issues.append(finding("FROZEN_ENUMERATION",
                      f"enumerates specific jobs ({'; '.join(how)}) with no "
                      "discovery (no follow/range/sitemap) => will decay as "
                      "those jobs expire; a healthy config discovers ids instead"))
    elif not has_discovery and len(start_urls) <= 1 and not gen_list:
        issues.append(finding("NO_DISCOVERY",
                      "no follow rule, no generated/sitemap discovery, "
                      f"{len(start_urls)} start_url => scrapes a fixed tiny set"))

    for fld in ("description", "employer", "location"):
        if extract_fields and fld not in extract_fields:
            issues.append(finding("NO_REQUIRED_SRC",
                          f"required field {fld!r} has no selector/section source"))
    return issues


def lint_db(db_path, project):
    """Loader: feed analyze_config() from the DB (data-side use)."""
    conn = connect_ro(db_path)
    spiders = conn.execute(
        "SELECT id, name, start_urls, callbacks_config FROM spiders WHERE project=?",
        [project]).fetchall()
    settings_by = {}
    for r in conn.execute("SELECT spider_id, key, value FROM spider_settings"):
        settings_by.setdefault(r["spider_id"], {})[r["key"]] = r["value"]

    findings = []
    for s in spiders:
        rules = conn.execute(
            "SELECT follow FROM spider_rules WHERE spider_id=?", [s["id"]]).fetchall()
        has_follow = any(r["follow"] for r in rules)
        start_urls = _parse_json(s["start_urls"]) or []
        cb = _parse_json(s["callbacks_config"]) or {}
        extract_fields = set()
        for callback in (cb or {}).values():
            ex = (callback or {}).get("extract", {}) if isinstance(callback, dict) else {}
            extract_fields.update(ex.keys())
        for f in analyze_config(
                s["name"], start_urls, settings_by.get(s["id"], {}),
                has_follow, extract_fields):
            findings.append({"spider": s["name"], **f})
    conn.close()
    return findings


def lint_files(paths):
    """Loader: feed analyze_config() from raw config JSON files (CI use, no DB).

    Reads the `sections` authoring format (what gets committed): a config has
    top-level `start_urls`, `settings`, and `sections[]` where each section has
    `match`, optional `follow`, and optional `extract` (a field->selector dict,
    "auto", or absent)."""
    findings = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            findings.append({"spider": os.path.basename(path),
                             "code": "UNREADABLE", "note": f"cannot parse: {e}"})
            continue
        name = cfg.get("name") or os.path.splitext(os.path.basename(path))[0]
        start_urls = cfg.get("start_urls") or []
        settings = cfg.get("settings") or {}
        sections = cfg.get("sections") or []

        has_follow = any(sec.get("follow") for sec in sections if isinstance(sec, dict))
        extract_fields = set()
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            ex = sec.get("extract")
            if isinstance(ex, dict):
                extract_fields.update(ex.keys())
            elif ex == "auto":
                extract_fields.update(("title", "content", "author", "published_date"))
        # legacy rules+callbacks shape: fall back to those if no sections
        if not sections and cfg.get("rules"):
            has_follow = any(r.get("follow") for r in cfg["rules"] if isinstance(r, dict))
            for callback in (cfg.get("callbacks") or {}).values():
                ex = (callback or {}).get("extract", {}) if isinstance(callback, dict) else {}
                extract_fields.update(ex.keys())

        for f in analyze_config(
                name, start_urls, settings, has_follow, extract_fields):
            findings.append({"spider": name, **f})
    return findings


def main():
    ap = argparse.ArgumentParser(
        description="Check spider configs for decay-prone shapes "
                    "(reads the database, or lints config JSON files directly).")
    ap.add_argument("files", nargs="*",
                    help="config JSON files to lint; needs no database. "
                         "If omitted, reads spider configs from the database.")
    ap.add_argument("--db", default=None,
                    help="database URL or sqlite path (used only when no files "
                         "are given); defaults to $DATABASE_URL, then scrapai.db")
    ap.add_argument("--project", default="jobfeed",
                    help="only check spiders in this project (database mode)")
    ap.add_argument("--json", default=None, help="write findings to this JSON file")
    args = ap.parse_args()

    if args.files:
        findings = lint_files(args.files)
        scope = f"{len(args.files)} file(s)"
    else:
        findings = lint_db(args.db, args.project)
        scope = f"project={args.project}"

    hard = [f for f in findings
            if f["code"] in ("FROZEN_ENUMERATION", "NO_REQUIRED_SRC", "UNREADABLE")]

    print(f"\nConfig check — {scope}  findings={len(findings)}  "
          f"blocking={len(hard)}\n")
    if not findings:
        print("No config-shape problems found.")
    for f in findings:
        print(f"  [{f['code']}] {f['spider']}")
        print(f"       {f['note']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"scope": scope, "findings": findings}, fh, indent=2)
        print(f"\nWrote {args.json}")

    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()

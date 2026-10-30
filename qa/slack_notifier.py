#!/usr/bin/env python3
"""
Post a Slack message summarizing a data-quality check run.

Used as a library: a check builds its report in memory and calls notify(report).
Nothing is sent when the report is clean, so it is quiet by default and speaks
up only on a problem.

Configuration comes from the environment:
    SLACK_TOKEN     a Slack bot token (xoxb-...), needs chat:write
    SLACK_CHANNEL   id for a channel the bot is in
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"

# At most this many spiders per severity group are listed in Slack; the rest
# are collapsed into "…and N more" so a large run cannot flood the channel.
CAP_PER_GROUP = 5

# Plain-English phrasing for each check code, so a reader needs no knowledge of
# the checker internals. "{n}" is filled with how many rows hit the code.
CODE_PHRASES = {
    # title
    "title_empty": "{n} rows have no title",
    "title_junk": "{n} rows have a button label instead of a title",
    "title_short": "{n} rows have a suspiciously short title",
    "title_long": "{n} rows have an over-long title (looks like a sentence)",
    "title_nonlatin": "{n} rows have a title in the wrong language/script",
    # description
    "desc_empty": "{n} rows have no description",
    "desc_too_short": "{n} rows have a description too short to be real",
    "desc_stub": "{n} rows have a thin, stub-like description",
    "desc_boilerplate": "{n} rows store a cookie/consent banner as the description",
    "desc_junk": "{n} rows have a button label instead of a description",
    # employer
    "employer_empty": "{n} rows have no employer",
    "employer_junk": "{n} rows have a button label instead of an employer",
    "employer_sentence": "{n} rows have a sentence instead of a company name",
    "employer_mismatch": "{n} rows have an employer that differs from the expected one",
    # location
    "location_empty": "{n} rows have no location",
    "location_junk": "{n} rows have a button label instead of a location",
    "location_is_flag": "{n} rows store a work-style tag (e.g. \"Remote\") as the location",
    "location_long": "{n} rows have an over-long location (likely the wrong field)",
    "location_street_address": "{n} rows have a street address instead of a city",
    "location_shape": "{n} rows have an oddly-shaped location",
    # whole-item / per-spider
    "meta_unparseable": "{n} rows have unreadable metadata",
    "desc_len_outlier": "{n} rows have an unusual description length vs the others",
}


def phrase_code(code, n):
    """Plain-English phrase for a code. Falls back to the raw code if unmapped."""
    template = CODE_PHRASES.get(code)
    if template is None:
        return f"{code}×{n}" if n else code
    return template.format(n=n)


def summarize(report):
    """Build the Slack message lines for a data-quality report.

    Returns a list of lines, or an empty list when nothing is worth reporting
    (so the caller stays silent).
    """
    project = report.get("project", "(unknown)")
    flagged = [spider for spider in report.get("spiders", [])
               if spider.get("verdict") in ("CRITICAL", "REVIEW")]
    if not flagged:
        return []

    crit = [s for s in flagged if s.get("verdict") == "CRITICAL"]
    review = [s for s in flagged if s.get("verdict") == "REVIEW"]
    lines = [f"*Data-quality check — project {project}*",
             f"{len(flagged)} spiders need review "
             f"({len(crit)} critical, {len(review)} to review)"]

    for label, group in (("CRITICAL", crit), ("REVIEW", review)):
        if not group:
            continue
        lines.append(f"\n{label}")
        for spider in group[:CAP_PER_GROUP]:
            lines.append(f"• `{spider['spider']}` — {_describe_spider(spider)}")
        if len(group) > CAP_PER_GROUP:
            lines.append(f"• …and {len(group) - CAP_PER_GROUP} more")
    return lines


def _describe_spider(spider):
    """One plain-English clause for a flagged spider: its top issue, translated."""
    codes = spider.get("codes") or {}
    if not codes:
        return spider.get("verdict", "flagged")
    top_code, top_n = max(codes.items(), key=lambda code_count: code_count[1])
    phrase = phrase_code(top_code, top_n)
    extra = len(codes) - 1
    return phrase + (f" (+{extra} other issue{'s' if extra > 1 else ''})"
                     if extra > 0 else "")


def notify(report, channel=None):
    """Summarize a data-quality report and post it to Slack if it has findings.

    report   the in-memory report dict (as written to --json by the check).
    channel  destination id; defaults to the SLACK_CHANNEL environment variable.

    Does nothing when the report is clean. Raises RuntimeError when the Slack
    credentials are missing or the send fails.
    """
    lines = summarize(report)
    if not lines:
        return

    token = os.getenv("SLACK_TOKEN")
    channel = channel or os.getenv("SLACK_CHANNEL")
    if not token:
        raise RuntimeError("SLACK_TOKEN is not set.")
    if not channel:
        raise RuntimeError("SLACK_CHANNEL is not set.")

    _post(token, channel, "\n".join(lines))


def _post(token, channel, text):
    """Post one message via chat.postMessage. Raises RuntimeError on failure."""
    payload = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_POST_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"Slack request failed: {e}") from e
    if not body.get("ok"):
        raise RuntimeError(f"Slack rejected the message: {body.get('error', 'unknown error')}")

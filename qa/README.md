# QA checks — data quality and config decay

Two small, **read-only** scripts that check scraped data and the spider configs
that produce it. Each reads the database (or, for the config check, plain JSON
files) and never writes anything. Each prints a report, supports `--json` for
automation, and exits non-zero when something needs a human to look at it.

A *spider* is one configured source website. Each scraped *row* describes one
item (for example, one job posting) and stores fields such as `title`,
`description`, `employer`, and `location`.

| Script | Question it answers | Needs |
|--------|--------------------|-------|
| `static_quality_check.py` | Are the rows we already stored actually good? | a database with rows |
| `static_config_check.py` | Can this spider config keep finding new items, or will it decay? | a database, or config JSON files |

## `static_quality_check.py`

Reads stored rows and flags fields that look wrong, for example:

- a field that is empty
- a field that is a leftover button label like `Apply Now`
- a `description` that is actually a cookie/consent banner
- a `location` that is a work-style tag (`Remote`, `Hybrid`) instead of a place
- a `location` that is a street address (`123 Main St`) instead of a city

It groups rows by spider and gives each spider a verdict:

| Verdict | Meaning |
|---------|---------|
| `CRITICAL` | many rows are unusable |
| `REVIEW` | at least one unusable row, or many suspicious ones |
| `WATCH` | only mild/odd values, probably fine |
| `OK` | nothing flagged |

```bash
python static_quality_check.py                      # summary table, worst first
python static_quality_check.py --project <name>     # only one project
python static_quality_check.py --spider <name> --rows   # list a spider's bad rows
python static_quality_check.py --json report.json   # machine-readable output
```

## `static_config_check.py`

Inspects the config, not the data, to catch a config that lists exactly *which*
pages to scrape instead of *discovering* pages on its own. Such a config returns
nothing once those specific pages disappear.

- **Bad shape:** start URLs are a fixed list of individual item pages (e.g.
  `/jobs/1001`, `/jobs/1002`) with no link-following, page numbering, or
  sitemap. When those items expire, nothing replaces them.
- **Healthy shape:** starts on a listing page and follows links, or sweeps page
  numbers/dates, or reads a sitemap — so new items appear automatically.

Findings it reports:

| Code | Meaning |
|------|---------|
| `FROZEN_ENUMERATION` | names specific pages and has no way to discover new ones; will decay |
| `NO_DISCOVERY` | no follow rule, generated URLs, or sitemap, and at most one start URL |
| `NO_REQUIRED_SRC` | a required field has no selector, so it can never be populated |
| `UNREADABLE` | a config file could not be parsed (file mode only) |

```bash
# Read configs from the database:
python static_config_check.py --project <name>

# Or lint config JSON files directly, with no database (useful in CI to block
# a decay-prone config before it is deployed):
python static_config_check.py path/to/config1.json path/to/config2.json
```

## Choosing the database

`static_quality_check.py` always needs a database; `static_config_check.py`
needs one only when no files are given. Both resolve the database in this order:

1. the `--db` value (a database URL or a SQLite file path)
2. the `DATABASE_URL` environment variable
3. a local SQLite file named `scrapai.db`

Both SQLite and PostgreSQL are supported and opened read-only. PostgreSQL
requires the `psycopg2` package; it is only imported when a PostgreSQL URL is
actually used.

## Tests

```bash
python -m pytest qa/tests/ -q
```

Unit tests with no database and no network. They check each rule twice: that
bad input is flagged, and that good input is left alone (so the checks do not
raise false alarms).

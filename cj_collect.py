#!/usr/bin/env python3
"""Collect ClearanceJobs listings into the commercial schema (personal-use layer).

Sitemap-driven: the job sitemaps are the activity signal (validThrough is unreliable
— evergreen 2016 postings are still listed). Each sweep set-diffs the sitemap against
commercial.jobs_raw to track sightings, then fetches detail pages for in-scope new ids
and stale refresh candidates, rate-limited to one request per second.
"""

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlparse

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

USER_AGENT = "usajobs-personal-collector/1.0 (matt.gargett@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT}

SOURCE = "clearancejobs"
JOB_BASE = "https://www.clearancejobs.com/jobs"
SITEMAP_BASE = "https://www.clearancejobs.com/data/sitemaps"
SITEMAP_URLS = [
    f"{SITEMAP_BASE}/job_postings.xml",
    f"{SITEMAP_BASE}/job_postings_2.xml",
]
COMPANY_SITEMAP_URL = f"{SITEMAP_BASE}/company.xml"

# The combined job sitemaps carry ~45k urls. A sweep far below this means the fetch
# failed; bail non-zero before touching the DB rather than marking everything absent.
MIN_HEALTHY_SWEEP = 30_000

# Seconds per socket op; a hang becomes requests.Timeout and fails the sweep.
REQUEST_TIMEOUT = 30

# Politeness floor between detail fetches — max one request per second, non-negotiable.
FETCH_DELAY = 1.0

# Stop after this many back-to-back network faults — the site is likely down, so
# bail politely instead of hammering it through the rest of the queue.
MAX_CONSECUTIVE_ERRORS = 5

DEFAULT_SLUG_KEYWORDS = [
    "engineer",
    "analyst",
    "intelligence",
    "cyber",
    "security",
    "architect",
    "data",
    "software",
    "cloud",
    "devops",
    "sigint",
    "geoint",
    "isr",
]

LD_JSON_RE = re.compile(
    r'<script\b[^>]*\btype="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.DOTALL)
JOB_URL_RE = re.compile(r"/jobs/(\d+)/([^/?#]+)")
COMPANY_URL_RE = re.compile(r"/jobs/([^/]+)$")


def _db_config(url_env: str = "DATABASE_URL_COLLECTOR") -> dict:
    """Parse a DATABASE_URL into psycopg2 connection kwargs."""
    url = os.getenv(url_env, "postgresql://usajobs:CHANGEME@localhost:5432/usajobs")
    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
    }


def _job_url(ext_id: str, slug: str) -> str:
    return f"{JOB_BASE}/{ext_id}/{slug}"


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the company match key."""
    return " ".join(re.sub(r"[^\w\s]", " ", name.lower()).split())


def _parse_job_sitemap(xml: str) -> list[tuple[str, str]]:
    """Extract (ext_id, slug) from <loc> job urls; xhtml:link alternates are ignored."""
    return [
        (m.group(1), m.group(2))
        for loc in LOC_RE.findall(xml)
        if (m := JOB_URL_RE.search(loc))
    ]


def _parse_company_sitemap(xml: str) -> list[str]:
    """Extract company slugs from <loc> profile urls (no numeric id segment)."""
    return [
        m.group(1)
        for loc in LOC_RE.findall(xml)
        if (m := COMPANY_URL_RE.search(loc.strip()))
    ]


def _parse_job_posting(html: str) -> dict | None:
    """Return the JobPosting JSON-LD block, skipping the decoy and Organization blocks."""
    for block in LD_JSON_RE.findall(html):
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(block)
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
    return None


def _strip_bad_z(ts: str) -> str:
    """Drop the trailing Z from the malformed offset+Z form ('...+00:00Z')."""
    time_part = ts.split("T", 1)[-1]
    if ts.endswith("Z") and ("+" in time_part or "-" in time_part):
        return ts[:-1]
    return ts


def _valid_through(data: dict) -> datetime | None:
    """Parse validThrough into an aware datetime; None when absent or unparseable."""
    raw = data.get("validThrough")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(_strip_bad_z(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _in_scope(slug: str, keywords: list[str]) -> bool:
    return any(kw in slug for kw in keywords)


def _country_ok(data: dict, countries: list[str]) -> bool:
    """True when any jobLocation country substring-matches a wanted country."""
    locs = data.get("jobLocation")
    if isinstance(locs, dict):
        locs = [locs]
    wanted = [c.lower() for c in countries]
    return any(
        w in ((loc.get("address") or {}).get("addressCountry") or "").lower()
        for loc in locs or []
        for w in wanted
    )


def _fetch_sitemap_entries() -> list[tuple[str, str]]:
    """Fetch both job sitemaps (two requests) and return combined (ext_id, slug) pairs."""
    entries: list[tuple[str, str]] = []
    for url in SITEMAP_URLS:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        entries.extend(_parse_job_sitemap(resp.text))
    return entries


def _load_companies(cur) -> dict[str, int]:
    """Preload the {name_normalized: id} lookup for company linking."""
    cur.execute(
        "SELECT name_normalized, id FROM commercial.companies "
        "WHERE name_normalized IS NOT NULL"
    )
    return dict(cur.fetchall())


def _apply_sightings(cur, seen: list[str]) -> int:
    """Log returns for reappeared ids, reset their miss counter, touch last_seen for
    every seen id. Returns the number of ids that came back after a gap."""
    cur.execute(
        "INSERT INTO commercial.sighting_returns (source, ext_id, missed_sweeps) "
        "SELECT source, ext_id, consecutive_misses FROM commercial.jobs_raw "
        "WHERE source = %s AND consecutive_misses > 0 AND ext_id = ANY(%s)",
        (SOURCE, seen),
    )
    returned = cur.rowcount
    cur.execute(
        "UPDATE commercial.jobs_raw SET consecutive_misses = 0, last_seen = now() "
        "WHERE source = %s AND ext_id = ANY(%s)",
        (SOURCE, seen),
    )
    return returned


def _backlog_candidates(cur, keywords: list[str]) -> list[tuple[str, str]]:
    """Never-fetched rows — this sweep's fresh inserts plus prior sweeps' leftovers
    (deferred, non-200, mid-crash). fetched_at IS NULL is the never-attempted marker,
    so deliberately skipped ids (_mark_id_only stamps fetched_at) don't churn."""
    cur.execute(
        "SELECT ext_id, url, slug FROM commercial.jobs_raw "
        "WHERE source = %s AND data IS NULL AND fetched_at IS NULL "
        "AND consecutive_misses = 0 ORDER BY first_seen, ext_id",
        (SOURCE,),
    )
    return [
        (ext_id, url)
        for ext_id, url, slug in cur.fetchall()
        if _in_scope(slug, keywords)
    ]


def _refresh_candidates(cur, refresh_days: int) -> list[tuple[str, str]]:
    """Data-bearing rows past their refresh age whose posting hasn't clearly expired."""
    cur.execute(
        "SELECT ext_id, url, data FROM commercial.jobs_raw "
        "WHERE source = %s AND data IS NOT NULL AND consecutive_misses = 0 "
        "AND fetched_at < now() - make_interval(days => %s)",
        (SOURCE, refresh_days),
    )
    now = datetime.now(UTC)
    return [
        (ext_id, url)
        for ext_id, url, data in cur.fetchall()
        if (vt := _valid_through(data)) is None or vt > now
    ]


def _mark_id_only(cur, ext_id: str, stats: Counter[str], category: str) -> None:
    """Clear any stored detail — the row keeps tracking sightings without content."""
    cur.execute(
        "UPDATE commercial.jobs_raw SET data = NULL, fetched_at = now(), "
        "last_seen = now() WHERE source = %s AND ext_id = %s",
        (SOURCE, ext_id),
    )
    stats[category] += 1


def _archive_history(cur, ext_id: str) -> None:
    """Copy the row's current payload into jobs_history before it's overwritten."""
    cur.execute(
        "INSERT INTO commercial.jobs_history (source, ext_id, data, captured_at) "
        "SELECT source, ext_id, data, last_seen FROM commercial.jobs_raw "
        "WHERE source = %s AND ext_id = %s",
        (SOURCE, ext_id),
    )


def _apply_data(
    cur,
    ext_id: str,
    new_data: str,
    old_data: str | None,
    company_id: int | None,
    stats: Counter[str],
) -> str:
    """Write freshly-parsed detail, archiving the prior version when it changed."""
    if old_data is None:
        category = "fetched"
    elif old_data != new_data:
        _archive_history(cur, ext_id)
        category = "changed"
    else:
        category = "unchanged"

    cur.execute(
        "UPDATE commercial.jobs_raw SET data = %s, company_id = %s, "
        "fetched_at = now(), last_seen = now() WHERE source = %s AND ext_id = %s",
        (new_data, company_id, SOURCE, ext_id),
    )
    stats[category] += 1
    return category


def fetch_detail(
    cur,
    ext_id: str,
    url: str,
    companies: dict[str, int],
    countries: list[str] | None,
    stats: Counter[str],
) -> None:
    """Fetch one detail page and reconcile it against the stored row."""
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        stats["http_error"] += 1
        return

    cur.execute(
        "SELECT data FROM commercial.jobs_raw WHERE source = %s AND ext_id = %s",
        (SOURCE, ext_id),
    )
    row = cur.fetchone()
    old_data = (
        json.dumps(row[0], sort_keys=True) if row and row[0] is not None else None
    )

    posting = _parse_job_posting(resp.text)
    if posting is None:
        # A refresh with good stored data: leave it untouched (fetched_at stays old,
        # so it retries next run) rather than nulling it. Dataless rows mark id-only.
        if old_data is None:
            _mark_id_only(cur, ext_id, stats, "parse_failed")
        else:
            stats["parse_failed"] += 1
        return

    if countries and not _country_ok(posting, countries):
        # Deliberate scope-out: archive any prior payload before nulling so the row
        # leaves the portal without losing its history.
        if old_data is not None:
            _archive_history(cur, ext_id)
        _mark_id_only(cur, ext_id, stats, "country_skipped")
        return

    org = posting.get("hiringOrganization") or {}
    name = org.get("name", "") if isinstance(org, dict) else ""
    company_id = companies.get(_normalize(name)) if name else None

    new_data = json.dumps(posting, sort_keys=True)
    _apply_data(cur, ext_id, new_data, old_data, company_id, stats)


def sweep(args) -> None:
    """Set-diff the sitemaps against jobs_raw, then fetch capped detail pages."""
    sitemap = dict(_fetch_sitemap_entries())
    if len(sitemap) < MIN_HEALTHY_SWEEP:
        print(
            f"Only {len(sitemap)} sitemap jobs (< {MIN_HEALTHY_SWEEP}) — likely a fetch "
            "failure; aborting before any DB write."
        )
        sys.exit(1)

    conn = psycopg2.connect(**_db_config())
    conn.autocommit = False
    stats: Counter[str] = Counter()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ext_id, consecutive_misses FROM commercial.jobs_raw "
            "WHERE source = %s",
            (SOURCE,),
        )
        existing = dict(cur.fetchall())
        sitemap_ids, db_ids = set(sitemap), set(existing)
        seen = sitemap_ids & db_ids
        absent = db_ids - sitemap_ids
        new = sitemap_ids - db_ids

        returned = _apply_sightings(cur, list(seen)) if seen else 0
        if absent:
            cur.execute(
                "UPDATE commercial.jobs_raw "
                "SET consecutive_misses = consecutive_misses + 1 "
                "WHERE source = %s AND ext_id = ANY(%s)",
                (SOURCE, list(absent)),
            )
        if new:
            cur.executemany(
                "INSERT INTO commercial.jobs_raw (source, ext_id, url, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (source, ext_id) DO NOTHING",
                [
                    (SOURCE, eid, _job_url(eid, sitemap[eid]), sitemap[eid])
                    for eid in new
                ],
            )
        conn.commit()
        print(
            f"Sitemap: {len(sitemap)} jobs. New: {len(new)}  Absent: {len(absent)}  "
            f"Returned after gap: {returned}"
        )

        companies = _load_companies(cur)
        backlog = _backlog_candidates(cur, args.slug_keywords)
        refresh = _refresh_candidates(cur, args.refresh_days)
        queue = backlog + refresh
        to_fetch = queue[: args.limit]
        print(
            f"Fetching {len(to_fetch)} detail pages "
            f"({len(backlog)} backlog, {len(refresh)} refresh); "
            f"{len(queue) - len(to_fetch)} deferred."
        )

        consecutive_errors = 0
        for i, (ext_id, url) in enumerate(to_fetch):
            if i:
                time.sleep(FETCH_DELAY)
            try:
                fetch_detail(cur, ext_id, url, companies, args.countries, stats)
            except requests.RequestException:
                # No db write, so the row stays fetched_at IS NULL and the backlog
                # retries it next run.
                stats["fetch_error"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(
                        f"Aborting: {consecutive_errors} consecutive fetch errors — "
                        f"site likely down. {dict(stats)}"
                    )
                    conn.close()
                    sys.exit(1)
                continue
            consecutive_errors = 0
            conn.commit()

    print(f"Done. {dict(stats)}")
    conn.close()


def harvest_roster() -> None:
    """Upsert the company roster from the company sitemap; does not sweep jobs."""
    resp = requests.get(COMPANY_SITEMAP_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    slugs = _parse_company_sitemap(resp.text)

    conn = psycopg2.connect(**_db_config())
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO commercial.companies (name, name_normalized, cj_profile_url) "
            "VALUES (%s, %s, %s) ON CONFLICT (cj_profile_url) DO NOTHING",
            [
                (
                    slug.replace("-", " ").title(),
                    slug.replace("-", " ").lower(),
                    f"{JOB_BASE}/{slug}",
                )
                for slug in slugs
            ],
        )
    conn.commit()
    conn.close()
    print(f"Roster: upserted {len(slugs)} company slugs.")


def _keyword_list(raw: str) -> list[str]:
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _country_list(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Collect ClearanceJobs into the commercial schema"
    )
    parser.add_argument(
        "--harvest-roster",
        action="store_true",
        help="Upsert the company roster from the company sitemap and exit",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="Max detail fetches per run (backfill spreads across runs)",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=7,
        help="Refetch data-bearing rows older than this many days",
    )
    parser.add_argument(
        "--slug-keywords",
        type=_keyword_list,
        default=DEFAULT_SLUG_KEYWORDS,
        help="Comma-separated slug substrings that put a new job in scope",
    )
    parser.add_argument(
        "--countries",
        type=_country_list,
        default=None,
        help="Comma-separated country filter applied after parse",
    )
    args = parser.parse_args()

    if args.harvest_roster:
        harvest_roster()
    else:
        sweep(args)

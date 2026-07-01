#!/usr/bin/env python3
"""Collect job listings from the USAJobs API directly into PostgreSQL."""

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from urllib.parse import urlparse

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("USAJOBS_API_KEY", "")
USER_AGENT = os.getenv("USAJOBS_USER_AGENT", "")
BASE_URL = "https://data.usajobs.gov/api/Search"
CODELIST_URL = "https://data.usajobs.gov/api/Codelist"

HEADERS = {
    "Authorization-Key": API_KEY,
    "User-Agent": USER_AGENT,
    "Host": "data.usajobs.gov",
}

EXCLUDED_COUNTRIES = {
    "United States",
    "Undefined",
    "Stateless Person",
    "Undesignated Sovereignty",
}

# A healthy full sweep returns ~12k active jobs. Far below that means the API auth
# or network failed; bail non-zero instead of refreshing geo from a near-empty result.
MIN_HEALTHY_SWEEP = 3000

# Page size requested from the API; also drives the pagination stop condition.
RESULTS_PER_PAGE = 500


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


def _codelist(slug: str) -> list[dict]:
    """Fetch one of the API's master code lists and return its valid values."""
    resp = requests.get(f"{CODELIST_URL}/{slug}", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()["CodeList"][0]["ValidValue"]


def _position_id(item: dict) -> str | None:
    return item.get("MatchedObjectDescriptor", {}).get("PositionID")


def get_locations_from_api() -> tuple[list[str], list[str]]:
    """Pull the full location list from the API's own code lists."""
    # US subdivisions (states, territories, armed forces APOs)
    us_subs = [
        v["Value"]
        for v in _codelist("CountrySubdivisions")
        if v.get("ParentCode") == "US" and v.get("IsDisabled") == "No"
    ]
    # All countries (minus US since we handle it via subdivisions)
    countries = [
        v["Value"]
        for v in _codelist("Countries")
        if v.get("IsDisabled") == "No" and v.get("Value") not in EXCLUDED_COUNTRIES
    ]
    return us_subs, countries


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs_raw (
    position_id     TEXT PRIMARY KEY,
    data            JSONB NOT NULL,
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs_history (
    id              SERIAL PRIMARY KEY,
    position_id     TEXT NOT NULL,
    data            JSONB NOT NULL,
    captured_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_raw_data ON jobs_raw USING gin(data);
CREATE INDEX IF NOT EXISTS idx_jobs_history_pid ON jobs_history(position_id);
CREATE INDEX IF NOT EXISTS idx_jobs_history_captured ON jobs_history(captured_at);
"""


def _ensure_schema(conn) -> None:
    """Create base tables only when the DB is empty; the collector role lacks CREATE."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.jobs_raw')")
        if cur.fetchone()[0] is None:
            cur.execute(SCHEMA_SQL)
    conn.commit()


def search_pages(params: dict, max_pages: int = 20) -> Iterator[dict]:
    """Yield job items from the API with automatic pagination."""
    params = {**params, "ResultsPerPage": RESULTS_PER_PAGE, "Page": 1}

    while params["Page"] <= max_pages:
        print(f"  Fetching page {params['Page']}...")
        resp = requests.get(BASE_URL, headers=HEADERS, params=params)
        resp.raise_for_status()

        sr = resp.json().get("SearchResult", {})
        total = int(sr.get("SearchResultCountAll", 0))
        items = sr.get("SearchResultItems", [])

        yield from items

        fetched = params["Page"] * RESULTS_PER_PAGE
        if fetched >= total or not items:
            break

        params["Page"] += 1
        time.sleep(0.5)


def _apply_change(
    cur, pid: str, new_data: str, old_data: str | None, stats: Counter[str]
) -> str:
    """Write one job based on how new_data compares to old_data; return the category."""
    if old_data is None:
        cur.execute(
            "INSERT INTO jobs_raw (position_id, data) VALUES (%s, %s)",
            (pid, new_data),
        )
        category = "new"
    elif old_data != new_data:
        # Changed — archive old version, then update current
        cur.execute(
            "INSERT INTO jobs_history (position_id, data, captured_at) "
            "SELECT position_id, data, last_seen FROM jobs_raw WHERE position_id = %s",
            (pid,),
        )
        cur.execute(
            "UPDATE jobs_raw SET data = %s, last_seen = NOW() WHERE position_id = %s",
            (new_data, pid),
        )
        category = "changed"
    else:
        cur.execute(
            "UPDATE jobs_raw SET last_seen = NOW() WHERE position_id = %s",
            (pid,),
        )
        category = "unchanged"

    stats[category] += 1
    return category


def upsert_item(cur, item: dict, stats: Counter[str]) -> str | None:
    """Upsert a job item, reading its current row to detect changes."""
    pid = _position_id(item)
    if not pid:
        return None

    new_data = json.dumps(item, sort_keys=True)
    cur.execute("SELECT data FROM jobs_raw WHERE position_id = %s", (pid,))
    row = cur.fetchone()
    old_data = json.dumps(row[0], sort_keys=True) if row else None
    _apply_change(cur, pid, new_data, old_data, stats)
    return pid


def refresh_geo(conn) -> None:
    """Refresh jobs_geo via the owner-defined function (collector doesn't own the view)."""
    print("Refreshing jobs_geo materialized view (CONCURRENTLY)...")
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_jobs_geo()")
    conn.commit()
    print("  Done.")


def _track_sightings(conn, seen: set[str]) -> None:
    """Record sighting gaps from a full sweep: log postings that reappeared after
    being absent, reset their miss counter, and bump it for recently-active postings
    not seen this sweep. Feeds the recency-window analysis (see migrations/07)."""
    seen_list = list(seen)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sighting_returns (position_id, missed_sweeps) "
            "SELECT position_id, consecutive_misses FROM jobs_raw "
            "WHERE consecutive_misses > 0 AND position_id = ANY(%s)",
            (seen_list,),
        )
        returned = cur.rowcount
        cur.execute(
            "UPDATE jobs_raw SET consecutive_misses = 0 "
            "WHERE consecutive_misses > 0 AND position_id = ANY(%s)",
            (seen_list,),
        )
        cur.execute(
            "UPDATE jobs_raw SET consecutive_misses = consecutive_misses + 1 "
            "WHERE last_seen > now() - interval '3 days' AND NOT (position_id = ANY(%s))",
            (seen_list,),
        )
        missed = cur.rowcount
    conn.commit()
    print(
        f"Sightings: {missed} recently-active jobs absent this sweep; {returned} returned after a gap."
    )


def collect_full() -> None:
    """Full sweep across all locations, writing directly to Postgres."""
    conn = psycopg2.connect(**_db_config())
    conn.autocommit = False

    _ensure_schema(conn)

    # Pull full location lists from the API's own master code lists
    us_subs, countries = get_locations_from_api()
    # Add catch-alls for remote jobs and overflow
    catch_all = ["United States", "Remote"]
    locations = us_subs + countries + catch_all
    print(
        f"Sweeping {len(us_subs)} US subdivisions + {len(countries)} countries + {len(catch_all)} catch-alls = {len(locations)} locations"
    )

    # Pre-load all existing JSONB for fast comparison
    print("Loading existing data for change detection...")
    with conn.cursor() as cur:
        cur.execute("SELECT position_id, data FROM jobs_raw")
        existing = {
            pid: json.dumps(data, sort_keys=True) for pid, data in cur.fetchall()
        }
    print(f"Loaded {len(existing)} existing jobs into memory.")

    seen: set[str] = set()
    stats: Counter[str] = Counter()
    skipped_count = 0

    with conn.cursor() as cur:
        for location in locations:
            print(f"\n[{location}]")
            loc_new = 0
            for item in search_pages({"LocationName": location}):
                pid = _position_id(item)
                if not pid or pid in seen:
                    skipped_count += 1
                    continue

                seen.add(pid)
                new_data = json.dumps(item, sort_keys=True)
                if _apply_change(cur, pid, new_data, existing.get(pid), stats) == "new":
                    loc_new += 1

            conn.commit()
            print(f"  {loc_new} new (total unique: {len(seen)})")

    print(f"\nDone. {len(seen)} unique jobs processed.")
    print(
        f"  New: {stats['new']}  Changed: {stats['changed']}  Unchanged: {stats['unchanged']}  Skipped: {skipped_count}"
    )

    if len(seen) < MIN_HEALTHY_SWEEP:
        print(
            f"Only {len(seen)} jobs (< {MIN_HEALTHY_SWEEP}) — likely an API/network failure; "
            "skipping sighting tracking and geo refresh."
        )
        conn.close()
        sys.exit(1)

    _track_sightings(conn, seen)
    refresh_geo(conn)
    conn.close()


def collect_daily() -> None:
    """Incremental daily pull — only jobs posted in last 1 day."""
    conn = psycopg2.connect(**_db_config())
    conn.autocommit = False

    _ensure_schema(conn)

    stats: Counter[str] = Counter()
    with conn.cursor() as cur:
        for item in search_pages({"DatePosted": "1"}):
            upsert_item(cur, item, stats)
        conn.commit()

    print(f"Daily pull done. {sum(stats.values())} jobs processed.")
    print(
        f"  New: {stats['new']}  Changed: {stats['changed']}  Unchanged: {stats['unchanged']}"
    )

    refresh_geo(conn)
    conn.close()


def print_stats() -> None:
    conn = psycopg2.connect(**_db_config())
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jobs_raw")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(DISTINCT data->'MatchedObjectDescriptor'->>'OrganizationName') FROM jobs_raw"
        )
        orgs = cur.fetchone()[0]
        cur.execute("SELECT MIN(first_seen), MAX(last_seen) FROM jobs_raw")
        first, last = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM jobs_history")
        history = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT position_id) FROM jobs_history")
        changed_jobs = cur.fetchone()[0]
        print(f"\nDatabase: {total} jobs, {orgs} agencies")
        print(f"History:  {history} snapshots across {changed_jobs} changed jobs")
        print(f"First seen: {first}")
        print(f"Last seen:  {last}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect USAJobs into PostgreSQL")
    parser.add_argument(
        "--full", "-f", action="store_true", help="Full sweep all locations"
    )
    parser.add_argument(
        "--daily", "-d", action="store_true", help="Incremental daily pull (last 24h)"
    )
    parser.add_argument(
        "--stats", "-s", action="store_true", help="Print database stats"
    )
    args = parser.parse_args()

    if args.daily:
        collect_daily()
    elif args.stats:
        print_stats()
    else:
        collect_full()

    print_stats()

#!/usr/bin/env python3
"""Load the commercial gazetteer tables from GeoNames dumps (CC-BY: geonames.org).

Populates commercial.geo_cities from cities15000 (every city over 15k people) and
commercial.geo_zips from the US zip-code dump. By default downloads both archives
from download.geonames.org into a temp dir; --cities-file / --zips-file accept
pre-downloaded paths (zip archive or extracted txt). Each table is truncated and
bulk-loaded via COPY; row counts are printed.

Runs as the schema OWNER role 'usajobs' (TRUNCATE + owner-level writes). Resolve the
connection from --database-url or the DATABASE_URL env var. --dry-run parses the
sources and prints counts without connecting.
"""

import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import psycopg2
from dotenv import load_dotenv

load_dotenv()

CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
ZIPS_URL = "https://download.geonames.org/export/zip/US.zip"
CITIES_MEMBER = "cities15000.txt"
ZIPS_MEMBER = "US.txt"

CITY_COLS = [
    "geonameid",
    "name",
    "ascii_name",
    "lat",
    "lon",
    "country",
    "admin1",
    "population",
]
ZIP_COLS = ["zip", "place", "state", "lat", "lon"]

# Seconds per socket op on the archive download.
REQUEST_TIMEOUT = 120


def _db_config(url: str) -> dict:
    """Parse a DATABASE_URL into psycopg2 connection kwargs."""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
    }


def _resolve(local: str | None, url: str, tmpdir: Path) -> Path:
    """Return a local path for a source — the given file, or a fresh download."""
    if local:
        return Path(local)
    dest = tmpdir / Path(urlparse(url).path).name
    print(f"Downloading {url} ...")
    with urlopen(url, timeout=REQUEST_TIMEOUT) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


def _dump_rows(path: Path, member: str) -> Iterator[list[str]]:
    """Yield tab-split fields from a GeoNames dump (zip archive or extracted txt)."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf, zf.open(member) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                yield line.rstrip("\n").split("\t")
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            yield line.rstrip("\n").split("\t")


def parse_cities(rows: Iterable[list[str]]) -> list[tuple]:
    """Project cities15000 rows to (geonameid, name, ascii_name, lat, lon, country,
    admin1, population); admin1/population blanks become NULL."""
    return [
        (
            int(f[0]),
            f[1],
            f[2],
            float(f[4]),
            float(f[5]),
            f[8],
            f[10] or None,
            int(f[14]) if f[14] else None,
        )
        for f in rows
        if len(f) >= 15
    ]


def parse_zips(rows: Iterable[list[str]]) -> list[tuple]:
    """Project US zip rows to (zip, place, state, lat, lon), keeping the first row for
    each zip and dropping later duplicates."""
    seen: set[str] = set()
    out: list[tuple] = []
    for f in rows:
        if len(f) < 11 or f[1] in seen:
            continue
        seen.add(f[1])
        out.append((f[1], f[2] or None, f[4] or None, float(f[9]), float(f[10])))
    return out


def _copy(cur, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Bulk-load rows into commercial.<table> via COPY, blanks as NULL."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    buf.seek(0)
    cols = ", ".join(columns)
    cur.copy_expert(
        f"COPY commercial.{table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')", buf
    )


def load(cities: list[tuple], zips: list[tuple], url: str) -> None:
    conn = psycopg2.connect(**_db_config(url))
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("TRUNCATE commercial.geo_cities")
        _copy(cur, "geo_cities", CITY_COLS, cities)
        cur.execute("TRUNCATE commercial.geo_zips")
        _copy(cur, "geo_zips", ZIP_COLS, zips)
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load commercial gazetteer tables from GeoNames dumps"
    )
    parser.add_argument("--cities-file", help="Pre-downloaded cities15000 zip or txt")
    parser.add_argument("--zips-file", help="Pre-downloaded US zip-code zip or txt")
    parser.add_argument(
        "--database-url", help="Owner DATABASE_URL (overrides the DATABASE_URL env var)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the sources and print counts without connecting",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cities_path = _resolve(args.cities_file, CITIES_URL, tmpdir)
        zips_path = _resolve(args.zips_file, ZIPS_URL, tmpdir)
        cities = parse_cities(_dump_rows(cities_path, CITIES_MEMBER))
        zips = parse_zips(_dump_rows(zips_path, ZIPS_MEMBER))

    print(f"Parsed {len(cities)} cities, {len(zips)} zips.")
    if args.dry_run:
        return

    url = args.database_url or os.getenv("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL or pass --database-url (owner role).")
    load(cities, zips, url)
    print(f"Loaded {len(cities)} cities, {len(zips)} zips into commercial gazetteer.")


if __name__ == "__main__":
    main()

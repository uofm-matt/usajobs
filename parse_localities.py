"""
Parse OPM locality pay area definitions from HTML into a FIPS->locality mapping.

Outputs:
  - Python dict printed to stdout: {locality_name: [fips1, fips2, ...]}
  - SQL file at /tmp/locality_areas.sql with CREATE TABLE + INSERT statements

Usage:
    python parse_localities.py
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

HTML_PATH: Final = Path(
    "/Users/mgargett/projects/usajobs/Locality Pay Area Definitions.html"
)
SQL_PATH: Final = Path("/tmp/locality_areas.sql")


class LocalityParser(HTMLParser):
    """State-machine HTML parser that extracts locality area names and FIPS codes.

    The document structure within the content section is:
        <h3><a name="..." id="..."></a>Locality Area Name</h3>
        [<h4>State Name</h4>]
        <table>
            ...
            <td class="FIPS">FIPS_CODE</td>
            ...
        </table>
        ... (repeats for each state within the locality)
    """

    # Tags whose content we never want to capture as locality/FIPS text
    _SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Final result: ordered dict of locality_name -> [fips, ...]
        self.localities: dict[str, list[str]] = {}

        # --- state machine fields ---
        # True once we pass <a name="content"> in the document
        self._in_content: bool = False

        # Pending locality name being assembled from text nodes
        self._current_locality: str | None = None
        self._capturing_h3: bool = False

        # True while inside <td class="FIPS">
        self._capturing_fips: bool = False
        self._pending_fips: str = ""

        # Depth tracker to handle nested tags we skip
        self._skip_depth: int = 0

    # ------------------------------------------------------------------
    # HTMLParser overrides
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)

        # Detect the content anchor that marks the start of locality data
        if tag == "a" and attr_dict.get("name") == "content":
            self._in_content = True
            return

        if not self._in_content:
            return

        # Track skip-worthy tags (script/style) so we don't capture their text
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        # <h3> that contains a locality anchor: start capturing name
        # We identify locality h3s by checking for a nested anchor with both
        # name and id attributes (navigation h3s don't have this pattern).
        # We set the flag here and wait for text nodes + </h3>.
        if tag == "h3":
            self._capturing_h3 = True
            self._current_locality = ""
            return

        # <td class="FIPS"> — start collecting the FIPS code
        if tag == "td" and attr_dict.get("class") == "FIPS":
            self._capturing_fips = True
            self._pending_fips = ""

    def handle_endtag(self, tag: str) -> None:
        if not self._in_content:
            return

        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        if self._skip_depth:
            return

        if tag == "h3" and self._capturing_h3:
            self._capturing_h3 = False
            name = (self._current_locality or "").strip()
            if name and name not in self.localities:
                self.localities[name] = []
                log.debug("Found locality area: %r", name)
            self._current_locality = None
            return

        if tag == "td" and self._capturing_fips:
            self._capturing_fips = False
            fips = self._pending_fips.strip()
            # Empty FIPS cell (e.g. "Rest of U.S.") — skip silently
            if fips and self._current_locality_key:
                self.localities[self._current_locality_key].append(fips)
            self._pending_fips = ""
            return

    def handle_data(self, data: str) -> None:
        if not self._in_content or self._skip_depth:
            return

        if self._capturing_h3:
            self._current_locality = (self._current_locality or "") + data

        if self._capturing_fips:
            self._pending_fips += data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _current_locality_key(self) -> str | None:
        """The most recently registered locality name (last key in dict)."""
        if not self.localities:
            return None
        return next(reversed(self.localities))


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------


def build_sql(localities: dict[str, list[str]]) -> str:
    """Return complete SQL to create and populate the locality_areas table."""
    lines: list[str] = [
        "DROP TABLE IF EXISTS locality_areas;",
        "",
        "CREATE TABLE locality_areas (",
        "    fips    TEXT PRIMARY KEY,",
        "    locality TEXT NOT NULL",
        ");",
        "",
    ]

    for locality, fips_list in localities.items():
        # Escape single quotes in locality name (e.g. "Coeur d'Alene")
        safe_locality = locality.replace("'", "''")
        for fips in fips_list:
            safe_fips = fips.replace("'", "''")
            lines.append(
                f"INSERT INTO locality_areas (fips, locality) VALUES ('{safe_fips}', '{safe_locality}');"
            )

    lines += [
        "",
        "CREATE INDEX idx_locality_areas_locality ON locality_areas(locality);",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    html_text = HTML_PATH.read_text(encoding="utf-8")

    parser = LocalityParser()
    parser.feed(html_text)

    localities = parser.localities

    # Drop locality areas with no FIPS codes (e.g. "Rest of U.S.")
    populated = {k: v for k, v in localities.items() if v}
    skipped = {k: v for k, v in localities.items() if not v}

    if skipped:
        log.info(
            "Skipped %d area(s) with no FIPS codes: %s",
            len(skipped),
            list(skipped.keys()),
        )

    # ---- summary stats ----
    total_areas = len(populated)
    total_fips = sum(len(v) for v in populated.values())

    print(f"\nTotal locality areas (with FIPS): {total_areas}")
    print(f"Total FIPS codes: {total_fips}")

    # ---- NCR verification ----
    ncr_key = next(
        (k for k in populated if "Washington" in k and "Baltimore" in k),
        None,
    )
    if ncr_key:
        ncr_count = len(populated[ncr_key])
        print(f"\nNCR locality area: {ncr_key!r}")
        print(f"NCR FIPS count:    {ncr_count}")
    else:
        print("\nWARNING: Could not find NCR locality area.")

    # ---- print dict ----
    print("\n--- localities dict ---")
    print("{")
    for name, fips_list in populated.items():
        print(f"    {name!r}: {fips_list!r},")
    print("}")

    # ---- write SQL ----
    sql = build_sql(populated)
    SQL_PATH.write_text(sql, encoding="utf-8")
    log.info("SQL written to %s (%d bytes)", SQL_PATH, SQL_PATH.stat().st_size)


if __name__ == "__main__":
    main()

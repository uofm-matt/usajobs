"""Audit OCONUS-looking commercial postings for mislabeled US locations.

ClearanceJobs' structured jobLocation sometimes tags a US city as its same-named
foreign twin (the "Melbourne, FL -> Melbourne, Australia" bug). This pass triggers
on the cheap structured signal — a US-clearance posting whose only locations are
foreign — narrows to the ambiguous cases (a foreign city name that also exists as a
US city, no explicit "overseas"/OCONUS wording), and adjudicates each with an
escalating model cascade: Haiku settles the easy calls; anything uncertain, or any
proposed mislabel (the actionable verdict), climbs Sonnet -> Opus -> Fable until two
adjacent tiers agree with high confidence, or Fable has the final say.

Without --repair it only reports. With --repair it writes each verdict to
commercial.location_audit (incremental — already-audited postings are skipped) and,
for confirmed two-tier-agreed high-confidence mislabels, rewrites the pin to the
corrected US location; the collector re-applies those overrides on every future
fetch. Auth uses the local Claude Code OAuth token (macOS keychain), so no
ANTHROPIC_API_KEY is needed — which is also why the LLM pass runs here, not in the
server cron.
"""

import argparse
import json
import os
import subprocess
from collections import defaultdict

import anthropic
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
SOURCE = "clearancejobs"

# Escalation ladder: (model, input $/M, output $/M). A suspect starts at the top of
# this list and climbs only as far as it must to be resolved.
LADDER = [
    ("claude-haiku-4-5", 1.0, 5.0),
    ("claude-sonnet-5", 3.0, 15.0),
    ("claude-opus-4-8", 5.0, 25.0),
    ("claude-fable-5", 10.0, 50.0),
]

SUSPECTS_SQL = """
SELECT DISTINCT ON (jr.ext_id)
    jr.ext_id,
    jr.data->>'title' AS title,
    jr.data->>'securityClearanceRequirement' AS clearance,
    lo.city, lo.country,
    left(regexp_replace(jr.data->>'description', '<[^>]+>', '', 'g'), 1200) AS descr,
    jr.url
FROM commercial.jobs_raw jr
JOIN commercial.job_locations lo USING (source, ext_id)
JOIN commercial.geo_cities gc
  ON lower(gc.ascii_name) = lower(lo.city) AND gc.country = 'US'
WHERE jr.data IS NOT NULL AND jr.consecutive_misses = 0
  AND left(jr.data->>'datePosted', 10)
      >= to_char(now() - interval '6 months', 'YYYY-MM-DD')
  AND jr.data->>'securityClearanceRequirement' ~* '(secret|sci|poly|public trust)'
  AND lo.country IS NOT NULL AND lo.country <> 'United States'
  AND NOT EXISTS (
      SELECT 1 FROM commercial.job_locations l2
      WHERE l2.source = jr.source AND l2.ext_id = jr.ext_id
        AND l2.country = 'United States')
  AND jr.data->>'description' !~* '\\y(oconus|overseas|forward.?deployed|SOFA)\\y'
ORDER BY jr.ext_id
"""

PROMPT = """You are auditing a job dataset for one specific error: a same-name location mix-up, where the structured data tags a job with a foreign city that merely shares its name with the real (usually US) city — e.g. a job in "Melbourne, FL" wrongly tagged "Melbourne, Australia".

Decide only whether the structured location is the ACTUAL location, or a same-name confusion.
- "correct": the job really is at the structured location. This INCLUDES genuine overseas jobs (foreign address/clearance/currency, a named base abroad, deployment language) AND US territories like Puerto Rico, Guam, or the US Virgin Islands — those are accurate, not errors.
- "mislabel": the posting's own text shows the real location is elsewhere (a US state in the title/description like "located in <city>, FL", US-citizenship + USD pay with no overseas context, a stateside worksite). Give the real location.

Structured location (from the job board): {city}, {country}
Title: {title}
Clearance: {clearance}
Description excerpt:
{descr}

Call record_verdict with your decision."""

# Tool-forced structured output: the model must return schema-valid args, so a
# stray quote in the reason can't produce unparseable JSON and crash the batch.
VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the location-accuracy verdict for this posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["correct", "mislabel", "unclear"]},
            "real_location": {
                "type": ["string", "null"],
                "description": "The true location when mislabel, else null.",
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string", "description": "<= 15 words"},
        },
        "required": ["verdict", "confidence", "reason"],
    },
}


def _oauth_token() -> str:
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def _classify(
    client: anthropic.Anthropic, model: str, row: dict
) -> tuple[dict, anthropic.types.Usage]:
    msg = client.messages.create(
        model=model,
        max_tokens=400,
        system=SYSTEM,
        tools=[VERDICT_TOOL],
        tool_choice={"type": "tool", "name": "record_verdict"},
        messages=[{"role": "user", "content": PROMPT.format(**row)}],
    )
    verdict = next(b.input for b in msg.content if b.type == "tool_use")
    return verdict, msg.usage


def _resolved(cur: dict, prev: dict | None) -> bool:
    """A verdict settles when it is high-confidence and either a plain 'correct'
    (safe default, no need to act) or a 'mislabel' the tier below already agreed
    with (two votes before we'd repair). Everything else keeps climbing."""
    if cur["confidence"] != "high" or cur["verdict"] == "unclear":
        return False
    if cur["verdict"] == "correct":
        return True
    return bool(prev) and prev.get("verdict") == "mislabel"


def _escalate(
    client: anthropic.Anthropic, row: dict, cost: dict[str, list[int]]
) -> tuple[dict, list[str], bool]:
    """Climb the ladder until a verdict resolves, or Fable has the last word.
    The bool is True when two adjacent tiers agreed (a confirmed result safe to act
    on), False when Fable arbitrated a tie (contested — flag, don't auto-repair)."""
    prev: dict = {}
    path: list[str] = []
    for model, *_ in LADDER:
        cur, usage = _classify(client, model, row)
        cost[model][0] += usage.input_tokens
        cost[model][1] += usage.output_tokens
        path.append(
            f"{model.split('-')[1]}:{cur['verdict'][:4]}/{cur['confidence'][0]}"
        )
        if _resolved(cur, prev):
            return cur, path, True
        prev = cur
    return prev, path, False  # Fable had the final, unconfirmed say


def _parse_real(real: str | None) -> tuple[str | None, str | None, str | None]:
    """Split an LLM real_location into (city, region, country). "Melbourne, FL" ->
    (Melbourne, FL, United States); a bare "United States ..." -> (None, None,
    United States); anything else -> all None (audited, but nothing to geocode)."""
    if not real:
        return None, None, None
    parts = [p.strip() for p in real.split(",")]
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[0], parts[1].upper(), "United States"
    if "united states" in real.lower():
        return None, None, "United States"
    return None, None, None


def _write_audit(
    cur, ext_id: str, v: dict, model: str
) -> tuple[str | None, str | None]:
    city, region, country = _parse_real(v.get("real_location"))
    cur.execute(
        "INSERT INTO commercial.location_audit (source, ext_id, verdict, confidence,"
        " real_city, real_region, real_country, reason, model) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source, ext_id) DO UPDATE SET verdict = EXCLUDED.verdict, "
        "confidence = EXCLUDED.confidence, real_city = EXCLUDED.real_city, "
        "real_region = EXCLUDED.real_region, real_country = EXCLUDED.real_country, "
        "reason = EXCLUDED.reason, model = EXCLUDED.model, checked_at = now()",
        (SOURCE, ext_id, v["verdict"], v["confidence"], city, region, country,
         v.get("reason"), model),
    )  # fmt: skip
    return city, region


def _repair_location(cur, ext_id: str, city: str, region: str) -> bool:
    """Rewrite job_locations for a confirmed mislabel: drop the wrong (foreign) rows
    and insert one geocoded US row for the corrected city. Returns True when a pin
    landed (city found in the gazetteer)."""
    cur.execute(
        "DELETE FROM commercial.job_locations WHERE source = %s AND ext_id = %s",
        (SOURCE, ext_id),
    )
    cur.execute(
        "INSERT INTO commercial.job_locations (source, ext_id, seq, city, region, "
        "country, lat, lon, geocode_method, county_fips, locality_area) "
        "SELECT %s, %s, 0, %s, %s, 'United States', gc.lat, gc.lon, 'repair', "
        "c.fips, la.locality FROM commercial.geo_cities gc "
        "LEFT JOIN public.us_counties c "
        "  ON ST_Contains(c.geom, ST_SetSRID(ST_MakePoint(gc.lon, gc.lat), 4326)) "
        "LEFT JOIN public.locality_areas la ON la.fips = c.fips "
        "WHERE gc.country = 'US' AND lower(gc.ascii_name) = lower(%s) "
        "AND gc.admin1 = %s ORDER BY gc.population DESC NULLS LAST LIMIT 1",
        (SOURCE, ext_id, city, region, city, region),
    )
    if cur.rowcount:
        return True
    # No gazetteer match — still drop the wrong foreign pin, keep a city/region row.
    cur.execute(
        "INSERT INTO commercial.job_locations "
        "(source, ext_id, seq, city, region, country) "
        "VALUES (%s, %s, 0, %s, %s, 'United States')",
        (SOURCE, ext_id, city, region),
    )
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Cap the number of suspects checked")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Write audit rows and repair confirmed high-confidence mislabels",
    )
    args = parser.parse_args()

    url = "DATABASE_URL_COLLECTOR" if args.repair else "DATABASE_URL_WEB"
    conn = psycopg2.connect(os.environ[url])
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(SUSPECTS_SQL)
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        if args.repair:  # incremental: skip suspects already audited
            cur.execute(
                "SELECT ext_id FROM commercial.location_audit WHERE source = %s",
                (SOURCE,),
            )
            done = {r[0] for r in cur.fetchall()}
            rows = [r for r in rows if r["ext_id"] not in done]
    if args.limit:
        rows = rows[: args.limit]

    client = anthropic.Anthropic(
        auth_token=_oauth_token(),
        default_headers={"anthropic-beta": "oauth-2025-04-20"},
    )

    mode = "repairing" if args.repair else "checking"
    ladder = " -> ".join(m[0].split("-")[1] for m in LADDER)
    print(f"{mode.title()} {len(rows)} suspects through {ladder}\n")
    verdicts: dict[str, int] = defaultdict(int)
    top_tier: dict[str, int] = defaultdict(int)
    cost: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    repaired = 0

    for i, row in enumerate(rows, 1):
        v, path, settled = _escalate(client, row, cost)
        verdicts[v["verdict"]] += 1
        top_tier[path[-1].split(":")[0]] += 1
        note = ""
        if args.repair:
            with conn.cursor() as cur:
                city, region = _write_audit(
                    cur, row["ext_id"], v, path[-1].split(":")[0]
                )
                # Repair only confirmed (two-tier-agreed) high-confidence mislabels.
                if settled and v["verdict"] == "mislabel" and city and region:
                    pinned = _repair_location(cur, row["ext_id"], city, region)
                    repaired += 1
                    note = f"  ✅ repaired -> {city}, {region}" + (
                        "" if pinned else " (no pin)"
                    )
                conn.commit()
        flag = "🚩" if v["verdict"] == "mislabel" else "  "
        fix = f" -> {v['real_location']}" if v.get("real_location") else ""
        tiers = f"  [{' > '.join(path)}]" if len(path) > 1 else ""
        print(
            f"{flag} [{i}/{len(rows)}] {row['title'][:50]}\n"
            f"     {row['city']}, {row['country']}  |  {v['verdict']} "
            f"({v['confidence']}){fix}{tiers}{note}\n"
            f"     {v['reason']}  {row['url']}"
        )
    conn.close()

    total = sum(
        (i * ip + o * op) / 1_000_000 for (m, ip, op) in LADDER for i, o in [cost[m]]
    )
    print(f"\n{'=' * 64}")
    print(f"Verdicts: {dict(verdicts)}")
    if args.repair:
        print(f"Repaired: {repaired} confirmed mislabels")
    print(f"Resolved at tier: {dict(top_tier)}")
    for m, ip, op in LADDER:
        i, o = cost[m]
        if i or o:
            print(f"  {m:20} {i:>7}in {o:>6}out  ${(i * ip + o * op) / 1e6:.4f}")
    print(f"Total: ${total:.4f}")


if __name__ == "__main__":
    main()

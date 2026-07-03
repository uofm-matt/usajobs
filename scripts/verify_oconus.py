"""Audit OCONUS-looking commercial postings for mislabeled US locations.

ClearanceJobs' structured jobLocation sometimes tags a US city as its same-named
foreign twin (the "Melbourne, FL -> Melbourne, Australia" bug). This pass triggers
on the cheap structured signal — a US-clearance posting whose only locations are
foreign — narrows to the ambiguous cases (a foreign city name that also exists as a
US city, no explicit "overseas"/OCONUS wording), and adjudicates each with an
escalating model cascade: Haiku settles the easy calls; anything uncertain, or any
proposed mislabel (the actionable verdict), climbs Sonnet -> Opus -> Fable until two
adjacent tiers agree with high confidence, or Fable has the final say.

Read-only: it reports; it does not write repairs. Auth uses the local Claude Code
OAuth token (macOS keychain), so no ANTHROPIC_API_KEY is needed.
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
    return prev is not None and prev["verdict"] == "mislabel"


def _escalate(
    client: anthropic.Anthropic, row: dict, cost: dict[str, list[int]]
) -> tuple[dict, list[str]]:
    """Climb the ladder until a verdict resolves, or Fable has the last word."""
    prev, path = None, []
    for model, *_ in LADDER:
        cur, usage = _classify(client, model, row)
        cost[model][0] += usage.input_tokens
        cost[model][1] += usage.output_tokens
        path.append(
            f"{model.split('-')[1]}:{cur['verdict'][:4]}/{cur['confidence'][0]}"
        )
        if _resolved(cur, prev):
            return cur, path
        prev = cur
    return prev, path  # settled at Fable (final arbiter)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Cap the number of suspects checked")
    args = parser.parse_args()

    conn = psycopg2.connect(os.environ["DATABASE_URL_WEB"])
    with conn.cursor() as cur:
        cur.execute(SUSPECTS_SQL)
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    conn.close()
    if args.limit:
        rows = rows[: args.limit]

    client = anthropic.Anthropic(
        auth_token=_oauth_token(),
        default_headers={"anthropic-beta": "oauth-2025-04-20"},
    )

    print(
        f"Escalating {len(rows)} suspects through {' -> '.join(m[0].split('-')[1] for m in LADDER)}\n"
    )
    verdicts: dict[str, int] = defaultdict(int)
    top_tier: dict[str, int] = defaultdict(int)
    cost: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for i, row in enumerate(rows, 1):
        v, path = _escalate(client, row, cost)
        verdicts[v["verdict"]] += 1
        top_tier[path[-1].split(":")[0]] += 1
        flag = "🚩" if v["verdict"] == "mislabel" else "  "
        fix = f" -> {v['real_location']}" if v.get("real_location") else ""
        tiers = f"  [{' > '.join(path)}]" if len(path) > 1 else ""
        print(
            f"{flag} [{i}/{len(rows)}] {row['title'][:50]}\n"
            f"     {row['city']}, {row['country']}  |  {v['verdict']} "
            f"({v['confidence']}){fix}{tiers}\n"
            f"     {v['reason']}  {row['url']}"
        )

    total = sum(
        (i * ip + o * op) / 1_000_000 for (m, ip, op) in LADDER for i, o in [cost[m]]
    )
    print(f"\n{'=' * 64}")
    print(f"Verdicts: {dict(verdicts)}")
    print(f"Resolved at tier: {dict(top_tier)}")
    for m, ip, op in LADDER:
        i, o = cost[m]
        if i or o:
            print(f"  {m:20} {i:>7}in {o:>6}out  ${(i * ip + o * op) / 1e6:.4f}")
    print(f"Total: ${total:.4f}")


if __name__ == "__main__":
    main()

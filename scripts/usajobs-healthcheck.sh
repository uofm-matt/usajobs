#!/bin/sh
# USAJobs collector freshness monitor — runs on the collector host via cron (every 30 min).
#
# The collector can "succeed" silently while writing nothing: it did for ~6 weeks
# in 2026 (CREATE TABLE permission error every run, cron exit code ignored). So this
# checks the data directly instead of trusting cron. Alerts via ntfy when the newest
# last_seen is older than the threshold, or when the DB can't be reached at all.
#
# Install on the collector host:
#   cp scripts/usajobs-healthcheck.sh /opt/usajobs/healthcheck.sh
#   sed -i "s|__TOPIC__|<your-private-topic>|" /opt/usajobs/healthcheck.sh
#   chmod +x /opt/usajobs/healthcheck.sh
#   ( crontab -l; echo "*/30 * * * * /opt/usajobs/healthcheck.sh 2>> /var/log/usajobs-health.log" ) | crontab -

NTFY_TOPIC="ntfy.sh/__TOPIC__"
THRESHOLD="130 minutes"   # hourly collection; tolerate 2 missed runs before alerting
LOG=/var/log/usajobs-health.log

stale=$(docker exec postgres psql -U usajobs_web -d usajobs -At \
    -c "SELECT (max(last_seen) < now() - interval '$THRESHOLD') FROM jobs_raw")
now=$(TZ=America/Denver date '+%Y-%m-%d %H:%M MT')

# Healthy only when the query explicitly returns 'f'. Anything else — 't' (stale) or
# empty (DB unreachable / query failed) — alerts; a monitor must not go quiet on error.
if [ "$stale" = f ]; then
    echo "$now ok" >> "$LOG"
    exit 0
fi

last=$(docker exec postgres psql -U usajobs_web -d usajobs -At \
    -c "SET timezone='America/Denver'; SELECT to_char(max(last_seen),'YYYY-MM-DD HH24:MI') FROM jobs_raw")
last=${last:-unknown}
echo "$now ALERT collector stale (newest data ${last} MT)" >> "$LOG"
curl -s -H "Title: USAJobs collector STALE" -H "Priority: high" -H "Tags: rotating_light" \
    -d "No fresh job data since ${last} MT on the collector host. Hourly collector may be failing." \
    "$NTFY_TOPIC" >/dev/null
exit 1

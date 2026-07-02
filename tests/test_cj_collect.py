"""Unit tests for cj_collect.py — pure unittest.mock, no live DB, no network."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cj_collect

FIXTURE = (Path(__file__).parent / "fixtures" / "cj_detail_sample.html").read_text(
    encoding="utf-8"
)


def _cursor_conn():
    """A conn whose .cursor() works both as context manager and plain call."""
    cur = MagicMock()

    class _CM:
        def __enter__(self):
            return cur

        def __exit__(self, *a):
            return False

    conn = MagicMock()
    conn.cursor.return_value = _CM()
    return conn, cur


def _args(**over):
    base = dict(
        limit=2000,
        refresh_days=7,
        slug_keywords=cj_collect.DEFAULT_SLUG_KEYWORDS,
        countries=None,
    )
    return SimpleNamespace(**{**base, **over})


JOB_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
 <url>
  <loc>https://www.clearancejobs.com/jobs/2254571/mission-manager</loc>
  <xhtml:link rel="alternate" hreflang="0"
    href="https://www.clearancejobs.com/jobs/2254571/mission-manager"/>
 </url>
 <url>
  <loc>https://www.clearancejobs.com/jobs/2285225/site-reliability-engineer</loc>
  <xhtml:link rel="alternate" hreflang="0"
    href="https://www.clearancejobs.com/jobs/2285225/site-reliability-engineer"/>
 </url>
</urlset>"""

COMPANY_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
 <url>
  <loc>https://www.clearancejobs.com/jobs/10x-national-security</loc>
  <xhtml:link rel="alternate" hreflang="0"
    href="https://www.clearancejobs.com/jobs/10x-national-security"/>
 </url>
 <url>
  <loc>https://www.clearancejobs.com/jobs/fictive-systems</loc>
 </url>
</urlset>"""


class TestSitemapParse:
    def test_job_entries_from_loc_only(self):
        out = cj_collect._parse_job_sitemap(JOB_SITEMAP)
        assert out == [
            ("2254571", "mission-manager"),
            ("2285225", "site-reliability-engineer"),
        ]

    def test_company_slugs_from_loc(self):
        assert cj_collect._parse_company_sitemap(COMPANY_SITEMAP) == [
            "10x-national-security",
            "fictive-systems",
        ]

    def test_company_regex_ignores_job_urls(self):
        # A numeric job url has no bare company slug at the end.
        assert cj_collect._parse_company_sitemap(JOB_SITEMAP) == []

    def test_job_regex_ignores_company_urls(self):
        assert cj_collect._parse_job_sitemap(COMPANY_SITEMAP) == []


class TestJsonLdParse:
    def test_selects_jobposting_skips_decoy_and_org(self):
        posting = cj_collect._parse_job_posting(FIXTURE)
        assert posting is not None
        assert posting["@type"] == "JobPosting"
        assert posting["title"] == "Senior Widget Reliability Engineer"
        assert posting["securityClearanceRequirement"] == "Secret"
        assert posting["identifier"]["value"] == 9990001
        assert isinstance(posting["jobLocation"], list)
        assert posting["jobLocation"][0]["address"]["addressLocality"] == "Washington"

    def test_missing_ld_json_returns_none(self):
        assert (
            cj_collect._parse_job_posting("<html><body>no json</body></html>") is None
        )

    def test_unparseable_block_returns_none(self):
        html = '<script type="application/ld+json" nonce="x">{bad json,,}</script>'
        assert cj_collect._parse_job_posting(html) is None

    def test_org_only_page_returns_none(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type": "Organization", "name": "X"}</script>'
        )
        assert cj_collect._parse_job_posting(html) is None


class TestValidThrough:
    def test_strips_trailing_z_with_offset(self):
        vt = cj_collect._valid_through({"validThrough": "2026-09-03T19:48:58+00:00Z"})
        assert vt is not None
        assert vt.year == 2026 and vt.month == 9 and vt.tzinfo is not None

    def test_plain_z_without_offset_parses(self):
        vt = cj_collect._valid_through({"validThrough": "2026-09-03T19:48:58Z"})
        assert vt is not None and vt.tzinfo is not None

    def test_offset_form_parses(self):
        vt = cj_collect._valid_through({"validThrough": "2016-10-04T14:48:58-05:00"})
        assert vt is not None and vt.year == 2016

    def test_absent_returns_none(self):
        assert cj_collect._valid_through({}) is None

    def test_garbage_returns_none(self):
        assert cj_collect._valid_through({"validThrough": "not-a-date"}) is None


class TestScopeAndCountry:
    def test_in_scope_matches_keyword_substring(self):
        kw = cj_collect.DEFAULT_SLUG_KEYWORDS
        assert cj_collect._in_scope("site-reliability-engineer", kw)
        assert cj_collect._in_scope("cyber-threat-lead", kw)

    def test_out_of_scope_slug(self):
        assert not cj_collect._in_scope(
            "mission-manager", cj_collect.DEFAULT_SLUG_KEYWORDS
        )

    def test_country_ok_list(self):
        data = {"jobLocation": [{"address": {"addressCountry": "United States"}}]}
        assert cj_collect._country_ok(data, ["united states"])
        assert not cj_collect._country_ok(data, ["germany"])

    def test_country_ok_tolerates_dict(self):
        data = {"jobLocation": {"address": {"addressCountry": "United States"}}}
        assert cj_collect._country_ok(data, ["United States"])


class TestNormalize:
    def test_lowercases_strips_punct_collapses_space(self):
        assert cj_collect._normalize("L3Harris  Technologies, Inc.") == (
            "l3harris technologies inc"
        )


class TestApplyData:
    def test_new_data_updates_no_history(self):
        cur = MagicMock()
        stats = Counter()
        cat = cj_collect._apply_data(cur, "42", "new", None, 7, stats)
        assert cat == "fetched"
        assert stats["fetched"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("jobs_history" in s for s in sqls)
        assert any("UPDATE commercial.jobs_raw SET data" in s for s in sqls)

    def test_changed_archives_then_updates(self):
        cur = MagicMock()
        stats = Counter()
        cat = cj_collect._apply_data(cur, "42", "new", "old", 7, stats)
        assert cat == "changed"
        assert stats["changed"] == 1
        first, second = cur.execute.call_args_list
        assert "INSERT INTO commercial.jobs_history" in first.args[0]
        assert "captured_at" in first.args[0] and "last_seen" in first.args[0]
        assert "UPDATE commercial.jobs_raw SET data" in second.args[0]

    def test_unchanged_touches_row_no_history(self):
        cur = MagicMock()
        stats = Counter()
        cat = cj_collect._apply_data(cur, "42", "same", "same", None, stats)
        assert cat == "unchanged"
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("jobs_history" in s for s in sqls)


class TestFetchDetail:
    def _resp(self, status=200, text=FIXTURE):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def test_http_error_counts_no_row_write(self):
        cur = MagicMock()
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp(503)):
            cj_collect.fetch_detail(cur, "42", "u", {}, None, stats)
        assert stats["http_error"] == 1
        cur.execute.assert_not_called()

    def test_parse_failure_marks_id_only(self):
        cur = MagicMock()
        stats = Counter()
        with patch(
            "cj_collect.requests.get", return_value=self._resp(text="<html></html>")
        ):
            cj_collect.fetch_detail(cur, "42", "u", {}, None, stats)
        assert stats["parse_failed"] == 1
        assert "SET data = NULL" in cur.execute.call_args_list[0].args[0]

    def test_country_mismatch_marks_id_only(self):
        cur = MagicMock()
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "42", "u", {}, ["Germany"], stats)
        assert stats["country_skipped"] == 1
        assert "SET data = NULL" in cur.execute.call_args_list[0].args[0]

    def test_success_links_company_and_stores_raw(self):
        cur = MagicMock()
        cur.fetchone.return_value = None  # no prior data -> "fetched"
        stats = Counter()
        companies = {cj_collect._normalize("Fictive Systems"): 99}
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "9990001", "u", companies, None, stats)
        assert stats["fetched"] == 1
        update = next(c for c in cur.execute.call_args_list if "SET data" in c.args[0])
        # company_id linked; stored data is the raw JobPosting json.
        assert update.args[1][1] == 99
        assert '"title": "Senior Widget Reliability Engineer"' in update.args[1][0]

    def test_country_match_stores_data(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "9990001", "u", {}, ["United States"], stats)
        assert stats["fetched"] == 1


class TestRefreshCandidates:
    def test_filters_expired_keeps_future_and_undated(self):
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("1", "u1", {"validThrough": "2000-01-01T00:00:00+00:00Z"}),  # expired
            ("2", "u2", {"validThrough": "2099-01-01T00:00:00+00:00Z"}),  # future
            ("3", "u3", {}),  # undated -> kept
        ]
        out = cj_collect._refresh_candidates(cur, 7)
        assert out == [("2", "u2"), ("3", "u3")]
        # Age/consecutive_misses predicates pushed into SQL.
        sql = cur.execute.call_args.args[0]
        assert "data IS NOT NULL" in sql and "consecutive_misses = 0" in sql


class TestSweepGuard:
    def test_below_min_exits_before_db(self):
        small = [("1", "a"), ("2", "b")]
        with (
            patch("cj_collect._fetch_sitemap_entries", return_value=small),
            patch("cj_collect.MIN_HEALTHY_SWEEP", 100),
            patch("cj_collect.psycopg2.connect") as connect,
        ):
            with pytest.raises(SystemExit) as exc:
                cj_collect.sweep(_args())
        assert exc.value.code == 1
        connect.assert_not_called()


class TestSweepSetDiff:
    def _run(self, sitemap_entries, existing_rows, refresh_rows=None, args=None):
        conn, cur = _cursor_conn()
        # 1st fetchall: existing (ext_id, misses); 2nd: companies; 3rd: refresh cands.
        cur.fetchall.side_effect = [
            existing_rows,
            [],  # companies
            refresh_rows or [],
        ]
        with (
            patch("cj_collect._fetch_sitemap_entries", return_value=sitemap_entries),
            patch("cj_collect.MIN_HEALTHY_SWEEP", 0),
            patch("cj_collect.psycopg2.connect", return_value=conn),
            patch("cj_collect.fetch_detail") as fetch,
            patch("cj_collect.time.sleep"),
        ):
            cj_collect.sweep(args or _args())
        return conn, cur, fetch

    def test_new_in_scope_queued_out_of_scope_id_only(self):
        # engineer -> in scope (queued); mission-manager -> out (id-only insert only).
        _, cur, fetch = self._run(
            [("100", "network-engineer"), ("200", "mission-manager")],
            existing_rows=[],
        )
        inserts = [
            c
            for c in cur.execute.call_args_list
            if "INSERT INTO commercial.jobs_raw" in c.args[0]
        ]
        # executemany carries both new ids as id-only rows.
        assert not inserts  # id-only inserts go through executemany
        rows = cur.executemany.call_args_list[0].args[1]
        assert sorted(r[1] for r in rows) == ["100", "200"]
        # Only the in-scope id is fetched.
        fetched_ids = [c.args[1] for c in fetch.call_args_list]
        assert fetched_ids == ["100"]

    def test_absent_id_increments_misses(self):
        _, cur, _ = self._run(
            [("100", "network-engineer")],
            existing_rows=[("999", 0)],  # 999 absent from sitemap
        )
        bump = next(
            c
            for c in cur.execute.call_args_list
            if "consecutive_misses = consecutive_misses + 1" in c.args[0]
        )
        assert bump.args[1] == (cj_collect.SOURCE, ["999"])

    def test_reappearance_logs_return_and_resets(self):
        _, cur, _ = self._run(
            [("100", "network-engineer")],
            existing_rows=[("100", 3)],  # was missed, now back in sitemap
        )
        sql = [c.args[0] for c in cur.execute.call_args_list]
        assert any("INSERT INTO commercial.sighting_returns" in s for s in sql)
        assert any(
            "SET consecutive_misses = 0" in s and "last_seen = now()" in s for s in sql
        )

    def test_limit_caps_fetches_and_reports_deferral(self, capsys):
        entries = [(str(i), "cyber-analyst") for i in range(5)]
        _, _, fetch = self._run(entries, existing_rows=[], args=_args(limit=2))
        assert fetch.call_count == 2
        assert "3 deferred" in capsys.readouterr().out

    def test_refresh_candidates_share_queue_after_new(self):
        # One new in-scope + two stale (seen, data-bearing) refresh candidates,
        # limit 2 -> new first, one refresh fetched, one deferred.
        _, _, fetch = self._run(
            [("100", "cyber-analyst"), ("500", "manager"), ("600", "manager")],
            existing_rows=[("500", 0), ("600", 0)],
            refresh_rows=[
                ("500", "u5", {}),
                ("600", "u6", {}),
            ],
            args=_args(limit=2),
        )
        fetched_ids = [c.args[1] for c in fetch.call_args_list]
        assert fetched_ids == ["100", "500"]


class TestHarvestRoster:
    def test_parses_and_upserts(self, capsys):
        conn, cur = _cursor_conn()
        resp = MagicMock()
        resp.text = COMPANY_SITEMAP
        with (
            patch("cj_collect.requests.get", return_value=resp),
            patch("cj_collect.psycopg2.connect", return_value=conn),
        ):
            cj_collect.harvest_roster()
        rows = cur.executemany.call_args.args[1]
        assert (
            "10X National Security",
            "10x national security",
            "https://www.clearancejobs.com/jobs/10x-national-security",
        ) in rows
        assert (
            "ON CONFLICT (cj_profile_url) DO NOTHING"
            in cur.executemany.call_args.args[0]
        )
        conn.commit.assert_called_once()
        assert "2 company slugs" in capsys.readouterr().out


class TestDbConfig:
    def test_parses_url_into_kwargs(self):
        with patch.dict(
            "cj_collect.os.environ",
            {"DATABASE_URL_COLLECTOR": "postgresql://u:p@host.local:6543/mydb"},
        ):
            cfg = cj_collect._db_config()
        assert cfg == {
            "host": "host.local",
            "port": 6543,
            "dbname": "mydb",
            "user": "u",
            "password": "p",
        }

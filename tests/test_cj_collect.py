"""Unit tests for cj_collect.py — pure unittest.mock, no live DB, no network."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import cj_collect

FIXTURE = (Path(__file__).parent / "fixtures" / "cj_detail_sample.html").read_text(
    encoding="utf-8"
)

_NCR = "Washington-Baltimore-Arlington, DC-MD-VA-WV-PA"


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
        all_slugs=False,
        fetch_only=False,
        shard=None,
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
        cat = cj_collect._apply_data(cur, "42", "new", None, 7, {}, stats)
        assert cat == "fetched"
        assert stats["fetched"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("jobs_history" in s for s in sqls)
        assert any("UPDATE commercial.jobs_raw SET data" in s for s in sqls)

    def test_changed_archives_then_updates(self):
        cur = MagicMock()
        stats = Counter()
        cat = cj_collect._apply_data(cur, "42", "new", "old", 7, {}, stats)
        assert cat == "changed"
        assert stats["changed"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        hist = next(i for i, s in enumerate(sqls) if "jobs_history" in s)
        upd = next(i for i, s in enumerate(sqls) if "jobs_raw SET data" in s)
        assert hist < upd  # archive precedes the update
        assert "captured_at" in sqls[hist] and "last_seen" in sqls[hist]

    def test_unchanged_touches_row_no_history(self):
        cur = MagicMock()
        stats = Counter()
        cat = cj_collect._apply_data(cur, "42", "same", "same", None, {}, stats)
        assert cat == "unchanged"
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("jobs_history" in s for s in sqls)

    def test_success_path_rewrites_job_locations(self):
        # A posting with no jobLocation still fires the delete-then-nothing rewrite.
        cur = MagicMock()
        cj_collect._apply_data(cur, "42", "new", None, 7, {}, Counter())
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("DELETE FROM commercial.job_locations" in s for s in sqls)


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
        cur.fetchone.return_value = None  # dataless row
        stats = Counter()
        with patch(
            "cj_collect.requests.get", return_value=self._resp(text="<html></html>")
        ):
            cj_collect.fetch_detail(cur, "42", "u", {}, None, stats)
        assert stats["parse_failed"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("SET data = NULL" in s for s in sqls)

    def test_parse_failure_on_data_bearing_row_preserves_data(self):
        cur = MagicMock()
        cur.fetchone.return_value = ({"title": "keep me"},)  # has stored data
        stats = Counter()
        with patch(
            "cj_collect.requests.get", return_value=self._resp(text="<html></html>")
        ):
            cj_collect.fetch_detail(cur, "42", "u", {}, None, stats)
        assert stats["parse_failed"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        # Only the pre-parse SELECT ran; the row is left untouched.
        assert all(s.startswith("SELECT data") for s in sqls)

    def test_country_mismatch_marks_id_only(self):
        cur = MagicMock()
        cur.fetchone.return_value = None  # dataless row
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "42", "u", {}, ["Germany"], stats)
        assert stats["country_skipped"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("SET data = NULL" in s for s in sqls)
        assert not any("jobs_history" in s for s in sqls)

    def test_country_skip_on_data_bearing_row_archives_then_nulls(self):
        cur = MagicMock()
        cur.fetchone.return_value = ({"title": "old"},)  # has stored data
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "9990001", "u", {}, ["Germany"], stats)
        assert stats["country_skipped"] == 1
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        hist = next(
            i for i, s in enumerate(sqls) if "INSERT INTO commercial.jobs_history" in s
        )
        null = next(i for i, s in enumerate(sqls) if "SET data = NULL" in s)
        assert hist < null  # archive precedes the null-out

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

    def test_success_writes_job_locations(self):
        # fetchone returns None for the pre-parse SELECT and for every geocode probe,
        # so the fixture's Washington DC location is written unmatched (lat/lon NULL).
        cur = MagicMock()
        cur.fetchone.return_value = None
        stats = Counter()
        with patch("cj_collect.requests.get", return_value=self._resp()):
            cj_collect.fetch_detail(cur, "9990001", "u", {}, None, stats)
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("DELETE FROM commercial.job_locations" in s for s in sqls)
        ins = next(
            c
            for c in cur.execute.call_args_list
            if "INSERT INTO commercial.job_locations" in c.args[0]
        )
        # seq 0, city Washington, region DC, country United States, postal 20001.
        assert ins.args[1][:7] == (
            "clearancejobs",
            "9990001",
            0,
            "Washington",
            "DC",
            "United States",
            "20001",
        )


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
    def _run(
        self,
        sitemap_entries,
        existing_rows,
        backlog_rows=None,
        refresh_rows=None,
        args=None,
    ):
        conn, cur = _cursor_conn()
        # fetchall order: existing (ext_id, misses); companies; backlog; refresh.
        cur.fetchall.side_effect = [
            existing_rows,
            [],  # companies
            backlog_rows or [],
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

    def test_new_ids_inserted_id_only_backlog_filters_scope(self):
        # Both new ids are inserted id-only; the backlog query (mocked below) then
        # yields both, and only the in-scope slug survives _in_scope filtering.
        _, cur, fetch = self._run(
            [("100", "network-engineer"), ("200", "mission-manager")],
            existing_rows=[],
            backlog_rows=[
                ("100", "u1", "network-engineer"),
                ("200", "u2", "mission-manager"),
            ],
        )
        inserts = [
            c
            for c in cur.execute.call_args_list
            if "INSERT INTO commercial.jobs_raw" in c.args[0]
        ]
        # id-only inserts go through executemany, carrying both new ids.
        assert not inserts
        rows = cur.executemany.call_args_list[0].args[1]
        assert sorted(r[1] for r in rows) == ["100", "200"]
        # Only the in-scope id is fetched.
        fetched_ids = [c.args[1] for c in fetch.call_args_list]
        assert fetched_ids == ["100"]

    def test_prior_sweep_unfetched_row_reenters_queue(self):
        # 777 is already in the DB (seen, not new) but was never fetched last sweep
        # (data NULL, fetched_at NULL). The backlog query must re-surface it.
        _, cur, fetch = self._run(
            [("777", "cyber-analyst")],
            existing_rows=[("777", 0)],
            backlog_rows=[("777", "u777", "cyber-analyst")],
        )
        fetched_ids = [c.args[1] for c in fetch.call_args_list]
        assert fetched_ids == ["777"]
        backlog_sql = next(
            c.args[0]
            for c in cur.execute.call_args_list
            if "data IS NULL" in c.args[0] and "fetched_at IS NULL" in c.args[0]
        )
        assert "consecutive_misses = 0" in backlog_sql
        # Newest postings first so the fresh (<6-month) set fills before stale ids.
        assert "ORDER BY ext_id::bigint DESC" in backlog_sql

    def test_all_slugs_bypasses_keyword_scope(self):
        cur = MagicMock()
        rows = [
            ("9", "u9", "warehouse-forklift-operator"),
            ("8", "u8", "cyber-analyst"),
        ]
        cur.fetchall.return_value = rows
        scoped = cj_collect._backlog_candidates(cur, cj_collect.DEFAULT_SLUG_KEYWORDS)
        assert scoped == [("8", "u8")]  # only the in-scope slug
        cur.fetchall.return_value = rows
        every = cj_collect._backlog_candidates(
            cur, cj_collect.DEFAULT_SLUG_KEYWORDS, all_slugs=True
        )
        assert every == [("9", "u9"), ("8", "u8")]  # both, scope ignored

    def test_shard_clause_partitions_backlog(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        cj_collect._backlog_candidates(cur, [], all_slugs=True, shard=(1, 2))
        assert "ext_id::bigint % 2 = 1" in cur.execute.call_args.args[0]

    def test_shard_arg_validates(self):
        assert cj_collect._shard_arg("0/2") == (0, 2)
        assert cj_collect._shard_arg("1/4") == (1, 4)
        with pytest.raises(Exception):
            cj_collect._shard_arg("2/2")  # n must be < m

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
        backlog = [(str(i), f"u{i}", "cyber-analyst") for i in range(5)]
        _, _, fetch = self._run(
            entries, existing_rows=[], backlog_rows=backlog, args=_args(limit=2)
        )
        assert fetch.call_count == 2
        assert "3 deferred" in capsys.readouterr().out

    def test_backlog_then_refresh_share_queue(self):
        # One in-scope backlog id + two stale (data-bearing) refresh candidates,
        # limit 2 -> backlog first, one refresh fetched, one deferred.
        _, _, fetch = self._run(
            [("100", "cyber-analyst"), ("500", "manager"), ("600", "manager")],
            existing_rows=[("500", 0), ("600", 0)],
            backlog_rows=[("100", "u1", "cyber-analyst")],
            refresh_rows=[
                ("500", "u5", {}),
                ("600", "u6", {}),
            ],
            args=_args(limit=2),
        )
        fetched_ids = [c.args[1] for c in fetch.call_args_list]
        assert fetched_ids == ["100", "500"]


class TestFetchErrorHandling:
    def _resp(self, status=200, text=FIXTURE):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def _run_sweep(self, backlog_rows, get, args=None):
        # Real fetch_detail; requests.get is the injected mock so we can assert on
        # its call count. Sitemap mirrors the backlog so every id is set-diffed in.
        conn, cur = _cursor_conn()
        cur.fetchall.side_effect = [[], [], backlog_rows, []]
        cur.fetchone.return_value = None  # fetch_detail: no prior stored data
        sitemap = [(eid, slug) for eid, _url, slug in backlog_rows]
        with (
            patch("cj_collect._fetch_sitemap_entries", return_value=sitemap),
            patch("cj_collect.MIN_HEALTHY_SWEEP", 0),
            patch("cj_collect.psycopg2.connect", return_value=conn),
            patch("cj_collect.requests.get", get),
            patch("cj_collect.time.sleep"),
        ):
            cj_collect.sweep(args or _args())

    def test_one_timeout_continues_and_counts(self, capsys):
        backlog = [(str(i), f"u{i}", "cyber-analyst") for i in range(3)]
        get = MagicMock(side_effect=[requests.Timeout(), self._resp(), self._resp()])
        self._run_sweep(backlog, get)
        assert get.call_count == 3  # the timeout did not abort the run
        out = capsys.readouterr().out
        assert "'fetch_error': 1" in out and "'fetched': 2" in out

    def test_five_consecutive_errors_abort(self, capsys):
        backlog = [(str(i), f"u{i}", "cyber-analyst") for i in range(8)]
        get = MagicMock(side_effect=[requests.ConnectionError()] * 8)
        with pytest.raises(SystemExit) as exc:
            self._run_sweep(backlog, get)
        assert exc.value.code == 1
        assert get.call_count == 5  # stopped at the 5th, no further requests
        assert "consecutive fetch errors" in capsys.readouterr().out


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


def _place(**address):
    return {"@type": "Place", "address": {"@type": "PostalAddress", **address}}


class TestNormalizeLocation:
    def test_state_name_mapped_to_usps(self):
        loc = cj_collect._normalize_location(
            _place(
                addressLocality="Reston",
                addressRegion="Virginia",
                addressCountry="United States",
            )
        )
        assert loc == {
            "city": "Reston",
            "region": "VA",
            "country": "United States",
            "postal": None,
        }

    def test_two_letter_region_uppercased(self):
        loc = cj_collect._normalize_location(
            _place(addressRegion="va", addressCountry="usa")
        )
        assert loc["region"] == "VA" and loc["country"] == "United States"

    @pytest.mark.parametrize(
        "region", ["Washington DC", "District of Columbia", "Washington D.C.", "dc"]
    )
    def test_dc_variants_map_to_dc(self, region):
        loc = cj_collect._normalize_location(
            _place(addressRegion=region, addressCountry="US")
        )
        assert loc["region"] == "DC"

    @pytest.mark.parametrize(
        "raw",
        ["USA", "us", "U.S.", "u.s.a.", "United States of America", "united states"],
    )
    def test_country_aliases_fold_to_united_states(self, raw):
        loc = cj_collect._normalize_location(_place(addressCountry=raw))
        assert loc["country"] == "United States"

    def test_non_us_country_title_cased_region_kept(self):
        loc = cj_collect._normalize_location(
            _place(
                addressLocality="stuttgart",
                addressRegion="Baden-Wurttemberg",
                addressCountry="germany",
            )
        )
        assert loc["country"] == "Germany"
        assert loc["region"] == "Baden-Wurttemberg"  # non-US region kept as-is
        assert loc["city"] == "Stuttgart"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ft. Belvoir", "Fort Belvoir"),
            ("Ft Meade", "Fort Meade"),
            ("St. Louis", "Saint Louis"),
            ("St Petersburg", "Saint Petersburg"),
            ("Sterling", "Sterling"),  # 'St' prefix must not fire mid-word
        ],
    )
    def test_ft_st_prefix_expansion(self, raw, expected):
        loc = cj_collect._normalize_location(_place(addressLocality=raw))
        assert loc["city"] == expected

    def test_acronym_tokens_preserved(self):
        loc = cj_collect._normalize_location(_place(addressLocality="andrews AFB"))
        assert loc["city"] == "Andrews AFB"
        loc = cj_collect._normalize_location(_place(addressLocality="Washington JBAB"))
        assert loc["city"] == "Washington JBAB"

    def test_all_caps_string_is_titled_not_acronymed(self):
        loc = cj_collect._normalize_location(_place(addressLocality="FORT MEADE"))
        assert loc["city"] == "Fort Meade"

    def test_whitespace_collapsed_and_titled(self):
        loc = cj_collect._normalize_location(
            _place(addressLocality="  colorado   springs  ")
        )
        assert loc["city"] == "Colorado Springs"

    def test_postal_kept_as_is(self):
        loc = cj_collect._normalize_location(_place(postalCode=" 20001-1234 "))
        assert loc["postal"] == "20001-1234"

    def test_no_address_returns_none(self):
        assert cj_collect._normalize_location({"@type": "Place"}) is None

    def test_empty_address_returns_none(self):
        assert (
            cj_collect._normalize_location(
                _place(addressLocality="", addressCountry="   ")
            )
            is None
        )

    def test_country_only_is_usable(self):
        loc = cj_collect._normalize_location(_place(addressCountry="Japan"))
        assert loc == {
            "city": None,
            "region": None,
            "country": "Japan",
            "postal": None,
        }


class TestGeocode:
    def test_us_zip_hit(self):
        cur = MagicMock()
        cur.fetchone.return_value = (38.9, -77.0)
        out = cj_collect._geocode(
            cur,
            {
                "city": "Washington",
                "region": "DC",
                "country": "United States",
                "postal": "20001-1234",
            },
        )
        assert out == (38.9, -77.0, "zip")
        sql, params = cur.execute.call_args.args
        # zip5 = first 5 digits; region passed twice for the state-agreement guard.
        assert "geo_zips" in sql and "upper(state) = upper" in sql
        assert params == ("20001", "DC", "DC")

    def test_zip_state_mismatch_falls_through_to_city(self):
        # The Milwaukee bug: postal 53201 (WI) with region "WA". The zip query's
        # state guard rejects it (fetchone None), so it falls back to the city.
        cur = MagicMock()
        cur.fetchone.side_effect = [None, (47.6, -122.3)]  # zip guarded out, city hit
        out = cj_collect._geocode(
            cur,
            {
                "city": "Seattle",
                "region": "WA",
                "country": "United States",
                "postal": "53201",
            },
        )
        assert out == (47.6, -122.3, "city")
        zip_params = cur.execute.call_args_list[0].args[1]
        assert zip_params == ("53201", "WA", "WA")

    def test_us_falls_back_to_city_when_zip_misses(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [None, (37.3, -122.0)]  # zip miss, city hit
        out = cj_collect._geocode(
            cur,
            {
                "city": "Reston",
                "region": "VA",
                "country": "United States",
                "postal": "99999",
            },
        )
        assert out == (37.3, -122.0, "city")
        city_sql, city_params = cur.execute.call_args.args
        assert "geo_cities" in city_sql and "country = 'US'" in city_sql
        assert city_params == ("Reston", "VA")

    def test_us_no_match_returns_nulls(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [None, None]
        out = cj_collect._geocode(
            cur,
            {
                "city": "Nowhere",
                "region": "ZZ",
                "country": "United States",
                "postal": "00000",
            },
        )
        assert out == (None, None, None)

    def test_non_us_city_by_iso2(self):
        cur = MagicMock()
        cur.fetchone.return_value = (48.8, 9.2)
        out = cj_collect._geocode(
            cur,
            {"city": "Stuttgart", "region": None, "country": "Germany", "postal": None},
        )
        assert out == (48.8, 9.2, "city")
        sql, params = cur.execute.call_args.args
        assert params == ("Stuttgart", "DE")

    def test_unknown_country_skips_query(self):
        cur = MagicMock()
        out = cj_collect._geocode(
            cur,
            {"city": "Timbuktu", "region": None, "country": "Narnia", "postal": None},
        )
        assert out == (None, None, None)
        cur.execute.assert_not_called()


class TestWriteJobLocations:
    def test_delete_then_insert_with_seq(self):
        cur = MagicMock()
        posting = {
            "jobLocation": [
                _place(
                    addressLocality="Reston",
                    addressRegion="VA",
                    addressCountry="United States",
                    postalCode="20190",
                ),
                _place(addressLocality="stuttgart", addressCountry="Germany"),
            ]
        }
        with (
            patch(
                "cj_collect._geocode",
                side_effect=[(1.0, 2.0, "zip"), (3.0, 4.0, "city")],
            ),
            patch(
                "cj_collect._resolve_locality",
                side_effect=[("51059", _NCR), (None, None)],
            ),
        ):
            methods = cj_collect._write_job_locations(cur, "42", posting)
        assert methods == ["zip", "city"]
        calls = cur.execute.call_args_list
        assert "DELETE FROM commercial.job_locations" in calls[0].args[0]
        assert calls[0].args[1] == ("clearancejobs", "42")
        ins0, ins1 = calls[1], calls[2]
        assert ins0.args[1] == (
            "clearancejobs",
            "42",
            0,
            "Reston",
            "VA",
            "United States",
            "20190",
            1.0,
            2.0,
            "zip",
            "51059",
            _NCR,
        )
        assert ins1.args[1] == (
            "clearancejobs",
            "42",
            1,
            "Stuttgart",
            None,
            "Germany",
            None,
            3.0,
            4.0,
            "city",
            None,
            None,
        )

    def test_ungeocoded_location_skips_locality_resolve(self):
        cur = MagicMock()
        posting = {
            "jobLocation": _place(addressLocality="Nowhere", addressCountry="Narnia")
        }
        with (
            patch("cj_collect._geocode", return_value=(None, None, None)),
            patch("cj_collect._resolve_locality") as resolve,
        ):
            cj_collect._write_job_locations(cur, "9", posting)
        resolve.assert_not_called()
        # county_fips + locality_area land as NULL when there is no point to resolve.
        assert cur.execute.call_args_list[1].args[1][-2:] == (None, None)

    def test_resolve_locality_point_in_polygon(self):
        cur = MagicMock()
        cur.fetchone.return_value = ("24003", _NCR)
        assert cj_collect._resolve_locality(cur, 39.108, -76.743) == ("24003", _NCR)
        sql, params = cur.execute.call_args.args
        assert "ST_Contains" in sql and "us_counties" in sql
        assert params == (-76.743, 39.108)  # (lon, lat) order into ST_MakePoint

    def test_resolve_locality_no_county_match(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        assert cj_collect._resolve_locality(cur, 0.0, 0.0) == (None, None)

    def test_single_dict_location_handled(self):
        cur = MagicMock()
        posting = {
            "jobLocation": _place(
                addressLocality="Reston", addressCountry="United States"
            )
        }
        with patch("cj_collect._geocode", return_value=(None, None, None)):
            methods = cj_collect._write_job_locations(cur, "9", posting)
        assert methods == [None]

    def test_unusable_locations_skipped_delete_still_runs(self):
        cur = MagicMock()
        posting = {"jobLocation": [{"@type": "Place"}]}  # no address
        methods = cj_collect._write_job_locations(cur, "9", posting)
        assert methods == []
        calls = cur.execute.call_args_list
        assert len(calls) == 1 and "DELETE" in calls[0].args[0]

    def test_no_joblocation_key_deletes_only(self):
        cur = MagicMock()
        assert cj_collect._write_job_locations(cur, "9", {}) == []
        assert cur.execute.call_count == 1


class TestRegeocode:
    def test_summary_counts_by_method(self, capsys):
        conn, cur = _cursor_conn()
        cur.fetchall.return_value = [
            (
                "1",
                {
                    "jobLocation": _place(
                        addressLocality="Reston", addressCountry="United States"
                    )
                },
            ),
            ("2", {"jobLocation": _place(addressCountry="Japan")}),  # country-only row
        ]
        with (
            patch("cj_collect.psycopg2.connect", return_value=conn),
            patch(
                "cj_collect._geocode",
                side_effect=[(1.0, 2.0, "zip"), (None, None, None)],
            ),
        ):
            cj_collect.regeocode()
        out = capsys.readouterr().out
        assert "'rows': 2" in out
        assert "'locations': 2" in out
        assert "'zip': 1" in out and "'unmatched': 1" in out
        conn.commit.assert_called_once()
        # Both rows had their locations rebuilt (delete per row).
        deletes = [
            c
            for c in cur.execute.call_args_list
            if "DELETE FROM commercial.job_locations" in c.args[0]
        ]
        assert len(deletes) == 2


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

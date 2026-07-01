"""Unit tests for collect.py — pure unittest.mock, no live DB, no network."""

from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

import collect


def _item(pid="PID-1", **extra):
    """Build a minimal SearchResultItem with the given PositionID."""
    desc = {"PositionID": pid, **extra}
    return {"MatchedObjectDescriptor": desc}


class TestApplyChange:
    def test_new_inserts_jobs_raw(self):
        cur = MagicMock()
        stats = Counter()
        result = collect._apply_change(cur, "PID-1", "{}", None, stats)
        assert result == "new"
        assert stats["new"] == 1
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "INSERT INTO jobs_raw" in sql
        assert params == ("PID-1", "{}")

    def test_changed_archives_then_updates(self):
        cur = MagicMock()
        stats = Counter()
        result = collect._apply_change(cur, "PID-2", "new", "old", stats)
        assert result == "changed"
        assert stats["changed"] == 1
        assert cur.execute.call_count == 2
        first_sql = cur.execute.call_args_list[0].args[0]
        second_sql = cur.execute.call_args_list[1].args[0]
        assert "INSERT INTO jobs_history" in first_sql
        assert "UPDATE jobs_raw SET data" in second_sql
        assert cur.execute.call_args_list[0].args[1] == ("PID-2",)
        assert cur.execute.call_args_list[1].args[1] == ("new", "PID-2")

    def test_unchanged_touches_last_seen(self):
        cur = MagicMock()
        stats = Counter()
        result = collect._apply_change(cur, "PID-3", "same", "same", stats)
        assert result == "unchanged"
        assert stats["unchanged"] == 1
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "UPDATE jobs_raw SET last_seen" in sql
        assert params == ("PID-3",)


class TestUpsertItem:
    def test_missing_position_id_returns_none_no_writes(self):
        cur = MagicMock()
        stats = Counter()
        result = collect.upsert_item(cur, {"MatchedObjectDescriptor": {}}, stats)
        assert result is None
        cur.execute.assert_not_called()
        assert sum(stats.values()) == 0

    def test_empty_item_returns_none(self):
        cur = MagicMock()
        stats = Counter()
        assert collect.upsert_item(cur, {}, stats) is None
        cur.execute.assert_not_called()

    def test_new_row_when_fetchone_none(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        stats = Counter()
        item = _item("PID-9")
        result = collect.upsert_item(cur, item, stats)
        assert result == "PID-9"
        assert stats["new"] == 1
        # First execute is the SELECT; subsequent is the INSERT from _apply_change.
        select_sql = cur.execute.call_args_list[0].args[0]
        assert "SELECT data FROM jobs_raw" in select_sql
        assert cur.execute.call_args_list[0].args[1] == ("PID-9",)
        assert any(
            "INSERT INTO jobs_raw" in c.args[0] for c in cur.execute.call_args_list
        )

    def test_unchanged_when_stored_equals_new(self):
        cur = MagicMock()
        item = _item("PID-7", Title="X")
        # Stored row holds the same dict the item serializes to.
        cur.fetchone.return_value = (item,)
        stats = Counter()
        result = collect.upsert_item(cur, item, stats)
        assert result == "PID-7"
        assert stats["unchanged"] == 1
        assert stats["new"] == 0

    def test_changed_when_stored_differs(self):
        cur = MagicMock()
        item = _item("PID-8", Title="New")
        cur.fetchone.return_value = (_item("PID-8", Title="Old"),)
        stats = Counter()
        result = collect.upsert_item(cur, item, stats)
        assert result == "PID-8"
        assert stats["changed"] == 1


class TestEnsureSchema:
    def test_creates_schema_when_table_absent(self):
        conn, cur = _cursor_conn()
        cur.fetchone.return_value = (None,)
        collect._ensure_schema(conn)
        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert any("to_regclass" in s for s in executed)
        assert collect.SCHEMA_SQL in executed
        conn.commit.assert_called_once()

    def test_skips_schema_when_table_present(self):
        conn, cur = _cursor_conn()
        cur.fetchone.return_value = ("jobs_raw",)
        collect._ensure_schema(conn)
        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert collect.SCHEMA_SQL not in executed
        conn.commit.assert_called_once()


class TestCodelist:
    def test_returns_valid_values_and_hits_url(self):
        resp = MagicMock()
        resp.json.return_value = {
            "CodeList": [{"ValidValue": [{"Value": "A"}, {"Value": "B"}]}]
        }
        with patch("collect.requests.get", return_value=resp) as get:
            out = collect._codelist("Countries")
        assert out == [{"Value": "A"}, {"Value": "B"}]
        resp.raise_for_status.assert_called_once()
        url = get.call_args.args[0]
        assert url == f"{collect.CODELIST_URL}/Countries"
        assert get.call_args.kwargs["headers"] is collect.HEADERS


class TestGetLocationsFromApi:
    def test_filters_subdivisions_and_excludes_countries(self):
        subs = [
            {"Value": "California", "ParentCode": "US", "IsDisabled": "No"},
            {"Value": "Ontario", "ParentCode": "CA", "IsDisabled": "No"},
            {"Value": "OldState", "ParentCode": "US", "IsDisabled": "Yes"},
        ]
        countries = [
            {"Value": "Germany", "IsDisabled": "No"},
            {"Value": "United States", "IsDisabled": "No"},  # excluded
            {"Value": "Undefined", "IsDisabled": "No"},  # excluded
            {"Value": "Atlantis", "IsDisabled": "Yes"},  # disabled
        ]

        def fake_codelist(slug):
            return subs if slug == "CountrySubdivisions" else countries

        with patch("collect._codelist", side_effect=fake_codelist):
            us_subs, country_list = collect.get_locations_from_api()
        assert us_subs == ["California"]
        assert country_list == ["Germany"]


def _page_resp(total, items):
    resp = MagicMock()
    resp.json.return_value = {
        "SearchResult": {
            "SearchResultCountAll": total,
            "SearchResultItems": items,
        }
    }
    return resp


class TestSearchPages:
    def test_stops_when_total_reached(self):
        # total=1 <= 500 fetched after page 1 -> single page only.
        items = [_item("A"), _item("B")]
        with (
            patch("collect.requests.get", return_value=_page_resp(1, items)) as get,
            patch("collect.time.sleep") as sleep,
        ):
            out = list(collect.search_pages({"LocationName": "X"}))
        assert out == items
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_paginates_until_total_consumed(self):
        # total=600 -> page1 (500 fetched < 600) continues, page2 (1000 >= 600) stops.
        page1 = [_item(f"P1-{i}") for i in range(3)]
        page2 = [_item(f"P2-{i}") for i in range(2)]
        responses = [_page_resp(600, page1), _page_resp(600, page2)]
        with (
            patch("collect.requests.get", side_effect=responses) as get,
            patch("collect.time.sleep"),
        ):
            out = list(collect.search_pages({"LocationName": "X"}))
        assert out == page1 + page2
        assert get.call_count == 2
        # Page param advanced on second call.
        assert get.call_args_list[1].kwargs["params"]["Page"] == 2

    def test_stops_on_empty_items(self):
        # High total but empty items -> break despite total not reached.
        with (
            patch("collect.requests.get", return_value=_page_resp(9999, [])) as get,
            patch("collect.time.sleep") as sleep,
        ):
            out = list(collect.search_pages({"LocationName": "X"}))
        assert out == []
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_raises_for_status_propagates(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = RuntimeError("boom")
        with patch("collect.requests.get", return_value=resp):
            gen = collect.search_pages({"LocationName": "X"})
            with pytest.raises(RuntimeError):
                next(gen)

    def test_respects_max_pages(self):
        # Never reaches total; max_pages caps iterations.
        resp = _page_resp(10**9, [_item("A")])
        with (
            patch("collect.requests.get", return_value=resp) as get,
            patch("collect.time.sleep"),
        ):
            list(collect.search_pages({"LocationName": "X"}, max_pages=3))
        assert get.call_count == 3


class TestRefreshGeo:
    def test_executes_refresh_and_commits(self):
        conn, cur = _cursor_conn()
        collect.refresh_geo(conn)
        cur.execute.assert_called_once_with("SELECT refresh_jobs_geo()")
        conn.commit.assert_called_once()


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


class TestCollectFull:
    def test_orchestration_inserts_new_and_dedupes(self):
        conn, cur = _cursor_conn()
        # _ensure_schema: to_regclass returns existing table -> no SCHEMA_SQL.
        # existing-data preload: fetchall returns empty (no existing jobs).
        cur.fetchone.return_value = ("jobs_raw",)
        cur.fetchall.return_value = []

        pages = {
            "California": [_item("J1"), _item("J2"), _item("J1")],  # J1 dupe
            "Germany": [_item("J3"), _item("BAD", junk=1)],
        }

        def fake_search(params):
            loc = params["LocationName"]
            # catch-alls return nothing
            return iter(pages.get(loc, []))

        # BAD item: strip PositionID so it counts as a dupe/skip path.
        pages["Germany"][1]["MatchedObjectDescriptor"].pop("PositionID")

        with (
            patch("collect.psycopg2.connect", return_value=conn),
            patch(
                "collect.get_locations_from_api",
                return_value=(["California"], ["Germany"]),
            ),
            patch("collect.search_pages", side_effect=fake_search),
            patch("collect.refresh_geo") as refresh,
            patch("collect.print_stats"),
        ):
            collect.collect_full()

        # New jobs J1, J2, J3 inserted exactly once each.
        inserts = [
            c for c in cur.execute.call_args_list if "INSERT INTO jobs_raw" in c.args[0]
        ]
        inserted_pids = sorted(c.args[1][0] for c in inserts)
        assert inserted_pids == ["J1", "J2", "J3"]
        refresh.assert_called_once_with(conn)
        conn.close.assert_called_once()
        # commit called per-location (4 locations: CA, DE, + 2 catch-alls).
        assert conn.commit.call_count >= 1

    def test_changed_existing_job_archived(self):
        conn, cur = _cursor_conn()
        cur.fetchone.return_value = ("jobs_raw",)
        old = _item("J1", Title="Old")
        cur.fetchall.return_value = [("J1", old["MatchedObjectDescriptor"])]
        new = _item("J1", Title="New")

        def fake_search(params):
            if params["LocationName"] == "California":
                return iter([new])
            return iter([])

        with (
            patch("collect.psycopg2.connect", return_value=conn),
            patch(
                "collect.get_locations_from_api",
                return_value=(["California"], []),
            ),
            patch("collect.search_pages", side_effect=fake_search),
            patch("collect.refresh_geo"),
            patch("collect.print_stats"),
        ):
            collect.collect_full()

        # Since existing serialized form differs from new, expect a history insert
        # or an update (changed path). The data dicts differ in Title.
        history = [
            c
            for c in cur.execute.call_args_list
            if "INSERT INTO jobs_history" in c.args[0]
        ]
        assert len(history) == 1


class TestCollectDaily:
    def test_orchestration_upserts_each_item(self):
        conn, cur = _cursor_conn()
        cur.fetchone.side_effect = [
            ("jobs_raw",),  # _ensure_schema: table present
            None,  # upsert_item SELECT for D1 -> new
            None,  # upsert_item SELECT for D2 -> new
        ]

        items = [_item("D1"), _item("D2")]

        with (
            patch("collect.psycopg2.connect", return_value=conn),
            patch("collect.search_pages", return_value=iter(items)),
            patch("collect.refresh_geo") as refresh,
            patch("collect.print_stats"),
        ):
            collect.collect_daily()

        inserts = [
            c for c in cur.execute.call_args_list if "INSERT INTO jobs_raw" in c.args[0]
        ]
        assert sorted(c.args[1][0] for c in inserts) == ["D1", "D2"]
        refresh.assert_called_once_with(conn)
        # _ensure_schema commits once, the daily loop commits once.
        assert conn.commit.call_count == 2
        conn.close.assert_called_once()

    def test_search_uses_dateposted_param(self):
        conn, cur = _cursor_conn()
        cur.fetchone.return_value = ("jobs_raw",)

        with (
            patch("collect.psycopg2.connect", return_value=conn),
            patch("collect.search_pages", return_value=iter([])) as sp,
            patch("collect.refresh_geo"),
            patch("collect.print_stats"),
        ):
            collect.collect_daily()

        sp.assert_called_once_with({"DatePosted": "1"})


class TestPrintStats:
    def test_print_stats_queries_and_closes(self):
        conn, cur = _cursor_conn()
        cur.fetchone.side_effect = [
            (15000,),  # total
            (300,),  # orgs
            ("2024", "2025"),  # first/last
            (42,),  # history
            (7,),  # changed jobs
        ]
        with patch("collect.psycopg2.connect", return_value=conn):
            collect.print_stats()
        conn.close.assert_called_once()
        assert cur.execute.call_count == 5


class TestDbConfig:
    def test_parses_url_into_kwargs(self):
        with patch.dict(
            "collect.os.environ",
            {"DATABASE_URL_COLLECTOR": "postgresql://u:p@host.local:6543/mydb"},
        ):
            cfg = collect._db_config()
        assert cfg == {
            "host": "host.local",
            "port": 6543,
            "dbname": "mydb",
            "user": "u",
            "password": "p",
        }

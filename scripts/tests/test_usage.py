"""Tests for the Usage Statistics queries (cbut_db). Standard library only.

Run:  python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_stats",
                                              SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

HOUR = 3600 * 1000


def make_db(path, rows):
    """rows: (mid, model, prompt, completion, hit, miss, write, ts, usage
    [, provider_total]). The optional 10th element is provider_total_tokens."""
    conn = db.open_db(path)
    db.ensure_schema(conn)
    for r in rows:
        mid, model, pt, ct, hit, miss, wr, ts, ua = r[:9]
        ptotal = r[9] if len(r) > 9 else None
        conn.execute(
            "INSERT INTO model_responses(message_id, model, prompt_tokens,"
            " completion_tokens, prompt_cache_hit_tokens,"
            " prompt_cache_miss_tokens, prompt_cache_write_tokens,"
            " provider_total_tokens, ts, usage_available)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, model, pt, ct, hit, miss, wr, ptotal, ts, ua),
        )
    conn.commit()
    conn.close()


class WindowTest(unittest.TestCase):
    NOW = 1_000_000_000_000

    def _midnight(self, ms):
        return datetime.fromtimestamp(ms / 1000).replace(
            hour=0, minute=0, second=0, microsecond=0)

    def _day_start(self, days_back):
        m = self._midnight(self.NOW) - timedelta(days=days_back)
        return int(m.timestamp() * 1000)

    def test_1d_is_today_midnight_to_now(self):
        # Windows are whole local calendar days ending at ``now``: "1d" is
        # today, from local midnight (not "now minus 24 hours").
        start, end = db.window_bounds("1d", self.NOW)
        self.assertEqual(end, self.NOW)
        self.assertEqual(start, self._day_start(0))

    def test_2d_and_3d_span_whole_days(self):
        self.assertEqual(db.window_bounds("2d", self.NOW)[0], self._day_start(1))
        self.assertEqual(db.window_bounds("3d", self.NOW)[0], self._day_start(2))

    def test_7d_and_30d(self):
        self.assertEqual(db.window_bounds("7d", self.NOW)[0], self._day_start(6))
        self.assertEqual(db.window_bounds("30d", self.NOW)[0], self._day_start(29))

    def test_all_time_has_no_lower_bound(self):
        start, end = db.window_bounds("all", self.NOW)
        self.assertIsNone(start)
        self.assertEqual(end, self.NOW)

    def test_start_is_a_natural_day_boundary_not_now_minus_hours(self):
        # A local 00:00:00 boundary proves the window is calendar-day aligned,
        # not derived from "now minus N hours".
        start, _ = db.window_bounds("1d", self.NOW)
        d = datetime.fromtimestamp(start / 1000)
        self.assertEqual((d.hour, d.minute, d.second, d.microsecond),
                         (0, 0, 0, 0))
        self.assertNotEqual(start, self.NOW - 24 * HOUR)
        # a later "now" on the same day keeps the same lower bound (fixed day)
        self.assertEqual(db.window_bounds("1d", self.NOW + 1 * HOUR)[0], start)


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "u.db"
        # Noon today (local) so a "now - 1h" row is unambiguously inside the
        # calendar-day window and a "now - 100h" row unambiguously outside —
        # regardless of the hour the suite runs at.
        noon = datetime.now().replace(hour=12, minute=0, second=0,
                                      microsecond=0)
        self.now = int(noon.timestamp() * 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def _conn(self):
        return db.open_db(self.p, readonly=True)

    def _all(self):
        return db.window_bounds("all", self.now)

    def _today(self):
        return db.window_bounds("1d", self.now)

    def test_summary_only_counts_window(self):
        make_db(self.p, [
            ("in", "m", 10, 2, 5, 5, 0, self.now - 1 * HOUR, 1),
            ("out", "m", 20, 4, 0, 0, 0, self.now - 100 * HOUR, 1),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["requests"], 1)
        self.assertEqual(s["with_usage"], 1)
        self.assertEqual(s["prompt_tokens"], 10)
        self.assertEqual(s["completion_tokens"], 2)
        self.assertEqual(s["total_tokens"], 12)
        self.assertEqual(s["cache_hit"], 5)

    def test_logs_ordered_newest_first(self):
        make_db(self.p, [
            ("old", "m", 1, 1, 0, 0, 0, self.now - 2 * HOUR, 1),
            ("new", "m", 10, 20, 0, 0, 0, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        rows = db.q_usage_request_logs(conn, *self._all())
        conn.close()
        self.assertEqual([r["message_id"] for r in rows], ["new", "old"])

    def test_logs_tiebreak_by_message_id(self):
        # equal ts must still order deterministically (ts DESC, message_id ASC)
        make_db(self.p, [
            ("bbb", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
            ("aaa", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        rows = db.q_usage_request_logs(conn, *self._all())
        conn.close()
        self.assertEqual([r["message_id"] for r in rows], ["aaa", "bbb"])

    def test_logs_total_is_input_plus_output_and_excludes_cache(self):
        make_db(self.p, [
            ("a", "m", 10, 20, 999999, 888888, 7, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        r = db.q_usage_request_logs(conn, *self._all())[0]
        conn.close()
        self.assertEqual(r["total_tokens"], 30)   # not 10+20+999999+888888

    def test_total_null_when_a_part_is_missing(self):
        make_db(self.p, [("a", "m", 10, None, 0, 0, 0, self.now - 1 * HOUR, 1)])
        conn = self._conn()
        r = db.q_usage_request_logs(conn, *self._all())[0]
        conn.close()
        self.assertIsNone(r["total_tokens"])

    def test_null_and_zero_are_distinguished(self):
        make_db(self.p, [
            ("zero", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
            ("null", "m", 1, 1, None, None, None, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        rows = {r["message_id"]: r
                for r in db.q_usage_request_logs(conn, *self._all())}
        conn.close()
        self.assertEqual(rows["zero"]["prompt_cache_hit_tokens"], 0)
        self.assertIsNone(rows["null"]["prompt_cache_hit_tokens"])

    def test_model_stats_aggregates(self):
        make_db(self.p, [
            ("a", "mA", 10, 2, 80, 20, 0, self.now - 1 * HOUR, 1),
            ("b", "mA", 5, 1, 40, 10, 0, self.now - 1 * HOUR, 1),
            ("c", "mB", 3, 1, None, None, None, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        stats = {r["model"]: r
                 for r in db.q_usage_model_stats(conn, *self._all())}
        conn.close()
        self.assertEqual(stats["mA"]["requests"], 2)
        self.assertEqual(stats["mA"]["prompt_tokens"], 15)
        self.assertEqual(stats["mA"]["completion_tokens"], 3)
        self.assertEqual(stats["mA"]["total_tokens"], 18)
        self.assertEqual(stats["mA"]["cache_hit"], 120)
        self.assertEqual(stats["mA"]["cache_miss"], 30)
        self.assertIsNone(stats["mB"]["cache_hit"])

    def test_model_stats_default_order_by_total(self):
        make_db(self.p, [
            ("a", "small", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
            ("b", "big", 1000, 1000, 0, 0, 0, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        rows = db.q_usage_model_stats(conn, *self._all())
        conn.close()
        self.assertEqual([r["model"] for r in rows], ["big", "small"])

    def test_model_stats_order_by_model(self):
        make_db(self.p, [
            ("a", "zeta", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
            ("b", "alpha", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        rows = db.q_usage_model_stats(conn, *self._all(), order_by="model")
        conn.close()
        self.assertEqual([r["model"] for r in rows], ["alpha", "zeta"])

    # -- provider_total_tokens precedence + provenance --------------------

    def test_summary_total_prefers_provider_and_is_mixed_source(self):
        # one provider-total row + one derived-only row -> mixed provenance
        make_db(self.p, [
            ("a", "m", 10, 2, 0, 0, 0, self.now - 1 * HOUR, 1, 999),
            ("b", "m", 5, 5, 0, 0, 0, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["total_tokens"], 999 + 10)   # 999 + (5+5)
        self.assertEqual(s["total_tokens_source"], "mixed")

    def test_summary_source_provider_when_all_provider(self):
        make_db(self.p, [
            ("a", "m", 10, 2, 0, 0, 0, self.now - 1 * HOUR, 1, 12),
            ("b", "m", 3, 3, 0, 0, 0, self.now - 1 * HOUR, 1, 6),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["total_tokens"], 18)
        self.assertEqual(s["total_tokens_source"], "provider")

    def test_summary_source_derived_when_none_provider(self):
        make_db(self.p, [("a", "m", 10, 2, 0, 0, 0, self.now - 1 * HOUR, 1)])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["total_tokens_source"], "derived")

    # -- display-only Usage Total (re-adds cache hit) ---------------------

    def test_summary_usage_total_readds_cache_hit(self):
        make_db(self.p, [
            ("a", "m", 100, 20, 80, 15, 5, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["total_tokens"], 120)        # API total (derived)
        self.assertEqual(s["usage_total_tokens"], 200)  # 100 + 20 + 80

    def test_usage_total_does_not_inflate_null_cache_rows(self):
        # A row without cache data contributes no hit (COALESCE to 0) and is
        # not dropped from the sum either.
        make_db(self.p, [
            ("a", "m", 100, 20, 80, 15, 5, self.now - 1 * HOUR, 1),
            ("b", "m", 5, 5, None, None, None, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertEqual(s["usage_total_tokens"], 200 + 10)

    def test_usage_total_null_when_api_parts_missing(self):
        make_db(self.p, [("a", "m", None, None, 80, 15, 5,
                          self.now - 1 * HOUR, 0)])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertIsNone(s["usage_total_tokens"])

    def test_model_tokens_exposes_usage_total(self):
        make_db(self.p, [("a", "mA", 100, 20, 80, 15, 5,
                          self.now - 1 * HOUR, 1)])
        conn = self._conn()
        r = {x["model"]: x for x in db.q_model_tokens(conn)}["mA"]
        conn.close()
        self.assertEqual(r["total_tokens"], 120)
        self.assertEqual(r["usage_total_tokens"], 200)

    def test_summary_source_null_when_no_rows(self):
        make_db(self.p, [("a", "m", 1, 1, 0, 0, 0, self.now - 100 * HOUR, 1)])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        conn.close()
        self.assertIsNone(s["total_tokens"])
        self.assertIsNone(s["total_tokens_source"])

    def test_aggregate_total_sums_per_row_not_provider_column(self):
        # SUM(provider_total_tokens) would drop the derived row; the aggregate
        # must sum the per-row totals instead.
        make_db(self.p, [
            ("prov", "m", 10, 2, 0, 0, 0, self.now - 1 * HOUR, 1, 100),
            ("der", "m", 4, 6, 0, 0, 0, self.now - 1 * HOUR, 1),   # no provider
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        m = {r["model"]: r for r in db.q_usage_model_stats(conn, *self._today())}
        conn.close()
        self.assertEqual(s["total_tokens"], 110)     # 100 + (4+6)
        self.assertEqual(m["m"]["total_tokens"], 110)

    def test_logs_total_prefers_provider_and_exposes_source(self):
        make_db(self.p, [
            ("a", "m", 10, 20, 0, 0, 0, self.now - 1 * HOUR, 1, 555),
        ])
        conn = self._conn()
        r = db.q_usage_request_logs(conn, *self._all())[0]
        conn.close()
        self.assertEqual(r["total_tokens"], 555)
        self.assertEqual(r["total_tokens_source"], "provider")

    def test_logs_expose_api_and_usage_total(self):
        make_db(self.p, [
            ("a", "m", 100, 20, 80, 15, 5, self.now - 1 * HOUR, 1),
        ])
        conn = self._conn()
        r = db.q_usage_request_logs(conn, *self._all())[0]
        conn.close()
        self.assertEqual(r["total_tokens"], 120)        # API total
        self.assertEqual(r["usage_total_tokens"], 200)  # + cache hit

    def test_model_stats_total_prefers_provider_and_source(self):
        make_db(self.p, [("a", "mA", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1, 77)])
        conn = self._conn()
        rows = db.q_usage_model_stats(conn, *self._all())
        conn.close()
        self.assertEqual(rows[0]["total_tokens"], 77)
        self.assertEqual(rows[0]["total_tokens_source"], "provider")

    def test_cache_never_in_total_with_provider(self):
        # provider total 30 while cache hit/miss are huge -> Total stays 30
        make_db(self.p, [
            ("a", "m", 10, 20, 999999, 888888, 7, self.now - 1 * HOUR, 1, 30),
        ])
        conn = self._conn()
        s = db.q_usage_summary(conn, *self._today())
        r = db.q_usage_request_logs(conn, *self._today())[0]
        conn.close()
        self.assertEqual(s["total_tokens"], 30)
        self.assertEqual(r["total_tokens"], 30)

    def test_null_vs_zero_provider_total(self):
        make_db(self.p, [
            ("zero", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1, 0),
            ("null", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1, None),
        ])
        conn = self._conn()
        rows = {r["message_id"]: r
                for r in db.q_usage_request_logs(conn, *self._all())}
        conn.close()
        self.assertEqual(rows["zero"]["total_tokens"], 0)        # real 0 kept
        self.assertEqual(rows["zero"]["total_tokens_source"], "provider")
        self.assertEqual(rows["null"]["total_tokens"], 2)        # derived 1+1
        self.assertEqual(rows["null"]["total_tokens_source"], "derived")

    def test_logs_default_limit_is_100(self):
        make_db(self.p, [
            (f"r{i}", "m", 1, 1, 0, 0, 0, self.now - 1 * HOUR, 1)
            for i in range(150)
        ])
        conn = self._conn()
        rows = db.q_usage_request_logs(conn, *self._all())
        conn.close()
        self.assertEqual(len(rows), 100)

    def test_missing_usage_is_null(self):
        make_db(self.p, [("a", "m", None, None, None, None, None,
                          self.now - 1 * HOUR, 0)])
        conn = self._conn()
        r = db.q_usage_request_logs(conn, *self._all())[0]
        s = db.q_usage_summary(conn, *self._all())
        conn.close()
        self.assertIsNone(r["prompt_tokens"])
        self.assertIsNone(r["total_tokens"])
        self.assertEqual(s["requests"], 1)
        self.assertEqual(s["with_usage"], 0)
        self.assertIsNone(s["total_tokens"])


class WindowedQueryTest(unittest.TestCase):
    """The non-Usage ``q_*`` queries accept an optional time window.

    ``start_ts``/``end_ts`` are both None by default (= all time), which is what
    the headless CLI relies on. A window is half-open: ``start_ts <= ts <
    end_ts``. Every query is keyed by NAME and unions "ever used" with the
    static inventory, so a window only shrinks the *counts* — an entity whose
    calls all fall outside the window is still listed, with a zero count.
    """

    NOW = 2_000_000_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "w.db"
        self.conn = db.open_db(self.p)
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _recent(self):
        return self.NOW - 24 * HOUR

    # small insert helpers -------------------------------------------------
    def _tool(self, cid, name, ts):
        self.conn.execute(
            "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
            " category, ts, duration_ms, status)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, "s1", "/p", name, "builtin", ts, 1, "completed"))

    def _skill(self, cid, name, ts):
        self.conn.execute(
            "INSERT INTO skill_usage(call_id, skill, plugin, session_id,"
            " project, ts, status, duration_ms)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, name, "pl", "s1", "/p", ts, "completed", 1))

    def _agent(self, cid, name, ts):
        self.conn.execute(
            "INSERT INTO agent_usage(call_id, agent_type, kind, source,"
            " session_id, project, ts, status, duration_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, name, "active", "tool", "s1", "/p", ts, "completed", 1))

    def _mcp(self, cid, server, tool, ts):
        self.conn.execute(
            "INSERT INTO mcp_usage(call_id, server, tool, session_id, project,"
            " ts, status, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
            (cid, server, tool, "s1", "/p", ts, "completed", 1))

    def _model(self, mid, model, pt, ct, ts):
        self.conn.execute(
            "INSERT INTO model_responses(message_id, session_id, model,"
            " prompt_tokens, completion_tokens, ts, usage_available)"
            " VALUES(?,?,?,?,?,?,?)",
            (mid, "s1", model, pt, ct, ts, 1))

    def _plugin(self, name):
        self.conn.execute(
            "INSERT INTO inventory(kind, name, owner_plugin, version, path,"
            " source) VALUES('plugin',?,NULL,'1.0','/x','user')", (name,))

    def _plugin_use(self, name, target, ts):
        self.conn.execute(
            "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
            " session_id, project, ts) VALUES(?,'mkt','skill',?,'s','/p',?)",
            (name, target, ts))

    # -- q_tools / q_skills / q_agents / q_mcp ----------------------------

    def test_q_tools_window_vs_all_time(self):
        self._tool("a", "Recent", self.NOW - HOUR)
        self._tool("b", "Old", self.NOW - 100 * HOUR)
        self.conn.commit()
        # Name is the key, so the list stays full under a window; only the
        # count is windowed ('Old' is listed with 0 in-window calls).
        recent = {r["tool_name"]: r for r in db.q_tools(
            self.conn, start_ts=self._recent(), end_ts=self.NOW)}
        self.assertEqual(set(recent), {"Recent", "Old"})
        self.assertEqual(recent["Recent"]["calls"], 1)
        self.assertEqual(recent["Old"]["calls"], 0)
        alltime = {r["tool_name"]: r for r in db.q_tools(self.conn)}
        self.assertEqual(alltime["Old"]["calls"], 1)

    def test_q_tools_limit_is_second_positional(self):
        # cbut-stats calls q_tools(conn, 10): ``limit`` must stay the second
        # positional argument, never reinterpreted as a time bound.
        for i in range(15):
            self._tool(f"t{i}", f"tool{i}", self.NOW - HOUR)
        self.conn.commit()
        rows = db.q_tools(self.conn, 10)
        self.assertEqual(len(rows), 10)

    def test_q_skills_agents_mcp_window_vs_all_time(self):
        self._skill("s1", "recent-skill", self.NOW - HOUR)
        self._skill("s2", "old-skill", self.NOW - 100 * HOUR)
        self._agent("a1", "recent-agent", self.NOW - HOUR)
        self._agent("a2", "old-agent", self.NOW - 100 * HOUR)
        self._mcp("m1", "srv", "recent-tool", self.NOW - HOUR)
        self._mcp("m2", "srv", "old-tool", self.NOW - 100 * HOUR)
        self.conn.commit()
        start, end = self._recent(), self.NOW
        # The list stays full; only the counts are windowed.
        skills = {r["skill"]: r for r in db.q_skills(
            self.conn, start_ts=start, end_ts=end)}
        self.assertEqual(set(skills), {"recent-skill", "old-skill"})
        self.assertEqual(skills["recent-skill"]["calls"], 1)
        self.assertEqual(skills["old-skill"]["calls"], 0)
        agents = {r["agent_type"]: r for r in db.q_agents(
            self.conn, start_ts=start, end_ts=end)}
        self.assertEqual(set(agents), {"recent-agent", "old-agent"})
        self.assertEqual(agents["old-agent"]["calls"], 0)
        mcp = {(r["server"], r["tool"]): r for r in db.q_mcp(
            self.conn, start_ts=start, end_ts=end)}
        self.assertEqual(set(mcp), {("srv", "recent-tool"),
                                    ("srv", "old-tool")})
        self.assertEqual(mcp[("srv", "old-tool")]["calls"], 0)
        # no bounds = all time: every entity keeps its real count
        self.assertEqual(len(db.q_skills(self.conn)), 2)
        self.assertEqual(len(db.q_agents(self.conn)), 2)
        self.assertEqual(len(db.q_mcp(self.conn)), 2)

    # -- q_plugins: all-time union keyed by name --------------------------

    def test_q_plugins_unions_installed_and_used_by_name(self):
        self._plugin("used")
        self._plugin("unused")
        self._plugin_use("used", "t", self.NOW - HOUR)
        self._plugin_use("used", "t2", self.NOW - 100 * HOUR)
        # a used-but-not-installed plugin must still appear (history survives)
        self._plugin_use("ghost", "t3", self.NOW - 5 * HOUR)
        self.conn.commit()
        rows = {r["plugin"]: r for r in db.q_plugins(self.conn)}
        self.assertEqual(set(rows), {"used", "unused", "ghost"})
        self.assertEqual(rows["used"]["uses"], 2)     # all-time, no window
        self.assertEqual(rows["unused"]["uses"], 0)   # installed, never used
        self.assertEqual(rows["ghost"]["uses"], 1)    # used, not installed

    def test_q_plugins_window_counts_uses_but_keeps_the_whole_list(self):
        """The two panels disagreed: Plugins ignored the window, so the same
        docx calls read 8 there and 1 under Skills at a 7-day range. The list
        stays all-time (the inventory has no timestamps); the counts do not."""
        self._plugin("used")
        self._plugin("unused")
        self._plugin_use("used", "t", self.NOW - HOUR)
        self._plugin_use("used", "t2", self.NOW - 100 * HOUR)
        self.conn.commit()
        rows = {r["plugin"]: r for r in db.q_plugins(
            self.conn, start_ts=self._recent(), end_ts=self.NOW)}
        self.assertEqual(set(rows), {"used", "unused"})
        self.assertEqual(rows["used"]["uses"], 1)
        self.assertEqual(rows["used"]["last_used"], self.NOW - HOUR)
        self.assertEqual(rows["unused"]["uses"], 0)
        # the state describes the entity, not the range
        self.assertEqual(rows["used"]["ever_used"], 1)

    # -- q_model_tokens ---------------------------------------------------

    def test_q_model_tokens_window_vs_all_time(self):
        self._model("a", "mA", 10, 2, self.NOW - HOUR)
        self._model("b", "mB", 5, 5, self.NOW - 100 * HOUR)
        self.conn.commit()
        win = {r["model"]: r for r in db.q_model_tokens(
            self.conn, start_ts=self._recent(), end_ts=self.NOW)}
        self.assertEqual(set(win), {"mA"})
        self.assertEqual(win["mA"]["prompt_tokens"], 10)
        alltime = {r["model"]: r for r in db.q_model_tokens(self.conn)}
        self.assertEqual(set(alltime), {"mA", "mB"})

    def test_q_model_tokens_lists_an_available_model_with_zero_rows(self):
        """The Tokens tab is a list too: a model the host offers must appear
        even before its first response, with 0 requests and NULL tokens (never
        a fabricated 0), sorted after every used model."""
        self._model("a", "used", 10, 2, self.NOW - HOUR)
        self.conn.execute(
            "INSERT INTO inventory(kind, name, owner_plugin, source)"
            " VALUES('model','available','builtin','builtin')")
        self.conn.commit()
        rows = db.q_model_tokens(self.conn)
        self.assertEqual([r["model"] for r in rows], ["used", "available"])
        zero = rows[-1]
        self.assertEqual(zero["provider"], "builtin")
        self.assertEqual(zero["responses"], 0)
        self.assertIsNone(zero["prompt_tokens"])
        self.assertIsNone(zero["total_tokens"])

    # -- _time_filter helpers ---------------------------------------------

    def test_time_filter_none_none_is_no_filter(self):
        self.assertEqual(db._time_filter(None, None), ("", []))

    def test_time_filter_end_only(self):
        where, params = db._time_filter(None, self.NOW)
        self.assertEqual(where, " WHERE ts < ?")
        self.assertEqual(params, [self.NOW])

    def test_time_filter_start_and_end(self):
        where, params = db._time_filter(self._recent(), self.NOW)
        self.assertEqual(where, " WHERE ts >= ? AND ts < ?")
        self.assertEqual(params, [self._recent(), self.NOW])

    # -- q_usage_kpi (Dashboard) ------------------------------------------

    def test_q_usage_kpi_window(self):
        self._tool("t1", "T", self.NOW - HOUR)
        self._tool("t2", "T", self.NOW - 100 * HOUR)
        self._skill("s1", "sk", self.NOW - HOUR)
        self._agent("a1", "ag", self.NOW - HOUR)
        self._agent("a2", "ag", self.NOW - 100 * HOUR)
        self._mcp("m1", "srv", "t", self.NOW - HOUR)
        self._plugin_use("p1", "t", self.NOW - HOUR)
        self._plugin_use("p2", "t", self.NOW - 100 * HOUR)
        self.conn.commit()
        k = db.q_usage_kpi(self.conn, self._recent(), self.NOW)
        self.assertEqual(k, {"tool_calls": 1, "skills": 1, "agents": 1,
                             "mcp": 1, "plugins": 1})
        allk = db.q_usage_kpi(self.conn, None, None)   # all-time
        self.assertEqual(allk["tool_calls"], 2)
        self.assertEqual(allk["agents"], 2)
        self.assertEqual(allk["plugins"], 2)           # distinct plugins

    def test_q_usage_kpi_empty_window(self):
        self.assertEqual(
            db.q_usage_kpi(self.conn, None, None),
            {"tool_calls": 0, "skills": 0, "agents": 0, "mcp": 0, "plugins": 0})

    def test_q_usage_kpi_matches_name_query_sums(self):
        # The KPI COUNT(*) must equal the sum of the per-name `calls` column in
        # the same window, so the Dashboard always agrees with the tabs.
        for i in range(3):
            self._tool(f"t{i}", f"tool{i}", self.NOW - HOUR)
        self._tool("old", "oldtool", self.NOW - 100 * HOUR)
        self._skill("s1", "sk", self.NOW - HOUR)
        self._agent("a1", "ag", self.NOW - HOUR)
        self._mcp("m1", "srv", "t", self.NOW - HOUR)
        self.conn.commit()
        start, end = self._recent(), self.NOW
        k = db.q_usage_kpi(self.conn, start, end)
        self.assertEqual(k["tool_calls"], sum(
            r["calls"] for r in db.q_tools(self.conn, start_ts=start, end_ts=end)))
        self.assertEqual(k["skills"], sum(
            r["calls"] for r in db.q_skills(self.conn, start_ts=start, end_ts=end)))
        self.assertEqual(k["agents"], sum(
            r["calls"] for r in db.q_agents(self.conn, start_ts=start, end_ts=end)))
        self.assertEqual(k["mcp"], sum(
            r["calls"] for r in db.q_mcp(self.conn, start_ts=start, end_ts=end)))

    # -- q_usage_activity / q_usage_daily (Dashboard extras) --------------

    def test_q_usage_activity_window(self):
        ins = ("INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
               " category, ts, duration_ms, status) VALUES(?,?,?,?,?,?,?,?)")
        self.conn.executemany(ins, [
            ("r1", "s1", "/pA", "T", "builtin", self.NOW - HOUR, 100, "completed"),
            ("r2", "s1", "/pA", "T", "builtin", self.NOW - 2 * HOUR, 300, "completed"),
            ("r3", "s2", "/pB", "T", "builtin", self.NOW - 3 * HOUR, None, "incomplete"),
            ("old", "s3", "/pC", "T", "builtin", self.NOW - 100 * HOUR, 50, "completed"),
        ])
        self.conn.commit()
        a = db.q_usage_activity(self.conn, self._recent(), self.NOW)
        self.assertEqual(a["calls"], 3)
        self.assertEqual(a["completed"], 2)
        self.assertEqual(a["incomplete"], 1)
        self.assertEqual(a["avg_ms"], 200)        # (100 + 300 + NULL) / 2
        self.assertEqual(a["sessions"], 2)        # s1, s2 (old s3 is outside)
        self.assertEqual(a["projects"], 2)        # /pA, /pB
        allt = db.q_usage_activity(self.conn)     # both bounds None = all-time
        self.assertEqual(allt["calls"], 4)
        self.assertEqual(allt["sessions"], 3)

    def test_q_usage_activity_empty(self):
        self.assertEqual(db.q_usage_activity(self.conn),
                         {"calls": 0, "completed": None, "incomplete": None,
                          "avg_ms": None, "sessions": 0, "projects": 0})

    def test_q_usage_daily_buckets_by_local_day(self):
        mid = datetime.fromtimestamp(self.NOW / 1000).replace(
            hour=12, minute=0, second=0, microsecond=0)
        day0 = int((mid - timedelta(days=2)).timestamp() * 1000)
        d1 = int(mid.timestamp() * 1000)                    # noon
        d1_later = int(mid.replace(hour=13).timestamp() * 1000)  # same local day
        self.conn.executemany(
            "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
            " category, ts, duration_ms, status) VALUES(?,?,?,?,?,?,?,?)",
            [("a", "s", "/p", "T", "builtin", day0, 1, "completed"),
             ("b", "s", "/p", "T", "builtin", d1, 1, "completed"),
             ("c", "s", "/p", "T", "builtin", d1_later, 1, "completed")])
        self.conn.commit()
        got = db.q_usage_daily(self.conn)
        key = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        self.assertEqual([(r["day"], r["calls"]) for r in got],
                         [(key(day0), 1), (key(d1), 2)])


class EntityStateTest(unittest.TestCase):
    """Installed / in use / removed — derived in the query, never stored.

    ``inventory`` is a snapshot of what is installed *now* (cbut-sync rebuilds
    it on every sync), so a name with usage history and no inventory row has
    been uninstalled, renamed or removed. Before the flags existed that row was
    indistinguishable from one that had simply never been called, which is the
    whole reason the Skills tab kept listing skills that were not on disk.

    Both flags come out of the name CTE, not out of a column: there is nothing
    to migrate, and — more to the point — nothing that can drift away from the
    snapshot it claims to describe.
    """

    NOW = 2_000_000_000_000
    DAY = 24 * HOUR

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "s.db")
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- seeders: one name per state, for every entity kind -----------------

    def _installed(self, kind, name):
        self.conn.execute(
            "INSERT INTO inventory(kind, name, source) VALUES(?,?,'user')",
            (kind, name))

    def _used(self, kind, name, ts=None):
        """One recorded call for ``name``, at ``ts`` (default: an hour ago)."""
        at = self.NOW - HOUR if ts is None else ts
        if kind == "tool":
            self.conn.execute(
                "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
                " category, ts, status) VALUES(?,'s','/p',?,'builtin',?,"
                "'completed')", (f"t{name}", name, at))
        elif kind == "skill":
            self.conn.execute(
                "INSERT INTO skill_usage(call_id, skill, session_id, project,"
                " ts, status) VALUES(?,?,'s','/p',?,'completed')",
                (f"s{name}", name, at))
        elif kind == "agent":
            self.conn.execute(
                "INSERT INTO agent_usage(call_id, agent_type, kind, source,"
                " session_id, project, ts, status)"
                " VALUES(?,?,'active','tool','s','/p',?,'completed')",
                (f"a{name}", name, at))
        elif kind == "plugin":
            self.conn.execute(
                "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
                " session_id, project, ts)"
                " VALUES(?,'mkt','skill','t','s','/p',?)", (name, at))
        elif kind == "mcp":
            self.conn.execute(
                "INSERT INTO mcp_usage(call_id, server, tool, session_id,"
                " project, ts, status) VALUES(?,?,'t','s','/p',?,"
                "'completed')", (f"m{name}", name, at))
        else:  # pragma: no cover - a typo in the table above, not a state
            raise AssertionError(kind)

    # -- the three states, on every entity kind ----------------------------

    def _states(self, kind):
        """{name: label} for one kind, from the query its own tab uses."""
        fetch, key = {
            "tool": (db.q_tools, "tool_name"),
            "skill": (db.q_skills, "skill"),
            "agent": (db.q_agents, "agent_type"),
            "plugin": (db.q_plugins, "plugin"),
            "mcp": (db.q_mcp, "server"),
        }[kind]
        count_key = "uses" if kind == "plugin" else "calls"
        return {r[key]: db.entity_state(r["installed"], r["ever_used"],
                                        r[count_key])
                for r in fetch(self.conn)}

    def test_a_name_with_history_and_no_inventory_row_is_removed(self):
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            with self.subTest(kind=kind):
                self._used(kind, "gone")
                self._installed(kind, "here")
        self.conn.commit()
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            self.assertEqual(self._states(kind)["gone"],
                             db.ENTITY_STATE_REMOVED, kind)

    def test_an_installed_entity_that_was_never_called_is_installed(self):
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            self._installed(kind, "quiet")
        self.conn.commit()
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            self.assertEqual(self._states(kind)["quiet"],
                             db.ENTITY_STATE_INSTALLED, kind)

    def test_a_live_entity_in_use_is_in_use(self):
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            self._installed(kind, "busy")
            self._used(kind, "busy")
        self.conn.commit()
        for kind in ("tool", "skill", "agent", "plugin", "mcp"):
            self.assertEqual(self._states(kind)["busy"],
                             db.ENTITY_STATE_USED, kind)

    def test_removing_an_entity_does_not_remove_its_history(self):
        # The point of the union: a sync that drops the inventory row must not
        # take the usage rows with it, or "was it ever used?" becomes
        # unanswerable the moment the plugin is uninstalled.
        self._installed("tool", "Read")
        self._used("tool", "Read")
        self.conn.commit()
        self.conn.execute("DELETE FROM inventory WHERE name='Read'")
        self.conn.commit()
        rows = db.q_tools(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 1)
        self.assertEqual(db.entity_state(rows[0]["installed"],
                                         rows[0]["ever_used"]),
                         db.ENTITY_STATE_REMOVED)

    # -- the two things that are easy to get wrong -------------------------

    def test_the_state_follows_the_window(self):
        # ``calls`` is windowed, and the label follows it: an entity last used a
        # week ago is installed but quiet inside a one-day window. The old
        # all-time label read "in use" beside a 0-call row, which is the display
        # the Tools tab was reported for.
        self._installed("tool", "Old")
        self._used("tool", "Old", ts=self.NOW - 7 * self.DAY)
        self.conn.commit()
        win = db.q_tools(self.conn, start_ts=self.NOW - self.DAY, end_ts=self.NOW)
        row = {r["tool_name"]: r for r in win}["Old"]
        self.assertEqual(row["calls"], 0)
        self.assertEqual(db.entity_state(row["installed"], row["ever_used"],
                                         row["calls"]),
                         db.ENTITY_STATE_INSTALLED)
        alltime = {r["tool_name"]: r for r in db.q_tools(self.conn)}["Old"]
        self.assertEqual(db.entity_state(alltime["installed"],
                                         alltime["ever_used"], alltime["calls"]),
                         db.ENTITY_STATE_USED)

    def test_an_uninstalled_entity_stays_removed_even_with_calls_in_range(self):
        self._used("tool", "gone", ts=self.NOW - HOUR)
        self.conn.commit()
        row = {r["tool_name"]: r for r in db.q_tools(self.conn)}["gone"]
        self.assertEqual(db.entity_state(row["installed"], row["ever_used"],
                                         row["calls"]),
                         db.ENTITY_STATE_REMOVED)

    def test_a_name_on_both_sides_is_counted_once(self):
        # The CTE used to emit one row per side, so the LEFT JOIN matched every
        # usage row twice and ``cbut plugins`` reported 4 uses for 2 calls.
        # Both wrong halves agreed with each other, which is why the existing
        # window/all-time comparison stayed green while it happened.
        self._installed("plugin", "p")
        self._used("plugin", "p")
        self._used("plugin", "p", ts=self.NOW - 2 * HOUR)
        self.conn.commit()
        row = {r["plugin"]: r for r in db.q_plugins(self.conn)}["p"]
        self.assertEqual(row["uses"], 2)
        self.assertEqual(row["installed"], 1)
        self.assertEqual(row["ever_used"], 1)

    def test_mcp_rows_inherit_their_servers_install_state(self):
        # mcp.json configures servers, not individual tools, so a tool under a
        # configured server is installed and a tool under a server that is no
        # longer configured is not.
        self._installed("mcp", "kept")
        self._installed("mcp", "quiet-server")
        self._used("mcp", "kept")
        self._used("mcp", "dropped")
        self.conn.commit()
        rows = {(r["server"], r["tool"]): r for r in db.q_mcp(self.conn)}
        self.assertEqual(db.entity_state(rows[("kept", "t")]["installed"],
                                         rows[("kept", "t")]["ever_used"]),
                         db.ENTITY_STATE_USED)
        self.assertEqual(db.entity_state(rows[("dropped", "t")]["installed"],
                                         rows[("dropped", "t")]["ever_used"]),
                         db.ENTITY_STATE_REMOVED)
        # a configured server with no calls is listed once, as (server, NULL)
        quiet = rows[("quiet-server", None)]
        self.assertEqual(db.entity_state(quiet["installed"], quiet["ever_used"]),
                         db.ENTITY_STATE_INSTALLED)

    # -- the flags are read, not looked up somewhere else ------------------

    def test_the_export_views_agree_with_the_queries(self):
        self._installed("tool", "Read")
        self._used("tool", "Read")
        self._used("tool", "gone")
        self.conn.commit()
        queried = {r["tool_name"]: (r["installed"], r["ever_used"])
                   for r in db.q_tools(self.conn)}
        exported = {r["tool_name"]: (r["installed"], r["ever_used"])
                    for r in self.conn.execute("SELECT * FROM v_tools")}
        self.assertEqual(exported, queried)

    def test_every_entity_view_carries_the_two_flags(self):
        for view, key in (("v_tools", "tool_name"), ("v_skills", "skill"),
                          ("v_agents", "agent_type"), ("v_mcp", "server"),
                          ("v_plugins", "plugin")):
            with self.subTest(view=view):
                cols = {c[1] for c in
                        self.conn.execute(f"PRAGMA table_info({view})")}
                self.assertLessEqual({"installed", "ever_used"}, cols)

    def test_the_inventory_table_still_has_no_state_column(self):
        # Q6 was decided as "derive it, do not migrate": the answer is a
        # function of the snapshot, and a stored copy of it is a second thing
        # that can be stale. This pins the shape so the next person does not
        # "helpfully" add a column that the DELETE-every-sync can never fill.
        cols = {c[1] for c in
                self.conn.execute("PRAGMA table_info(inventory)")}
        self.assertNotIn("installed", cols)
        self.assertNotIn("ever_used", cols)

    # -- one label, two front ends -----------------------------------------

    def test_both_front_ends_render_the_shared_label(self):
        for name in ("cbut-tui.py", "cbut-stats.py"):
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("db.entity_state(", source)
                # and neither re-declares the wording next to its own columns
                for label in (db.ENTITY_STATE_USED, db.ENTITY_STATE_INSTALLED,
                              db.ENTITY_STATE_REMOVED):
                    self.assertNotIn(
                        f"= \"{label}\"", source,
                        f"{name} re-declares {label!r} instead of calling "
                        "db.entity_state")


class HitRateTest(unittest.TestCase):
    def test_rate(self):
        self.assertAlmostEqual(db.cache_hit_rate(80, 20), 0.8)

    def test_rate_undefined(self):
        self.assertIsNone(db.cache_hit_rate(None, None))
        self.assertIsNone(db.cache_hit_rate(0, 0))
        self.assertIsNone(db.cache_hit_rate(5, None))
        self.assertIsNone(db.cache_hit_rate(None, 5))

    def test_rate_includes_writes_in_denominator(self):
        # cacheable input = hit + miss + write (cc-switch's definition, and the
        # exact partition of prompt_tokens). Writes lower the rate.
        self.assertAlmostEqual(db.cache_hit_rate(1, 3, 0), 0.25)
        self.assertAlmostEqual(db.cache_hit_rate(1, 3, 1), 0.2)
        self.assertAlmostEqual(db.cache_hit_rate(80, 20, 0), 0.8)
        # write=None (a legacy 2-arg call) counts as 0 -> unchanged.
        self.assertAlmostEqual(db.cache_hit_rate(1, 3), 0.25)


class MigrateTest(unittest.TestCase):
    def test_v2_db_migrates_and_queries(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "old.db"
            c = sqlite3.connect(p)
            c.executescript(
                "CREATE TABLE model_responses("
                " message_id TEXT PRIMARY KEY, session_id TEXT,"
                " conversation_request_id TEXT, model TEXT,"
                " prompt_tokens INTEGER, completion_tokens INTEGER,"
                " cache_read_input_tokens INTEGER,"
                " cache_creation_input_tokens INTEGER, ts INTEGER,"
                " project TEXT, source TEXT, usage_available INTEGER, missing TEXT);"
            )
            c.execute("INSERT INTO model_responses(message_id, model,"
                      " prompt_tokens, completion_tokens, ts, usage_available)"
                      " VALUES('o','m',5,6,1,1)")
            c.commit()
            c.close()

            self.assertEqual(db.migrate(p), "migrated")
            conn = db.open_db(p, readonly=True)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
            self.assertIn("prompt_cache_hit_tokens", cols)
            self.assertIn("provider_total_tokens", cols)
            s = db.q_usage_summary(conn, *db.window_bounds("all", 10))
            conn.close()
            self.assertEqual(s["total_tokens"], 11)
        finally:
            tmp.cleanup()

    def test_migrate_is_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "u.db"
            conn = db.open_db(p)
            db.ensure_schema(conn)
            conn.close()
            # A status string, not a boolean: "current" and "failed: …" are both
            # truthy, so assertTrue(migrate(p)) would pass on a broken database.
            self.assertEqual(db.migrate(p), "current")
            self.assertEqual(db.migrate(p), "current")
        finally:
            tmp.cleanup()

    def test_a_database_from_a_future_build_is_left_alone(self):
        # The point of the version gate: an older cbut must not clear state a
        # newer one wrote just because its own CREATE TABLE is a no-op.
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "future.db"
            conn = db.open_db(p)
            db.ensure_schema(conn)
            conn.execute("INSERT INTO sync_state(file_path, size, mtime, offset)"
                         " VALUES('a',1,1.0,1)")
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                         (str(db.SCHEMA_VERSION + 1),))
            conn.commit()
            conn.close()

            self.assertEqual(db.migrate(p), "newer")
            conn = db.open_db(p, readonly=True)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 1,
                    "a refused upgrade must still clear nothing")
                self.assertEqual(
                    int(conn.execute("SELECT value FROM meta "
                                     "WHERE key='schema_version'").fetchone()[0]),
                    db.SCHEMA_VERSION + 1, "the version must not be overwritten")
            finally:
                conn.close()
        finally:
            tmp.cleanup()

    def test_v4_database_loses_the_prose_columns(self):
        # v5 dropped sessions.title and agent_usage.description because both held
        # prose. The metadata in those same rows has to survive the drop.
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "v4.db"
            c = sqlite3.connect(p)
            c.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, project TEXT,"
                " title TEXT, started_at INTEGER, ended_at INTEGER, model TEXT,"
                " tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0);"
                "CREATE TABLE agent_usage(call_id TEXT PRIMARY KEY, agent_type TEXT,"
                " description TEXT, kind TEXT, source TEXT, session_id TEXT,"
                " project TEXT, ts INTEGER, status TEXT, duration_ms INTEGER);"
            )
            c.execute("INSERT INTO sessions VALUES('S','/p','a generated title',"
                      "1,2,'m',100,50)")
            c.execute("INSERT INTO agent_usage VALUES('c1','Explore',"
                      "'what the model wrote','active','tool','S','/p',1,'done',5)")
            c.execute("INSERT INTO meta(key, value) VALUES('schema_version','4')")
            c.commit()
            c.close()

            self.assertEqual(db.migrate(p), "migrated")
            conn = db.open_db(p, readonly=True)
            try:
                s_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
                a_cols = {r[1] for r in
                          conn.execute("PRAGMA table_info(agent_usage)")}
                self.assertNotIn("title", s_cols)
                self.assertNotIn("description", a_cols)
                row = conn.execute("SELECT project, tokens FROM sessions"
                                   " WHERE session_id='S'").fetchone()
                self.assertEqual((row["project"], row["tokens"]), ("/p", 100))
                self.assertEqual(
                    conn.execute("SELECT agent_type FROM agent_usage"
                                 " WHERE call_id='c1'").fetchone()[0], "Explore")
            finally:
                conn.close()
        finally:
            tmp.cleanup()

    def test_migrated_schema_matches_a_fresh_database(self):
        """The drift guard for the hand-maintained migration list.

        A column added to CREATE TABLE but not registered in _NEW_MODEL_COLUMNS
        would leave every upgraded database permanently short of it, with nothing
        failing until a query asked for it. Comparing the two shapes turns that
        into a failure at commit time instead.
        """
        fresh_tmp = tempfile.TemporaryDirectory()
        old_tmp = tempfile.TemporaryDirectory()
        try:
            fresh_path = Path(fresh_tmp.name) / "fresh.db"
            conn = db.open_db(fresh_path)
            db.ensure_schema(conn)
            fresh_shape = _table_shape(conn)
            conn.close()
            self.assertGreater(len(fresh_shape), 8, "the fixture schema is empty")

            old_path = Path(old_tmp.name) / "old.db"
            c = sqlite3.connect(old_path)
            c.executescript(
                "CREATE TABLE model_responses("
                " message_id TEXT PRIMARY KEY, session_id TEXT,"
                " conversation_request_id TEXT, model TEXT,"
                " prompt_tokens INTEGER, completion_tokens INTEGER,"
                " cache_read_input_tokens INTEGER,"
                " cache_creation_input_tokens INTEGER, ts INTEGER,"
                " project TEXT, source TEXT, usage_available INTEGER,"
                " missing TEXT);"
            )
            c.execute("INSERT INTO model_responses(message_id, model,"
                      " prompt_tokens, completion_tokens, ts, usage_available)"
                      " VALUES('o','m',1,1,1,1)")
            c.commit()
            c.close()

            self.assertEqual(db.migrate(old_path), "migrated")
            conn = db.open_db(old_path)
            migrated = _table_shape(conn)
            conn.close()
            self.assertEqual(
                migrated, fresh_shape,
                "an upgraded database is not shaped like a fresh one: register "
                "the difference as a migration")
        finally:
            fresh_tmp.cleanup()
            old_tmp.cleanup()


def _table_shape(conn) -> dict:
    """{table: ((column, type, notnull, pk), …)} sorted by column — what exists.

    Two things are deliberately *not* compared, because neither is part of the
    contract and both produced false alarms when included:

    * ``dflt_value`` — an older database can declare the same column without a
      default the current CREATE TABLE added, and every INSERT here names those
      columns explicitly, so the difference is cosmetic;
    * **declaration order** — a migrated table gains its columns at the end via
      ``ALTER TABLE``, while a fresh one has them interleaved. SQLite reads by
      name, so only the set of columns is meaningful.

    A *missing column* is what this guard is for: adding one to CREATE TABLE
    without registering it in the migration list leaves every upgraded database
    short of it, silently, until a query asks for it.
    """
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {
        n: sorted((r["name"], r["type"], r["notnull"], r["pk"])
                  for r in conn.execute(f"PRAGMA table_info({n})"))
        for n in names
    }


class ViewGroupByTest(unittest.TestCase):
    """The v_* views merge by NAME (name is the primary key), so the same name
    appearing under several owners becomes a single row. A bare column that is
    not functionally dependent on the name would let SQLite pick an arbitrary
    row, so only name-dependent columns survive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "v.db"
        self.conn = db.open_db(self.p)
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_skills_merge_by_name(self):
        self.conn.execute("INSERT INTO skill_usage(call_id, skill, plugin, ts)"
                          " VALUES('c1','shared','pA',1)")
        self.conn.execute("INSERT INTO skill_usage(call_id, skill, plugin, ts)"
                          " VALUES('c2','shared','pB',2)")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT skill, calls FROM v_skills WHERE skill='shared'").fetchall()
        self.assertEqual(len(rows), 1)          # one merged row, not two
        self.assertEqual(rows[0]["calls"], 2)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(v_skills)")}
        self.assertNotIn("plugin", cols)        # the source plugin is gone

    def test_agents_merge_by_name(self):
        self.conn.execute("INSERT INTO agent_usage(call_id, agent_type, ts)"
                          " VALUES('a1','shared',1)")
        self.conn.execute("INSERT INTO agent_usage(call_id, agent_type, ts)"
                          " VALUES('a2','shared',2)")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT agent_type, calls FROM v_agents"
            " WHERE agent_type='shared'").fetchall()
        self.assertEqual(len(rows), 1)          # one merged row, not two
        self.assertEqual(rows[0]["calls"], 2)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(v_agents)")}
        self.assertNotIn("kind", cols)          # kind carries no signal

    def test_plugins_merge_by_name_and_drop_marketplace(self):
        self.conn.execute(
            "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
            " session_id, ts) VALUES('p','m1','skill','t','s',1)")
        self.conn.execute(
            "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
            " session_id, ts) VALUES('p','m2','skill','t','s',2)")
        self.conn.commit()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(v_plugins)")}
        self.assertNotIn("marketplace", cols)
        self.assertNotIn("version", cols)
        rows = self.conn.execute(
            "SELECT plugin, uses FROM v_plugins WHERE plugin='p'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uses"], 2)


def _queries_filtering(needle: str) -> list[str]:
    """String constants in `cbut_db.py` that look like SQL and match `needle`.

    Read out of the AST, with the module's own docstrings removed. The first version of this
    helper scraped double-quoted spans out of the raw bytes, and with `re.I` that counts the
    English word "with" as the SQL keyword `WITH` — so a docstring *describing* a query was
    extracted and reported as one, and it went red on prose the moment a docstring named a
    filtered column. A guard that fails on prose is a guard somebody deletes, so the
    docstrings come out. The `SELECT`/`WITH` requirement stays, so a UI label is still not
    mistaken for a query, and the floor below is what keeps a broken extractor from passing
    by finding nothing.
    """
    import ast
    import re as _re

    tree = ast.parse((Path(__file__).resolve().parents[1] / "cbut_db.py").read_text(
        encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    queries = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value not in docstrings
               and _re.search(r"\b(?:SELECT|WITH)\b", n.value, _re.I)]
    if len(queries) <= 40:
        raise AssertionError(f"the extractor found only {len(queries)} queries — it is "
                             "broken, not the schema")
    return [q for q in queries if _re.search(needle, q, _re.I)]


class IndexTest(unittest.TestCase):
    def test_expected_indexes_exist(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.open_db(Path(tmp.name) / "usage.db")
            db.ensure_schema(conn)
            model_idx = {r[1] for r in
                         conn.execute("PRAGMA index_list(model_responses)")}
            mcp_idx = {r[1] for r in
                       conn.execute("PRAGMA index_list(mcp_usage)")}
            conn.close()
            self.assertIn("idx_model_resp_model_ts", model_idx)
            self.assertIn("idx_mcp_tool", mcp_idx)
        finally:
            tmp.cleanup()

    def test_no_query_filters_by_session_id(self):
        """Why the two session indexes were dropped: nothing asks for them.

        A guard on the *justification*, not on the schema — if a session-keyed screen is
        ever built, this fails and the index comes back with it. Reads the real source,
        so it cannot drift into a claim about code that no longer exists.
        """
        filtering = _queries_filtering(r"session_id\s*(?:=|IN|IS|LIKE)")
        self.assertEqual(filtering, [],
                         f"{len(filtering)} query(ies) filter by session_id")

    def test_no_query_filters_by_category(self):
        """Why `idx_tool_cat` was dropped: nothing compares the column.

        A tripwire on the justification rather than a proof of the claim, and the difference
        is worth stating: this reads string constants out of the source, so a query assembled
        where it cannot see would walk past it — which is how this index survived the v6
        sweep, and why the cut was decided on the 8,339 statements the suite really executes
        (captured with `set_trace_callback`, re-planned with `EXPLAIN QUERY PLAN`: 0 of them
        name the index, against 13-703 for the ones that are kept). What it can do is go red
        the moment somebody writes the query that would justify the index coming back.
        """
        filtering = _queries_filtering(r"category\s*(?:=|IN|IS|LIKE)")
        self.assertEqual(filtering, [],
                         f"{len(filtering)} query(ies) filter by category")

    def test_a_new_database_carries_no_session_indexes(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.open_db(Path(tmp.name) / "usage.db")
            db.ensure_schema(conn)
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            conn.close()
        finally:
            tmp.cleanup()
        self.assertNotIn("idx_tool_session", names)
        self.assertNotIn("idx_model_resp_session", names)

    def test_migrating_a_v5_database_drops_the_dead_session_indexes(self):
        """The two indexes are removed on upgrade, and every other one survives."""
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "usage.db"
            conn = db.open_db(path)
            db.ensure_schema(conn)
            # Re-create the v5 shape by hand: the dead indexes plus a real one,
            # and a version row that says this database predates the drop.
            conn.executescript(
                "CREATE INDEX idx_tool_session ON tool_calls(session_id);"
                "CREATE INDEX idx_model_resp_session ON model_responses(session_id);"
                "UPDATE meta SET value='5' WHERE key='schema_version';")
            conn.commit()
            before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_tool_session", before, "fixture did not build")

            status = db.ensure_schema(conn)
            after = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            conn.close()
        finally:
            tmp.cleanup()

        self.assertNotIn("idx_tool_session", after)
        self.assertNotIn("idx_model_resp_session", after)
        self.assertIn("idx_tool_name", after, "the drop took a live index with it")
        self.assertIn("idx_model_resp_model_ts", after)
        self.assertEqual(status, "migrated")
        self.assertEqual(version, str(db.SCHEMA_VERSION))

    def test_the_category_index_is_neither_declared_nor_present(self):
        """The assertion that catches a re-add has to read the declaration, not the result.

        This test was written the other way first — assert `idx_tool_cat` is absent from a
        fresh database — and the negative check proved it empty: putting the `CREATE INDEX`
        line back into `TABLES_SQL` left it green, because `SCHEMA_SQL` creates the index
        and `_drop_dead_category_index` drops it again in the same open. The database that
        comes out is indistinguishable from the cut one, so a name-based check cannot tell
        the two apart and no longer guards the thing that must not return. The declaration
        can, so here it is: if an index on `tool_calls(category)` is declared again, this
        fails, and `test_no_query_filters_by_category` is the check that should have had to
        change first.
        """
        import re as _re
        declared = _re.search(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*ON\s+tool_calls\s*\(\s*category\s*\)",
            db.TABLES_SQL, _re.I)
        self.assertIsNone(
            declared,
            "an index on tool_calls(category) is declared again: "
            f"{declared.group(0).strip() if declared else ''}")
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.open_db(Path(tmp.name) / "usage.db")
            db.ensure_schema(conn)
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            conn.close()
        finally:
            tmp.cleanup()
        self.assertNotIn("idx_tool_cat", names)
        self.assertIn("idx_tool_name", names, "the schema lost a live index instead")

    def test_migrating_a_v7_database_drops_the_dead_category_index(self):
        """The category index goes on upgrade, and every other one survives."""
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "usage.db"
            conn = db.open_db(path)
            db.ensure_schema(conn)
            # Re-create the v7 shape by hand: the dead index plus the live ones it
            # sits beside, and a version row that says this database predates the drop.
            conn.executescript(
                "CREATE INDEX idx_tool_cat ON tool_calls(category);"
                "UPDATE meta SET value='7' WHERE key='schema_version';")
            conn.commit()
            before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_tool_cat", before, "fixture did not build")

            status = db.ensure_schema(conn)
            after = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            conn.close()
        finally:
            tmp.cleanup()

        self.assertNotIn("idx_tool_cat", after)
        self.assertIn("idx_tool_name", after, "the drop took a live index with it")
        self.assertIn("idx_tool_ts", after)
        self.assertEqual(status, "migrated")
        self.assertEqual(version, str(db.SCHEMA_VERSION))


class OldDatabaseOnAReadOnlyCliTest(unittest.TestCase):
    """`cbut stats` cannot repair the schema, so it must name the fix."""

    def test_an_older_database_reports_a_command_instead_of_a_traceback(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "old.db"
            conn = db.open_db(p)
            db.ensure_schema(conn)
            # Simulate a database written before the table existed.
            conn.execute("DROP TABLE unparsed")
            conn.commit()
            conn.close()

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = stats.main(["--db", str(p), "stats"])
            text = err.getvalue()
            self.assertEqual(rc, 2, f"expected a refusal, got rc={rc}")
            self.assertIn("run: cbut sync", text)
            self.assertNotIn("Traceback", text,
                             "a CLI that cannot migrate must not dump a stack")
        finally:
            tmp.cleanup()

    def test_a_current_database_is_unaffected_by_that_handler(self):
        # The handler above must not swallow genuine queries: prove the same
        # command still succeeds on a database that has the table.
        tmp = tempfile.TemporaryDirectory()
        try:
            p = Path(tmp.name) / "new.db"
            conn = db.open_db(p)
            db.ensure_schema(conn)
            conn.execute("INSERT INTO unparsed(reason, count) VALUES('x', 2)")
            conn.commit()
            conn.close()
            out = io.StringIO()
            # `health` stats and globs the host dir; point it at a temp path so the test
            # never reads the real ~/.codebuddy (the tripwire refuses that).
            old_host = db.CODEBUDDY_DIR
            db.CODEBUDDY_DIR = Path(tmp.name) / "no-such-codebuddy"
            try:
                with contextlib.redirect_stdout(out):
                    rc = stats.main(["--db", str(p), "health"])
            finally:
                db.CODEBUDDY_DIR = old_host
            self.assertEqual(rc, 0)
            self.assertIn("unparsed", out.getvalue().lower())
            self.assertIn("2 records", out.getvalue())
        finally:
            tmp.cleanup()


class WindowLabelTest(unittest.TestCase):
    """A window value renders as a preset's name or a custom span."""

    def test_preset_uses_its_label(self):
        self.assertEqual(db.window_label("7d"), "7 days")
        self.assertEqual(db.window_label("all"), "All time")

    def test_custom_follow_now_reads_as_now(self):
        start = int(datetime(2026, 10, 5, 9, 0).timestamp() * 1000)
        self.assertEqual(db.window_label(db.CustomWindow(start, None, True)),
                         "10-05 09:00 → now")
        self.assertEqual(db.window_label(db.CustomWindow(None, None, True)),
                         "all → now")

    def test_custom_fixed_span_shows_both_ends(self):
        start = int(datetime(2026, 10, 5, 9, 0).timestamp() * 1000)
        end = int(datetime(2026, 10, 6, 18, 30).timestamp() * 1000)
        self.assertEqual(db.window_label(db.CustomWindow(start, end, False)),
                         "10-05 09:00 → 10-06 18:30")

    def test_the_14d_preset_exists_for_the_picker(self):
        self.assertEqual(db.USAGE_RANGES["14d"], 14)
        self.assertEqual(db.USAGE_RANGE_LABELS["14d"], "14 days")


class TypedDateTest(unittest.TestCase):
    """A typed date is normalised, so "10-01" and "10-1" are one day."""

    def test_leading_zeros_and_separators_do_not_matter(self):
        for text in ("10-01", "10-1", "10/1", "10.01", "2026-10-01",
                     "2026/10/1", "10-01-2026", "10/1/26"):
            with self.subTest(text=text):
                self.assertEqual(db.parse_date(text, 2026).date(),
                                 date(2026, 10, 1))

    def test_the_default_year_fills_a_yearless_date(self):
        self.assertEqual(db.parse_date("3-9", 1999).date(), date(1999, 3, 9))

    def test_a_four_digit_leading_field_is_the_year(self):
        # 2026 is read as the year, not as a month or a day
        self.assertEqual(db.parse_date("2026-10-01", 1999).date(),
                         date(2026, 10, 1))

    def test_an_impossible_or_empty_date_is_an_error(self):
        for text in ("13-01", "2026-02-30", "not-a-date", "", "   "):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    db.parse_date(text, 2026)

    def test_parse_datetime_pairs_a_date_and_a_time(self):
        ms = db.parse_datetime("10-1", "9:5 AM", 2026)
        self.assertEqual(
            datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M"),
            "2026-10-01 09:05")
        # a blank date means "all time", which is not the same as a bad one
        self.assertIsNone(db.parse_datetime("", "", 2026))
        # a blank time means midnight
        self.assertEqual(
            datetime.fromtimestamp(
                db.parse_datetime("10-1", "", 2026) / 1000).strftime("%H:%M"),
            "00:00")


class TypedTimeTest(unittest.TestCase):
    """A typed time is normalised, and 12-hour is told from 24-hour."""

    def test_ampm_forms_are_understood(self):
        for text in ("9:05 AM", "9:5 AM", "09:05 am", "9:05am"):
            with self.subTest(text=text):
                self.assertEqual(db.parse_time(text), (9, 5))
        self.assertEqual(db.parse_time("9:05 PM"), (21, 5))

    def test_the_midnight_and_noon_edges(self):
        self.assertEqual(db.parse_time("12:00 AM"), (0, 0))
        self.assertEqual(db.parse_time("12:00 PM"), (12, 0))

    def test_a_markerless_time_is_24_hour(self):
        self.assertEqual(db.parse_time("21:05"), (21, 5))
        self.assertEqual(db.parse_time("00:00"), (0, 0))

    def test_an_impossible_or_empty_time_is_an_error(self):
        for text in ("24:00", "13:00 PM", "not-a-time", "", "   "):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    db.parse_time(text)

    def test_the_display_round_trips(self):
        for hour, minute in ((0, 0), (9, 5), (12, 0), (23, 59)):
            with self.subTest(hour=hour, minute=minute):
                shown = db.format_time(hour, minute)
                self.assertEqual(db.parse_time(shown), (hour, minute))
        # The picker shows a 24-hour clock, the same one the tables print.
        self.assertEqual(db.format_time(0, 0), "00:00")
        self.assertEqual(db.format_time(22, 30), "22:30")
        self.assertEqual(db.format_date(datetime(2026, 10, 1)), "10/01/2026")

    def test_a_12_hour_time_is_still_accepted_on_input(self):
        # The *display* moved to 24-hour; an am/pm marker typed by habit still
        # has to work, so "12:00 AM" is midnight and not a refusal.
        self.assertEqual(db.parse_time("12:00 AM"), (0, 0))
        self.assertEqual(db.format_time(*db.parse_time("12:00 AM")), "00:00")



class ProviderGroupingTest(unittest.TestCase):
    """The provider column is returned and joins the grouping key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _row(self, mid, model, provider, pt=10, ct=5, ts=1000):
        self.conn.execute(
            "INSERT INTO model_responses(message_id, model, provider,"
            " prompt_tokens, completion_tokens, ts, usage_available)"
            " VALUES(?,?,?,?,?,?,1)",
            (mid, model, provider, pt, ct, ts))

    def test_q_model_tokens_splits_a_model_across_providers(self):
        self._row("a", "shared-name", "builtin")
        self._row("b", "shared-name", "Volcano Ark")
        self.conn.commit()
        rows = db.q_model_tokens(self.conn)
        by_provider = {r["provider"]: r for r in rows}
        self.assertEqual(set(by_provider), {"builtin", "Volcano Ark"})
        self.assertEqual(by_provider["builtin"]["prompt_tokens"], 10)
        self.assertEqual(by_provider["Volcano Ark"]["prompt_tokens"], 10)

    def test_q_usage_model_stats_carries_and_groups_by_provider(self):
        self._row("a", "shared-name", "builtin")
        self._row("b", "shared-name", None)
        self.conn.commit()
        rows = db.q_usage_model_stats(self.conn, 0, 10 ** 15)
        self.assertEqual({r["provider"] for r in rows}, {"builtin", None})
        self.assertTrue(all(r["requests"] == 1 for r in rows))

    def test_q_usage_request_logs_carries_provider(self):
        self._row("a", "m", "Volcano Ark")
        self.conn.commit()
        row = db.q_usage_request_logs(self.conn, 0, 10 ** 15)[0]
        self.assertEqual(row["provider"], "Volcano Ark")


class DrilldownQueryTest(unittest.TestCase):
    """The windowed drill-down queries: q_history (+call_id), the per-entity
    summaries, and q_call_detail (one call's full row)."""

    NOW = 1_000_000_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "d.db"
        self.conn = db.open_db(self.p)
        db.ensure_schema(self.conn)
        # Two Bash calls inside a 24h window, one 100h back, one Read call.
        self._tool("c1", "Bash", "completed", self.NOW - 3 * HOUR, 120, "s1", "/p/a")
        self._tool("c2", "Bash", None, self.NOW - 2 * HOUR, 80, "s2", "/p/b")
        self._tool("c3", "Bash", "completed", self.NOW - 100 * HOUR, 999, "s1", "/p/a")
        self._tool("c4", "Read", "incomplete", self.NOW - 1 * HOUR, 10, "s1", "/p/a")
        self.conn.execute(
            "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
            " session_id, project, ts) VALUES(?,?,?,?,?,?,?)",
            ("myplug", "mp", "skill", "s/t", "s1", "/p/a", self.NOW - HOUR))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _tool(self, cid, name, status, ts, dur, sess, proj):
        self.conn.execute(
            "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
            " category, ts, duration_ms, status, model)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, sess, proj, name, "builtin", ts, dur, status, "m"))

    def _model(self, mid, model, pt, ct, ts, ua=1):
        self.conn.execute(
            "INSERT INTO model_responses(message_id, model, prompt_tokens,"
            " completion_tokens, ts, usage_available) VALUES(?,?,?,?,?,?)",
            (mid, model, pt, ct, ts, ua))

    def _window(self):
        return self.NOW - 24 * HOUR, self.NOW

    def test_q_history_carries_the_call_id(self):
        rows = db.q_history(self.conn, "tool", "Bash")
        self.assertEqual([r["call_id"] for r in rows], ["c2", "c1", "c3"])

    def test_q_history_window_excludes_older_calls(self):
        start, end = self._window()
        rows = db.q_history(self.conn, "tool", "Bash", start_ts=start, end_ts=end)
        self.assertEqual([r["call_id"] for r in rows], ["c2", "c1"])

    def test_q_entity_summary_counts_and_window(self):
        alltime = dict(db.q_entity_summary(self.conn, "tool", "Bash"))
        self.assertEqual(alltime["calls"], 3)
        self.assertEqual(alltime["completed"], 2)
        self.assertEqual(alltime["pending"], 1)
        self.assertEqual(alltime["projects"], 2)
        start, end = self._window()
        win = dict(db.q_entity_summary(self.conn, "tool", "Bash",
                                       start_ts=start, end_ts=end))
        self.assertEqual(win["calls"], 2)
        self.assertEqual(win["avg_ms"], 100)
        self.assertEqual(win["first_used"], self.NOW - 3 * HOUR)

    def test_q_entity_summary_of_an_unused_name_is_zero_not_null(self):
        # "0 of 0 calls completed" is a fact; the average and stamps stay NULL.
        s = dict(db.q_entity_summary(self.conn, "tool", "NeverUsed"))
        self.assertEqual((s["calls"], s["completed"], s["failed"]), (0, 0, 0))
        self.assertIsNone(s["avg_ms"])
        self.assertIsNone(s["last_used"])

    def test_q_plugin_summary_and_history(self):
        s = dict(db.q_plugin_summary(self.conn, "myplug"))
        self.assertEqual((s["uses"], s["kinds"], s["targets"]), (1, 1, 1))
        rows = db.q_plugin_history(self.conn, "myplug")
        self.assertEqual([r["id"] for r in rows], [1])
        self.assertEqual(rows[0]["marketplace"], "mp")
        self.assertEqual(rows[0]["kind"], "skill")

    def test_q_model_summary_windows_and_uses_the_api_total(self):
        self._model("in", "m", 10, 2, self.NOW - HOUR)
        self._model("out", "m", 20, 4, self.NOW - 100 * HOUR)
        self.conn.commit()
        start, end = self._window()
        s = dict(db.q_model_summary(self.conn, "m", start_ts=start, end_ts=end))
        self.assertEqual(s["responses"], 1)
        self.assertEqual(s["total_tokens"], 12)      # prompt + completion

    def test_q_model_responses_window_excludes_older_rows(self):
        self._model("in", "m", 10, 2, self.NOW - HOUR)
        self._model("out", "m", 20, 4, self.NOW - 100 * HOUR)
        self.conn.commit()
        start, end = self._window()
        rows = db.q_model_responses(self.conn, model="m",
                                    start_ts=start, end_ts=end)
        self.assertEqual([r["message_id"] for r in rows], ["in"])

    def test_q_call_detail_returns_every_field(self):
        row = db.q_call_detail(self.conn, "tool", "c1")
        self.assertEqual(row["call_id"], "c1")
        self.assertEqual(row["tool_name"], "Bash")
        self.assertEqual(row["category"], "builtin")
        self.assertEqual(row["model"], "m")
        self.assertEqual(row["duration_ms"], 120)

    def test_q_call_detail_unknown_key_is_none(self):
        self.assertIsNone(db.q_call_detail(self.conn, "tool", "nope"))
        self.assertIsNone(db.q_call_detail(self.conn, "model", "nope"))
        self.assertIsNone(db.q_call_detail(self.conn, "plugin", 999))

    def test_q_call_detail_model_carries_the_derived_total(self):
        self._model("mid1", "m", 10, 2, self.NOW)
        self.conn.commit()
        row = db.q_call_detail(self.conn, "model", "mid1")
        self.assertEqual(row["total_tokens"], 12)
        self.assertEqual(row["total_tokens_source"], "derived")


if __name__ == "__main__":
    unittest.main()

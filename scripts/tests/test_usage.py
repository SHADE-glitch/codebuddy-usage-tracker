"""Tests for the Usage Statistics queries (cbut_db). Standard library only.

Run:  python3 -m unittest discover -s scripts/tests
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

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

            self.assertTrue(db.migrate(p))
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
            self.assertTrue(db.migrate(p))
            self.assertTrue(db.migrate(p))
        finally:
            tmp.cleanup()


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


class IndexTest(unittest.TestCase):
    def test_expected_indexes_exist(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.open_db(Path(tmp.name) / "i.db")
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


if __name__ == "__main__":
    unittest.main()

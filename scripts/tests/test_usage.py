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
from datetime import datetime, timedelta
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
        import re as _re
        src = (Path(__file__).resolve().parents[1] / "cbut_db.py").read_text(
            encoding="utf-8")
        queries = _re.findall(
            r'"((?:[^"]|\\.)*?(?:SELECT|WITH)[^"]*)"', src, _re.I)
        self.assertGreater(len(queries), 50,
                           "the extractor found almost no queries — it is broken, "
                           "not the schema")
        filtering = [q for q in queries
                     if _re.search(r"session_id\s*(?:=|IN|IS|LIKE)", q, _re.I)]
        self.assertEqual(filtering, [],
                         f"{len(filtering)} query(ies) filter by session_id")

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
            with contextlib.redirect_stdout(out):
                rc = stats.main(["--db", str(p), "health"])
            self.assertEqual(rc, 0)
            self.assertIn("unparsed", out.getvalue().lower())
            self.assertIn("2 records", out.getvalue())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

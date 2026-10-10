"""Tests for cbut-tui (the interactive Textual viewer).

Standard library only (unittest + Textual's built-in run_test harness).
Run:  python3 -m unittest discover -s scripts/tests
"""

import ast
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_tui", SCRIPTS / "cbut-tui.py")
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)
TrackerApp = tui.TrackerApp

_stats_spec = importlib.util.spec_from_file_location("cbut_stats", SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_stats_spec)
_stats_spec.loader.exec_module(stats)


# --- fixtures ---------------------------------------------------------------

def write_records(path: Path, recs) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def model_rec(message_id, raw=None, rtype="function_call",
              model="test-model", session_id="s1", cwd="/proj", ts=1000,
              conv="conv-1"):
    rec = {
        "type": rtype,
        "id": f"{message_id}-rec",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": ts,
        "providerData": {
            "conversationRequestId": conv,
            "messageId": message_id,
            "model": model,
        },
    }
    if raw is not None:
        rec["providerData"]["rawUsage"] = raw
    return rec


def make_db(path, responses, session_token=12345, cache=None, ptotals=None):
    """Build a tracker DB with model_responses rows (and a tokenDelta session).

    ``responses`` rows: (mid, sid, model, prompt, completion, cache_r, cache_w,
    ts, usage). ``cache`` optionally maps mid -> (hit, miss, write) for the v3
    ``prompt_cache_*`` columns (defaults to NULL). ``ptotals`` optionally maps
    mid -> provider_total_tokens (defaults to NULL).
    """
    cache = cache or {}
    ptotals = ptotals or {}
    conn = db.open_db(path)
    db.ensure_schema(conn)
    if session_token is not None:
        # sessions.tokens is the turn-metrics tokenDelta — must NEVER appear in
        # the per-model token totals.
        conn.execute(
            "INSERT INTO sessions(session_id, tokens) VALUES(?, ?)",
            ("s1", session_token),
        )
    for (mid, sid, model, pt, ct, cr, cw, ts, ua) in responses:
        hit, miss, write = cache.get(mid, (None, None, None))
        ptotal = ptotals.get(mid)
        conn.execute(
            "INSERT INTO model_responses"
            "(message_id, session_id, model, prompt_tokens, completion_tokens,"
            " cache_read_input_tokens, cache_creation_input_tokens,"
            " prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
            " prompt_cache_write_tokens, provider_total_tokens, ts,"
            " usage_available)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, sid, model, pt, ct, cr, cw, hit, miss, write, ptotal, ts, ua),
        )
    conn.commit()
    conn.close()


def all_rows(table):
    return [table.get_row_at(i) for i in range(table.row_count)]


# Textual's default test terminal is 80x24, and below the width where a table's
# columns fit, the column plan drops the tail (see test 58). A case that reads
# cells by position therefore has to ask for a terminal wide enough to show all
# of them, otherwise it asserts against a table that was never meant to be full.
WIDE = (160, 40)


def insert_tool_calls(path, rows):
    """rows: (call_id, session_id, project, tool_name, category, ts,
    duration_ms, status)."""
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
        " category, ts, duration_ms, status) VALUES(?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    conn.close()


def insert_mcp_usage(path, rows):
    """rows: (call_id, server, tool, session_id, project, ts, status,
    duration_ms)."""
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO mcp_usage(call_id, server, tool, session_id, project, ts,"
        " status, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    conn.close()


# --- tests ------------------------------------------------------------------

class TuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "u.db"

    def tearDown(self):
        self.tmp.cleanup()

    async def _usage_all_time(self, app, pilot):
        """Open the Usage tab and widen it to all-time.

        Usage defaults to Today (a calendar-day window), so a fixture row at
        "now - 1h" would fall outside it when the suite runs just after local
        midnight. All-time removes that time-of-day dependency.
        """
        app.query_one(tui.TabbedContent).active = "tab-usage"
        await pilot.pause()
        app.query_one("#range", tui.Select).value = "all"
        await pilot.pause()

    def _noon_ms(self) -> int:
        """Local noon today, in epoch ms.

        The app's calendar-day windows derive from its clock. Pinning that clock
        to noon makes a `now - 1h` fixture unambiguously inside the Today window
        (and `now - 100h` unambiguously outside) at whatever hour the suite
        actually runs — rather than holding only after 01:00 local.
        """
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        return int(noon.timestamp() * 1000)

    def _pinned_app(self, ms: int):
        """A TrackerApp whose clock is frozen at ``ms`` (see :meth:`_noon_ms`)."""
        return TrackerApp(str(self.db_path), clock=lambda: ms / 1000)

    # 1. eight tabs present (Dashboard + the entity tabs + Tokens + Usage)
    async def test_eight_tabs_present(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            self.assertEqual(tc.tab_count, 8)
            self.assertEqual(tc.tab_count, len(TrackerApp.TAB_IDS))
            self.assertEqual(TrackerApp.TAB_IDS[0], "tab-dashboard")
            self.assertEqual(tc.active, "tab-dashboard")   # Dashboard is initial

    # 2. Tokens tab lists each model
    async def test_tokens_tab_lists_models(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "a-model", 10, 20, 1, 2, now - 3600_000, 1),
            ("m2", "s1", "b-model", 5, 5, 0, 0, now - 3600_000 + 1, 1),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            models = [r[0] for r in all_rows(t)]
            self.assertIn("a-model", models)
            self.assertIn("b-model", models)

    # 3. sessions.tokens (tokenDelta) is excluded from per-model totals
    async def test_sessions_tokens_excluded_from_totals(self):
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 0, 0, 0, 1000, 1),
        ])  # sessions.tokens = 12345 is inserted by make_db
        conn = db.open_db(self.db_path)
        total = sum((r["prompt_tokens"] or 0) for r in db.q_model_tokens(conn))
        conn.close()
        self.assertEqual(total, 10)  # NOT 12355

    # 4. responses without usage show "-" not "0"
    async def test_no_usage_shows_dash_not_zero(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", None, None, None, None, now - 3600_000, 0),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[0], "m")
            self.assertEqual(row[1], "-")    # Usage Total (no API parts)
            self.assertEqual(row[2], "1")    # Requests (the row exists)
            # Input / Output / API Total / Cache columns are "-", never "0"
            for i in range(3, 9):
                self.assertEqual(row[i], "-", f"col {i} should be '-'")

    # 5. prompt / completion / total / cache shown in separate columns
    async def test_input_output_separate_columns(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 20, 3, 4, now - 3600_000, 1),
        ], cache={"m1": (7, 8, 9)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            # Order: Model · Usage Total · Requests · Input · Output ·
            #        API Total · Cache hit · Cache miss · Cache write
            self.assertEqual(row[1], "37")   # Usage Total = 10 + 20 + 7
            self.assertEqual(row[2], "1")    # Requests
            self.assertEqual(row[3], "10")   # Input  = prompt_tokens
            self.assertEqual(row[4], "20")   # Output = completion_tokens
            self.assertEqual(row[5], "30")   # API Total = Input + Output
            self.assertEqual(row[6], "7")    # Cache hit
            self.assertEqual(row[7], "8")    # Cache miss
            self.assertEqual(row[8], "9")    # Cache write

    # 5b. explicit 0 is shown as "0", absent stays "-"
    async def test_zero_vs_null_distinction(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "zeroed", 1, 1, 0, 0, now - 3600_000, 1),
            ("m2", "s1", "nulled", 1, 1, None, None, now - 3600_000 + 1, 1),
        ], cache={"m1": (0, 0, 0)})   # m2 has no cache entry -> NULL
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(rows["zeroed"][6], "0")     # real 0
            self.assertEqual(rows["zeroed"][7], "0")
            self.assertEqual(rows["zeroed"][8], "0")
            self.assertEqual(rows["nulled"][6], "-")     # NULL, not 0
            self.assertEqual(rows["nulled"][7], "-")
            self.assertEqual(rows["nulled"][8], "-")

    # 5c. Total is not inflated by cache hit/miss
    async def test_total_excludes_cache(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 20, 0, 0, now - 3600_000, 1),
        ], cache={"m1": (999999, 888888, 7)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[5], "30")           # API Total, not +cache
            self.assertEqual(row[6], "999,999")      # cache hit still shown

    # 5d. Tokens tab has exactly the expected columns
    async def test_tokens_tab_columns(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertEqual(labels, ["Model", "Usage Total", "Requests",
                                      "Input", "Output", "API Total",
                                      "Cache hit", "Cache miss", "Cache write"])

    # 6. Tab cycling wraps around, no AttributeError (the old crash)
    async def test_tab_cycle_wraps(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            self.assertIn(tc.active, TrackerApp.TAB_IDS)
            start_idx = TrackerApp.TAB_IDS.index(tc.active)
            n = len(TrackerApp.TAB_IDS)
            # Forward through every tab must wrap back to the start.
            for _ in range(n):
                await pilot.press("tab")
            self.assertEqual(tc.active, TrackerApp.TAB_IDS[start_idx])
            # shift+tab from the start wraps to the last tab (Tokens).
            await pilot.press("shift+tab")
            self.assertEqual(tc.active, TrackerApp.TAB_IDS[(start_idx - 1) % n])
            # one forward from the last wraps back to the start.
            await pilot.press("tab")
            self.assertEqual(tc.active, TrackerApp.TAB_IDS[start_idx])

    # 7. R triggers an incremental sync and refreshes the UI
    async def test_r_triggers_incremental_sync_and_refresh(self):
        calls = []
        captured = {}

        def fake_run(quiet=False, db_path=None, full=False):
            calls.append(db_path)
            captured["quiet"] = quiet
            return {"files_indexed": 3}

        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", 1, 1, 0, 0, now - 3600_000, 1),
        ])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=fake_run)
        async with app.run_test() as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(calls, [str(self.db_path)])
            self.assertTrue(captured["quiet"])
            # Tokens tab still renders after the refresh
            t = app.query_one("#t-tokens", tui.DataTable)
            self.assertTrue(any(r[0] == "m" for r in all_rows(t)))

    # 8. S toggles auto-sync only; it does not itself trigger a sync
    async def test_s_toggles_auto_sync_only(self):
        calls = []

        def fake_run(**kw):
            calls.append(1)
            return {"files_indexed": 0}

        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=fake_run)
        async with app.run_test() as pilot:
            self.assertTrue(app._auto_sync)
            await pilot.press("s")
            await pilot.pause()
            self.assertFalse(app._auto_sync)
            self.assertEqual(calls, [])  # no sync happened
            await pilot.press("s")
            self.assertTrue(app._auto_sync)
            self.assertEqual(calls, [])  # still no sync

    # 9. running the real indexer twice does not double-insert (messageId dedup)
    async def test_sync_dedup_no_double_insert(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            proj = Path(tmp.name) / "projects" / "X"
            proj.mkdir(parents=True)
            f = proj / "s.jsonl"
            write_records(f, [
                model_rec("m-dup", raw={"prompt_tokens": 1, "completion_tokens": 2},
                          model="dup-model", session_id="s1", ts=1),
            ])
            dbp = Path(tmp.name) / "u.db"
            spec = importlib.util.spec_from_file_location(
                "cbut_sync_dedup", SCRIPTS / "cbut-sync.py")
            s = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(s)
            saved = db.CODEBUDDY_DIR
            db.CODEBUDDY_DIR = Path(tmp.name)  # point the scanner at temp
            try:
                s.run(full=True, quiet=True, db_path=str(dbp))
                s.run(full=False, quiet=True, db_path=str(dbp))
            finally:
                db.CODEBUDDY_DIR = saved
            conn = db.open_db(dbp)
            cnt = conn.execute(
                "SELECT COUNT(*) FROM model_responses WHERE model='dup-model'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(cnt, 1)
        finally:
            tmp.cleanup()

    # 10. a sync failure keeps the old data and reports the error
    async def test_sync_failure_keeps_old_data(self):
        def bad_run(**kw):
            raise RuntimeError("disk on fire")

        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "good", 1, 1, 0, 0, now - 3600_000, 1),
        ])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=bad_run)
        async with app.run_test() as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = str(app.query_one("#status").content)
            self.assertIn("Sync failed", status)
            t = app.query_one("#t-tokens", tui.DataTable)
            self.assertTrue(any("good" in r for r in all_rows(t)))

    # 11. syncs never run concurrently (a guard collapses rapid requests)
    async def test_no_concurrent_sync(self):
        state = {"active": 0, "max": 0, "calls": 0}

        def fake_run(**kw):
            state["calls"] += 1
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            state["active"] -= 1
            return {"files_indexed": 0}

        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=fake_run)
        async with app.run_test() as pilot:
            app._request_sync()
            app._request_sync()  # while the first is still running
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertLessEqual(state["max"], 1)  # never two at once
            self.assertLessEqual(state["calls"], 2)  # pending collapses to <=1 extra

    # 12. fmt_n: NULL -> "-", never a fabricated 0
    def test_fmt_n(self):
        self.assertEqual(tui.fmt_n(None), "-")
        self.assertEqual(tui.fmt_n(0), "0")
        self.assertEqual(tui.fmt_n(1234567), "1,234,567")

    # 13. per-model detail screen: "not counted" + masked message id, no body
    async def test_model_detail_not_counted_and_masked(self):
        mid = "verylongfullmessageid123"
        make_db(self.db_path, [
            (mid, "s1", "m", None, None, None, None, 1000, 0),
        ])
        app = TrackerApp(str(self.db_path))
        # Twelve columns need ~133 cells, so at the default 80 the plan would drop the
        # very columns this case is about — and `assertNotIn(mid, ...)` would then pass
        # for the wrong reason (nothing shown, not masked). Ask for a terminal wide
        # enough that the masking itself is what is being tested.
        async with app.run_test(size=WIDE) as pilot:
            await app.push_screen(tui.ModelResponsesScreen("m"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            t = scr.query_one("#resp-table", tui.DataTable)
            rows = all_rows(t)
            flat = [str(c) for r in rows for c in r]
            self.assertTrue(any("not counted" in c for c in flat))
            # the raw message id must be masked, not shown in full
            self.assertNotIn(mid, flat)
            self.assertIn(mid[:8] + "…" + mid[-8:], flat)

    # 14. `cbut models` prints the new columns (Total + cache hit/miss/write)
    def test_cmd_models_output_has_new_columns(self):
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 20, 0, 0, 1000, 1),
        ], cache={"m1": (7, 8, 9)})
        saved = stats.shutil.get_terminal_size
        stats.shutil.get_terminal_size = lambda *a, **k: os.terminal_size((200, 24))
        conn = db.open_db(self.db_path, readonly=True)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                stats.cmd_models(conn, SimpleNamespace(limit=5))
        finally:
            conn.close()
            stats.shutil.get_terminal_size = saved
        out = buf.getvalue()
        for col in ("total", "cache hit", "cache miss", "cache write"):
            self.assertIn(col, out)
        self.assertIn("30", out)   # total = 10 + 20

    # 15. Usage and Tokens remain separate tabs (the Dashboard only summarizes)
    async def test_usage_tab_present(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = [p.id for p in app.query(tui.TabPane)]
            self.assertIn("tab-usage", ids)
            self.assertIn("tab-tokens", ids)

    # 16. Usage window is recomputed from now and reacts to range changes
    async def test_usage_window_and_range(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # Usage keeps its own range (default Today); the Select drives it
            # only while the Usage tab is active.
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            self.assertIn("Today", str(app.query_one("#usage-window").content))
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            self.assertIn("All time", str(app.query_one("#usage-window").content))

    # 17. Request Logs: Usage Total first after Model; cache hit rate last
    async def test_usage_logs_total_excludes_cache(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "m", 10, 20, 0, 0, now - 3600_000, 1),
        ], cache={"a": (999999, 888888, 7)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await self._usage_all_time(app, pilot)
            t = app.query_one("#t-usage", tui.DataTable)
            row = all_rows(t)[0]
            # Order: Time · Model · Usage Total · Input · Output · API Total ·
            #        Cache hit · Cache miss · Cache write · Cache hit rate
            self.assertEqual(row[1], "m")            # model
            self.assertEqual(row[2], "1,000,029")    # Usage Total = 10+20+999999
            self.assertEqual(row[3], "10")           # Input
            self.assertEqual(row[4], "20")           # Output
            self.assertEqual(row[5], "30")           # API Total, not +cache
            self.assertEqual(row[6], "999,999")      # Cache hit (read)
            self.assertEqual(row[7], "888,888")      # Cache miss
            self.assertEqual(row[8], "7")            # Cache write (create)
            # 999999 / (999999 + 888888 + 7) = 52.9%
            self.assertEqual(row[9], "52.9%")        # Cache hit rate

    # 17b. empty window shows an explicit empty state, never a blank region
    async def test_usage_empty_state(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # The message lives in the table, like the other seven tables
            # (`_fill_or_empty`); a note above an untouched table reads as a page
            # that failed to load. The note only carries column hints.
            t = app.query_one("#t-usage", tui.DataTable)
            self.assertEqual(t.row_count, 1)
            self.assertIn("No requests in this window", all_rows(t)[0][0])
            self.assertEqual(str(app.query_one("#usage-note").content), "")

    # 18. the Usage page has no view/sort controls (single Request Logs list)
    async def test_usage_has_no_view_or_sort_controls(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query("#usage-view")), 0)
            self.assertEqual(len(app.query("#usage-sort")), 0)
            # the range Select lives in the shared top bar, not the Usage pane
            self.assertTrue(app.query_one("#range", tui.Select))
            self.assertEqual(len(app.query("#tab-usage #range")), 0)

    # 19. Summary separates API Total from the display-only Usage Total
    async def test_usage_summary_api_vs_usage_total(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "m", 100, 20, 0, 0, now - 3600_000, 1),
        ], cache={"a": (80, 15, 5)}, ptotals={"a": 120})
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._usage_all_time(app, pilot)
            api = str(app.query_one("#sum-total").content)
            usage = str(app.query_one("#sum-usage-total").content)
            self.assertIn("API Total", api)
            self.assertIn("120", api)
            self.assertIn("Usage Total", usage)
            self.assertIn("200", usage)          # 100 + 20 + 80

    # 20. a v2 DB still launches (on_mount auto-migrates)
    async def test_v2_db_starts(self):
        c = sqlite3.connect(self.db_path)
        c.executescript(
            "CREATE TABLE model_responses(message_id TEXT PRIMARY KEY,"
            " session_id TEXT, conversation_request_id TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
            " ts INTEGER, project TEXT, source TEXT, usage_available INTEGER,"
            " missing TEXT);")
        c.execute("INSERT INTO model_responses(message_id, model, prompt_tokens,"
                  " completion_tokens, ts, usage_available) VALUES('o','m',5,6,?,1)",
                  (int(time.time() * 1000) - 1000,))
        c.commit()
        c.close()
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # the grouped Summary panels render after the auto-migration
            self.assertTrue(app.query_one("#usage-panels"))
            self.assertIn("Requests", str(app.query_one("#sum-requests").content))

    # 21. Usage Summary is grouped into four bordered panels
    async def test_usage_summary_panels_present(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#usage-panels"))
            for pid in ("#panel-window", "#panel-tokens", "#panel-cache",
                        "#panel-runtime"):
                self.assertTrue(app.query_one(pid))
            for wid in ("#sum-requests", "#sum-input", "#sum-total",
                        "#sum-cache-hit", "#sum-hit-rate", "#sum-usage-total",
                        "#sum-missing", "#usage-status"):
                self.assertTrue(app.query_one(wid))
            # the dropped widgets are gone
            self.assertEqual(len(app.query("#sum-with-usage")), 0)
            self.assertEqual(len(app.query("#sum-coverage")), 0)
            self.assertEqual(len(app.query("#sum-total-source")), 0)

    # 23. Tokens tab: Usage Total + Requests columns (no Coverage)
    async def test_tokens_usage_total_and_requests(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "m", 1, 1, 0, 0, now - 3600_000, 1),
            ("b", "s1", "m", None, None, None, None, now - 3600_000 + 1, 0),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[1], "2")        # Usage Total = 1 + 1
            self.assertEqual(row[2], "2")        # Requests

    # 24. hidden tabs are not re-queried by the 5s refresh
    async def test_hidden_tab_not_requeried(self):
        calls = {"n": 0}
        real = db.q_usage_summary

        def spy(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            tc.active = "tab-tools"
            await pilot.pause()
            db.q_usage_summary = spy
            try:
                calls["n"] = 0
                app._auto_refresh_tick()
                await pilot.pause()
                self.assertEqual(calls["n"], 0)     # tools tab -> no usage query
                tc.active = "tab-usage"
                await pilot.pause()
                calls["n"] = 0
                app._auto_refresh_tick()
                await pilot.pause()
                self.assertGreater(calls["n"], 0)   # usage tab -> queried
            finally:
                db.q_usage_summary = real

    # 25. switching to a tab refreshes that tab's data
    async def test_tab_activation_refreshes_that_tab(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "alpha", 1, 1, 0, 0, now - 3600_000, 1),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tokens"
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            self.assertTrue(any(r[0] == "alpha" for r in all_rows(t)))

    # 26. a full refresh opens exactly one readonly connection
    async def test_single_connection_per_full_refresh(self):
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, 1000, 1)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            opens = {"n": 0}
            real = db.open_db

            def counting(*a, **k):
                opens["n"] += 1
                return real(*a, **k)

            db.open_db = counting
            try:
                opens["n"] = 0
                app.refresh_data()
                self.assertEqual(opens["n"], 1)
            finally:
                db.open_db = real

    # 27. a no-op sync does not trigger a full refresh
    async def test_sync_no_files_skips_full_refresh(self):
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, 1000, 1)])
        app = TrackerApp(str(self.db_path))
        calls = {"full": 0}
        async with app.run_test() as pilot:
            await pilot.pause()
            real_full = app.refresh_data

            def spy_full(*a, **k):
                calls["full"] += 1
                return real_full(*a, **k)

            app.refresh_data = spy_full
            app._on_sync_done(True, {"files_indexed": 0}, None)
            await pilot.pause()
            self.assertEqual(calls["full"], 0)

    # 28. _request_sync no longer carries the dead `force` parameter
    def test_request_sync_has_no_force_param(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        self.assertNotIn("force", inspect.signature(app._request_sync).parameters)

    # 28b. the refresh tick must not collide with Textual's DOMNode._auto_refresh
    # instance attribute (which would shadow the method and break the timer)
    def test_auto_refresh_tick_is_callable(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        self.assertTrue(callable(app._auto_refresh_tick))

    # 28c. a refresh tick that fires while the app is not running (e.g. once
    # more during run_test teardown) must be a no-op. Otherwise query_one on the
    # now-unmounted widgets raises NoMatches from inside the timer and fails the
    # whole test run — the flake CI caught on test_tab_cycle_wraps.
    async def test_auto_refresh_tick_is_noop_when_not_running(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-dashboard"
            await pilot.pause()
            ran = {"n": 0}
            real = app._refresh_active_tab

            def spy(*a, **k):
                ran["n"] += 1
                return real(*a, **k)

            app._refresh_active_tab = spy
            # is_running is True inside run_test, so the tick still refreshes...
            app._auto_refresh_tick()
            self.assertEqual(ran["n"], 1)

        # ...but once the app has stopped it must return before touching the DOM
        # (query_one would raise NoMatches here).
        self.assertFalse(app.is_running)
        app._auto_refresh_tick()
        self.assertEqual(ran["n"], 1)

    # 28d. the auto-sync timer has the same teardown window as the refresh one,
    # and D-003 only guarded the refresh side. Without the guard _request_sync
    # reaches run_worker on an app that has already stopped: "RuntimeError: no
    # running event loop", plus a coroutine that is never awaited.
    async def test_auto_sync_tick_is_noop_when_not_running(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        requested = []
        async with app.run_test() as pilot:
            await pilot.pause()
            # Stubbed, not wrapped: the real _request_sync starts a worker that
            # would scan this machine's ~/.codebuddy from inside a test.
            app._request_sync = lambda *a, **k: requested.append(1)
            app._auto_sync_tick()
            self.assertEqual(len(requested), 1,
                             "the tick must still sync while the app is running")

        self.assertFalse(app.is_running)
        app._auto_sync_tick()                       # must not raise
        self.assertEqual(len(requested), 1,
                         "no sync may be queued after the app has stopped")

    # 28e. and toggling auto-sync off must stop the ticks, not just the label
    async def test_auto_sync_tick_honours_the_switch(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        requested = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._request_sync = lambda *a, **k: requested.append(1)
            app.action_toggle_auto_sync()
            app._auto_sync_tick()
            self.assertEqual(requested, [],
                             "auto-sync is off; the tick must not sync")
            app.action_toggle_auto_sync()
            app._auto_sync_tick()
            self.assertEqual(len(requested), 1)

    # 29. narrow terminals switch the panels to a compact (stacked) layout
    async def test_usage_summary_compact_fallback(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            panels = app.query_one("#usage-panels")
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            self.assertTrue(panels.has_class("compact"))
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            self.assertFalse(panels.has_class("compact"))

    # 30. the Request Logs table stays visible (and unclipped) at every size
    async def test_usage_table_visible_at_all_sizes(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            (f"m{i}", "s1", "m", 100, 20, 0, 0, now - 3600_000 * i, 1)
            for i in range(30)
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._usage_all_time(app, pilot)
            box = app.query_one("#usage-box")
            tbl = app.query_one("#t-usage", tui.DataTable)
            for (w, h) in ((80, 24), (100, 30), (120, 40), (140, 45)):
                await pilot.resize_terminal(w, h)
                await pilot.pause()
                await pilot.pause()
                # >= 6 data rows + header. size.height alone can pass while the
                # widget is clipped below the pane, so also assert the region.
                self.assertGreaterEqual(
                    tbl.size.height, 7, f"{w}x{h}: table too short")
                self.assertLessEqual(
                    tbl.region.bottom, box.region.bottom,
                    f"{w}x{h}: table clipped out of the pane")
                self.assertGreater(tbl.row_count, 0)

    # 31. MCP row select queries history by TOOL (q_history maps mcp -> tool)
    async def test_mcp_row_select_shows_history(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_mcp_usage(self.db_path, [
            ("c1", "srv", "toolA", "s1", "/p", now - 7200_000, "completed", 5),
            ("c2", "srv", "toolA", "s1", "/p", now - 3600_000, "completed", 6),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-mcp"
            await pilot.pause()
            t = app.query_one("#t-mcp", tui.DataTable)
            self.assertEqual(t.row_count, 1)
            t.move_cursor(row=0)
            await pilot.pause()
            key = list(t.rows.keys())[0]
            # exercise the real routing handler (same message a click posts)
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, t.cursor_row, key))
            await pilot.pause()
            await pilot.pause()
            scr = app.screen_stack[-1]
            self.assertIsInstance(scr, tui.HistoryScreen)
            self.assertEqual(scr.entity, "toolA")     # queried by the TOOL
            self.assertIn("srv", scr.heading)         # title shows server · tool
            ht = scr.query_one("#hist-table", tui.DataTable)
            self.assertEqual(ht.row_count, 2)         # history is non-empty

    # 32. two tools under one server: unique row keys, no DuplicateKey crash
    async def test_mcp_two_tools_one_server_no_crash(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_mcp_usage(self.db_path, [
            ("c1", "srv", "toolA", "s1", "/p", now - 7200_000, "completed", 5),
            ("c2", "srv", "toolB", "s1", "/p", now - 3600_000, "completed", 6),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-mcp"
            await pilot.pause()
            t = app.query_one("#t-mcp", tui.DataTable)
            self.assertEqual(t.row_count, 2)
            self.assertEqual(sorted(r[1] for r in all_rows(t)),
                             ["toolA", "toolB"])
            self.assertEqual(len(set(t.rows.keys())), 2)   # keys are unique

    # 33. every empty tab shows an explicit empty-state row (never blank)
    async def test_empty_tabs_show_explicit_empty_state(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            for tid, msg in (
                ("#t-tools", "No tool calls yet"),
                ("#t-skills", "No skills used yet"),
                ("#t-agents", "No agents used yet"),
                ("#t-plugins", "No plugins installed"),
                ("#t-mcp", "No MCP calls yet"),
                ("#t-tokens", "No model responses yet"),
            ):
                t = app.query_one(tid, tui.DataTable)
                self.assertEqual(t.row_count, 1, tid)
                self.assertIn(msg, str(all_rows(t)[0][0]), tid)

    # 34. count columns use thousands separators (Tools + MCP)
    async def test_counts_thousands_separated(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            (f"t{i}", "s1", "/p", "BigTool", "builtin", now - 3600_000 + i, 10,
             "completed") for i in range(1234)
        ])
        insert_mcp_usage(self.db_path, [
            (f"m{i}", "srv", "big", "s1", "/p", now - 3600_000 + i, "completed", 1)
            for i in range(1234)
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tools = all_rows(app.query_one("#t-tools", tui.DataTable))[0]
            self.assertEqual(tools[1], "1,234")     # calls
            self.assertEqual(tools[2], "1,234")     # completed
            mcp = all_rows(app.query_one("#t-mcp", tui.DataTable))[0]
            self.assertEqual(mcp[2], "1,234")       # calls

    # 35. a NULL cell renders "-" (matching fmt_n), never a blank string
    async def test_none_cell_renders_dash(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.execute(
            "INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
            " category, ts, duration_ms, status)"
            " VALUES('c1','s1','/p','toolX','builtin',?,NULL,'completed')",
            (now - 3600_000,))
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[0], "toolX")
            self.assertEqual(row[4], "-")   # NULL avg, not ""

    # 36. cursor is restored by ROW KEY, so a re-sort keeps the same entity
    async def test_cursor_restored_by_row_key(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            (f"a{i}", "s1", "/p", "ToolA", "builtin", now - 3600_000 + i, 10,
             "completed") for i in range(3)
        ] + [("b0", "s1", "/p", "ToolB", "builtin", now - 3600_000, 10,
              "completed")])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            # calls DESC -> [ToolA(3), ToolB(1)]; highlight ToolB at row 1
            self.assertEqual(all_rows(t)[1][0], "ToolB")
            t.move_cursor(row=1)
            await pilot.pause()
            # give ToolB more calls so it sorts to row 0
            insert_tool_calls(self.db_path, [
                (f"c{i}", "s1", "/p", "ToolB", "builtin", now - 1800_000 + i, 10,
                 "completed") for i in range(5)
            ])
            app.refresh_data()
            await pilot.pause()
            self.assertEqual(all_rows(t)[0][0], "ToolB")   # re-sorted
            self.assertEqual(t.cursor_row, 0)              # cursor followed ToolB

    # 37. the shared range Select filters a non-Usage tab (Tools), default 7d
    async def test_range_filters_tools_tab(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("recent", "s1", "/p", "Recent", "builtin", now - 3600_000, 10,
             "completed"),
            ("old", "s1", "/p", "Old", "builtin", now - 200 * 3600_000, 10,
             "completed"),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # The Dashboard is the initial tab; switch to Tools so the shared
            # Select drives the Tools window.
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            # Default entity range is 7d. Name is the key, so both tools are
            # listed; Old's calls (200h ago) fall outside 7d and count 0.
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(set(rows), {"Recent", "Old"})
            self.assertEqual(rows["Recent"][1], "1")
            self.assertEqual(rows["Old"][1], "0")
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(rows["Old"][1], "1")    # now counted

    # 37b. every tab keeps its OWN range — changing one never moves another
    async def test_tab_ranges_are_independent(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            sel = app.query_one("#range", tui.Select)
            self.assertEqual(sel.value, "1d")        # Dashboard default
            self.assertEqual(app.tab_range["tab-dashboard"], "1d")
            tc.active = "tab-tools"
            await pilot.pause()
            self.assertEqual(sel.value, "7d")        # Tools default
            sel.value = "all"                        # widen only Tools
            await pilot.pause()
            self.assertEqual(app.tab_range["tab-tools"], "all")
            self.assertEqual(app.tab_range["tab-dashboard"], "1d")  # untouched
            tc.active = "tab-skills"                 # Skills keeps its own default
            await pilot.pause()
            self.assertEqual(sel.value, "7d")
            self.assertEqual(app.tab_range["tab-skills"], "7d")
            tc.active = "tab-usage"                  # Usage keeps its own default
            await pilot.pause()
            self.assertEqual(sel.value, "1d")
            self.assertEqual(app.tab_range["tab-usage"], "1d")
            tc.active = "tab-tools"                  # Tools remembers its widening
            await pilot.pause()
            self.assertEqual(sel.value, "all")

    # 38. the shared top bar replaces the built-in Header; palette is disabled
    async def test_top_bar_replaces_header_and_disables_palette(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#topbar"))
            self.assertTrue(app.query_one("#range", tui.Select))
            self.assertEqual(len(app.query("Header")), 0)
            self.assertIs(TrackerApp.ENABLE_COMMAND_PALETTE, False)
            # the range Select lives in the top bar, not inside the Usage pane
            self.assertEqual(len(app.query("#tab-usage #range")), 0)

    # 39. Plugins is all-time, keyed by name, no version column, no range Select
    async def test_plugins_all_time_no_version_no_range(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.execute(
            "INSERT INTO inventory(kind, name, owner_plugin, version, path,"
            " source) VALUES('plugin','myplug',NULL,'1.0','/x','user')")
        conn.execute(
            "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
            " session_id, project, ts) VALUES('myplug','mkt','skill','t','s',"
            "'/p',?)", (now - 100 * 3600_000,))
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-plugins"
            await pilot.pause()
            t = app.query_one("#t-plugins", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertNotIn("version", labels)
            row = all_rows(t)[0]
            self.assertEqual(row[0], "myplug")   # still listed
            self.assertEqual(row[1], "1")        # all-time use (no window)
            # the range Select is hidden while the Plugins tab is active
            self.assertFalse(app.query_one("#range", tui.Select).display)

    # 39b. the Skills tab has no plugin column (name is the key)
    async def test_skills_tab_has_no_plugin_column(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-skills", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertNotIn("plugin", labels)

    # 40. Dashboard: a grid of panel rows, no table, all expected widgets
    async def test_dashboard_layout(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            for rid in ("#dash-row-1", "#dash-row-2", "#dash-row-3"):
                self.assertTrue(app.query_one(rid))
            for pid in ("#panel-dash-kpi", "#panel-dash-tokens",
                        "#panel-dash-cache", "#panel-dash-models",
                        "#panel-dash-tools", "#panel-dash-runtime",
                        "#panel-dash-activity"):
                self.assertTrue(app.query_one(pid))
            for wid in ("#dash-kpi-tools", "#dash-kpi-skills",
                        "#dash-kpi-agents", "#dash-kpi-mcp", "#dash-kpi-plugins",
                        "#dash-tok-requests", "#dash-tok-input",
                        "#dash-tok-output", "#dash-tok-api", "#dash-tok-usage",
                        "#dash-tok-hit-rate", "#dash-cache-hit",
                        "#dash-cache-miss", "#dash-cache-write",
                        "#dash-rt-completed", "#dash-rt-incomplete",
                        "#dash-rt-avg", "#dash-rt-sessions", "#dash-rt-projects",
                        "#dash-models", "#dash-tools",
                        "#dash-activity", "#dash-activity-axis", "#dash-note"):
                self.assertTrue(app.query_one(wid))
            # the leaderboards and the trend are text, not tables
            self.assertEqual(len(app.query("#tab-dashboard DataTable")), 0)

    # 40b. Dashboard has its own range, defaulting to Today
    async def test_dashboard_defaults_to_today(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.tab_range["tab-dashboard"], "1d")
            self.assertEqual(app.query_one("#range", tui.Select).value, "1d")

    # 40c. empty DB -> KPI zeros, token dashes, explicit empty-state note
    async def test_dashboard_empty_state(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("0", str(app.query_one("#dash-kpi-tools").content))
            self.assertIn("-", str(app.query_one("#dash-tok-input").content))
            self.assertIn("No data", str(app.query_one("#dash-note").content))

    # 40d. KPI reflects the Dashboard's own window (recent vs 200h-old rows)
    async def test_dashboard_kpi_reflects_window(self):
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            (f"r{i}", "s1", "/p", "T", "builtin", now - 3600_000 + i, 1,
             "completed") for i in range(1234)
        ] + [
            (f"o{i}", "s1", "/p", "T", "builtin", now - 200 * 3600_000 + i, 1,
             "completed") for i in range(1000)
        ])
        app = self._pinned_app(now)          # clock pinned, so "now - 1h" is Today
        async with app.run_test() as pilot:
            await pilot.pause()
            # Today: only the 1,234 recent calls
            self.assertIn("1,234",
                          str(app.query_one("#dash-kpi-tools").content))
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            self.assertIn("2,234",
                          str(app.query_one("#dash-kpi-tools").content))

    # 40e. Tokens block reflects the Dashboard's own window too
    async def test_dashboard_tokens_reflect_window(self):
        now = self._noon_ms()
        make_db(self.db_path, [
            ("r", "s1", "m", 100, 20, 0, 0, now - 3600_000, 1),
            ("o", "s1", "m", 10, 2, 0, 0, now - 200 * 3600_000, 1),
        ], cache={"r": (80, 15, 5)})
        app = self._pinned_app(now)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Today: Usage Total = 100 + 20 + 80
            self.assertIn("200", str(app.query_one("#dash-tok-usage").content))
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            # All time: 200 + (10 + 2)
            self.assertIn("212", str(app.query_one("#dash-tok-usage").content))

    # 40f. narrow terminals stack the Dashboard rows (shared compact class)
    async def test_dashboard_compact_fallback(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = [app.query_one(f"#dash-row-{i}") for i in (1, 2, 3)]
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            for r in rows:
                self.assertTrue(r.has_class("compact"))
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            for r in rows:
                self.assertFalse(r.has_class("compact"))

    # 40g. Dashboard extras render: cache, runtime, leaderboards, sparkline
    async def test_dashboard_extras_render(self):
        now = self._noon_ms()
        make_db(self.db_path, [
            ("m1", "s1", "alpha", 100, 20, 80, 15, now - 3600_000, 1),
        ], cache={"m1": (80, 15, 5)})
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now - 3600_000, 40, "completed"),
            ("c2", "s1", "/p", "Bash", "builtin", now - 3500_000, 60, "completed"),
            ("c3", "s1", "/p", "Read", "builtin", now - 3400_000, None, "incomplete"),
        ])
        app = self._pinned_app(now)
        async with app.run_test() as pilot:
            await pilot.pause()
            # cache panel: absolute hit/miss/write
            self.assertIn("80", str(app.query_one("#dash-cache-hit").content))
            self.assertIn("15", str(app.query_one("#dash-cache-miss").content))
            self.assertIn("5", str(app.query_one("#dash-cache-write").content))
            # runtime panel
            self.assertIn("2", str(app.query_one("#dash-rt-completed").content))
            self.assertIn("1", str(app.query_one("#dash-rt-incomplete").content))
            self.assertIn("50", str(app.query_one("#dash-rt-avg").content))  # (40+60)/2
            # leaderboards pick the entities with calls
            self.assertIn("alpha", str(app.query_one("#dash-models").content))
            tools = str(app.query_one("#dash-tools").content)
            self.assertIn("Bash", tools)
            self.assertIn("2 calls", tools)          # Bash had two calls
            self.assertNotIn("1 calls", tools)       # Edit/Read had one -> singular
            self.assertIn("1 call ", tools)
            # sparkline has a bar for the busy day + a range/peak caption
            self.assertTrue(str(app.query_one("#dash-activity").content).strip())
            self.assertIn("peak", str(app.query_one("#dash-activity-axis").content))

    # 41. the injected clock drives "now", and therefore the tab windows
    async def test_injected_clock_drives_the_window(self):
        pinned = 1_700_000_000_000          # a fixed instant (seconds*1000)
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: pinned / 1000)
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app._now_ms(), pinned)
            # the window is derived from the pinned clock, not the wall clock
            start, end = app._bounds_for("tab-dashboard")
            self.assertEqual(end, pinned)
            self.assertEqual(start, db.window_bounds("1d", pinned)[0])

    # 42. a sync that indexed nothing must not leave "Syncing…" on the bar
    async def test_zero_file_sync_restores_the_data_line(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=lambda **kw: {"files_indexed": 0})
        async with app.run_test() as pilot:
            await pilot.pause()
            before = str(app.query_one("#status").content)
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            after = str(app.query_one("#status").content)
            self.assertNotIn("Syncing", after,
                             "the sync finished; the bar still advertises it")
            self.assertIn("tool calls", after)
            self.assertEqual(after, before)

    # 43. `s` marks the switch *and* keeps the counts it used to wipe out
    async def test_auto_sync_toggle_keeps_the_data_line(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            status = str(app.query_one("#status").content)
            self.assertIn("auto-sync OFF", status)
            self.assertIn("tool calls", status,
                         "the toggle replaced the whole line, and nothing put "
                         "the numbers back until an unrelated refresh")
            await pilot.press("s")
            await pilot.pause()
            self.assertNotIn("auto-sync OFF",
                             str(app.query_one("#status").content))

    # 44/45. a re-query that fails says so; the tick that recovers says it stopped
    async def test_a_failed_requery_is_reported_and_recovers(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, now, 1)])
        broken = Path(self.tmp.name) / "broken.db"
        broken.write_bytes(b"this is not a database at all")
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            good = app.db_path

            app.db_path = str(broken)
            app._refresh_active_tab()
            status = str(app.query_one("#status").content)
            self.assertTrue(app._status_note,
                            "a re-query that failed left no trace on the bar")
            self.assertIn("!", status)
            self.assertIn("Error", status)
            # The counts stay visible *with* the warning: they are the last known
            # numbers, and silently dropping either half would be a different lie.
            self.assertIn("tool calls", status)

            app.db_path = good
            app._refresh_active_tab()
            status = str(app.query_one("#status").content)
            self.assertEqual(app._status_note, "")
            self.assertNotIn("Error", status)
            self.assertIn("tool calls", status)

    # 46/47. narrow terminals get two panels per row, very narrow ones stack
    async def test_usage_panels_go_two_up_before_they_stack(self):
        make_db(self.db_path, [])
        # per_row is how many panels share one row of the region: 4 wide, 2 on a
        # narrow terminal, 1 when a second column would wrap a panel line.
        for width, per_row in ((140, 4), (80, 2), (50, 1)):
            with self.subTest(width=width):
                app = TrackerApp(str(self.db_path))
                async with app.run_test(size=(width, 24)) as pilot:
                    await pilot.pause()
                    app.query_one(tui.TabbedContent).active = "tab-usage"
                    await pilot.pause()
                    region = app.query_one("#usage-panels")
                    panel = app.query_one("#panel-tokens")
                    self.assertEqual(region.has_class("stacked"),
                                     width < TrackerApp.STACKED_WIDTH)
                    self.assertEqual(region.size.width // panel.size.width,
                                     per_row,
                                     f"at {width}: panel={panel.size.width} "
                                     f"region={region.size.width}")

    # 48. the two-up layout must not cost the table its rows
    async def test_narrow_usage_keeps_the_table_visible(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            table = app.query_one("#t-usage", tui.DataTable)
            self.assertGreater(table.size.height, 6,
                               "the panels took the table's rows")

    # 49. each tab's own window drives the Usage table's empty state
    async def test_usage_empty_state_follows_the_window(self):
        pinned = 1_700_000_000_000            # "now" for this app
        # All-time still ends at the clock's now, so the row has to sit *before*
        # it: a fixture at the wall clock would be ten years in the future here.
        old = pinned - 10 * 86_400_000
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, old, 1)])
        app = TrackerApp(str(self.db_path), clock=lambda: pinned / 1000)
        async with app.run_test() as pilot:
            await pilot.pause()
            # The shared range Select drives the *active* tab, so the Usage tab
            # has to be open before the window means anything to it.
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            t = app.query_one("#t-usage", tui.DataTable)
            app.query_one("#range", tui.Select).value = "1d"
            await pilot.pause()
            self.assertIn("No requests in this window", all_rows(t)[0][0])
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            self.assertEqual(all_rows(t)[0][1], "m")     # the real row returns
            self.assertNotIn("No requests",
                             str(app.query_one("#usage-note").content))

    # 50. a quiet window draws a baseline instead of a blank, and says so
    async def test_dashboard_activity_is_never_a_blank_region(self):
        pinned = 1_700_000_000_000
        now = int(time.time() * 1000)
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, now, 1)])
        conn = db.open_db(self.db_path)
        # q_usage_daily counts tool calls, so the control needs one: without it
        # both clocks would read "no activity" and prove nothing.
        conn.execute("INSERT INTO tool_calls(call_id, session_id, project,"
                     " tool_name, category, ts)"
                     " VALUES('c1','s1','/p','Bash','builtin',?)", (now,))
        conn.commit()
        conn.close()

        quiet = TrackerApp(str(self.db_path), clock=lambda: pinned / 1000)
        async with quiet.run_test() as pilot:
            await pilot.pause()
            activity = str(quiet.query_one("#dash-activity").content)
            self.assertTrue(activity.strip(),
                            "no calls rendered an empty region on the Dashboard")
            self.assertEqual(set(activity.strip()), {"·"},
                             "a quiet window must draw the baseline, not bars "
                             "it did not measure")
            self.assertIn("no activity",
                          str(quiet.query_one("#dash-activity-axis").content))

        busy = TrackerApp(str(self.db_path))
        async with busy.run_test() as pilot:
            await pilot.pause()
            axis = str(busy.query_one("#dash-activity-axis").content)
            self.assertNotIn("no activity", axis)
            self.assertIn("peak", axis)
            self.assertEqual(set(str(busy.query_one("#dash-activity").content)),
                             {"█"})

    # 51. every count in the status line agrees with its own number
    async def test_status_counts_agree_with_their_number(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, now, 1)])
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.execute("INSERT INTO tool_calls(call_id, session_id, project,"
                     " tool_name, category, ts, duration_ms, status)"
                     " VALUES('c1','s1','/p','Bash','builtin',?,1500,'completed')",
                     (now,))
        conn.execute("INSERT INTO skill_usage(call_id, skill, has_args, plugin,"
                     " session_id, project, ts, status, duration_ms)"
                     " VALUES('c2','sk',0,NULL,'s1','/p',?,'completed',100)",
                     (now,))
        conn.execute("INSERT INTO agent_usage(call_id, agent_type, kind, source,"
                     " session_id, project, ts, status, duration_ms)"
                     " VALUES('c3','Explore','active','tool','s1','/p',?,"
                     "'completed',100)", (now,))
        conn.execute("INSERT INTO mcp_usage(call_id, server, tool, session_id,"
                     " project, ts, status, duration_ms)"
                     " VALUES('c4','srv','t','s1','/p',?,'completed',100)", (now,))
        conn.execute("INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
                     " session_id, project, ts)"
                     " VALUES('pl','mk','skill','sk','s1','/p',?)", (now,))
        conn.commit()
        conn.close()

        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            status = str(app.query_one("#status").content)
            # Exactly one of everything. Scanning the rendered line instead of
            # naming each phrase is what stops a newly added count from escaping
            # the rule D-005 was supposed to install.
            bad = [m.group(0) for m in re.finditer(r"(?<![\d,])1 \w+s\b", status)]
            self.assertEqual(bad, [], f"plural disagreement in: {status}")
            self.assertIn("1 tool call ·", status)
            self.assertIn("1 model response ·", status)

    # 52. a duration is a duration on both pages, not a token count
    async def test_durations_use_one_formatter(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now, 1500, "completed")])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            dash = str(app.query_one("#dash-rt-avg").content)
            self.assertIn("1.5s", dash)
            self.assertNotIn("1,500", dash,
                             "the Dashboard formatted a duration with the token "
                             "formatter, so the two pages disagreed by shape")
            rows = [c for c in all_rows(app.query_one("#t-tools", tui.DataTable))]
            self.assertIn("1.5s", rows[0])

    # 53. the completeness panel cannot read "Incomplete: complete"
    async def test_completeness_label_matches_its_value(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await self._usage_all_time(app, pilot)
            line = str(app.query_one("#sum-missing").content)
            self.assertIn("Completeness", line)
            self.assertTrue(line.rstrip().endswith("complete"), line)
            self.assertNotIn("Incomplete", line)

    # 54. a one-day window is one date, not a range of the same date twice
    async def test_today_window_shows_one_date(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "1d"
            await pilot.pause()
            win = str(app.query_one("#usage-window").content)
            self.assertNotIn("—", win, f"single-day range printed twice: {win!r}")
            app.query_one("#range", tui.Select).value = "3d"
            await pilot.pause()
            self.assertIn("—", str(app.query_one("#usage-window").content))

    # 55. a long name may not push the leaderboard's columns out of line
    async def test_leaderboard_columns_stay_aligned(self):
        now = int(time.time() * 1000)
        long_name = "claude-with-a-really-unnecessarily-long-name-for-testing"
        self.assertEqual(len(long_name), 56)
        make_db(self.db_path, [
            ("msg-1", "s1", long_name, 1_000, 10, 0, 0, now, 1),
            ("msg-2", "s1", "short", 900, 10, 0, 0, now, 1),
        ])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", long_name, "builtin", now, 10, "completed"),
            ("c2", "s1", "/p", "Bash", "builtin", now, 10, "completed")])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            models = str(app.query_one("#dash-models").content).splitlines()
            self.assertEqual(len(set(map(len, models))), 1,
                             f"ragged leaderboard: {models}")
            # The ellipsis must land exactly on the field edge, not past it, and
            # the short name must be padded to that same edge.
            cut = [l for l in models if "…" in l]
            self.assertEqual(len(cut), 1, models)
            self.assertEqual(cut[0][28], "…")
            self.assertEqual([l for l in models if l not in cut][0][28], " ")
            tools = str(app.query_one("#dash-tools").content).splitlines()
            self.assertEqual(len(set(map(len, tools))), 1, f"ragged: {tools}")
            cut = [l for l in tools if "…" in l]
            self.assertEqual(len(cut), 1, tools)      # only the long name is cut
            self.assertEqual(cut[0][24], "…")

    # 56. the Agents tab renders its rows (it had no page assertion at all)
    async def test_agents_tab_renders_its_rows(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.execute("INSERT INTO agent_usage(call_id, agent_type, kind, source,"
                     " session_id, project, ts, status, duration_ms)"
                     " VALUES('a1','general-purpose','active','tool','s1','/p',?,"
                     "'completed',500)", (now,))
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            app.query_one(tui.TabbedContent).active = "tab-agents"
            await pilot.pause()
            rows = all_rows(app.query_one("#t-agents", tui.DataTable))
            self.assertEqual(rows[0][0], "general-purpose")
            self.assertEqual(rows[0][1], "1")        # singular count, grouped

    # 57. every tab lays out at 60x24 (only 70/140 were covered before)
    async def test_every_tab_lays_out_on_a_narrow_terminal(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("msg-1", "s1", "m", 1, 1, 0, 0, now, 1)])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now, 10, "completed")])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(60, 24)) as pilot:
            for tab in app.TAB_IDS:
                with self.subTest(tab=tab):
                    app.query_one(tui.TabbedContent).active = tab
                    await pilot.pause(0.05)
                    # The Dashboard has no table, so the pane itself is the
                    # assertion that holds for all eight tabs.
                    pane = app.query_one(f"#{tab}", tui.TabPane)
                    self.assertGreater(pane.size.width, 0,
                                       "the tab never laid out")
                    self.assertGreater(pane.size.height, 0)
                    for table in app.query(f"#{tab} DataTable"):
                        self.assertGreater(table.size.height, 0)

    # 58. the two wide tables get a column plan instead of a clipped tail
    async def test_narrow_terminals_get_a_column_plan_not_a_cut(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            (f"m{i}", f"s{i % 2}", "claude-sonnet-4-5-20260101-ext",
             120000 + i, 15000 + i, 90000 + i, 1000 + i, now - i * 60000, 1)
            for i in range(6)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(6)},
            ptotals={f"m{i}": 135000 + i for i in range(6)})
        for tab, tid, note_id, cols in (
                ("tab-tokens", "#t-tokens", "#colnote-tokens", tui.TOKEN_COLUMNS),
                ("tab-usage", "#t-usage", "#colnote-usage", tui.USAGE_COLUMNS)):
            with self.subTest(tab=tab):
                app = TrackerApp(str(self.db_path))
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    app.query_one(tui.TabbedContent).active = tab
                    await pilot.pause()
                    t = app.query_one(tid, tui.DataTable)
                    labels = [str(c.label) for c in t.columns.values()]
                    self.assertLess(len(labels), len(cols),
                                    "the full set was kept and cut at the edge")
                    # The claim of a *plan* (not a scroll): what is shown fits.
                    self.assertLessEqual(
                        sum(c.content_width for c in t.columns.values())
                        + 2 * len(labels), t.region.width,
                        f"{labels} still exceed {t.region.width}")
                    note = app.query_one(note_id, tui.Static)
                    text = str(note.content)
                    for hidden in [c for c in cols if c not in labels]:
                        self.assertIn(hidden, text,
                                      f"{hidden} is gone and the note does not say so")
                    self.assertIn(f"{len(labels)} of {len(cols)}", text,
                                  "the note's numbers must be the table's numbers")
                    # The note is not allowed to be cut the way the table was: a
                    # message about hidden columns that is itself hidden is a lie.
                    # Wrapping is fine, so the rendered lines are joined flat.
                    rendered = " ".join(" ".join(
                        "".join(s.text for s in note.render_line(y))
                        for y in range(note.region.height)).split())
                    for hidden in [c for c in cols if c not in labels]:
                        self.assertIn(hidden, rendered,
                                      f"the note names {hidden} but the screen cuts it off")

    # 59. a wide terminal keeps every column and says nothing
    async def test_a_wide_terminal_hides_no_columns_and_notes_nothing(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            (f"m{i}", f"s{i % 2}", "claude-sonnet-4-5-20260101-ext",
             120000 + i, 15000 + i, 90000 + i, 1000 + i, now - i * 60000, 1)
            for i in range(6)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(6)},
            ptotals={f"m{i}": 135000 + i for i in range(6)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            for tab, tid, note_id, cols in (
                    ("tab-tokens", "#t-tokens", "#colnote-tokens", tui.TOKEN_COLUMNS),
                    ("tab-usage", "#t-usage", "#colnote-usage", tui.USAGE_COLUMNS)):
                with self.subTest(tab=tab):
                    app.query_one(tui.TabbedContent).active = tab
                    await pilot.pause()
                    t = app.query_one(tid, tui.DataTable)
                    self.assertEqual([str(c.label) for c in t.columns.values()],
                                     list(cols))
                    self.assertEqual(str(app.query_one(note_id, tui.Static).content), "",
                                     "a note on a page that hides nothing is noise")

    # 60. widening hands the columns back — without losing the rows
    async def test_widening_the_terminal_restores_the_dropped_columns(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            (f"m{i}", f"s{i % 2}", "claude-sonnet-4-5",
             120000 + i, 15000 + i, 90000 + i, 1000 + i, now - i * 60000, 1)
            for i in range(6)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(6)},
            ptotals={f"m{i}": 135000 + i for i in range(6)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tokens"
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            narrow_labels = [str(c.label) for c in t.columns.values()]
            rows = t.row_count
            self.assertLess(len(narrow_labels), len(tui.TOKEN_COLUMNS))
            await pilot.resize_terminal(160, 30)
            await pilot.pause()
            self.assertEqual([str(c.label) for c in t.columns.values()],
                             list(tui.TOKEN_COLUMNS),
                             "widening never gave the columns back")
            self.assertEqual(t.row_count, rows, "the rebuild lost rows")
            self.assertEqual(str(app.query_one("#colnote-tokens", tui.Static).content), "")

    # 61. a rebuilt table must still know which entity its row is
    async def test_the_row_key_survives_the_narrow_rebuild(self):
        now = int(time.time() * 1000)
        long_names = ["claude-sonnet-4-5-20260101-ext", "gpt-5.1-codex-max-20260101"]
        make_db(self.db_path, [
            (f"m{i}", f"s{i % 2}", long_names[i % 2],
             120000 + i, 15000 + i, 90000 + i, 1000 + i, now - i * 60000, 1)
            for i in range(6)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(6)},
            ptotals={f"m{i}": 135000 + i for i in range(6)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tokens"
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            self.assertEqual(t.row_count, 2)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertLess(len(labels), len(tui.TOKEN_COLUMNS),
                            "this case is only about a *rebuilt* table")
            t.move_cursor(row=1)
            await pilot.pause()
            key = list(t.rows.keys())[1]
            # the real routing handler, same message a click posts
            shown = str(t.get_cell_at(t.cursor_coordinate))
            full = key.value.partition("\t")[2]
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, t.cursor_row, key))
            await pilot.pause()
            await pilot.pause()
            top = app.screen_stack[-1]
            self.assertIsInstance(top, tui.ModelResponsesScreen,
                                  f"a cut table cannot open its detail screen ({top!r})")
            self.assertEqual(top.model, full,
                             "the rebuilt rows lost the key that names their model")
            # The cell and the key are deliberately different objects now: the cell
            # is the capped rendering, the route is the whole name. Asserting only
            # `top.model == shown` would have passed either way.
            self.assertEqual(shown, app._cap_cell(full))
            if len(full) > app.name_cap:
                self.assertNotEqual(shown, full, "a long name was not capped at all")
            self.assertIn(shown, [app._cap_cell(n) for n in long_names],
                          "the cell is not one of the two models' renderings")

    # 62. the plan is a rule for every table, not two of them
    async def test_no_table_shows_a_half_column_at_any_width(self):
        """Loop every tab at 60 and 80 wide: what is shown fits, or the note lies.

        The floor at the bottom is the point of the case. A loop that only ever
        measured tables which happen to fit would pass without the plan existing at
        all, so it also counts the tables that had to drop something.
        """
        now = int(time.time() * 1000)
        LONG = "an-entity-name-long-enough-to-push-a-column-off-an-80-column-screen"
        make_db(self.db_path, [
            (f"m{i}", f"s{i % 2}", LONG, 120000 + i, 15000 + i, 90000 + i,
             1000 + i, now - i * 500_000, 1) for i in range(4)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(4)},
            ptotals={f"m{i}": 135000 + i for i in range(4)})
        insert_tool_calls(self.db_path, [
            (f"c{i}", "s1", "/p", LONG, "builtin", now - i * 60000, 10, "completed")
            for i in range(3)])
        insert_mcp_usage(self.db_path, [
            (f"mc{i}", LONG, LONG + "-tool", "s1", "/p", now - i * 60000,
             "completed", 10) for i in range(2)])
        conn = db.open_db(self.db_path)
        conn.executemany(
            "INSERT INTO skill_usage(call_id, skill, plugin, session_id, project, ts,"
            " status, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
            [(f"sk{i}", LONG, LONG, "s1", "/p", now - i * 60000, "completed", 20)
             for i in range(2)])
        conn.executemany(
            "INSERT INTO agent_usage(call_id, agent_type, kind, source, session_id,"
            " project, ts, status, duration_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"ag{i}", LONG, "active", "tool", "s1", "/p", now - i * 60000,
              "completed", 30) for i in range(2)])
        conn.commit()
        conn.close()

        for width in (60, 80):
            app = TrackerApp(str(self.db_path))
            dropped = []
            async with app.run_test(size=(width, 24)) as pilot:
                await pilot.pause()
                for tab in app.TAB_IDS:
                    with self.subTest(width=width, tab=tab):
                        app.query_one(tui.TabbedContent).active = tab
                        await pilot.pause(0.05)
                        for t in app.query(f"#{tab} DataTable"):
                            full = list(app.WIDE_TABLES[t.id][0])
                            labels = [str(c.label) for c in t.columns.values()]
                            note = app.query_one(f"#{app.WIDE_TABLES[t.id][1]}",
                                                 tui.Static)
                            if labels == full:
                                # No column is hidden, so the note has exactly one
                                # thing it may say: that characters were cut. And when
                                # something was cut it must say it — a silent cap is
                                # the same lie as a silent column drop.
                                rows = app._wide_rows[t.id][0]
                                over = any(c is not None and len(str(c)) > app.name_cap
                                           for r in rows for c in r)
                                flat = " ".join(" ".join(
                                    "".join(s.text for s in note.render_line(y))
                                    for y in range(note.region.height)).split())
                                self.assertEqual(note.display, over,
                                                 f"{t.id}: note visible={note.display} "
                                                 f"but a cell needs capping={over}")
                                if over:
                                    self.assertEqual(
                                        flat, f"cells capped at {app.name_cap}",
                                        f"{t.id} capped characters and the note says "
                                        f"{flat!r}")
                                else:
                                    self.assertEqual(flat, "")
                                continue
                            dropped.append(t.id)
                            # what remains is a prefix of the registry, every name
                            # that went away is on the note, and the row is whole.
                            self.assertEqual(labels, full[:len(labels)],
                                             f"{t.id} shows columns out of order")
                            need = sum(c.content_width for c in t.columns.values()) \
                                + 2 * len(labels)
                            flat = " ".join(" ".join(
                                "".join(s.text for s in note.render_line(y))
                                for y in range(note.region.height)).split())
                            for hidden in full[len(labels):]:
                                self.assertIn(hidden, flat,
                                              f"{hidden} is gone and unnamed")
                            self.assertIn(f"{len(labels)} of {len(full)}", flat,
                                          "the note's numbers are not the table's")
                            if need > t.region.width:
                                # Only legal at the floor: two name columns wider than
                                # the screen cannot be cut further, so the note has to
                                # say so instead of the table pretending it fits.
                                self.assertLessEqual(len(labels), app.MIN_SHOWN,
                                                     f"{t.id} stops above the floor")
                                self.assertIn("even these are cut", flat,
                                              f"{t.id} is still cut and does not say so")
                            else:
                                self.assertNotIn("even these are cut", flat,
                                                 f"{t.id} fits but warns about being cut")
            # The two the fixture is built to overflow, at every width — without this
            # the loop could pass by never planning anything at all.
            self.assertIn("t-mcp", dropped)
            self.assertIn("t-usage", dropped)

    # 63. the pushed screens are planned too — they are the widest tables here
    async def test_the_detail_screens_get_the_column_plan(self):
        now = int(time.time() * 1000)
        LONG = "claude-sonnet-4-5-20260101-plus-an-extra-qualifier-segment"
        make_db(self.db_path, [
            (f"m{i}", "s1", LONG, 120000 + i, 15000 + i, 90000 + i, 1000 + i,
             now - i * 60000, 1) for i in range(3)],
            cache={f"m{i}": (88000 + i, 30000 + i, 1200 + i) for i in range(3)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(tui.ModelResponsesScreen(LONG))
            await pilot.pause()
            scr = app.screen_stack[-1]
            t = scr.query_one("#resp-table", tui.DataTable)
            cols = list(t.columns.values())
            self.assertLess(len(cols), len(tui.RESPONSE_COLUMNS),
                            "the 12-column table was left to clip")
            self.assertLessEqual(sum(c.content_width for c in cols) + 2 * len(cols),
                                 t.region.width)
            note = scr.query_one("#colnote-resp", tui.Static)
            self.assertTrue(note.display)
            hidden = [c for c in tui.RESPONSE_COLUMNS if c not in
                      [str(x.label) for x in cols]]
            flat = " ".join(" ".join("".join(s.text for s in note.render_line(y))
                                    for y in range(note.region.height)).split())
            for label in hidden:
                self.assertIn(label, flat, f"{label} is gone and unnamed")

    # 64. a wide terminal shows every column on the screens and warns about nothing
    async def test_the_detail_screens_show_all_columns_when_they_fit(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(220, 40)) as pilot:
            await pilot.pause()
            for screen, tid, note_id, cols in (
                    (tui.ModelResponsesScreen("m"), "#resp-table", "#colnote-resp",
                     tui.RESPONSE_COLUMNS),
                    (tui.HistoryScreen("tool", "Bash"), "#hist-table", "#colnote-hist",
                     tui.HISTORY_COLUMNS)):
                with self.subTest(table=tid):
                    app.push_screen(screen)
                    await pilot.pause()
                    t = app.screen_stack[-1].query_one(tid, tui.DataTable)
                    self.assertEqual([str(c.label) for c in t.columns.values()],
                                     list(cols))
                    note = app.screen_stack[-1].query_one(note_id, tui.Static)
                    self.assertFalse(note.display,
                                     "a note above a table that hides nothing is noise")
                    await pilot.press("escape")
                    await pilot.pause()

    # 65. the registry is the only source of column truth
    async def test_the_columns_on_screen_are_exactly_the_registered_ones(self):
        """`on_mount` builds from WIDE_TABLES; this pins every entry to that.

        Without it a table could gain a column in one place and the plan would drop a
        name it never declared — the note would then be describing a column that does
        not exist.
        """
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(220, 40)) as pilot:
            await pilot.pause()
            for tid, (cols, note_id, _chrome) in app.WIDE_TABLES.items():
                with self.subTest(table=tid):
                    t = app.query_one(f"#{tid}", tui.DataTable)
                    self.assertEqual([str(c.label) for c in t.columns.values()],
                                     list(cols))
                    self.assertTrue(app.query_one(f"#{note_id}", tui.Static))
            for screen_cls, attrs in ((tui.HistoryScreen, "WIDE_TABLES"),
                                      (tui.ModelResponsesScreen, "WIDE_TABLES")):
                self.assertTrue(getattr(screen_cls, attrs))

    # 66. a planned table must still route its row to the right entity
    async def test_the_mcp_row_key_survives_the_narrow_plan(self):
        now = int(time.time() * 1000)
        LONG = "server-name-long-enough-to-force-the-plan-to-drop-columns-here"
        insert_mcp_usage(self.db_path, [
            (f"mc{i}", LONG, f"{LONG}-tool-{i}", "s1", "/p", now - i * 60000,
             "completed", 10) for i in range(2)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-mcp"
            await pilot.pause()
            t = app.query_one("#t-mcp", tui.DataTable)
            self.assertEqual([str(c.label) for c in t.columns.values()],
                             ["server", "tool"],
                             "the plan cut into the two columns the row key is built from")
            t.move_cursor(row=1)
            await pilot.pause()
            key = list(t.rows.keys())[1]
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, t.cursor_row, key))
            await pilot.pause()
            await pilot.pause()
            scr = app.screen_stack[-1]
            self.assertIsInstance(scr, tui.HistoryScreen)
            self.assertEqual(scr.entity, f"{LONG}-tool-1",
                             "the rebuilt rows lost the key that names their tool")

    # 67. a long project path costs a column, not the whole table
    async def test_a_long_path_is_capped_so_the_columns_come_back(self):
        """The real-data reason for a cap: history rows all share one long prefix.

        Measured on this machine's database: 162 of 482 project paths exceed 24
        cells and the longest is 74 — while every entity name column tops out at
        28. So the column that was eating the History screen is the one whose
        beginning says nothing (`/home/…/Public/`) and whose end says everything.
        """
        now = int(time.time() * 1000)
        BASE = "/home/user/very-long/shared-prefix-that-says-nothing"
        insert_tool_calls(self.db_path, [
            (f"c{i}", f"s{i}", f"{BASE}/project-{i}", "Bash", "builtin",
             now - i * 60_000, 10, "completed") for i in range(3)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(tui.HistoryScreen("tool", "Bash"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            t = scr.query_one("#hist-table", tui.DataTable)
            cap = app.name_cap
            labels = [str(c.label) for c in t.columns.values()]
            self.assertEqual(labels, list(tui.HISTORY_COLUMNS),
                             f"a 74-cell path still pushed columns off the screen: {labels}")
            paths = [t.get_row_at(r)[1] for r in range(t.row_count)]
            for p in paths:
                self.assertLessEqual(len(p), cap, f"uncapped cell: {p!r}")
                self.assertTrue(p.startswith("…"),
                                f"a path capped from the head keeps only the shared prefix: {p!r}")
            self.assertEqual(sorted(p.split("/")[-1] for p in paths),
                             ["project-0", "project-1", "project-2"],
                             "the tails that distinguish the rows were cut away")

    # 68. a plain name keeps its beginning; only paths keep their end
    async def test_a_name_capped_from_the_left_would_say_nothing(self):
        now = int(time.time() * 1000)
        LONG_TOOL = "an_entity_name_without_any_slash_that_is_far_too_long_for_a_column"
        insert_tool_calls(self.db_path, [
            (f"c{i}", f"s{i}", "/p", LONG_TOOL, "builtin", now - i * 60_000,
             10, "completed") for i in range(2)])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            cap = app.name_cap
            cell = t.get_row_at(0)[0]
            self.assertLessEqual(len(cell), cap)
            self.assertTrue(cell.startswith(LONG_TOOL[:cap - 1]),
                            f"a name must keep its head, got {cell!r}")
            self.assertTrue(cell.endswith("…"), f"the cut is silent: {cell!r}")
            self.assertNotIn("/", cell)

    # 69. capping must not merge two entities into one route
    async def test_two_names_sharing_a_prefix_longer_than_the_cap_still_route_apart(self):
        """The failure a cap would ship: identical cells, and a key built from them.

        Both rows render as the same 28 cells. Selection decodes the entity from
        the ROW KEY, so the key has to come from the uncapped value — otherwise
        picking the second tool shows the first tool's history and every test
        about "the row still routes" stays green on a table of one name.
        """
        now = int(time.time() * 1000)
        SHARED = "tool-name-sharing-its-whole-visible-prefix-with"
        insert_tool_calls(self.db_path, [
            ("c0", "s0", "/p", f"{SHARED}-first", "builtin", now, 10, "completed"),
            ("c1", "s1", "/p", f"{SHARED}-second", "builtin", now - 1, 10, "completed"),
            ("c2", "s2", "/p", f"{SHARED}-third", "builtin", now - 2, 10, "completed"),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            cells = [t.get_row_at(r)[0] for r in range(3)]
            self.assertEqual(len(set(cells)), 1,
                             f"the cap did not even engage: {cells}")
            keys = list(t.rows.keys())
            second = [i for i, k in enumerate(keys)
                      if k.value.endswith(f"-second")][0]
            t.move_cursor(row=second)
            await pilot.pause()
            key = keys[second]
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, t.cursor_row, key))
            await pilot.pause()
            await pilot.pause()
            scr = app.screen_stack[-1]
            self.assertIsInstance(scr, tui.HistoryScreen)
            self.assertEqual(scr.entity, f"{SHARED}-second",
                             "the capped cell was used as the key — both rows route to one tool")

    # 70. the note says when it cut characters rather than columns
    async def test_the_note_announces_that_cells_were_capped(self):
        now = int(time.time() * 1000)
        BASE = "/home/user/a-project-path-long-enough-to-need-capping-here-ok"
        insert_tool_calls(self.db_path, [
            ("c0", "s0", BASE, "Bash", "builtin", now, 10, "completed")])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(tui.HistoryScreen("tool", "Bash"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            t = scr.query_one("#hist-table", tui.DataTable)
            self.assertEqual(len(t.columns), len(tui.HISTORY_COLUMNS),
                             "no column is hidden, so the note should be describing characters")
            cap = app.name_cap
            flat = " ".join(" ".join("".join(s.text for s in
                                             scr.query_one("#colnote-hist", tui.Static)
                                             .render_line(y))
                                     for y in range(scr.query_one("#colnote-hist",
                                                                  tui.Static).region.height)
                                     ).split())
            self.assertIn(f"cells capped at {cap}", flat,
                          f"a silent character cut: {flat!r}")

    # 71. a configured cap has to be the number that actually renders, on every host
    async def test_a_configured_cap_reaches_the_detail_screen_too(self):
        """The cap lives on a mixin as a CLASS attribute, so a per-instance setting
        can be plumbed and still miss a host. The pushed screens are where the long
        project paths really are — a cap honoured only by the tabs would leave the
        worst column untouched, which is C6's shape: written, wired, not in force on
        the path the user takes.
        """
        now = int(time.time() * 1000)
        LONG_TOOL = "an_entity_name_without_any_slash_that_is_far_too_long_for_a_column"
        BASE = "/home/user/a-path-whose-tail-is-the-only-part-that-differs"
        insert_tool_calls(self.db_path, [
            (f"c{i}", f"s{i}", f"{BASE}/proj-{i}", LONG_TOOL, "builtin",
             now - i * 60_000, 10, "completed") for i in range(2)])
        app = TrackerApp(str(self.db_path), config={
            "top_n": 5, "log_limit": 100, "detail_limit": 200, "refresh_secs": 5,
            "sync_secs": 30, "name_cap": 12})
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app.name_cap, 12)
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            for r in range(t.row_count):
                self.assertLessEqual(len(str(t.get_row_at(r)[0])), 12,
                                     "the tab ignored the configured cap")
            flat = " ".join(" ".join("".join(s.text for s in
                            app.query_one("#colnote-tools", tui.Static).render_line(y))
                            for y in range(app.query_one("#colnote-tools",
                                                         tui.Static).region.height)).split())
            self.assertIn("cells capped at 12", flat,
                          f"the note still quotes the class default: {flat!r}")
            app.push_screen(tui.HistoryScreen("tool", LONG_TOOL))
            await pilot.pause()
            scr = app.screen_stack[-1]
            self.assertEqual(scr.name_cap, 12,
                             "the pushed screen renders with the class attribute instead")
            ht = scr.query_one("#hist-table", tui.DataTable)
            for r in range(ht.row_count):
                self.assertLessEqual(len(str(ht.get_row_at(r)[1])), 12,
                                     "a detail screen ignored the configured cap")
            self.assertEqual(scr._cap_cell("a" * 40), "a" * 11 + "…",
                             "the cap helper itself is not reading the instance value")

    # 72. the next detail screen cannot inherit the class default by accident
    def test_every_pushed_screen_resolves_the_cap_from_the_app(self):
        """``name_cap`` is overridden per class, so a screen that forgets it renders
        with the shipped 28 while the settings file says otherwise — and nothing the
        suite does today would notice, because the behaviour cases can only push the
        screens that already exist. This one enumerates the hosts from the module,
        so a new one has to earn the override.
        """
        hosts = [obj for obj in vars(tui).values()
                 if isinstance(obj, type) and issubclass(obj, tui.WideTableMixin)
                 and issubclass(obj, tui.Screen)]
        self.assertGreaterEqual(len(hosts), 2,
                                f"only {len(hosts)} screen hosts enumerated — "
                                "the query broke, so this guard proves nothing")
        missing = sorted(c.__name__ for c in hosts if "name_cap" not in c.__dict__)
        self.assertFalse(missing,
                         f"these screens cap with the class attribute, not the setting: "
                         f"{missing}")
        # Control: a mixin screen without the override really does fail the predicate,
        # so the pass above is not the result of a test that can never go red.
        probe = type("UnoverriddenScreen", (tui.WideTableMixin, tui.Screen), {})
        self.assertTrue(hasattr(probe, "name_cap"),
                        "the mixin stopped supplying a default; the guard below is vacuous")
        self.assertNotIn("name_cap", probe.__dict__)

    # 73. every table the source can build has a plan
    def test_every_datatable_in_the_source_is_registered(self):
        """`The plan covers every table` is a claim about the source, so check it there.

        Every layout test loops over a registry, so a `DataTable` added without a
        registry entry would clip mid-label and no test would notice — the prose would
        still be true of the tables it knew about.
        """
        tree = ast.parse((SCRIPTS / "cbut-tui.py").read_text(encoding="utf-8"))
        built = {kw.value.value for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "id", None) == "DataTable"
                 for kw in node.keywords
                 if kw.arg == "id" and isinstance(kw.value, ast.Constant)}
        registered = (set(TrackerApp.WIDE_TABLES) | set(tui.HistoryScreen.WIDE_TABLES)
                      | set(tui.ModelResponsesScreen.WIDE_TABLES))
        self.assertGreaterEqual(len(built), 9,
                                f"only {len(built)} DataTable(id=…) found — the query broke")
        self.assertFalse(built - registered,
                         f"unplanned tables, free to clip: {sorted(built - registered)}")
        self.assertFalse(registered - built,
                         f"a registry names a table the source never builds: "
                         f"{sorted(registered - built)}")


if __name__ == "__main__":
    unittest.main()

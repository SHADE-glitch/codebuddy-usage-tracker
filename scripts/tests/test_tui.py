"""Tests for cbut-tui (the interactive Textual viewer).

Standard library only (unittest + Textual's built-in run_test harness).
Run:  python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import inspect
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
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

    # 1. seven tabs present (Tokens + the new Usage page)
    async def test_seven_tabs_present(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            self.assertEqual(tc.tab_count, 7)
            self.assertEqual(tc.tab_count, len(TrackerApp.TAB_IDS))

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
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[0], "m")
            # Input / Output / API Total / Usage Total / Cache columns are "-",
            # never "0"
            for i in range(3, 10):
                self.assertEqual(row[i], "-", f"col {i} should be '-'")

    # 5. prompt / completion / total / cache shown in separate columns
    async def test_input_output_separate_columns(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 20, 3, 4, now - 3600_000, 1),
        ], cache={"m1": (7, 8, 9)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[3], "10")   # Input  = prompt_tokens
            self.assertEqual(row[4], "20")   # Output = completion_tokens
            self.assertEqual(row[5], "30")   # API Total = Input + Output
            self.assertEqual(row[6], "37")   # Usage Total = 10 + 20 + 7
            self.assertEqual(row[7], "7")    # Cache hit
            self.assertEqual(row[8], "8")    # Cache miss
            self.assertEqual(row[9], "9")    # Cache write

    # 5b. explicit 0 is shown as "0", absent stays "-"
    async def test_zero_vs_null_distinction(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "zeroed", 1, 1, 0, 0, now - 3600_000, 1),
            ("m2", "s1", "nulled", 1, 1, None, None, now - 3600_000 + 1, 1),
        ], cache={"m1": (0, 0, 0)})   # m2 has no cache entry -> NULL
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(rows["zeroed"][7], "0")     # real 0
            self.assertEqual(rows["zeroed"][8], "0")
            self.assertEqual(rows["zeroed"][9], "0")
            self.assertEqual(rows["nulled"][7], "-")     # NULL, not 0
            self.assertEqual(rows["nulled"][8], "-")
            self.assertEqual(rows["nulled"][9], "-")

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
            self.assertEqual(row[7], "999,999")      # cache hit still shown

    # 5d. Tokens tab has all ten expected columns
    async def test_tokens_tab_columns(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            for want in ("Model", "Requests", "With usage", "Input", "Output",
                         "API Total", "Usage Total", "Cache hit", "Cache miss",
                         "Cache write", "Coverage"):
                self.assertIn(want, labels)

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
        async with app.run_test() as pilot:
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

    # 15. Usage tab exists (7 tabs total)
    async def test_usage_tab_present(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(tui.TabbedContent).tab_count, 7)

    # 16. Usage window is recomputed from now and reacts to range changes
    async def test_usage_window_and_range(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # Usage keeps its own range (default 24h); the Select drives it only
            # while the Usage tab is active.
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            self.assertIn("Last 24 hours", str(app.query_one("#usage-window").content))
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            self.assertIn("All time", str(app.query_one("#usage-window").content))

    # 17. Request Logs: Total = in + out, cache shown separately
    async def test_usage_logs_total_excludes_cache(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "m", 10, 20, 0, 0, now - 3600_000, 1),
        ], cache={"a": (999999, 888888, 7)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-usage", tui.DataTable)
            row = all_rows(t)[0]
            # column order: Time Model In Out API-Total Usage-Total
            #               Cache-hit Cache-miss Cache-write Usage Source
            self.assertEqual(row[1], "m")            # model (no provider column)
            self.assertEqual(row[4], "30")           # API Total, not +cache
            self.assertEqual(row[5], "1,000,029")    # Usage Total = 10+20+999999
            self.assertEqual(row[6], "999,999")      # Cache hit (read)
            self.assertEqual(row[7], "888,888")      # Cache miss
            self.assertEqual(row[8], "7")            # Cache write (create)
            self.assertEqual(row[9], "Real")

    # 17b. empty window shows an explicit empty state, never a blank region
    async def test_usage_empty_state(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("No requests", str(app.query_one("#usage-note").content))
            self.assertEqual(app.query_one("#t-usage", tui.DataTable).row_count, 0)

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
                        "#sum-cache-hit", "#sum-hit-rate", "#sum-total-source",
                        "#sum-usage-total", "#usage-status"):
                self.assertTrue(app.query_one(wid))

    # 22. #sum-total-source shows provider vs derived
    async def test_usage_total_source_displayed(self):
        now = int(time.time() * 1000)
        p1 = Path(self.tmp.name) / "prov.db"
        make_db(p1, [("a", "s1", "m", 10, 2, 0, 0, now - 3600_000, 1)],
                ptotals={"a": 12})
        app = TrackerApp(str(p1))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("provider",
                          str(app.query_one("#sum-total-source").content))
        p2 = Path(self.tmp.name) / "der.db"
        make_db(p2, [("b", "s1", "m", 10, 2, 0, 0, now - 3600_000, 1)])
        app = TrackerApp(str(p2))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("derived",
                          str(app.query_one("#sum-total-source").content))

    # 23. Tokens tab Coverage column (col 9) + thousands-separated counts
    async def test_tokens_coverage_value(self):
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
            self.assertEqual(row[1], "2")        # Requests
            self.assertEqual(row[2], "1")        # With usage
            self.assertEqual(row[10], "50.0%")   # Coverage

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
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
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
        conn.execute("INSERT INTO agent_usage(call_id, agent_type, kind, ts)"
                     " VALUES('a1','myagent',NULL,?)", (now - 3600_000,))
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-agents", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[0], "myagent")
            self.assertEqual(row[1], "-")   # NULL kind, not ""

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
            self.assertEqual(sel.value, "7d")        # Tools default
            sel.value = "all"                        # widen only Tools
            await pilot.pause()
            self.assertEqual(app.tab_range["tab-tools"], "all")
            tc.active = "tab-skills"                 # Skills keeps its own default
            await pilot.pause()
            self.assertEqual(sel.value, "7d")
            self.assertEqual(app.tab_range["tab-skills"], "7d")
            tc.active = "tab-usage"                  # Usage keeps its own default
            await pilot.pause()
            self.assertEqual(sel.value, "24h")
            self.assertEqual(app.tab_range["tab-usage"], "24h")
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


if __name__ == "__main__":
    unittest.main()

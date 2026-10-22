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
from datetime import date, datetime
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database
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


def make_db(path, responses, session_token=12345, cache=None, ptotals=None,
            providers=None, credits=None, projects=None):
    """Build a tracker DB with model_responses rows (and a tokenDelta session).

    ``responses`` rows: (mid, sid, model, prompt, completion, cache_r, cache_w,
    ts, usage). ``cache`` optionally maps mid -> (hit, miss, write) for the v3
    ``prompt_cache_*`` columns (defaults to NULL). ``ptotals`` optionally maps
    mid -> provider_total_tokens (defaults to NULL). ``providers`` optionally
    maps mid -> provider (defaults to NULL, the "unknown" origin). ``credits``
    maps mid -> rawUsage.credit (defaults to NULL), and ``projects`` maps mid ->
    the decoded cwd (defaults to NULL).
    """
    cache = cache or {}
    ptotals = ptotals or {}
    providers = providers or {}
    credits = credits or {}
    projects = projects or {}
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
            "(message_id, session_id, model, provider, prompt_tokens,"
            " completion_tokens, cache_read_input_tokens,"
            " cache_creation_input_tokens, prompt_cache_hit_tokens,"
            " prompt_cache_miss_tokens, prompt_cache_write_tokens,"
            " provider_total_tokens, credit, ts, usage_available, project)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, sid, model, providers.get(mid), pt, ct, cr, cw, hit, miss,
             write, ptotal, credits.get(mid), ts, ua, projects.get(mid)),
        )
    conn.commit()
    conn.close()


def all_rows(table):
    return [table.get_row_at(i) for i in range(table.row_count)]


def active_note(app):
    """The active tab's summary line (each pane owns its own note)."""
    pane = app.query_one(tui.TabbedContent).active
    return app.query_one("#usage-note" if pane == "tab-usage"
                         else f"#note-{pane}")


# Textual's default test terminal is 80x24, and below the width where a table's
# columns fit, the column plan drops the tail (see test 58). A case that reads
# cells by position therefore has to ask for a terminal wide enough to show all
# of them, otherwise it asserts against a table that was never meant to be full.
WIDE = (220, 40)

# A fixed instant the calendar cases pin the clock to, so "today", the month
# they open on and the preset spans are all deterministic.
FIXED = datetime(2026, 10, 12, 10, 33)
FIXED_TS = FIXED.timestamp()
FIXED_MS = int(FIXED_TS * 1000)


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


def insert_plugin_usage(path, rows):
    """rows: (plugin, marketplace, kind, target, session_id, project, ts)."""
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO plugin_usage(plugin, marketplace, kind, target,"
        " session_id, project, ts) VALUES(?,?,?,?,?,?,?)", rows)
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
            self.assertEqual(row[1], "Built-in")   # route absent -> built-in
            self.assertEqual(row[2], "-")    # Usage Total (no API parts)
            self.assertEqual(row[3], "-")    # Credits (no billing field)
            self.assertEqual(row[4], "1")    # Requests (the row exists)
            # Input / Output / API Total / Cache / hit-rate cells are "-", never "0"
            for i in range(5, 12):
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
            # Order: Model · Provider · Usage Total · Credits · Requests · Input ·
            #        Output · API Total · Cache hit · Cache miss · Cache write ·
            #        Cache hit rate · Last used
            self.assertEqual(row[1], "Built-in")   # route absent -> built-in
            self.assertEqual(row[2], "37")   # Usage Total = 10 + 20 + 7
            self.assertEqual(row[3], "-")    # Credits (absent, not 0)
            self.assertEqual(row[4], "1")    # Requests
            self.assertEqual(row[5], "10")   # Input  = prompt_tokens
            self.assertEqual(row[6], "20")   # Output = completion_tokens
            self.assertEqual(row[7], "30")   # API Total = Input + Output
            self.assertEqual(row[8], "7")    # Cache hit
            self.assertEqual(row[9], "8")    # Cache miss
            self.assertEqual(row[10], "9")   # Cache write
            self.assertEqual(row[11], "29.2%")   # 7 / (7+8+9)

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
            self.assertEqual(rows["zeroed"][8], "0")     # real 0
            self.assertEqual(rows["zeroed"][9], "0")
            self.assertEqual(rows["zeroed"][10], "0")
            self.assertEqual(rows["nulled"][8], "-")     # NULL, not 0
            self.assertEqual(rows["nulled"][9], "-")
            self.assertEqual(rows["nulled"][10], "-")

    # 5c. Total is not inflated by cache hit/miss
    async def test_total_excludes_cache(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("m1", "s1", "m", 10, 20, 0, 0, now - 3600_000, 1),
        ], cache={"m1": (999999, 888888, 7)})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            row = all_rows(t)[0]
            self.assertEqual(row[7], "30")           # API Total, not +cache
            self.assertEqual(row[8], "999,999")      # cache hit still shown

    # 5d. Tokens tab has exactly the expected columns
    async def test_tokens_tab_columns(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertEqual(labels, ["Model", "Provider", "Usage Total",
                                      "Credits", "Requests", "Input", "Output",
                                      "API Total", "Cache hit", "Cache miss",
                                      "Cache write", "Cache hit rate",
                                      "Last used"])

    # 5e. the Provider column shows Built-in / a vendor
    async def test_tokens_shows_provider_origin(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "built-in-model", 1, 1, 0, 0, now - 3600_000, 1),
            ("b", "s1", "custom-alias", 1, 1, 0, 0, now - 3600_000 + 1, 1),
            ("c", "s1", "unknown-route", 1, 1, 0, 0, now - 3600_000 + 2, 1),
        ], providers={"a": "builtin", "b": "Volcano Ark"})
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tokens", tui.DataTable)
            by_model = {r[0]: r for r in all_rows(t)}
            self.assertEqual(by_model["built-in-model"][1], "Built-in")
            self.assertEqual(by_model["custom-alias"][1], "Volcano Ark")
            # No requestModelId -> built-in, never a second, unattributable row.
            self.assertEqual(by_model["unknown-route"][1], "Built-in")

    # 5f. the Usage tab carries the Provider column too
    async def test_usage_tab_has_provider_column(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-usage", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertEqual(labels[:3], ["Time", "Model", "Provider"])

    # 5g. the date picker: "Custom…" opens it, Confirm sets a custom window
    async def test_date_picker_sets_a_custom_window(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, tui.DateRangeModal)
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-date", tui.Input).value = "2026-10-01"
            modal.query_one("#dr-start-time", tui.Input).value = "08:00"
            modal.query_one("#dr-end-date", tui.Input).value = "2026-10-03"
            modal.query_one("#dr-end-time", tui.Input).value = "18:30"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            win = app.tab_range["tab-tools"]
            self.assertIsInstance(win, db.CustomWindow)
            self.assertFalse(win.follow_now)
            start, end = app._bounds_for("tab-tools")
            fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
            self.assertEqual(fmt(start), "2026-10-01 08:00")
            self.assertEqual(fmt(end), "2026-10-03 18:30")
            # the Select reads "Custom…", and the 📅 control carries the span
            self.assertEqual(app.query_one("#range", tui.Select).value, "custom")
            # the 📅 control carries the chosen span (month-day, no year)
            self.assertIn("10-01 08:00 → 10-03 18:30",
                          str(app.query_one("#date-range", tui.Button).label))

    # 5h. a custom window is kept per tab, and "follow now" pins the end
    async def test_custom_window_is_per_tab_and_can_follow_now(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#dr-start-date", tui.Input).value = "2026-10-01"
            modal.query_one("#dr-ok", tui.Button).press()   # follow stays on
            await pilot.pause()
            self.assertTrue(app.tab_range["tab-tools"].follow_now)
            start, end = app._bounds_for("tab-tools")
            self.assertEqual(
                datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d"),
                "2026-10-01")
            self.assertLess(abs(end - app._now_ms()), 5000)   # end ≈ now
            # another tab is untouched
            self.assertEqual(app.tab_range["tab-skills"], "7d")

    # 5i. Cancel and a bad date both leave the window alone
    async def test_date_picker_cancel_and_invalid_input(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-date", tui.Input).value = "not-a-date"
            modal.query_one("#dr-end-date", tui.Input).value = "2026-10-03"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen_stack[-1], tui.DateRangeModal,
                                  "invalid input must not close the picker")
            modal.query_one("#dr-cancel", tui.Button).press()
            await pilot.pause()
            self.assertEqual(app.tab_range["tab-tools"], "7d")   # unchanged
            self.assertEqual(app.query_one("#range", tui.Select).value, "7d")

    # 5j. clicking a card aims the calendar; a day then fills that card in
    async def test_date_picker_calendar_fills_the_active_card(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            # the START card is the target first; click the 5th
            self.assertEqual(modal._active, "start")
            self.assertIn("-active", modal.query_one("#dr-start-card").classes)
            idx = (date(2026, 10, 5) - modal._grid_start()).days
            await pilot.click(f"#dr-day-{idx}")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-start-date", tui.Input).value,
                             "10/05/2026")
            # clicking the END card moves the target, and picking a day there
            # switches "follow now" off (a fixed end was clearly intended)
            await pilot.click("#dr-end-card .dr-card-title")
            await pilot.pause()
            self.assertEqual(modal._active, "end")
            self.assertIn("-active", modal.query_one("#dr-end-card").classes)
            idx2 = (date(2026, 10, 8) - modal._grid_start()).days
            await pilot.click(f"#dr-day-{idx2}")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-end-date", tui.Input).value,
                             "10/08/2026")
            self.assertFalse(modal.query_one("#dr-follow", tui.Checkbox).value)

    # 5k. the calendar steps a month, and a year at the year's ends
    async def test_date_picker_steps_months_and_wraps_the_year(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            month = lambda: str(modal.query_one("#dr-cal-month", tui.Static).render())
            self.assertEqual(month(), "October 2026")
            for _ in range(3):                       # Oct -> Jan, next year
                await pilot.click("#dr-next")
                await pilot.pause()
            self.assertEqual(month(), "January 2027")
            await pilot.click("#dr-prev")
            await pilot.pause()
            self.assertEqual(month(), "December 2026")
            # the cells before the 1st are greyed days of the previous month
            self.assertIn("-other",
                          modal.query_one("#dr-day-0", tui.Static).classes)

    # 5l. today is marked, and the highlight follows the active card's date
    async def test_date_picker_marks_today_and_the_active_selection(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            cells = {modal._day_at(i): i for i in range(modal.CELLS)}
            today = cells[date(2026, 10, 12)]
            self.assertIn("-today",
                          modal.query_one(f"#dr-day-{today}", tui.Static).classes)
            # the START card opens on the 7d window's first day, and that cell
            # is the marked one
            start = modal.query_one("#dr-start-date", tui.Input).value
            self.assertEqual(start, "10/06/2026")
            self.assertIn("-selected",
                          modal.query_one(f"#dr-day-{cells[date(2026, 10, 6)]}",
                                          tui.Static).classes)
            # aiming at END re-marks the end date instead
            modal._set_active("end")
            marked = [i for i in range(modal.CELLS)
                      if "-selected" in modal.query_one(f"#dr-day-{i}",
                                                        tui.Static).classes]
            self.assertEqual([modal._day_at(i) for i in marked],
                             [date(2026, 10, 12)])

    # 5m. a time field can be nudged a minute, keyboard or wheel
    async def test_time_field_nudges_by_a_minute(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            field = modal.query_one("#dr-start-time", tui.TimeInput)
            field.value = "23:59"
            field.action_nudge_up()
            self.assertEqual(field.value, "00:00")     # wraps past midnight
            field.action_nudge_down()
            self.assertEqual(field.value, "23:59")
            # the ↑ binding drives the same nudge
            field.focus()
            await pilot.press("up")
            self.assertEqual(field.value, "00:00")

    # 5m2. every calendar cell shows its whole day number
    async def test_every_calendar_day_renders_its_number(self):
        # A regression guard: at a 3-cell width Textual renders "30" as " 3 ",
        # so the calendar read as garbage while the clicks were still right.
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            for i in range(modal.CELLS):
                cell = modal.query_one(f"#dr-day-{i}", tui.Static)
                with self.subTest(cell=i, day=modal._day_at(i)):
                    self.assertEqual(cell.render_line(0).text.strip(),
                                     str(modal._day_at(i).day))

    # 5m3. the end fields are never disabled, so a click can always aim END
    async def test_clicking_an_end_field_switches_the_target(self):
        # Disabling the end fields while "follows now" was on made them swallow
        # the click, so the calendar could not be aimed at END at all.
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            self.assertTrue(modal.query_one("#dr-follow", tui.Checkbox).value)
            for wid in ("#dr-end-date", "#dr-end-time"):
                self.assertFalse(modal.query_one(wid, tui.Input).disabled,
                                 f"{wid} must stay clickable")
            await pilot.click("#dr-end-date")
            await pilot.pause()
            self.assertEqual(modal._active, "end")
            # and the day click now lands in END
            idx = (date(2026, 10, 9) - modal._grid_start()).days
            await pilot.click(f"#dr-day-{idx}")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-end-date", tui.Input).value,
                             "10/09/2026")

    # 5m4. editing an end field turns "follows now" off by itself
    async def test_editing_an_end_field_drops_follow_now(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            follow = modal.query_one("#dr-follow", tui.Checkbox)
            self.assertTrue(follow.value)
            # the prefill's own write must not count as an edit
            modal._set_field("#dr-end", FIXED_MS)
            await pilot.pause()
            self.assertTrue(follow.value, "a programmatic write dropped follow")
            # a typed time does
            modal.query_one("#dr-end-time", tui.TimeInput).value = "7:15 PM"
            await pilot.pause()
            self.assertFalse(follow.value)

    # 5n. a loose date and time are understood, not rejected
    async def test_date_picker_accepts_loose_dates_and_times(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-date", tui.Input).value = "10-1"
            modal.query_one("#dr-start-time", tui.Input).value = "9:5 AM"
            modal.query_one("#dr-end-date", tui.Input).value = "2026-10-03"
            modal.query_one("#dr-end-time", tui.Input).value = "6:30 PM"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            start, end = app._bounds_for("tab-tools")
            fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
            self.assertEqual(fmt(start), "2026-10-01 09:05")
            self.assertEqual(fmt(end), "2026-10-03 18:30")

    # 5o. a preset chip sets the start and turns "follow now" on
    async def test_date_picker_preset_chip_and_all_time(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            await pilot.click("#dr-14d")
            await pilot.pause()
            self.assertTrue(modal.query_one("#dr-follow", tui.Checkbox).value)
            self.assertEqual(modal.query_one("#dr-start-date", tui.Input).value,
                             "09/29/2026")
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            start, _ = app._bounds_for("tab-tools")
            self.assertEqual(start, db.window_bounds("14d", FIXED_MS)[0])
            # "All time" clears the start; the end keeps following now. Reopen
            # through the 📅 control — the Select already reads "Custom…", so
            # re-picking it would post no change.
            await pilot.click("#date-range")
            await pilot.pause()
            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, tui.DateRangeModal)
            await pilot.click("#dr-all")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-start-date", tui.Input).value, "")
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            win = app.tab_range["tab-tools"]
            self.assertIsNone(win.start_ms)
            self.assertTrue(win.follow_now)

    # 5p. the shortcut chips fit their column, whole
    async def test_date_picker_chips_fit_their_column(self):
        # The six chips span x=2..53 (52 cells) and the column was 47 wide, so
        # "All time" ran 6 cells past its edge. It is 53 now, one cell spare.
        # The quick-time rows under the cards share that column, so they are
        # held to the same budget.
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            rows = {
                "#dr-presets": ["Today", "1d", "7d", "14d", "30d", "All time"],
                "#dr-start-quick": ["00:00", "06:00", "12:00", "18:00", "23:59"],
                "#dr-end-quick": ["now", "00:00", "12:00", "23:59"],
            }
            for wid, labels in rows.items():
                row = modal.query_one(wid)
                self.assertEqual([str(c.label) for c in row.children], labels)
                for chip in row.children:
                    with self.subTest(row=wid, chip=str(chip.label)):
                        self.assertLessEqual(chip.region.right, row.region.right,
                                             "the chip runs past its column")
                        drawn = "".join(chip.render_line(y).text
                                        for y in range(chip.size.height))
                        self.assertIn(str(chip.label), drawn)

    # 5q. the 📅 span survives a tab switch at its full width
    async def test_the_date_label_keeps_the_whole_span_after_a_tab_switch(self):
        # A Button does not re-lay-out when its label grows, so the control kept
        # the 10 cells it was built with and drew "📅 10-06" until the next full
        # relayout — which a tab switch is. Check the width after one.
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            app.query_one("#range", tui.Select).value = "custom"
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-date", tui.Input).value = "10/01/2026"
            modal.query_one("#dr-end-date", tui.Input).value = "10/03/2026"
            # pin the end time too, or it stays on the prefill's "now"
            modal.query_one("#dr-end-time", tui.Input).value = "00:00"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-skills"
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            btn = app.query_one("#date-range", tui.Button)
            drawn = "".join(btn.render_line(y).text
                            for y in range(btn.size.height))
            self.assertIn("10-01 00:00 → 10-03 00:00", drawn)

    # --- 5r..5y: the picker's clock — 24-hour display, grids, quick chips ----

    async def _open_picker(self, app, pilot, pane="tab-tools"):
        """Open the picker on ``pane`` and return the modal it pushed, once mounted.

        ``push_screen`` only queues the screen; ``on_mount`` → ``_prefill`` writes
        the tab's current window into the fields. A caller that assigns values
        before that runs has them silently overwritten by the defaults — the
        modal is in ``screen_stack`` a pump cycle before it is mounted, which is
        why this waits on ``is_mounted`` rather than on a fixed pause.
        """
        app.query_one(tui.TabbedContent).active = pane
        await pilot.pause()
        app.query_one("#range", tui.Select).value = "custom"
        await pilot.pause()
        modal = app.screen_stack[-1]
        for _ in range(100):
            if modal.is_mounted:
                break
            await pilot.pause(0.01)
        assert modal.is_mounted, "the date-range modal never mounted"
        return modal

    # 5r. the fields show a 24-hour clock, so a day's start reads 00:00
    async def test_the_picker_shows_times_in_24_hour(self):
        # These fields used to render "%I:%M %p", so midnight read "12:00 AM"
        # and the obvious way to write it — "00:00 AM" — was rejected outright.
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            # the 7d window opens on its first day, at that day's very start
            self.assertEqual(modal.query_one("#dr-start-date", tui.Input).value,
                             "10/06/2026")
            self.assertEqual(modal.query_one("#dr-start-time", tui.Input).value,
                             "00:00")
            self.assertEqual(modal.query_one("#dr-end-time", tui.Input).value,
                             "10:33")            # now, not "10:33 AM"

    # 5s. typing 00:00 is accepted and means midnight
    async def test_typing_00_00_is_midnight(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-date", tui.Input).value = "10/01/2026"
            modal.query_one("#dr-start-time", tui.Input).value = "00:00"
            modal.query_one("#dr-end-date", tui.Input).value = "10/03/2026"
            modal.query_one("#dr-end-time", tui.Input).value = "00:00"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            self.assertIsInstance(app.tab_range["tab-tools"], db.CustomWindow)
            start, end = app._bounds_for("tab-tools")
            self.assertEqual(start, int(datetime(2026, 10, 1).timestamp() * 1000))
            self.assertEqual(end, int(datetime(2026, 10, 3).timestamp() * 1000))

    # 5t. the hour grid writes the active card, and does not steal the target
    async def test_the_hour_grid_fills_the_active_card(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            self.assertEqual(modal._active, "start")
            await pilot.click("#dr-hour-9")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-start-time", tui.Input).value,
                             "09:00")
            self.assertEqual(modal._active, "start",
                             "clicking an hour must not re-aim the calendar")
            # aim END, then the same grid writes there and fixes the end. The
            # clicked hour replaces the hour only — the minute rides along.
            await pilot.click("#dr-end-card .dr-card-title")
            await pilot.pause()
            await pilot.click("#dr-hour-23")
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-end-time", tui.Input).value,
                             "23:33")
            self.assertFalse(modal.query_one("#dr-follow", tui.Checkbox).value)

    # 5u. the hour grid marks the active card's hour, and follows it
    async def test_the_hour_grid_marks_the_active_cards_hour(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            marked = lambda: [h for h in range(modal.HOURS)
                              if "-selected" in modal.query_one(
                                  f"#dr-hour-{h}", tui.Static).classes]
            self.assertEqual(marked(), [0])          # START opens at 00:00
            modal._set_active("end")
            await pilot.pause()
            self.assertEqual(marked(), [10])         # END follows now (10:33)
            await pilot.click("#dr-hour-9")
            await pilot.pause()
            self.assertEqual(marked(), [9])

    # 5v. the END card's quick buttons offer both ends of the choice
    async def test_the_end_quick_buttons_offer_now_and_end_of_day(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            follow = modal.query_one("#dr-follow", tui.Checkbox)
            await pilot.click("#dr-end-quick-3")     # "23:59"
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-end-time", tui.Input).value,
                             "23:59")
            self.assertFalse(follow.value, "a fixed time must drop follow")
            await pilot.click("#dr-end-quick-0")     # "now"
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-end-time", tui.Input).value,
                             "10:33")
            self.assertTrue(follow.value, "now must put follow back on")

    # 5w. the START card's quick buttons fill the day's beginning
    async def test_the_start_quick_buttons_fill_the_beginning_of_day(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            modal.query_one("#dr-start-time", tui.Input).value = "07:30"
            await pilot.pause()
            await pilot.click("#dr-start-quick-0")   # "00:00"
            await pilot.pause()
            self.assertEqual(modal.query_one("#dr-start-time", tui.Input).value,
                             "00:00")
            # a start chip never touches the end's follow switch
            self.assertTrue(modal.query_one("#dr-follow", tui.Checkbox).value)

    # 5x. the step selector changes what ↑/↓ and the wheel move by
    async def test_the_nudge_step_selector_changes_the_step(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            field = modal.query_one("#dr-start-time", tui.TimeInput)
            field.value = "10:00"
            await pilot.pause()
            await pilot.click("#dr-step-60")         # an hour
            await pilot.pause()
            self.assertEqual(field.step, 60)
            self.assertIn("-selected",
                          modal.query_one("#dr-step-60", tui.Button).classes)
            field.focus()
            await pilot.press("up")
            self.assertEqual(field.value, "11:00")
            await pilot.click("#dr-step-10")         # ten minutes
            await pilot.pause()
            self.assertEqual(field.step, 10)
            self.assertNotIn("-selected",
                             modal.query_one("#dr-step-60", tui.Button).classes)
            field.focus()
            await pilot.press("down")
            self.assertEqual(field.value, "10:50")

    # 5y. a bad time names the 24-hour form it wants
    async def test_a_bad_time_message_names_the_24_hour_form(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path), clock=lambda: FIXED_TS)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            modal = await self._open_picker(app, pilot)
            seen = []
            modal.notify = lambda message, **kw: seen.append(message)
            modal.query_one("#dr-follow", tui.Checkbox).value = False
            modal.query_one("#dr-start-time", tui.Input).value = "half past ten"
            modal.query_one("#dr-end-date", tui.Input).value = "10/03/2026"
            modal.query_one("#dr-end-time", tui.Input).value = "00:00"
            modal.query_one("#dr-ok", tui.Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen_stack[-1], tui.DateRangeModal,
                                  "a bad time must not close the picker")
            self.assertTrue(seen, "the refusal must say something")
            self.assertIn("HH:MM", seen[0])
            self.assertIn("24-hour", seen[0])


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
            # The launch sync has its own case; start this one from a clean call
            # list so it measures `r`, not mount.
            await app.workers.wait_for_complete()
            calls.clear()
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(calls, [str(self.db_path)])
            self.assertTrue(captured["quiet"])
            # Tokens tab still renders after the refresh
            t = app.query_one("#t-tokens", tui.DataTable)
            self.assertTrue(any(r[0] == "m" for r in all_rows(t)))

    # 8. S cycles the active tab's sort; it never starts a sync
    async def test_s_cycles_the_active_tab_sort(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("a", "s1", "/p", "Alpha", "builtin", now - 60_000, 10, "completed"),
            ("b", "s1", "/p", "Beta", "builtin", now - 3_600_000, 10, "completed"),
            ("c", "s1", "/p", "Beta", "builtin", now - 3_500_000, 10, "completed"),
        ])
        app = TrackerApp(str(self.db_path))
        app.sync_mod = SimpleNamespace(run=lambda **kw: {"files_indexed": 0})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            note = active_note(app)
            self.assertEqual([r[0] for r in all_rows(t)], ["Beta", "Alpha"])
            self.assertIn("sort: tab order", str(note.content))
            await pilot.press("s")          # calls
            await pilot.press("s")          # recent
            await pilot.press("s")          # name
            await pilot.pause()
            self.assertIn("sort: name", str(note.content))
            self.assertEqual([r[0] for r in all_rows(t)], ["Alpha", "Beta"])
            await pilot.press("s")          # fails
            await pilot.press("s")          # tab order
            await pilot.pause()
            self.assertIn("sort: tab order", str(note.content))
            self.assertEqual([r[0] for r in all_rows(t)], ["Beta", "Alpha"])

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
            # Mount has its own launch sync; measure the two explicit requests.
            await app.workers.wait_for_complete()
            state.update(active=0, max=0, calls=0)
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
            # Order: Time · Model · Provider · Usage Total · Credits · Input ·
            #        Output · API Total · Cache hit · Cache miss · Cache write ·
            #        Cache hit rate
            self.assertEqual(row[1], "m")            # model
            self.assertEqual(row[2], "Built-in")      # route absent -> built-in
            self.assertEqual(row[3], "1,000,029")    # Usage Total = 10+20+999999
            self.assertEqual(row[4], "-")            # Credits (absent, not 0)
            self.assertEqual(row[5], "10")           # Input
            self.assertEqual(row[6], "20")           # Output
            self.assertEqual(row[7], "30")           # API Total, not +cache
            self.assertEqual(row[8], "999,999")      # Cache hit (read)
            self.assertEqual(row[9], "888,888")      # Cache miss
            self.assertEqual(row[10], "7")           # Cache write (create)
            # 999999 / (999999 + 888888 + 7) = 52.9%
            self.assertEqual(row[11], "52.9%")       # Cache hit rate

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
            for wid in ("#sum-requests", "#sum-sessions", "#sum-projects",
                        "#sum-credits", "#sum-input", "#sum-total",
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
            self.assertEqual(row[1], "Built-in")  # route absent -> built-in
            self.assertEqual(row[2], "2")        # Usage Total = 1 + 1
            self.assertEqual(row[3], "-")        # Credits (absent, not 0)
            self.assertEqual(row[4], "2")        # Requests

    # 23b. credits are shown on every page the provider reports them for
    async def test_credits_show_on_tokens_usage_and_dashboard(self):
        now = self._noon_ms()
        make_db(self.db_path, [
            ("a", "s1", "paid", 10, 2, 0, 0, now - 3600_000, 1),
            ("b", "s1", "free", 20, 4, 0, 0, now - 3500_000, 1),
        ], credits={"a": 1.5}, projects={"a": "/work/one", "b": "/work/two"})
        app = self._pinned_app(now)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            tokens = {r[0]: r for r in all_rows(
                app.query_one("#t-tokens", tui.DataTable))}
            self.assertEqual(tokens["paid"][3], "1.50")
            self.assertEqual(tokens["free"][3], "-")   # absent, never 0
            await self._usage_all_time(app, pilot)
            logs = {r[1]: r for r in all_rows(
                app.query_one("#t-usage", tui.DataTable))}
            self.assertEqual(logs["paid"][4], "1.50")
            self.assertEqual(logs["free"][4], "-")
            # Dashboard: both panels and the project board carry the credit.
            self.assertIn("1.50", str(app.query_one("#dash-sum-credits").content))
            self.assertIn("1.50", str(app.query_one("#dash-tok-credits").content))
            self.assertIn(" cr", str(app.query_one("#dash-models").content))
            board = {r[0]: r for r in all_rows(
                app.query_one("#dash-projects", tui.DataTable))}
            self.assertEqual(board["/work/one"][3], "1.50")
            self.assertEqual(board["/work/two"][3], "-")

    # 24. refreshing the visible tab never queries the hidden Requests page
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
                app._refresh_active_tab()
                await pilot.pause()
                self.assertEqual(calls["n"], 0)     # tools tab -> no usage query
                tc.active = "tab-usage"
                await pilot.pause()
                calls["n"] = 0
                app._refresh_active_tab()
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

    # 28b. one launch sync, and no background timer that redraws the page
    async def test_one_launch_sync_and_no_background_timer(self):
        make_db(self.db_path, [])
        requested = []

        def fake_request_sync(self):
            requested.append(self)

        with mock.patch.object(TrackerApp, "_request_sync", fake_request_sync):
            app = TrackerApp(str(self.db_path))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(requested, [app])
                periods = [t._interval for t in app._timers]
                self.assertFalse(
                    any(period in (5, 5.0, 30, 30.0) for period in periods),
                    f"a background timer survived: {periods}")

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
            # Read the cell by its header, not its index: a column inserted
            # ahead of `avg` (pend was) silently moved what this assertion was
            # looking at, and it passed on a different cell.
            header = [str(c.label) for c in t.columns.values()]
            cell = dict(zip(header, row))
            self.assertEqual(cell["tool"], "toolX")
            self.assertEqual(cell["avg"], "-")   # NULL avg, not ""

    # 35b. a call that never came back is counted, not lost between ok and fail
    async def test_a_call_with_no_result_record_is_counted_as_pending(self):
        """ok + fail fell short of calls, and nothing said why.

        A call with no result record is a session killed mid-call: not a
        failure, not a success, and until `pend` existed it was simply absent
        from the row it belonged to. The headless `cbut tools` had shown the
        column all along, so the two surfaces disagreed about the same table."""
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now - 60_000, 100, "completed"),
            ("c2", "s1", "/p", "Bash", "builtin", now - 50_000, 200, "incomplete"),
            ("c3", "s1", "/p", "Bash", "builtin", now - 40_000, None, None)])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            header = [str(c.label) for c in t.columns.values()]
            cell = dict(zip(header, all_rows(t)[0]))
            self.assertEqual(cell["pend"], "1")
            self.assertEqual(
                int(cell["ok"]) + int(cell["fail"]) + int(cell["pend"]),
                int(cell["calls"]), "the three outcomes must account for every call")

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
            # Default entity range is 7d, and the default view is used-only:
            # Old's calls (200h ago) fall outside 7d and count 0, so the row
            # is hidden — the note line is what names the hidden half.
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(set(rows), {"Recent"})
            self.assertEqual(rows["Recent"][1], "1")
            # The note names the visible count and the sort in force. The
            # hidden half is only counted once `a` includes it.
            note = str(active_note(app).content)
            self.assertIn("1 row", note)
            self.assertIn("sort: tab order", note)
            # a includes the installed-but-unused half; Old shows its 0.
            await pilot.press("a")
            await pilot.pause()
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(set(rows), {"Recent", "Old"})
            self.assertEqual(rows["Old"][1], "0")
            # and the window only ever shrinks the counts, never the list
            app.query_one("#range", tui.Select).value = "all"
            await pilot.pause()
            rows = {r[0]: r for r in all_rows(t)}
            self.assertEqual(rows["Old"][1], "1")    # now counted
            self.assertEqual(set(rows), {"Recent", "Old"})

    # 39. / opens the name-filter bar; typing narrows live; Esc restores
    async def test_slash_filter_narrows_and_esc_restores(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("a", "s1", "/p", "Alpha", "builtin", now - 3600_000, 10,
             "completed"),
            ("b", "s1", "/p", "Beta", "builtin", now - 3600_000, 10,
             "completed"),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            self.assertEqual({r[0] for r in all_rows(t)}, {"Alpha", "Beta"})
            # / shows the bar and moves focus into it, so typing edits the
            # filter instead of tripping tab bindings
            await pilot.press("/")
            await pilot.pause()
            inp = app.query_one("#filter-input", tui.Input)
            self.assertTrue(inp.display)
            self.assertTrue(inp.has_focus)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual({r[0] for r in all_rows(t)}, {"Alpha"})
            note = str(active_note(app).content)
            self.assertIn('filter "l"', note)
            self.assertIn("1 of 2", note)

            # Esc closes the bar, clears the needle and restores every row
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(inp.display)
            self.assertEqual({r[0] for r in all_rows(t)}, {"Alpha", "Beta"})
            note = str(active_note(app).content)
            self.assertNotIn("filter", note)
            self.assertIn("2 rows", note)

    # 39b. the name-filter is per-tab and combines with the a-toggle
    async def test_name_filter_is_per_tab_and_combines_with_unused(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("a", "s1", "/p", "Alpha", "builtin", now - 3600_000, 10,
             "completed"),
            ("o", "s1", "/p", "Old", "builtin", now - 200 * 3600_000, 10,
             "completed"),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tc = app.query_one(tui.TabbedContent)
            tc.active = "tab-tools"
            await pilot.pause()

            inp = app.query_one("#filter-input", tui.Input)
            await pilot.press("/")
            await pilot.pause()
            self.assertTrue(inp.display)
            for ch in "old":
                await pilot.press(ch)
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            # used-only hides Old (0 calls in 7d) and the needle hides Alpha,
            # so the tab reads as its empty state, not as silently broken
            self.assertEqual([r[0] for r in all_rows(t)],
                             ["No tool calls yet"])
            note = str(active_note(app).content)
            self.assertIn('filter "old"', note)
            self.assertIn("0 of 2", note)
            # Enter applies: focus returns to the table, but the bar stays
            # visible because the filter is still on.
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(inp.display)
            self.assertEqual([r[0] for r in all_rows(t)],
                             ["No tool calls yet"])
            self.assertIn('filter "old"',
                          str(active_note(app).content))
            # a includes the unused half under the same needle; the table got
            # focus back on close, so the binding lands
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual({r[0] for r in all_rows(t)}, {"Old"})
            note = str(active_note(app).content)
            self.assertIn("incl. installed-unused", note)
            self.assertIn("1 of 2", note)
            # the needle rides its own tab: leaving and coming back,
            # the filter is still applied to Tools and nothing else
            tc = app.query_one(tui.TabbedContent)
            tc.active = "tab-skills"
            await pilot.pause()
            tc.active = "tab-tools"
            await pilot.pause()
            self.assertEqual({r[0] for r in all_rows(t)}, {"Old"})
            self.assertIn('filter "old"',
                          str(active_note(app).content))
    # 39c. filter edits start at the first match; the bar survives Enter
    async def test_filter_bar_stays_visible_and_anchors_the_list(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            (f"c{i}", "s1", "/p", f"Tool{i:02d}", "builtin", now - i * 1000,
             10, "completed") for i in range(30)
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            t = app.query_one("#t-tools", tui.DataTable)
            inp = app.query_one("#filter-input", tui.Input)
            t.focus()
            t.move_cursor(row=20)
            await pilot.pause()
            self.assertGreater(t.scroll_y, 0)

            await pilot.press("/")
            await pilot.pause()
            for ch in "tool":
                await pilot.press(ch)
                await pilot.pause()
            self.assertEqual(t.cursor_row, 0)
            self.assertEqual(t.scroll_y, 0,
                             "a live filter kept a deep cursor and scrolled away")
            note = str(active_note(app).content)
            self.assertIn('filter "tool"', note)

            for ch in "29":
                await pilot.press(ch)
                await pilot.pause()
            self.assertEqual([r[0] for r in all_rows(t)], ["Tool29"])
            self.assertIn('filter "tool29" · 1 of 30', str(
                active_note(app).content))

            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(inp.display,
                            "Enter hid a filter that is still applied")
            self.assertEqual(app.focused.id, "t-tools")

            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(inp.display)
            self.assertEqual(len(all_rows(t)), 30)

    # 39d. Requests filters in SQL and sorts over the whole window
    async def test_requests_filter_and_sort(self):
        now = int(time.time() * 1000)
        make_db(self.db_path, [
            ("a", "s1", "alpha", 100, 10, 0, 0, now - 60_000, 1),
            ("b", "s1", "beta", 1, 1, 0, 0, now - 30_000, 1),
        ])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            t = app.query_one("#t-usage", tui.DataTable)
            note = app.query_one("#usage-note")
            self.assertIn("sort: time", str(note.content))
            self.assertEqual([r[1] for r in all_rows(t)], ["beta", "alpha"])
            await pilot.press("s")
            await pilot.pause()
            self.assertIn("sort: tokens", str(note.content))
            self.assertEqual([r[1] for r in all_rows(t)], ["alpha", "beta"])

            await pilot.press("/")
            await pilot.pause()
            for ch in "alpha":
                await pilot.press(ch)
                await pilot.pause()
            self.assertEqual([r[1] for r in all_rows(t)], ["alpha"])
            self.assertIn('filter "alpha" · 1 matches', str(note.content))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(all_rows(t)), 2)

    # 39e. the page never scrolls into blank space when its content fits
    async def test_the_main_screen_cannot_scroll_when_its_content_fits(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app.screen.max_scroll_y, 0)
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            self.assertEqual(app.screen.max_scroll_y, 0)

    # 39f. a pushed detail screen does not inherit the app's shortcuts
    async def test_a_pushed_detail_screen_carries_no_app_shortcuts(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(tui.HistoryScreen("tool", "bash"))
            await pilot.pause()
            bindings = app.screen.active_bindings
            keys = set(bindings)
            self.assertTrue(any(k in keys for k in ("q", "escape", "escape,q")),
                            keys)
            actions = {b.binding.action for b in bindings.values()}
            self.assertFalse(actions & tui.TrackerApp.APP_SHORTCUT_ACTIONS,
                             actions & tui.TrackerApp.APP_SHORTCUT_ACTIONS)

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

    # 39. Plugins: no version column, all-time list, windowed counts
    async def test_plugins_list_is_all_time_but_its_counts_follow_the_window(self):
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
            "'/p',?)", (now - 10 * 24 * 3600_000,))   # older than the 7d default
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-plugins"
            await pilot.pause()
            t = app.query_one("#t-plugins", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertNotIn("version", labels)
            # the range control is live on this tab, like every other one
            self.assertTrue(app.query_one("#range", tui.Select).display)
            self.assertEqual(app.tab_range["tab-plugins"], "7d")
            # 100 hours ago: outside the default week, inside "all". The row
            # is never dropped by the window — but the used-only default hides
            # it while it counts 0, so the a toggle is what brings it back.
            self.assertEqual(all_rows(t)[0][0], "No plugins installed")
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(all_rows(t)[0][1], "0")
            app.tab_range["tab-plugins"] = "all"
            app._refresh_active_tab()
            await pilot.pause()
            row = all_rows(t)[0]
            self.assertEqual(row[0], "myplug")
            self.assertEqual(row[1], "1")

    # 39b. the Skills tab has no plugin column (name is the key)
    async def test_skills_tab_has_no_plugin_column(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            t = app.query_one("#t-skills", tui.DataTable)
            labels = [str(c.label) for c in t.columns.values()]
            self.assertNotIn("plugin", labels)

    # 40. Dashboard: a grid of panel rows plus the Projects board
    async def test_dashboard_layout(self):
        make_db(self.db_path, [])
        app = TrackerApp(str(self.db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            for rid in ("#dash-row-1", "#dash-row-2", "#dash-row-3",
                        "#dash-row-4"):
                self.assertTrue(app.query_one(rid))
            for pid in ("#panel-dash-kpi", "#panel-dash-tokens",
                        "#panel-dash-cache", "#panel-dash-models",
                        "#panel-dash-tools", "#panel-dash-runtime",
                        "#panel-dash-activity", "#panel-dash-projects"):
                self.assertTrue(app.query_one(pid))
            for wid in ("#dash-sum-requests", "#dash-sum-credits",
                        "#dash-sum-sessions", "#dash-sum-projects",
                        "#dash-kpi-tools", "#dash-kpi-skills",
                        "#dash-kpi-agents", "#dash-kpi-mcp", "#dash-kpi-plugins",
                        "#dash-tok-usage", "#dash-tok-input",
                        "#dash-tok-output", "#dash-tok-api", "#dash-tok-credits",
                        "#dash-tok-hit-rate", "#dash-cache-hit",
                        "#dash-cache-miss", "#dash-cache-write",
                        "#dash-rt-completed", "#dash-rt-incomplete",
                        "#dash-rt-avg", "#dash-rt-sessions", "#dash-rt-projects",
                        "#dash-models", "#dash-tools", "#dash-projects",
                        "#dash-activity", "#dash-activity-axis", "#dash-note"):
                self.assertTrue(app.query_one(wid))
            # Only the Projects board is a table; the rest are text panels.
            self.assertEqual(len(app.query("#tab-dashboard DataTable")), 1)

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
            # A session killed mid-call: no result record, so no status. It is
            # the third outcome, and Completed + Incomplete + Pending has to
            # reach the Tool calls KPI or the panel is quietly short.
            ("c4", "s1", "/p", "Edit", "builtin", now - 3300_000, None, None),
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
            pend = str(app.query_one("#dash-rt-pending").content)
            self.assertTrue(pend.rstrip().endswith("1"), pend)
            kpi = str(app.query_one("#dash-kpi-tools").content)
            self.assertTrue(kpi.rstrip().endswith("4"), kpi)
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

    # 81. the unparsed count says *which* reasons, so the "!" can mean drift again
    async def test_the_status_line_only_shouts_about_a_reason_it_has_not_seen(self):
        """The bar used to print one total, and that total could not be read.

        The three prose types this build declines are tens of thousands of records
        on any real database, so `! 29,989 unparsed` was on screen every single day
        — and the one thing the number exists to say (a record type nothing here
        has been taught about) looked exactly like it. This pins both halves: the
        familiar reasons are still counted and still shown, and the alarm goes to
        the reason that is new.
        """
        now = int(time.time() * 1000)
        make_db(self.db_path, [("m1", "s1", "m", 1, 1, 0, 0, now, 1)])
        conn = db.open_db(self.db_path)
        for reason, n in (("type:reasoning", 22146), ("type:summary", 528)):
            conn.execute("INSERT INTO unparsed(reason, count) VALUES(?,?)",
                         (reason, n))
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path))
        # The launch sync's fixture host is deliberately absent; its failure
        # would add a second, unrelated `!` to the line this case reads.
        with mock.patch.object(TrackerApp, "_request_sync", lambda self: None):
            async with app.run_test() as pilot:
                await pilot.pause()
                status = str(app.query_one("#status").content)
                self.assertIn("22,674 records unparsed (2 known)", status)
                self.assertNotIn("!", status,
                                 "a known reason raised the alarm the alarm is for")

                conn = db.open_db(self.db_path)
                conn.execute("INSERT INTO unparsed(reason, count) VALUES(?,?)",
                             ("type:brand-new", 3))
                conn.commit()
                conn.close()
                # `refresh_data` is the full refresh (every tab + the status bar) —
                # `_refresh_active_tab` is not enough: it fills the visible tab and
                # leaves the bar showing its previous line.
                app.refresh_data()
                await pilot.pause()
                status = str(app.query_one("#status").content)
                self.assertIn("!", status)
                self.assertIn("3 records unparsed under a new reason: type:brand-new",
                              status)
                self.assertNotIn("22,674", status,
                                 "the alarm count is the new records, not the familiar "
                                 "ones it would otherwise be buried under")

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

    # 49b. a cut-short request log names the cut instead of implying it is whole
    async def test_a_truncated_request_log_says_how_many_it_is_showing(self):
        """The table holds 100 rows; the window had 120.

        A silently shortened list is the row-level version of the half column
        the wide plan refuses to render — a reader cannot tell "that is all"
        from "that is the first page". There is no page key: the footer stays
        identical on every tab, and the note names the cut.
        """
        now = self._noon_ms()
        make_db(self.db_path, [
            (f"m{i}", "s1", "alpha", 10, 5, 0, 0, now - 60_000, 1)
            for i in range(120)
        ])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-usage"
            await pilot.pause()
            note = str(app.query_one("#usage-note").content)
            self.assertIn("showing 100 of 120", note)
            self.assertIn("sort: time", note)
            self.assertNotIn("Pg", note)
            self.assertEqual(
                len(all_rows(app.query_one("#t-usage", tui.DataTable))), 100)
            await pilot.press("pagedown")
            await pilot.pause()
            self.assertEqual(
                len(all_rows(app.query_one("#t-usage", tui.DataTable))), 100)

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
        async with app.run_test(size=(220, 30)) as pilot:
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
            await pilot.resize_terminal(220, 30)
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
            _kind, _, rest = key.value.partition("\t")
            full, _, provider = rest.partition("\t")
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, t.cursor_row, key))
            await pilot.pause()
            await pilot.pause()
            top = app.screen_stack[-1]
            self.assertIsInstance(top, tui.ModelResponsesScreen,
                                  f"a cut table cannot open its detail screen ({top!r})")
            self.assertEqual((top.model, top.provider), (full, provider),
                             "the rebuilt rows lost the key that names their row")
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
                            # What remains keeps the registry's order; the
                            # *state* column and the row key are protected, so
                            # what is shown is a prefix plus those, never an
                            # arbitrary re-ordering. Every name that went away is
                            # on the note, and the row is whole.
                            self.assertEqual(
                                labels, [c for c in full if c in labels],
                                f"{t.id} shows columns out of order")
                            if db.ENTITY_STATE_COLUMN in full and len(labels) > app.MIN_SHOWN:
                                self.assertIn(db.ENTITY_STATE_COLUMN, labels,
                                              f"{t.id} dropped state instead of an "
                                              "optional column")
                            need = sum(c.content_width for c in t.columns.values()) \
                                + 2 * len(labels)
                            flat = " ".join(" ".join(
                                "".join(s.text for s in note.render_line(y))
                                for y in range(note.region.height)).split())
                            hidden = [c for c in full if c not in labels]
                            for name in hidden:
                                self.assertIn(name, flat,
                                              f"{name} is gone and unnamed")
                            self.assertIn(f"{len(labels)} of {len(full)}", flat,
                                          "the note's numbers are not the table's")
                            if need > t.region.width:
                                # Only legal at the floor: the protected columns
                                # (the row key plus state) are wider than the
                                # screen, so the note has to say so instead of the
                                # table pretending it fits.
                                self.assertLessEqual(len(labels), app.MIN_SHOWN + 1,
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
            labels = [str(c.label) for c in t.columns.values()]
            self.assertEqual(labels[:2], ["server", "tool"],
                             "the plan cut into the two columns the row key is built from")
            self.assertIn(db.ENTITY_STATE_COLUMN, labels,
                          "state was dropped while the key columns survived")
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
            "top_n": 5, "log_limit": 100, "detail_limit": 200, "name_cap": 12})
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
        built = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "DataTable"):
                continue
            for kw in node.keywords:
                if kw.arg != "id":
                    continue
                if isinstance(kw.value, ast.Constant):
                    built.add(kw.value.value)
                elif (isinstance(kw.value, ast.Attribute)
                      and kw.value.attr == "table_id"):
                    # Entity tables are built from the registry; the registry is
                    # what the test is checking, so name every one of them here.
                    built |= {t.table_id for t in tui.ENTITY_TABS}
        registered = (set(TrackerApp.WIDE_TABLES) | set(tui.HistoryScreen.WIDE_TABLES)
                      | set(tui.ModelResponsesScreen.WIDE_TABLES))
        self.assertGreaterEqual(len(built), 9,
                                f"only {len(built)} DataTable(id=…) found — the query broke")
        self.assertFalse(built - registered,
                         f"unplanned tables, free to clip: {sorted(built - registered)}")
        self.assertFalse(registered - built,
                         f"a registry names a table the source never builds: "
                         f"{sorted(registered - built)}")


    # 74. the drill-down header summarises the entity's windowed counts
    async def test_the_drilldown_header_summarises_the_window(self):
        now = self._noon_ms()
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p/a", "Bash", "builtin", now - 60_000, 120, "completed"),
            ("c2", "s2", "/p/b", "Bash", "builtin", now - 30_000, 80, "incomplete")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.push_screen(tui.HistoryScreen("tool", "Bash"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            text = " ".join(
                str(scr.query_one("#hist-summary", tui.Static).render()).split())
            for piece in ("2 calls", "1 ok", "1 fail", "0 pending", "avg 100ms"):
                with self.subTest(piece=piece):
                    self.assertIn(piece, text)

    # 75. selecting a call on the drill-down opens its own detail
    async def test_enter_on_a_drilldown_row_opens_the_call_detail(self):
        now = self._noon_ms()
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p/a", "Bash", "builtin", now, 120, "completed")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.push_screen(tui.HistoryScreen("tool", "Bash"))
            await pilot.pause()
            hist = app.screen_stack[-1]
            t = hist.query_one("#hist-table", tui.DataTable)
            key = list(t.rows.keys())[0]
            self.assertEqual(key.value, "call\ttool\tc1")
            app.on_data_table_row_selected(tui.DataTable.RowSelected(t, 0, key))
            await pilot.pause()
            top = app.screen_stack[-1]
            self.assertIsInstance(top, tui.CallDetailScreen)
            self.assertEqual((top.kind, top.key), ("tool", "c1"))

    # 76. the call detail shows every field, NULL rendered as "-"
    async def test_the_call_detail_shows_every_field(self):
        now = self._noon_ms()
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p/a", "Bash", "builtin", now, 120, "completed")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.push_screen(tui.CallDetailScreen("tool", "c1"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            labels = [str(w.render()).strip() for w in scr.query(".call-label")]
            values = [str(w.render()).strip() for w in scr.query(".call-value")]
            self.assertEqual(labels, ["call id", "tool", "category", "model",
                                      "status", "when", "duration", "session",
                                      "project"])
            self.assertEqual(values[0], "c1")
            self.assertEqual(values[1], "Bash")
            self.assertEqual(values[2], "builtin")
            self.assertEqual(values[3], "-", "a NULL field must read as '-'")
            self.assertEqual(values[4], "completed")
            self.assertEqual(values[6], "120ms")

    # 77. a long unbreakable value folds rather than clipping
    async def test_a_long_call_value_folds_instead_of_clipping(self):
        now = self._noon_ms()
        long_path = "/home/user/" + "x" * 70
        insert_tool_calls(self.db_path, [
            ("c1", "s1", long_path, "Bash", "builtin", now, 10, "completed")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            app.push_screen(tui.CallDetailScreen("tool", "c1"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            proj = [w for w in scr.query(".call-value")
                    if str(w.render()).strip() == long_path]
            self.assertEqual(len(proj), 1)
            self.assertGreaterEqual(proj[0].region.height, 2,
                                    "a long value was clipped, not folded")

    # 78. a model-response row opens the single-response detail
    async def test_a_model_response_row_opens_the_call_detail(self):
        now = self._noon_ms()
        make_db(self.db_path, [("mid1", "s1", "m", 10, 2, 0, 0, now, 1)])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.push_screen(tui.ModelResponsesScreen("m"))
            await pilot.pause()
            scr = app.screen_stack[-1]
            t = scr.query_one("#resp-table", tui.DataTable)
            key = list(t.rows.keys())[0]
            self.assertEqual(key.value, "call\tmodel\tmid1")
            app.on_data_table_row_selected(tui.DataTable.RowSelected(t, 0, key))
            await pilot.pause()
            det = app.screen_stack[-1]
            self.assertIsInstance(det, tui.CallDetailScreen)
            labels = [str(w.render()).strip() for w in det.query(".call-label")]
            self.assertIn("message id", labels)
            self.assertIn("api total", labels)

    # 79. Plugins drills down too — two levels, its own columns
    async def test_the_plugin_tab_drills_down_two_levels(self):
        now = self._noon_ms()
        insert_plugin_usage(self.db_path, [
            # an hour back, not exactly `now`: the tab's window ends at `now`
            # exclusive, so a row on the boundary would be filtered out
            ("myplug", "mp", "skill", "s/t", "s1", "/p/a", now - 3600_000)])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-plugins"
            await pilot.pause()
            t = app.query_one("#t-plugins", tui.DataTable)
            self.assertEqual(list(t.rows.keys())[0].value, "plugin\tmyplug")
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(t, 0, list(t.rows.keys())[0]))
            await pilot.pause()
            hist = app.screen_stack[-1]
            self.assertIsInstance(hist, tui.HistoryScreen)
            self.assertEqual(hist.kind, "plugin")
            ht = hist.query_one("#hist-table", tui.DataTable)
            self.assertEqual([str(c.label) for c in ht.columns.values()],
                             list(tui.PLUGIN_HISTORY_COLUMNS))
            hkey = list(ht.rows.keys())[0]
            self.assertTrue(hkey.value.startswith("call\tplugin\t"))
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(ht, 0, hkey))
            await pilot.pause()
            det = app.screen_stack[-1]
            self.assertIsInstance(det, tui.CallDetailScreen)
            values = [str(w.render()).strip() for w in det.query(".call-value")]
            for piece in ("myplug", "skill", "s/t", "mp"):
                with self.subTest(piece=piece):
                    self.assertIn(piece, values)

    # 80. the drill-down follows the tab's own window
    async def test_the_drilldown_follows_the_tab_window(self):
        now = self._noon_ms()
        insert_tool_calls(self.db_path, [
            ("in", "s1", "/p/a", "Bash", "builtin", now - 60_000, 10, "completed"),
            ("old", "s1", "/p/a", "Bash", "builtin",
             now - 10 * 24 * 3600 * 1000, 10, "completed")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            self.assertEqual(app.tab_range["tab-tools"], "7d")
            t = app.query_one("#t-tools", tui.DataTable)
            key = list(t.rows.keys())[0]
            app.on_data_table_row_selected(tui.DataTable.RowSelected(t, 0, key))
            await pilot.pause()
            hist = app.screen_stack[-1]
            self.assertIsInstance(hist, tui.HistoryScreen)
            self.assertEqual(hist.window_label, "7 days")
            self.assertIsNotNone(hist.start_ts, "the drill-down ignored the window")
            # the 10-day-old call is outside the Tools tab's 7-day window
            self.assertEqual(hist.query_one("#hist-table", tui.DataTable).row_count, 1)
            text = " ".join(
                str(hist.query_one("#hist-summary", tui.Static).render()).split())
            self.assertIn("1 call", text)

    # 76. every entity tab ends with the shared state column
    async def test_every_entity_tab_ends_with_the_state_column(self):
        """The header comes from cbut_db so it cannot drift from the labels.

        Five tab column tuples and five stats header tuples are six independent
        copies of "does this row still exist on disk". The *wording* is shared
        (db.entity_state); this pins that all five tabs also agree on the
        column's name, and that a filled row's last cell is one of the three
        labels rather than a number nobody decodes.
        """
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now - 60_000, 120, "completed"),
            ("c2", "s1", "/p", "Uninstalled", "builtin", now - 60_000, 120,
             "completed")])
        conn = db.open_db(self.db_path)
        conn.execute(
            "INSERT INTO inventory(kind, name, source)"
            " VALUES('tool','Bash','builtin')")
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            labels = {db.ENTITY_STATE_USED, db.ENTITY_STATE_INSTALLED,
                      db.ENTITY_STATE_REMOVED}
            for const, table_id in ((tui.TOOLS_COLUMNS, "#t-tools"),
                                    (tui.SKILLS_COLUMNS, "#t-skills"),
                                    (tui.AGENTS_COLUMNS, "#t-agents"),
                                    (tui.PLUGINS_COLUMNS, "#t-plugins"),
                                    (tui.MCP_COLUMNS, "#t-mcp")):
                with self.subTest(table=table_id):
                    self.assertEqual(const[-1], db.ENTITY_STATE_COLUMN)
                    table = app.query_one(table_id, tui.DataTable)
                    shown = [str(c.label) for c in table.columns.values()]
                    self.assertEqual(shown, list(const))
                    for row in all_rows(table):
                        # an empty tab renders one placeholder row: the empty
                        # message, then a dash per remaining column
                        if str(row[-1]) == "-":
                            continue
                        self.assertIn(str(row[-1]), labels)

    # 77. an entity that is no longer installed says so, and says it quietly
    async def test_a_removed_entity_reads_removed_and_is_dimmed(self):
        """The bug this exists for: a stale row was indistinguishable from a
        quiet one, because the only signal was calls=0. Both still read 0 — the
        label is what separates "never called" from "not there any more"."""
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "Bash", "builtin", now - 60_000, 1, "completed"),
            ("c2", "s1", "/p", "Uninstalled", "builtin", now - 60_000, 1,
             "completed")])
        conn = db.open_db(self.db_path)
        conn.execute(
            "INSERT INTO inventory(kind, name, source)"
            " VALUES('tool','Bash','builtin')")
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            table = app.query_one("#t-tools", tui.DataTable)
            cells = {str(r[0]): r[-1] for r in all_rows(table)}
            self.assertEqual(str(cells["Bash"]), db.ENTITY_STATE_USED)
            gone = cells["Uninstalled"]
            self.assertEqual(str(gone), db.ENTITY_STATE_REMOVED)
            self.assertEqual(gone.style, "dim")
            # the installed-but-uncalled half of the distinction
            self.assertNotIsInstance(gone, str)

    # 78. the state column survives the narrow plan
    async def test_the_state_column_survives_a_narrow_terminal(self):
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "a-really-long-tool-name-for-the-plan",
             "builtin", now - 60_000, 1, "completed")])
        conn = db.open_db(self.db_path)
        conn.execute("INSERT INTO inventory(kind, name, source)"
                     " VALUES('tool','a-really-long-tool-name-for-the-plan','builtin')")
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            table = app.query_one("#t-tools", tui.DataTable)
            labels = [str(c.label) for c in table.columns.values()]
            self.assertIn(db.ENTITY_STATE_COLUMN, labels,
                          "state was the first column dropped, which is the bug")
            note = app.query_one("#colnote-tools", tui.Static)
            flat = " ".join("".join(s.text for s in note.render_line(y))
                            for y in range(note.region.height)).split()
            self.assertIn("last", flat, "the note did not name what it hid")
            self.assertIn("used", flat, "the note did not name what it hid")

    # 79. a newline in a transcript-derived name is a display bug, not a layout
    async def test_a_control_character_in_a_name_renders_on_one_line(self):
        now = self._noon_ms()
        make_db(self.db_path, [])
        insert_tool_calls(self.db_path, [
            ("c1", "s1", "/p", "REPO\n<parameter name=", "builtin",
             now - 60_000, 1, "completed")])
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tools"
            await pilot.pause()
            table = app.query_one("#t-tools", tui.DataTable)
            cells = [str(c) for c in table.get_row_at(0)]
            self.assertEqual(cells[0], "REPO <parameter name=")
            self.assertNotIn("\n", "".join(cells))
            # header + one row: a newline in a cell used to add a line
            self.assertEqual(table.virtual_size.height, table.row_count + 1)

    # 80. the Tokens tab is usage-only: a catalogue row is not a model record
    async def test_tokens_does_not_list_an_uncalled_catalogue_model(self):
        now = self._noon_ms()
        make_db(self.db_path, [("m1", "s1", "used-model", 10, 2, 0, 0,
                                now - 60_000, 1)])
        conn = db.open_db(self.db_path)
        conn.execute("INSERT INTO inventory(kind, name, owner_plugin, source)"
                     " VALUES('model','available-model','builtin','builtin')")
        conn.commit()
        conn.close()
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tokens"
            await pilot.pause()
            table = app.query_one("#t-tokens", tui.DataTable)
            models = {str(r[0]) for r in all_rows(table)}
            self.assertEqual(models, {"used-model"})

    async def test_two_routes_for_one_model_do_not_collide(self):
        """The crash: one model answered twice, once without requestModelId.

        The row key used to be the model name alone, so ``Tokens`` raised
        ``DuplicateKey`` as soon as a model had rows from two routes (and All
        time was the window that included both). The key now carries the
        provider, and each row opens only its own responses.
        """
        now = self._noon_ms()
        make_db(self.db_path, [
            ("a", "s1", "shared", 10, 2, 0, 0, now - 60_000, 1),
            ("b", "s1", "shared", 5, 1, 0, 0, now - 50_000, 1),
            ("c", "s1", "shared", 1, 1, 0, 0, now - 40_000, 1),
        ], providers={"a": "builtin", "c": "Volcano Ark"})
        app = TrackerApp(str(self.db_path), clock=lambda: now / 1000)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.query_one(tui.TabbedContent).active = "tab-tokens"
            app.tab_range["tab-tokens"] = "all"
            await pilot.pause()
            table = app.query_one("#t-tokens", tui.DataTable)
            keys = {k.value for k in table.rows.keys()}
            self.assertEqual(keys, {"model\tshared\tbuiltin",
                                    "model\tshared\tVolcano Ark"})
            self.assertEqual(table.row_count, 2)
            # Opens the route the row was selected from, not the model's whole history.
            key = next(k for k in table.rows.keys()
                       if k.value.endswith("Volcano Ark"))
            app.on_data_table_row_selected(
                tui.DataTable.RowSelected(table, 0, key))
            await pilot.pause()
            top = app.screen_stack[-1]
            self.assertIsInstance(top, tui.ModelResponsesScreen)
            self.assertEqual((top.provider, top.window_label),
                             ("Volcano Ark", "All time"))
            self.assertEqual(
                top.query_one("#resp-table", tui.DataTable).row_count, 1)

    # 78b. a styled cell keeps its style through the name_cap
    def test_a_styled_cell_survives_the_name_cap(self):
        """name_cap=8 is inside the documented range, and "installed" is 9.

        Capping on str() would return a plain string, so a stale row would read
        as loudly on a narrow terminal as on a wide one.
        """
        from rich.text import Text

        class _Host(tui.WideTableMixin):
            name_cap = 8

        host = _Host()
        capped = host._cap_cell(Text(db.ENTITY_STATE_INSTALLED, style="dim"))
        self.assertEqual(str(capped), "install…")
        self.assertEqual(capped.style, "dim")
        short = host._cap_cell(Text(db.ENTITY_STATE_REMOVED, style="dim"))
        self.assertEqual(str(short), db.ENTITY_STATE_REMOVED)
        self.assertEqual(short.style, "dim")


class ModelLeaderLabelsTest(unittest.TestCase):
    """Dashboard Top models disambiguates one name served by two routes.

    q_usage_model_stats groups by (model, provider), so a model with two
    routes appears twice; bare duplicates read as a bug, so shared names
    get a provider suffix and single-route names stay bare.
    """

    def test_shared_name_gets_a_provider_suffix(self):
        rows = [
            {"model": "glm", "provider": None},
            {"model": "glm", "provider": "proxy.example"},
            {"model": "solo", "provider": None},
        ]
        self.assertEqual(tui.model_leader_labels(rows),
                         ["glm · -", "glm · proxy.example", "solo"])

    def test_single_route_keeps_the_bare_name(self):
        rows = [{"model": "solo", "provider": None}]
        self.assertEqual(tui.model_leader_labels(rows), ["solo"])


if __name__ == "__main__":
    unittest.main()

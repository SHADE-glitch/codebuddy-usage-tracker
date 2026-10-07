#!/usr/bin/env python3
"""cbut-tui — interactive viewer for the CodeBuddy usage tracker.

Tabs: Tools · Skills · Agents · Plugins · MCP · Tokens · Usage.

Requires ``textual`` (see requirements.txt). The headless reports in
cbut-stats.py need no third-party packages; this file is the only place that
imports textual, so importing it without textual fails with a clear message
instead of a traceback.
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import Screen
    from textual.widgets import (
        DataTable, Footer, Header, Select, Static, TabbedContent, TabPane,
    )

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover - exercised only without the dep
    HAVE_TEXTUAL = False


def ts(v):
    if not v:
        return "-"
    return datetime.fromtimestamp(v / 1000).strftime("%m-%d %H:%M")


def ms(v):
    if v is None:
        return "-"
    return f"{v}ms" if v < 1000 else f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count, showing ``-`` for NULL (never a fabricated 0)."""
    if v is None:
        return "-"
    return f"{v:,}"


def local_time(ms):
    """Local-time render of an epoch-ms value (query bounds stay in Unix ms)."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# Usage-page option list (values mirror cbut_db so the two stay in sync).
RANGE_OPTIONS = [(db.USAGE_RANGE_LABELS[k], k)
                 for k in ("24h", "48h", "72h", "7d", "30d", "all")]
# The Usage page is a single Request Logs list. Column order follows the
# requested cc-switch-style request record: time · provider · model · input ·
# output · API total · usage total · cache hit/miss/write · usage · source.
# cc-switch's cost / duration / HTTP-status columns have no transcript source,
# so they are deliberately absent rather than estimated. Time/Provider/Model/
# Input/Output/Total/Usage are the priority columns (leftmost); the cache
# detail is reachable by horizontal scroll and by the row detail screen.
USAGE_COLUMNS = ("Time", "Provider", "Model", "Input", "Output",
                 "API Total", "Usage Total", "Cache hit", "Cache miss",
                 "Cache write", "Usage", "Source")

# Tokens-tab column order: API Total and Usage Total sit side by side, then the
# cache parts, then coverage.
TOKEN_COLUMNS = ("Model", "Requests", "With usage", "Input", "Output",
                 "API Total", "Usage Total", "Cache hit", "Cache miss",
                 "Cache write", "Coverage")


if HAVE_TEXTUAL:

    class HistoryScreen(Screen):
        """Recent calls for one entity, pushed on row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, kind: str, name: str, display: str | None = None):
            super().__init__()
            self.kind = kind
            self.entity = name              # the value queried against the DB
            self.heading = display or name  # what the title shows

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Vertical(
                Static(f"[b]{self.kind}[/b] · {self.heading}", id="hist-title"),
                DataTable(id="hist-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind}: {self.heading}"
            t = self.query_one("#hist-table", DataTable)
            t.cursor_type = "row"
            t.add_columns("when", "project", "session", "status", "duration")
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                for r in db.q_history(conn, self.kind, self.entity, 200):
                    t.add_row(ts(r["ts"]), r["project"] or "-",
                              (r["session_id"] or "")[:8], r["status"] or "-",
                              ms(r["duration_ms"]))
            finally:
                conn.close()

    class ModelResponsesScreen(Screen):
        """Per-model recent responses, pushed on a Tokens-tab row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, model: str):
            super().__init__()
            self.model = model

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Vertical(
                Static(f"[b]Model responses[/b] · {self.model}", id="resp-title"),
                DataTable(id="resp-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"Model: {self.model}"
            t = self.query_one("#resp-table", DataTable)
            t.cursor_type = "row"
            t.add_columns(
                "when", "model", "prompt", "completion", "total",
                "cache_r", "cache_w", "cache_hit", "cache_miss", "cache_write",
                "usage", "message_id",
            )
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                for r in db.q_model_responses(conn, model=self.model, limit=200):
                    mid = r["message_id"] or ""
                    masked = (mid[:8] + "…" + mid[-8:]) if len(mid) > 16 else mid
                    t.add_row(
                        ts(r["ts"]), r["model"] or "-",
                        fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                        fmt_n(r["total_tokens"]),
                        fmt_n(r["cache_read_input_tokens"]),
                        fmt_n(r["cache_creation_input_tokens"]),
                        fmt_n(r["prompt_cache_hit_tokens"]),
                        fmt_n(r["prompt_cache_miss_tokens"]),
                        fmt_n(r["prompt_cache_write_tokens"]),
                        ("yes" if r["usage_available"] else "not counted"),
                        masked,
                    )
            finally:
                conn.close()

    class TrackerApp(App):
        CSS = """
        #hist-title, #resp-title { padding: 1 2; }
        DataTable { height: 1fr; }
        #status { padding: 0 2; color: $text-muted; }
        /* Usage page: the summary panels are height-capped and scroll
           internally, so the Request Logs table below always keeps its rows.
           #usage-panels is a Horizontal (four columns); .compact stacks it. */
        #usage-box { height: 1fr; padding: 0 1; }
        #usage-range { width: 46; }
        #usage-panels { height: auto; max-height: 40%; overflow-y: auto; }
        .usage-panel {
            border: round $primary;
            padding: 0 1;
            margin: 0 1 0 0;
            width: 1fr;
            height: auto;
        }
        #panel-runtime { margin: 0; }
        .panel-title { text-style: bold; color: $accent; }
        #usage-status, #usage-window { padding: 0; }
        #usage-note { padding: 0 1; color: $text-muted; }
        #t-usage { height: 1fr; min-height: 6; }
        #usage-panels.compact { layout: vertical; }
        #usage-panels.compact .usage-panel { width: 100%; margin: 0 0 1 0; }
        """
        # Pane ids in display order. Used for wrap-around tab cycling.
        TAB_IDS = [
            "tab-tools", "tab-skills", "tab-agents",
            "tab-plugins", "tab-mcp", "tab-tokens", "tab-usage",
        ]
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("s", "toggle_auto_sync", "Auto sync"),
            Binding("tab", "next_tab", "Next tab", priority=True),
            Binding("shift+tab", "previous_tab", "Prev tab", priority=True),
        ]
        TITLE = "CodeBuddy usage tracker"

        USAGE_LOG_LIMIT = 100

        def __init__(self, db_path):
            super().__init__()
            self.db_path = str(db_path)
            self._auto_sync = True
            self._sync_running = False
            self._sync_pending = False
            self.sync_mod = None
            # Usage page state (a single Request Logs list; range only).
            self.usage_range = "24h"
            self._last_refresh = None      # epoch ms
            self._last_sync = None         # epoch ms
            self._usage_ready = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with TabbedContent(initial="tab-tools"):
                with TabPane("Tools", id="tab-tools"):
                    yield DataTable(id="t-tools", zebra_stripes=True)
                with TabPane("Skills", id="tab-skills"):
                    yield DataTable(id="t-skills", zebra_stripes=True)
                with TabPane("Agents", id="tab-agents"):
                    yield DataTable(id="t-agents", zebra_stripes=True)
                with TabPane("Plugins", id="tab-plugins"):
                    yield DataTable(id="t-plugins", zebra_stripes=True)
                with TabPane("MCP", id="tab-mcp"):
                    yield DataTable(id="t-mcp", zebra_stripes=True)
                with TabPane("Tokens", id="tab-tokens"):
                    yield DataTable(id="t-tokens", zebra_stripes=True)
                with TabPane("Usage", id="tab-usage"):
                    yield Vertical(
                        Select(RANGE_OPTIONS, value="24h", allow_blank=False,
                               id="usage-range"),
                        Horizontal(
                            Vertical(
                                Static("Window / Requests", classes="panel-title"),
                                Static(id="usage-window"),
                                Static(id="sum-requests"),
                                Static(id="sum-with-usage"),
                                Static(id="sum-coverage"),
                                Static(id="sum-missing"),
                                classes="usage-panel", id="panel-window",
                            ),
                            Vertical(
                                Static("Model tokens", classes="panel-title"),
                                Static(id="sum-input"),
                                Static(id="sum-output"),
                                Static(id="sum-total"),
                                Static(id="sum-total-source"),
                                Static(id="sum-usage-total"),
                                classes="usage-panel", id="panel-tokens",
                            ),
                            Vertical(
                                Static("Cache breakdown", classes="panel-title"),
                                Static(id="sum-cache-hit"),
                                Static(id="sum-cache-miss"),
                                Static(id="sum-cache-write"),
                                Static(id="sum-hit-rate"),
                                classes="usage-panel", id="panel-cache",
                            ),
                            Vertical(
                                Static("Runtime", classes="panel-title"),
                                Static(id="usage-status"),
                                classes="usage-panel", id="panel-runtime",
                            ),
                            id="usage-panels",
                        ),
                        Static(id="usage-note"),
                        DataTable(id="t-usage", zebra_stripes=True),
                        id="usage-box",
                    )
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            for tid, cols in (
                ("t-tools", ("tool", "calls", "ok", "fail", "avg", "last used")),
                ("t-skills", ("skill", "plugin", "calls", "ok", "last used")),
                ("t-agents", ("agent", "kind", "calls", "last used")),
                ("t-plugins", ("plugin", "version", "uses", "skills", "agents", "cmds")),
                ("t-mcp", ("server", "tool", "calls", "ok", "last used")),
            ):
                t = self.query_one(f"#{tid}", DataTable)
                t.cursor_type = "row"
                t.add_columns(*cols)
            tt = self.query_one("#t-tokens", DataTable)
            tt.cursor_type = "row"
            tt.add_columns(*TOKEN_COLUMNS)
            tu = self.query_one("#t-usage", DataTable)
            tu.cursor_type = "row"
            tu.add_columns(*USAGE_COLUMNS)
            # Best-effort schema upgrade: a pre-v3/v4 DB lacks the cache and
            # provider-total columns, which every usage query needs. No-op once
            # already migrated.
            db.migrate(self.db_path)
            self._usage_ready = True
            # Auto sync (incremental, ~30s) + UI refresh (~5s). Both are timers,
            # not subprocesses; the sync runs in a worker thread (see _do_sync).
            self.set_interval(30, self._auto_sync_tick)
            self.set_interval(5, self._auto_refresh_tick)
            self.refresh_data()

        # --- tab cycling (uses the real TabbedContent.active API) -----------

        def _cycle_tab(self, delta: int) -> None:
            tc = self.query_one(TabbedContent)
            try:
                idx = self.TAB_IDS.index(tc.active)
            except ValueError:
                idx = 0
            nxt = (idx + delta) % len(self.TAB_IDS)
            tc.active = self.TAB_IDS[nxt]

        def action_next_tab(self) -> None:
            self._cycle_tab(1)

        def action_previous_tab(self) -> None:
            self._cycle_tab(-1)

        # --- data fill / refresh --------------------------------------------

        def _fill(self, table, rows, key_index=0, key_kind=None):
            # Remember the highlighted row by its ROW KEY, not by index: a
            # re-sort between refreshes must not move the highlight onto a
            # different entity (index-based restore would).
            cur_key = None
            cur = table.cursor_row
            if key_kind and table.row_count:
                try:
                    cur_key = table.coordinate_to_cell_key(
                        table.cursor_coordinate).row_key
                except Exception:
                    cur_key = None
            table.clear()
            n = 0
            for r in rows:
                # NULL renders as "-", matching fmt_n (never a blank cell).
                cells = [("-" if c is None else str(c)) for c in r]
                key = None
                if key_kind:
                    # key_index may be a single index or a tuple of indices for
                    # a composite (unique) key, e.g. mcp -> (server, tool).
                    idx = (key_index if isinstance(key_index, (tuple, list))
                           else (key_index,))
                    key = "\t".join([key_kind] + [str(r[i]) for i in idx])
                table.add_row(*cells, key=key)
                n += 1
            if cur_key is not None and cur_key in table.rows:
                table.move_cursor(row=table.get_row_index(cur_key))
            elif not key_kind and 0 <= cur < n:
                # Unkeyed tables (e.g. the Request Logs list) keep the old
                # index-based restore.
                table.move_cursor(row=cur)

        def _fill_or_empty(self, table, rows, empty_msg, key_index=0,
                           key_kind=None):
            """Fill a table, or show an explicit empty-state row.

            Mirrors ``_fill_tokens``: an empty tab must never render a blank
            region. The empty row carries no key, so selecting it is a no-op.
            """
            if rows:
                self._fill(table, rows, key_index=key_index, key_kind=key_kind)
            else:
                table.clear()
                table.add_row(empty_msg, *(["-"] * (len(table.columns) - 1)))

        def _set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        def _fill_tools(self, conn) -> None:
            self._fill_or_empty(
                self.query_one("#t-tools", DataTable),
                [[r["tool_name"], f"{r['calls']:,}", f"{r['completed']:,}",
                  f"{r['failed']:,}", ms(r["avg_ms"]), ts(r["last_used"])]
                 for r in db.q_tools(conn)],
                "No tool calls yet", key_kind="tool")

        def _fill_skills(self, conn) -> None:
            self._fill_or_empty(
                self.query_one("#t-skills", DataTable),
                [[r["skill"], r["plugin"] or "-", f"{r['calls']:,}",
                  f"{r['completed']:,}", ts(r["last_used"])]
                 for r in db.q_skills(conn)],
                "No skills used yet", key_kind="skill")

        def _fill_agents(self, conn) -> None:
            self._fill_or_empty(
                self.query_one("#t-agents", DataTable),
                [[r["agent_type"], r["kind"], f"{r['calls']:,}",
                  ts(r["last_used"])]
                 for r in db.q_agents(conn)],
                "No agents used yet", key_kind="agent")

        def _fill_plugins(self, conn) -> None:
            self._fill_or_empty(
                self.query_one("#t-plugins", DataTable),
                [[r["plugin"], r["version"] or "-", f"{r['uses']:,}",
                  f"{r['skills']:,}", f"{r['agents']:,}", f"{r['commands']:,}"]
                 for r in db.q_plugins(conn)],
                "No plugins installed")

        def _fill_mcp(self, conn) -> None:
            # Row key is unique per (server, tool): v_mcp groups by both, so two
            # tools under one server would otherwise collide and abort the mount.
            self._fill_or_empty(
                self.query_one("#t-mcp", DataTable),
                [[r["server"], r["tool"], f"{r['calls']:,}",
                  f"{r['completed']:,}", ts(r["last_used"])]
                 for r in db.q_mcp(conn)],
                "No MCP calls yet", key_index=(0, 1), key_kind="mcp")

        def _fill_tokens(self, conn) -> None:
            # Reuses q_model_tokens; never re-parses transcripts and never mixes
            # in sessions.tokens (tokenDelta). Total = API total (provider total
            # when present). Usage Total is the display-only re-add of cache hit.
            mt = self.query_one("#t-tokens", DataTable)
            rows = [[r["model"], f"{r['responses']:,}", f"{r['with_usage']:,}",
                     fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                     fmt_n(r["total_tokens"]), fmt_n(r["usage_total_tokens"]),
                     fmt_n(r["prompt_cache_hit_tokens"]),
                     fmt_n(r["prompt_cache_miss_tokens"]),
                     fmt_n(r["prompt_cache_write_tokens"]),
                     self._coverage(r["with_usage"], r["responses"])]
                    for r in db.q_model_tokens(conn)]
            self._fill_or_empty(mt, rows, "No model responses yet",
                                key_kind="model")

        # pane id -> the fill method for that tab (usage handled separately).
        _PANE_FILL = {
            "tab-tools": "_fill_tools", "tab-skills": "_fill_skills",
            "tab-agents": "_fill_agents", "tab-plugins": "_fill_plugins",
            "tab-mcp": "_fill_mcp", "tab-tokens": "_fill_tokens",
        }

        def refresh_data(self, conn=None) -> None:
            """Full refresh: every tab + the status bar.

            Used on mount and after a sync that actually indexed new files. Pass
            an existing readonly ``conn`` to avoid opening a second one.
            """
            own = conn is None
            if own:
                conn = db.open_db(self.db_path, readonly=True)
            try:
                self._fill_tools(conn)
                self._fill_skills(conn)
                self._fill_agents(conn)
                self._fill_plugins(conn)
                self._fill_mcp(conn)
                self._fill_tokens(conn)
                o = db.overview(conn)
                if self._usage_ready:
                    self._refresh_usage(conn)   # same connection, no second open
            finally:
                if own:
                    conn.close()
            status = (
                f"db={self.db_path} · {o['tool_calls']} tool calls · "
                f"{o['skills']} skills · {o['agents']} agents · "
                f"{o['plugins_used']} plugins · {o['mcp']} mcp · "
                f"{o['model_responses']} model responses · "
                f"last {ts(o['last_ts'])}"
            )
            if not self._auto_sync:
                status += " · auto-sync OFF"
            self._set_status(status)

        def _refresh_active_tab(self, conn=None) -> None:
            """Refresh only the visible tab (5s timer / tab activation).

            Hidden tabs are left alone; they are populated when the user opens
            them (see ``on_tabbed_content_tab_activated``).
            """
            if not self._usage_ready:
                return
            pane = self.query_one(TabbedContent).active
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error:
                    return
            try:
                if pane == "tab-usage":
                    self._refresh_usage(conn)
                else:
                    fn = self._PANE_FILL.get(pane)
                    if fn:
                        getattr(self, fn)(conn)
            finally:
                if own:
                    conn.close()

        def on_tabbed_content_tab_activated(
                self, event: TabbedContent.TabActivated) -> None:
            # Populate the tab the user just switched to (hidden tabs are not
            # polled by the 5s timer).
            if self._usage_ready:
                self._refresh_active_tab()

        # --- Usage Statistics page -----------------------------------------

        def _render_usage_status(self) -> None:
            self.query_one("#usage-status", Static).update(
                f"Auto sync: {'ON' if self._auto_sync else 'OFF'}\n"
                f"Last sync: {local_time(self._last_sync)}\n"
                f"Last refresh: {local_time(self._last_refresh)}"
            )

        @staticmethod
        def _coverage(with_usage, requests) -> str:
            """Share of requests that carry usage; ``-`` when there are none."""
            if not requests:
                return "-"
            return f"{(with_usage or 0) / requests * 100:.1f}%"

        @staticmethod
        def _panel_line(label: str, value: str) -> str:
            # Fixed-width label + right-aligned value keeps the panels aligned
            # without relying on text-align. 12 + 12 = 24 columns, so the line
            # does not wrap inside a quarter-width panel at 120+ columns.
            return f"[dim]{label:<12}[/dim]{value:>12}"

        # Below this width the four panels stack instead of sitting side by
        # side: each column needs ~28 cells for a 24-char panel line plus
        # border/padding, so 4 x 28 = 112.
        COMPACT_WIDTH = 112

        def _apply_responsive_layout(self, width=None) -> None:
            # Four panels side by side on wide terminals; stacked when narrow.
            if width is None:
                width = self.size.width
            try:
                self.query_one("#usage-panels").set_class(
                    width < self.COMPACT_WIDTH, "compact")
            except Exception:
                pass

        def on_resize(self, event) -> None:  # textual.events.Resize
            # event.size carries the NEW size (self.size still lags here).
            self._apply_responsive_layout(event.size.width)

        def _refresh_usage(self, conn=None) -> None:
            # Recompute the window from the CURRENT time on every refresh — never
            # a cached/opening-time range.
            if not self._usage_ready:
                return
            now_ms = int(time.time() * 1000)
            start, end = db.window_bounds(self.usage_range, now_ms)
            self._last_refresh = now_ms
            shown_start = local_time(start) if start is not None else "(no lower bound)"
            self.query_one("#usage-window", Static).update(
                f"{db.USAGE_RANGE_LABELS[self.usage_range]}\n"
                f"{shown_start} — {local_time(end)}"
            )
            self._render_usage_status()
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as e:
                    self.query_one("#usage-status", Static).update(
                        f"usage unavailable: {e}")
                    return
            try:
                s = db.q_usage_summary(conn, start, end)
                self._render_summary_panels(s)
                self._fill_usage_logs(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#usage-status", Static).update(
                    f"usage query failed ({e}); run: cbut sync")
            finally:
                if own:
                    conn.close()
            self._apply_responsive_layout()

        def _render_summary_panels(self, s: dict) -> None:
            def put(wid: str, label: str, val: str) -> None:
                self.query_one(wid, Static).update(self._panel_line(label, val))

            req = s["requests"] or 0
            put("#sum-requests", "Requests", f"{req:,}")
            put("#sum-with-usage", "With usage", f"{s['with_usage'] or 0:,}")
            put("#sum-coverage", "Coverage", self._coverage(s["with_usage"], req))
            put("#sum-input", "Input", fmt_n(s["prompt_tokens"]))
            put("#sum-output", "Output", fmt_n(s["completion_tokens"]))
            put("#sum-total", "API Total", fmt_n(s["total_tokens"]))
            put("#sum-total-source", "Source",
                s.get("total_tokens_source") or "-")
            # Usage Total = Input + Output + cache hit.
            put("#sum-usage-total", "Usage Total",
                fmt_n(s["usage_total_tokens"]))
            put("#sum-cache-hit", "Cache hit", fmt_n(s["cache_hit"]))
            put("#sum-cache-miss", "Cache miss", fmt_n(s["cache_miss"]))
            put("#sum-cache-write", "Cache write", fmt_n(s["cache_write"]))
            # hit / (hit + miss + write) — the cacheable input.
            frac = db.cache_hit_rate(s["cache_hit"], s["cache_miss"],
                                     s["cache_write"])
            put("#sum-hit-rate", "Hit rate",
                "-" if frac is None else f"{frac * 100:.1f}%")
            missing = []
            if s["missing_prompt"]:
                missing.append(f"{s['missing_prompt']} missing input")
            if s["missing_completion"]:
                missing.append(f"{s['missing_completion']} missing output")
            if req == 0:
                note = "no data in window"
            elif missing:
                note = ", ".join(missing)
            else:
                note = "complete"
            put("#sum-missing", "Incomplete", note)

        @staticmethod
        def _usage_kind(r) -> str:
            pt, ct = r["prompt_tokens"], r["completion_tokens"]
            if r["usage_available"] and pt is not None and ct is not None:
                return "Real"
            if r["usage_available"]:
                return "Partial"
            return "Missing"

        def _fill_usage_logs(self, conn, start, end) -> None:
            rows = db.q_usage_request_logs(conn, start, end,
                                           limit=self.USAGE_LOG_LIMIT)
            if rows:
                self.query_one("#usage-note", Static).update(
                    "Newest first · cache hit=read, write=create · "
                    "no cost/duration/status"
                )
            else:
                # Explicit empty state, never a blank region.
                self.query_one("#usage-note", Static).update(
                    "No requests in this window — widen the time range above."
                )
            out = [[
                local_time(r["ts"]), db.UNKNOWN_PROVIDER, r["model"] or "-",
                fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                fmt_n(r["total_tokens"]), fmt_n(r["usage_total_tokens"]),
                fmt_n(r["prompt_cache_hit_tokens"]),
                fmt_n(r["prompt_cache_miss_tokens"]),
                fmt_n(r["prompt_cache_write_tokens"]),
                self._usage_kind(r), r["source"] or "-",
            ] for r in rows]
            self._fill(self.query_one("#t-usage", DataTable), out)

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id != "usage-range":
                return
            self.usage_range = event.value
            self._refresh_usage()

        # --- row select: route to detail screens ---------------------------

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            key = event.row_key.value
            if not key or "\t" not in key:
                return
            kind, _, rest = key.partition("\t")
            if kind == "model":
                self.push_screen(ModelResponsesScreen(rest))
            elif kind == "mcp":
                # Key is mcp\t<server>\t<tool>. q_history maps "mcp" to the
                # mcp_usage.tool column, so query by the TOOL and show
                # "server · tool" as the title.
                server, _, tool = rest.partition("\t")
                self.push_screen(
                    HistoryScreen("mcp", tool, display=f"{server} · {tool}"))
            else:
                self.push_screen(HistoryScreen(kind, rest))

        # --- R / S shortcuts ------------------------------------------------

        def action_refresh(self) -> None:
            # R = incremental sync + reload DB + refresh the UI (not just a
            # re-render of already-loaded data).
            self._set_status("Syncing…")
            self._request_sync()

        def action_toggle_auto_sync(self) -> None:
            # S = toggle the automatic sync switch. Distinct from R: it does not
            # trigger a sync itself.
            self._auto_sync = not self._auto_sync
            self._set_status(f"Auto sync: {'ON' if self._auto_sync else 'OFF'}")
            self.notify(f"Auto sync: {'ON' if self._auto_sync else 'OFF'}")
            if self._usage_ready:
                self._render_usage_status()

        # --- automatic sync / refresh timers --------------------------------

        def _auto_sync_tick(self) -> None:
            if self._auto_sync:
                self._request_sync()

        def _auto_refresh_tick(self) -> None:
            # Only the visible tab is re-queried; hidden tabs refresh when the
            # user opens them.
            self._refresh_active_tab()

        def _load_sync_module(self):
            """Load cbut-sync.py (hyphenated name) as a module.

            Registered under a fixed name in sys.modules so tests can patch
            ``app.sync_mod.run`` without triggering a real filesystem scan.
            """
            name = "cbut_sync_embedded"
            mod = sys.modules.get(name)
            if mod is None:
                path = Path(__file__).with_name("cbut-sync.py")
                spec = importlib.util.spec_from_file_location(name, str(path))
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
            return mod

        def _request_sync(self) -> None:
            # Guard against concurrent syncs: if one is running, mark a pending
            # request and return; the running worker will retry on completion.
            if self._sync_running:
                self._sync_pending = True
                return
            self._sync_running = True
            self.run_worker(self._do_sync, thread=True, group="sync")

        def _do_sync(self) -> None:
            # Runs in a worker thread. cbut-sync.run() opens its own RW
            # connection and uses the incremental sync_state table, so no
            # connection is shared across threads.
            try:
                if self.sync_mod is None:
                    self.sync_mod = self._load_sync_module()
                stats = self.sync_mod.run(quiet=True, db_path=self.db_path)
                self.call_from_thread(self._on_sync_done, True, stats, None)
            except Exception as e:  # report, never swallow
                self.call_from_thread(self._on_sync_done, False, None, str(e))

        def _on_sync_done(self, ok: bool, stats, err) -> None:
            self._sync_running = False
            if ok:
                self._last_sync = int(time.time() * 1000)
                if (stats or {}).get("files_indexed", 0) > 0:
                    self.refresh_data()          # new rows can touch any tab
                else:
                    self._refresh_active_tab()   # nothing changed -> light refresh
                self.notify(f"Synced {stats.get('files_indexed', 0)} files")
            else:
                self._set_status("Sync failed: " + (err or "unknown error")[:200])
                self.notify("Sync failed — keeping current data", severity="error")
            if self._sync_pending:
                self._sync_pending = False
                self._request_sync()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Interactive CodeBuddy usage tracker.")
    ap.add_argument("--db", default=str(db.DB_PATH))
    args = ap.parse_args(argv)

    if not HAVE_TEXTUAL:
        print(
            "textual is not installed — the TUI needs it.\n"
            "  pip install -r requirements.txt   (or run ./install.sh)\n"
            "Headless reports still work:  cbut stats",
            file=sys.stderr,
        )
        return 2
    if not Path(args.db).exists():
        print(f"no database at {args.db} — run: cbut sync", file=sys.stderr)
        return 1
    TrackerApp(args.db).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""cbut-tui — interactive viewer for the CodeBuddy usage tracker.

Tabs: Dashboard · Tools · Skills · Agents · Plugins · MCP · Tokens · Usage.

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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import Screen
    from textual.widgets import (
        DataTable, Footer, Select, Static, TabbedContent, TabPane,
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


def count_n(v, singular, plural=None):
    """``1 tool call`` / ``12 tool calls``, grouped like every count cell.

    D-005 fixed the plural in one place (Top tools) and left the rest: "1 skills"
    in the status bar and "Synced 1 files" survived it. Counts that read as prose
    are one class, so they agree here rather than being re-remembered per site.
    """
    word = singular if v == 1 else (plural or singular + "s")
    return f"{v:,} {word}"


def clip(text, width):
    """Fit ``text`` into ``width`` cells, marking a cut with an ellipsis.

    The Dashboard's leaderboard lines are column-formatted (``{:<26}``), so a
    name longer than its field does not wrap — it pushes every column after it.
    Observed at 41 characters. Clipping keeps the columns aligned; the full name
    stays in the table on that entity's own tab.
    """
    s = str(text)
    return s if len(s) <= width else s[: width - 1] + "…"


def local_time(ms):
    """Local-time render of an epoch-ms value (query bounds stay in Unix ms)."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# Usage-page option list (values mirror cbut_db so the two stay in sync).
# Windows are whole local calendar days ending now ("Today", "2 days", …).
RANGE_OPTIONS = [(db.USAGE_RANGE_LABELS[k], k)
                 for k in ("1d", "2d", "3d", "7d", "30d", "all")]
# The Usage page is a single Request Logs list. Column order: time · model ·
# usage total · input · output · API total · cache hit/miss/write · cache hit
# rate. cc-switch's provider / cost / duration / HTTP-status columns have no
# transcript source, so they are deliberately absent rather than estimated.
USAGE_COLUMNS = ("Time", "Model", "Usage Total", "Input", "Output",
                 "API Total", "Cache hit", "Cache miss",
                 "Cache write", "Cache hit rate")

# Tokens-tab column order: Usage Total right after the model, then the request
# count and the token parts. The cache detail is kept; the near-always-100%
# "With usage" and "Coverage" columns were dropped.
TOKEN_COLUMNS = ("Model", "Usage Total", "Requests", "Input", "Output",
                 "API Total", "Cache hit", "Cache miss", "Cache write")

# Entity tabs and the two detail screens. Declared here rather than inline at
# `add_columns` because the column plan needs the full list to compute a prefix from,
# and a plan that cannot name the columns it dropped is a silent truncation wearing a
# guard's clothes.
TOOLS_COLUMNS = ("tool", "calls", "ok", "fail", "avg", "last used")
SKILLS_COLUMNS = ("skill", "calls", "ok", "last used")
AGENTS_COLUMNS = ("agent", "calls", "last used")
PLUGINS_COLUMNS = ("plugin", "uses", "skills", "agents", "cmds")
MCP_COLUMNS = ("server", "tool", "calls", "ok", "last used")
HISTORY_COLUMNS = ("when", "project", "session", "status", "duration")
# Twelve columns need 170 cells: on an 80-wide terminal more than half of this table
# used to sit past the right edge with no key that would bring it back.
RESPONSE_COLUMNS = ("when", "model", "prompt", "completion", "total",
                    "cache_r", "cache_w", "cache_hit", "cache_miss",
                    "cache_write", "usage", "message_id")


if HAVE_TEXTUAL:

    class TopBar(Horizontal):
        """标题栏 + 共享时间范围选择器（取代 textual 的 Header）。

        去掉两个内置行为：点击不再切换 'tall' 高度；没有左上角图标，不会打开命令面板。
        只有主界面带选择器；详情屏用 with_range=False。
        """
        DEFAULT_CSS = """
        TopBar { dock: top; width: 100%; height: 1; background: $panel; color: $foreground; }
        TopBar > #topbar-title { width: 1fr; padding: 0 1; content-align: left middle; }
        TopBar > #range { width: 30; }
        TopBar > #range SelectCurrent { border: none; padding: 0 1; background: $panel; }
        """

        def __init__(self, title: str, *, with_range: bool = False,
                     range_value: str = "7d", id: str | None = None):
            super().__init__(id=id)
            self._title = title
            self._with_range = with_range
            self._range_value = range_value

        def compose(self) -> ComposeResult:
            yield Static(self._title, id="topbar-title")
            if self._with_range:
                yield Select(RANGE_OPTIONS, value=self._range_value,
                             allow_blank=False, id="range")

    class WideTableMixin:
        """The narrow-terminal column plan, shared by every host that owns a table.

        A rule that covers two of the nine tables is not a rule, so the mechanism
        lives here and each host declares its own ``WIDE_TABLES``. ``_wide_rows`` is
        created by each host's ``__init__`` — it must not be a class attribute, or two
        open screens would share one row cache.

        The numbers a host registers are measured on this machine, not derived: a column
        costs its content width plus a 2-cell gutter (``virtual_size.width ==
        sum(content_width) + 2 * n_columns``, checked on every table), and a table loses
        a fixed number of cells to its surroundings — 2 inside a tab pane, 4 on the
        padded Usage page, 0 on a pushed screen that spans the terminal.
        """

        CELL_COST = 2
        MIN_SHOWN = 2      # never narrow a table down to a single unnamed column
        # Measured on this machine's database, not guessed: every entity name fits
        # 28 cells (tool 22, model 24, agent 20, plugin 19, mcp tool 23, skill 28,
        # command 31 once) — the only column that runs past it is the project path,
        # where 162 of 482 rows exceed 24 and the longest is 74. So a cap at 28
        # costs no name and buys back the 46 cells a path was spending.
        NAME_CAP = 28

        @property
        def name_cap(self) -> int:
            """The cap this host actually renders with.

            ``NAME_CAP`` is the shipped default, which the app shadows from the
            settings file. A screen pushed on top of the app has no config of its
            own, so it overrides this to read the app — otherwise a configured cap
            would silently apply to the tabs and not to the detail screens.
            """
            return self.NAME_CAP

        def _cap_cell(self, text: str) -> str:
            """Fit one cell to ``name_cap``, keeping the half that carries meaning.

            A path's beginning is the shared prefix (`/home/user/…` repeated down
            the column) and its end is what distinguishes the rows, so paths are
            capped from the left. Everything else — a tool, a model, a skill — is
            read from the start, so it is capped from the right.
            """
            cap = self.name_cap
            if len(text) <= cap:
                return text
            keep = cap - 1
            return ("…" + text[-keep:]) if "/" in text else (text[:keep] + "…")

        def _fill(self, table, rows, key_index=0, key_kind=None, key_rows=None):
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
            for i, r in enumerate(rows):
                # NULL renders as "-", matching fmt_n (never a blank cell).
                cells = [("-" if c is None else str(c)) for c in r]
                key = None
                if key_kind:
                    # key_index may be a single index or a tuple of indices for
                    # a composite (unique) key, e.g. mcp -> (server, tool).
                    # ``key_rows`` carries the UNCAPTED values: two entities whose
                    # names share the visible prefix render identically and must
                    # still route apart, so the key cannot come from the cell.
                    idx = (key_index if isinstance(key_index, (tuple, list))
                           else (key_index,))
                    src = key_rows[i] if key_rows is not None else r
                    key = "\t".join([key_kind] + [str(src[j]) for j in idx])
                table.add_row(*cells, key=key)
                n += 1
            if cur_key is not None and cur_key in table.rows:
                table.move_cursor(row=table.get_row_index(cur_key))
            elif not key_kind and 0 <= cur < n:
                # Unkeyed tables (e.g. the Request Logs list) keep the old
                # index-based restore.
                table.move_cursor(row=cur)

        def _fill_or_empty(self, table, rows, empty_msg, key_index=0,
                           key_kind=None, key_rows=None):
            """Fill a table, or show an explicit empty-state row.

            Mirrors ``_fill_tokens``: an empty tab must never render a blank
            region. The empty row carries no key, so selecting it is a no-op.
            """
            if rows:
                self._fill(table, rows, key_index=key_index, key_kind=key_kind,
                           key_rows=key_rows)
            else:
                table.clear()
                table.add_row(empty_msg, *(["-"] * (len(table.columns) - 1)))

        @staticmethod
        def _column_widths(labels, rows):
            """What DataTable will charge for each column: label or widest cell."""
            return [max(len(str(label)),
                        max((len(str(r[i])) for r in rows), default=0))
                    for i, label in enumerate(labels)]

        def _set_columns(self, table, cols) -> None:
            if [str(c.label) for c in table.columns.values()] == list(cols):
                return
            table.clear(columns=True)
            table.add_columns(*cols)

        def _fill_wide(self, table_id, rows, empty_msg, key_index=0, key_kind=None,
                       width=None):
            """Fill a wide table with the longest prefix of columns that fits.

            Prefix, not priority re-ordering: both tables are already ordered most
            essential first (name, total, then the cache detail), so dropping the
            tail is the plan. The cells past the right edge used to be cut
            mid-label with no keyboard way to reach them.
            """
            table = self.query_one(f"#{table_id}", DataTable)
            labels, note_id, chrome = self.WIDE_TABLES[table_id]
            self._wide_rows[table_id] = (rows, empty_msg, key_index, key_kind)
            note = self.query_one(f"#{note_id}", Static)
            width = width or self.size.width or table.region.width
            if not width or not rows:
                # Either the first layout has not happened yet (both are 0, and
                # planning against 0 would drop everything) or there is nothing to
                # protect: an empty-state row spans the whole label set.
                self._set_columns(table, labels)
                self._fill_or_empty(table, rows, empty_msg, key_index, key_kind)
                note.update("")
                note.display = False
                if not width:
                    self.call_after_refresh(self._plan_wide_tables)
                return
            raw = [[("-" if c is None else str(c)) for c in r] for r in rows]
            shown = [[self._cap_cell(c) for c in rr] for rr in raw]
            capped = any(a != b for rr, ss in zip(raw, shown) for a, b in zip(rr, ss))
            full = self._column_widths(labels, raw)
            budget = width - chrome
            if sum(full) + self.CELL_COST * len(labels) <= budget:
                # Everything fits at its real length, so the cap stays out: a wide
                # terminal has no reason to lose characters, and "widen to N" would
                # otherwise promise a number that buys nothing.
                self._set_columns(table, labels)
                self._fill_or_empty(table, raw, empty_msg, key_index, key_kind)
                note.update("")
                note.display = False
                return
            widths = self._column_widths(labels, shown)
            keep = len(labels)
            while keep > self.MIN_SHOWN and sum(widths[:keep]) + self.CELL_COST * keep > budget:
                keep -= 1
            self._set_columns(table, labels[:keep])
            self._fill_or_empty(table, [r[:keep] for r in shown],
                                empty_msg, key_index, key_kind, key_rows=rows)
            cap_note = f" · cells capped at {self.name_cap}" if capped else ""
            if keep >= len(labels):
                # Nothing is hidden, so the only thing worth saying is that
                # characters were cut — and the full value only comes back at the
                # width named below, which is why it has to be said out loud.
                note.update(f"cells capped at {self.name_cap}" if capped else "")
                note.display = capped
                return
            cuts = sum(widths[:keep]) + self.CELL_COST * keep > budget
            note.update(f"{keep} of {len(labels)} columns · hidden: "
                        f"{', '.join(labels[keep:])} · widen to "
                        f"{sum(full) + self.CELL_COST * len(labels) + chrome}"
                        + (" · even these are cut" if cuts else "")
                        + cap_note)
            note.display = True

        def _plan_wide_tables(self, width=None) -> None:
            """Re-plan from the cached rows — a resize must not re-query."""
            for table_id, (rows, empty_msg, key_index, key_kind) in list(
                    self._wide_rows.items()):
                self._fill_wide(table_id, rows, empty_msg,
                                key_index=key_index, key_kind=key_kind,
                                width=width)

    class HistoryScreen(WideTableMixin, Screen):
        """Recent calls for one entity, pushed on row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        # A pushed screen spans the terminal, so its table loses no cells to its
        # surroundings — measured: region width == terminal width at 60/80/100.
        WIDE_TABLES = {"hist-table": (HISTORY_COLUMNS, "colnote-hist", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, kind: str, name: str, display: str | None = None):
            super().__init__()
            self.kind = kind
            self.entity = name              # the value queried against the DB
            self.heading = display or name  # what the title shows
            self._wide_rows = {}

        def compose(self) -> ComposeResult:
            yield TopBar(f"{self.kind} · {self.heading}")
            yield Vertical(
                Static(f"[b]{self.kind}[/b] · {self.heading}", id="hist-title"),
                Static(id="colnote-hist", classes="col-note"),
                DataTable(id="hist-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind}: {self.heading}"
            self.query_one("#hist-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                rows = [[ts(r["ts"]), r["project"] or "-",
                         (r["session_id"] or "")[:8], r["status"] or "-",
                         ms(r["duration_ms"])]
                        for r in db.q_history(conn, self.kind, self.entity,
                                              self.app.DETAIL_LIMIT)]
            finally:
                conn.close()
            self._fill_wide("hist-table", rows, "No calls recorded for this name")

    class ModelResponsesScreen(WideTableMixin, Screen):
        """Per-model recent responses, pushed on a Tokens-tab row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        WIDE_TABLES = {"resp-table": (RESPONSE_COLUMNS, "colnote-resp", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, model: str):
            super().__init__()
            self.model = model
            self._wide_rows = {}

        def compose(self) -> ComposeResult:
            yield TopBar(f"Model responses · {self.model}")
            yield Vertical(
                Static(f"[b]Model responses[/b] · {self.model}", id="resp-title"),
                Static(id="colnote-resp", classes="col-note"),
                DataTable(id="resp-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"Model: {self.model}"
            self.query_one("#resp-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                rows = []
                for r in db.q_model_responses(conn, model=self.model,
                                              limit=self.app.DETAIL_LIMIT):
                    mid = r["message_id"] or ""
                    masked = (mid[:8] + "…" + mid[-8:]) if len(mid) > 16 else mid
                    rows.append([
                        ts(r["ts"]), r["model"] or "-",
                        fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                        fmt_n(r["total_tokens"]),
                        fmt_n(r["cache_read_input_tokens"]),
                        fmt_n(r["cache_creation_input_tokens"]),
                        fmt_n(r["prompt_cache_hit_tokens"]),
                        fmt_n(r["prompt_cache_miss_tokens"]),
                        fmt_n(r["prompt_cache_write_tokens"]),
                        "yes" if r["usage_available"] else "not counted",
                        masked,
                    ])
            finally:
                conn.close()
            self._fill_wide("resp-table", rows,
                           "No responses recorded for this model")

    class TrackerApp(WideTableMixin, App):
        CSS = """
        #hist-title, #resp-title { padding: 1 2; }
        DataTable { height: 1fr; }
        #status { padding: 0 2; color: $text-muted; }
        /* Summary pages (Dashboard + Usage). Both share .usage-panel /
           .panel-title. The Usage page has one .summary-panels row (height
           capped, scrolls internally so its table below keeps its rows). The
           Dashboard is a stack of .dash-row rows; #dash-box scrolls. Each row
           is a Horizontal; .compact stacks it on narrow terminals. */
        #usage-box, #dash-box { height: 1fr; padding: 0 1; }
        #dash-box { overflow-y: auto; }
        .summary-panels { height: auto; max-height: 40%; overflow-y: auto; }
        .usage-panel {
            border: round $primary;
            padding: 0 1;
            margin: 0 1 0 0;
            width: 1fr;
            height: auto;
        }
        #panel-runtime, #panel-dash-cache, #panel-dash-tools,
        #panel-dash-activity { margin: 0; }
        .panel-title { text-style: bold; color: $accent; }
        #usage-status, #usage-window { padding: 0; }
        #usage-note, #dash-note { padding: 0 1; color: $text-muted; }
        /* One line per wide table, only when columns were dropped — an always-on
           note above a table that hides nothing is noise that costs a row. Auto
           height because a note that is itself cut off at the right edge would be
           the same defect it exists to report. */
        .col-note { padding: 0 1; color: $text-muted; height: auto; display: none; }
        #dash-activity { color: $accent; }
        #dash-activity-axis { color: $text-muted; }
        #t-usage { height: 1fr; min-height: 6; }
        .summary-panels.compact, .dash-row.compact { layout: vertical; }
        /* Stacked one-per-row wastes the width a narrow terminal still has: at
           80x24 four stacked panels push the table's rows off the visible area.
           Two columns keep a 24-char panel line plus its border and padding
           (~28 cells) legible and leave the table room. */
        .summary-panels.compact { layout: grid; grid-size: 2; }
        /* Below two cells' minimum, stacking is the only layout that does not
           wrap a panel line mid-value. */
        .summary-panels.compact.stacked { layout: vertical; }
        .summary-panels.compact .usage-panel,
        .dash-row.compact .usage-panel { width: 100%; margin: 0 0 1 0; }
        .dash-row { height: auto; margin: 0 0 1 0; }
        """
        # Pane ids in display order. Used for wrap-around tab cycling.
        TAB_IDS = [
            "tab-dashboard", "tab-tools", "tab-skills", "tab-agents",
            "tab-plugins", "tab-mcp", "tab-tokens", "tab-usage",
        ]
        # The tab shown on launch. The TopBar Select must start on this tab's
        # own range (see compose), or its mount-time Changed would overwrite
        # that tab's default before the user touches anything.
        INITIAL_TAB = "tab-dashboard"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("s", "toggle_auto_sync", "Auto sync"),
            Binding("tab", "next_tab", "Next tab", priority=True),
            Binding("shift+tab", "previous_tab", "Prev tab", priority=True),
        ]
        TITLE = "CodeBuddy usage tracker"
        # No built-in Header means no ctrl+p palette; disable it outright too.
        ENABLE_COMMAND_PALETTE = False

        USAGE_LOG_LIMIT = 100
        DASH_TOP = 5            # rows in the Dashboard's Top models / Top tools
        DETAIL_LIMIT = 200      # rows on a history / model-response screen

        def __init__(self, db_path, clock=None, config=None):
            super().__init__()
            self.db_path = str(db_path)
            # Settings come from ~/.config/cbut/config.toml (or CBUT_CONFIG), with the
            # environment able to override one key. Tests pass a dict to avoid reading a
            # real file; a broken file raises ConfigError, which main() turns into a
            # message rather than a traceback.
            self.config = dict(db.DEFAULTS) if config is None else dict(config)
            self.DASH_TOP = self.config["top_n"]
            self.USAGE_LOG_LIMIT = self.config["log_limit"]
            self.DETAIL_LIMIT = self.config["detail_limit"]
            # Shadows WideTableMixin.NAME_CAP for this host and every screen pushed
            # on top of it; the property ``name_cap`` is what reads it.
            self.NAME_CAP = self.config["name_cap"]
            # Injectable wall clock (seconds since the epoch). Defaults to
            # time.time, so a real run is unchanged; tests pass a fixed value to
            # pin "now". The calendar-day windows are derived from this clock,
            # so without a pin a `now - 1h` fixture falls out of a Today window
            # when the suite runs in the first hour after local midnight.
            self._clock = clock or time.time
            self._auto_sync = True
            self._sync_running = False
            self._sync_pending = False
            self.sync_mod = None
            # Per-tab range state: each tab keeps its own window, so changing
            # one tab's range never moves another's. Entity tabs default to 7
            # days; the Dashboard and the Usage page default to Today; Plugins
            # has no window (all-time).
            self.tab_range = {
                "tab-dashboard": "1d",
                "tab-tools": "7d", "tab-skills": "7d", "tab-agents": "7d",
                "tab-mcp": "7d", "tab-tokens": "7d", "tab-usage": "1d",
            }
            self._last_refresh = None      # epoch ms
            self._last_sync = None         # epoch ms
            self._usage_ready = False
            # table id -> (rows, empty message, key index, key kind) for the two
            # wide tables, so a resize can re-plan them without re-querying.
            self._wide_rows = {}
            # Set from migrate() on mount: "" when the schema is usable,
            # otherwise the reason it is not ("newer", "failed: …").
            self._schema_note = ""
            # The status line is composed from these in one function
            # (`_status_line`), because several events change it — a full
            # refresh, a failed re-query, the `s` toggle — and a line overwritten
            # by one of them used to stay wrong until an unrelated refresh.
            self._overview = None            # db.overview() dict, or None
            self._status_note = ""           # last failure the bar is reporting
            # Bumped by every report, so a refresh can tell "nothing complained
            # during this call" from "this call cleared the old warning" without
            # comparing message text.
            self._report_seq = 0

        def compose(self) -> ComposeResult:
            yield TopBar(self.TITLE, with_range=True,
                         range_value=self.tab_range[self.INITIAL_TAB],
                         id="topbar")
            with TabbedContent(initial=self.INITIAL_TAB):
                with TabPane("Dashboard", id="tab-dashboard"):
                    yield Vertical(
                        # Row 1: headline counts, tokens, cache totals.
                        Horizontal(
                            Vertical(
                                Static("Usage", classes="panel-title"),
                                Static(id="dash-kpi-tools"),
                                Static(id="dash-kpi-skills"),
                                Static(id="dash-kpi-agents"),
                                Static(id="dash-kpi-mcp"),
                                Static(id="dash-kpi-plugins"),
                                classes="usage-panel", id="panel-dash-kpi",
                            ),
                            Vertical(
                                Static("Tokens", classes="panel-title"),
                                Static(id="dash-tok-requests"),
                                Static(id="dash-tok-input"),
                                Static(id="dash-tok-output"),
                                Static(id="dash-tok-api"),
                                Static(id="dash-tok-usage"),
                                Static(id="dash-tok-hit-rate"),
                                classes="usage-panel", id="panel-dash-tokens",
                            ),
                            Vertical(
                                Static("Cache", classes="panel-title"),
                                Static(id="dash-cache-hit"),
                                Static(id="dash-cache-miss"),
                                Static(id="dash-cache-write"),
                                classes="usage-panel", id="panel-dash-cache",
                            ),
                            classes="dash-row", id="dash-row-1",
                        ),
                        # Row 2: the leaderboards.
                        Horizontal(
                            Vertical(
                                Static("Top models", classes="panel-title"),
                                Static(id="dash-models"),
                                classes="usage-panel", id="panel-dash-models",
                            ),
                            Vertical(
                                Static("Top tools", classes="panel-title"),
                                Static(id="dash-tools"),
                                classes="usage-panel", id="panel-dash-tools",
                            ),
                            classes="dash-row", id="dash-row-2",
                        ),
                        # Row 3: runtime health + the per-day trend.
                        Horizontal(
                            Vertical(
                                Static("Runtime", classes="panel-title"),
                                Static(id="dash-rt-completed"),
                                Static(id="dash-rt-incomplete"),
                                Static(id="dash-rt-avg"),
                                Static(id="dash-rt-sessions"),
                                Static(id="dash-rt-projects"),
                                classes="usage-panel", id="panel-dash-runtime",
                            ),
                            Vertical(
                                Static("Calls per day", classes="panel-title"),
                                Static(id="dash-activity"),
                                Static(id="dash-activity-axis"),
                                classes="usage-panel", id="panel-dash-activity",
                            ),
                            classes="dash-row", id="dash-row-3",
                        ),
                        Static(id="dash-note"),
                        id="dash-box",
                    )
                with TabPane("Tools", id="tab-tools"):
                    yield Static(id="colnote-tools", classes="col-note")
                    yield DataTable(id="t-tools", zebra_stripes=True)
                with TabPane("Skills", id="tab-skills"):
                    yield Static(id="colnote-skills", classes="col-note")
                    yield DataTable(id="t-skills", zebra_stripes=True)
                with TabPane("Agents", id="tab-agents"):
                    yield Static(id="colnote-agents", classes="col-note")
                    yield DataTable(id="t-agents", zebra_stripes=True)
                with TabPane("Plugins", id="tab-plugins"):
                    yield Static(id="colnote-plugins", classes="col-note")
                    yield DataTable(id="t-plugins", zebra_stripes=True)
                with TabPane("MCP", id="tab-mcp"):
                    yield Static(id="colnote-mcp", classes="col-note")
                    yield DataTable(id="t-mcp", zebra_stripes=True)
                with TabPane("Tokens", id="tab-tokens"):
                    yield Static(id="colnote-tokens", classes="col-note")
                    yield DataTable(id="t-tokens", zebra_stripes=True)
                with TabPane("Usage", id="tab-usage"):
                    yield Vertical(
                        Horizontal(
                            Vertical(
                                Static("Window / Requests", classes="panel-title"),
                                Static(id="usage-window"),
                                Static(id="sum-requests"),
                                Static(id="sum-missing"),
                                classes="usage-panel", id="panel-window",
                            ),
                            Vertical(
                                Static("Model tokens", classes="panel-title"),
                                Static(id="sum-usage-total"),
                                Static(id="sum-input"),
                                Static(id="sum-output"),
                                Static(id="sum-total"),
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
                            id="usage-panels", classes="summary-panels",
                        ),
                        Static(id="usage-note"),
                        Static(id="colnote-usage", classes="col-note"),
                        DataTable(id="t-usage", zebra_stripes=True),
                        id="usage-box",
                    )
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            # One source of column truth: the registry the plan reads is the registry
            # that creates the columns, so a table cannot gain a column the plan does
            # not know how to drop.
            for tid, (cols, _note, _chrome) in self.WIDE_TABLES.items():
                t = self.query_one(f"#{tid}", DataTable)
                t.cursor_type = "row"
                t.add_columns(*cols)
            # Best-effort schema upgrade: an old DB lacks the columns every usage
            # query needs. migrate() returns a status now, so a locked or damaged
            # file is named on the status line instead of showing an empty panel
            # as though that were the user's actual usage.
            note = db.migrate(self.db_path)
            self._schema_note = "" if note in ("current", "migrated") else note
            self._usage_ready = True
            # Auto sync (incremental, ~30s) + UI refresh (~5s). Both are timers,
            # not subprocesses; the sync runs in a worker thread (see _do_sync).
            self.set_interval(self.config["sync_secs"], self._auto_sync_tick)
            self.set_interval(self.config["refresh_secs"], self._auto_refresh_tick)
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



        def _set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        # --- narrow-terminal column plan -------------------------------------





        def _fill_tools(self, conn, start=None, end=None) -> None:
            self._fill_wide(
                "t-tools",
                [[r["tool_name"], f"{r['calls']:,}", f"{r['completed']:,}",
                  f"{r['failed']:,}", ms(r["avg_ms"]), ts(r["last_used"])]
                 for r in db.q_tools(conn, start_ts=start, end_ts=end)],
                "No tool calls yet", key_kind="tool")

        def _fill_skills(self, conn, start=None, end=None) -> None:
            self._fill_wide(
                "t-skills",
                [[r["skill"], f"{r['calls']:,}", f"{r['completed']:,}",
                  ts(r["last_used"])]
                 for r in db.q_skills(conn, start_ts=start, end_ts=end)],
                "No skills used yet", key_kind="skill")

        def _fill_agents(self, conn, start=None, end=None) -> None:
            self._fill_wide(
                "t-agents",
                [[r["agent_type"], f"{r['calls']:,}", ts(r["last_used"])]
                 for r in db.q_agents(conn, start_ts=start, end_ts=end)],
                "No agents used yet", key_kind="agent")

        def _fill_plugins(self, conn, start=None, end=None) -> None:
            # No time window: the plugin list is all-time (the inventory carries
            # no timestamps), so start/end are accepted for the shared fill
            # signature but ignored. Name is the key; version is not shown.
            self._fill_wide(
                "t-plugins",
                [[r["plugin"], f"{r['uses']:,}", f"{r['skills']:,}",
                  f"{r['agents']:,}", f"{r['commands']:,}"]
                 for r in db.q_plugins(conn)],
                "No plugins installed")

        def _fill_mcp(self, conn, start=None, end=None) -> None:
            # Row key is unique per (server, tool): v_mcp groups by both, so two
            # tools under one server would otherwise collide and abort the mount.
            self._fill_wide(
                "t-mcp",
                [[r["server"], r["tool"], f"{r['calls']:,}",
                  f"{r['completed']:,}", ts(r["last_used"])]
                 for r in db.q_mcp(conn, start_ts=start, end_ts=end)],
                "No MCP calls yet", key_index=(0, 1), key_kind="mcp")

        def _fill_tokens(self, conn, start=None, end=None) -> None:
            # Reuses q_model_tokens; never re-parses transcripts and never mixes
            # in sessions.tokens (tokenDelta). Total = API total (provider total
            # when present). Usage Total is the display-only re-add of cache hit.
            rows = [[r["model"], fmt_n(r["usage_total_tokens"]),
                     f"{r['responses']:,}",
                     fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                     fmt_n(r["total_tokens"]),
                     fmt_n(r["prompt_cache_hit_tokens"]),
                     fmt_n(r["prompt_cache_miss_tokens"]),
                     fmt_n(r["prompt_cache_write_tokens"])]
                    for r in db.q_model_tokens(conn, start_ts=start, end_ts=end)]
            self._fill_wide("t-tokens", rows, "No model responses yet",
                            key_kind="model")

        # pane id -> the fill method for that tab (usage handled separately).
        _PANE_FILL = {
            "tab-dashboard": "_fill_dashboard",
            "tab-tools": "_fill_tools", "tab-skills": "_fill_skills",
            "tab-agents": "_fill_agents", "tab-plugins": "_fill_plugins",
            "tab-mcp": "_fill_mcp", "tab-tokens": "_fill_tokens",
        }

        def refresh_data(self, conn=None) -> None:
            """Full refresh: every tab + the status bar.

            Used on mount and after a sync that actually indexed new files. Pass
            an existing readonly ``conn`` to avoid opening a second one.
            """
            seq = self._report_seq
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as exc:
                    self._report_query_error(exc)
                    return
            try:
                # Each tab is filled with its own window, so one tab's range
                # never moves another's.
                self._fill_dashboard(conn, *self._bounds_for("tab-dashboard"))
                self._fill_tools(conn, *self._bounds_for("tab-tools"))
                self._fill_skills(conn, *self._bounds_for("tab-skills"))
                self._fill_agents(conn, *self._bounds_for("tab-agents"))
                self._fill_plugins(conn)          # all-time, no window
                self._fill_mcp(conn, *self._bounds_for("tab-mcp"))
                self._fill_tokens(conn, *self._bounds_for("tab-tokens"))
                o = db.overview(conn)          # status bar stays all-time
                if self._usage_ready:
                    self._refresh_usage(conn)   # same connection, no second open
            except sqlite3.Error as exc:
                # This runs during on_mount: letting it raise kills the app
                # before it draws a frame, and the other alternative — panels of
                # zeros — is indistinguishable from a quiet day.
                self._report_query_error(exc)
                return
            finally:
                if own:
                    conn.close()
            self._overview = o
            if self._report_seq == seq:
                # Nothing complained during a refresh that re-queried every tab,
                # so any warning the bar was carrying has been disproved.
                self._status_note = ""
            self._set_status(self._status_line())

        def _status_line(self) -> str:
            """The status line, composed from what the app currently knows.

            One composer because `s`, a failed re-query and a sync with nothing to
            index each used to overwrite the line with a message that then stayed
            there — "Syncing…" outliving a finished sync was the visible one.
            """
            if self._overview is None:
                return f"! {self._status_note}" if self._status_note else ""
            o = self._overview
            status = (
                f"db={self.db_path} · {count_n(o['tool_calls'], 'tool call')} · "
                f"{count_n(o['skills'], 'skill')} · "
                f"{count_n(o['agents'], 'agent')} · "
                f"{count_n(o['plugins_used'], 'plugin')} · "
                f"{o['mcp']:,} mcp · "
                f"{count_n(o['model_responses'], 'model response')} · "
                f"last {ts(o['last_ts'])}"
            )
            if not self._auto_sync:
                status += " · auto-sync OFF"
            if o["unparsed_records"]:
                # Keep the status line honest: panels showing 0 while records
                # went unclaimed means the format moved, not that usage stopped.
                status += f" · ! {count_n(o['unparsed_records'], 'record')} unparsed"
            if self._schema_note:
                status += f" · schema: {self._schema_note}"
            if self._status_note:
                # Every number above is the last known one, not the current one.
                status += f" · ! {self._status_note}"
            return status

        def _report_query_error(self, exc: sqlite3.Error) -> None:
            """Name a failed re-query instead of leaving the old page on screen.

            A tab that could not be refreshed keeps its previous rows, which look
            exactly like usage the user still has; a locked or damaged file has to
            say so in the one place the user reads.
            """
            self._report_seq += 1
            self._status_note = f"{type(exc).__name__}: {exc}"
            self._set_status(self._status_line())

        def _refresh_active_tab(self, conn=None) -> None:
            """Refresh only the visible tab (5s timer / tab activation).

            Hidden tabs are left alone; they are populated when the user opens
            them (see ``on_tabbed_content_tab_activated``).
            """
            if not self._usage_ready:
                return
            seq = self._report_seq
            pane = self.query_one(TabbedContent).active
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as exc:
                    self._report_query_error(exc)
                    return
            try:
                if pane == "tab-usage":
                    self._refresh_usage(conn)
                else:
                    fn = self._PANE_FILL.get(pane)
                    if fn:
                        # Plugins ignores its bounds (all-time).
                        getattr(self, fn)(conn, *self._bounds_for(pane))
            except sqlite3.Error as exc:
                self._report_query_error(exc)
                return
            finally:
                if own:
                    conn.close()
            if self._report_seq == seq and self._status_note:
                # These queries went through and nothing in them complained, so
                # the bar's warning has been disproved. No full refresh is owed:
                # a hidden tab is re-queried the moment the user opens it.
                self._status_note = ""
                self._set_status(self._status_line())

        def on_tabbed_content_tab_activated(
                self, event: TabbedContent.TabActivated) -> None:
            # Populate the tab the user just switched to (hidden tabs are not
            # polled by the 5s timer) and point the range Select at its window.
            if self._usage_ready:
                self._sync_range_widget()
                self._refresh_active_tab()

        # --- Usage Statistics page -----------------------------------------

        def _render_usage_status(self) -> None:
            self.query_one("#usage-status", Static).update(
                f"Auto sync: {'ON' if self._auto_sync else 'OFF'}\n"
                f"Last sync: {local_time(self._last_sync)}\n"
                f"Last refresh: {local_time(self._last_refresh)}"
            )

        @staticmethod
        def _panel_line(label: str, value: str) -> str:
            # Fixed-width label + right-aligned value keeps the panels aligned
            # without relying on text-align. 12 + 12 = 24 columns, so the line
            # does not wrap inside a quarter-width panel at 120+ columns.
            return f"[dim]{label:<12}[/dim]{value:>12}"

        # Below this width the panels stack instead of sitting side by side:
        # each column needs ~28 cells for a 24-char panel line plus
        # border/padding, so 4 x 28 = 112.
        COMPACT_WIDTH = 112
        # A panel line is 24 columns of content plus 2 border and 2 padding, so a
        # two-up cell needs 28. Measured: the panels region is 2 cells narrower
        # than the terminal, so two columns stop fitting below 60.
        STACKED_WIDTH = 60

        # Every table on this screen: id -> (its columns in display order, the note
        # that names what the plan dropped, the cells it loses to its surroundings).
        # Chrome is 2 for a table sitting directly in a tab pane and 4 on the padded
        # Usage page — measured at 80/100/160 columns, not derived. A table that fits
        # keeps every column and shows no note, so registering one that does not need
        # the plan costs nothing.
        WIDE_TABLES = {
            "t-tools": (TOOLS_COLUMNS, "colnote-tools", 2),
            "t-skills": (SKILLS_COLUMNS, "colnote-skills", 2),
            "t-agents": (AGENTS_COLUMNS, "colnote-agents", 2),
            "t-plugins": (PLUGINS_COLUMNS, "colnote-plugins", 2),
            "t-mcp": (MCP_COLUMNS, "colnote-mcp", 2),
            "t-tokens": (TOKEN_COLUMNS, "colnote-tokens", 2),
            "t-usage": (USAGE_COLUMNS, "colnote-usage", 4),
        }

        def _apply_responsive_layout(self, width=None) -> None:
            # Panels side by side on wide terminals; two-up when narrow; stacked
            # below that, where a second column would wrap a value mid-line. Every
            # summary page (Dashboard + Usage) uses .summary-panels. Only
            # .summary-panels has a rule for `stacked`; dash rows never two-up.
            if width is None:
                width = self.size.width
            try:
                compact = width < self.COMPACT_WIDTH
                stacked = width < self.STACKED_WIDTH
                for selector in (".summary-panels", ".dash-row"):
                    for el in self.query(selector):
                        el.set_class(compact, "compact")
                        el.set_class(stacked, "stacked")
            except Exception:
                pass

        def on_resize(self, event) -> None:  # textual.events.Resize
            # event.size carries the NEW size (self.size still lags here).
            self._apply_responsive_layout(event.size.width)
            self._plan_wide_tables(event.size.width)

        def _range_of(self, pane):
            """The window key for a pane, or ``None`` for the all-time Plugins
            tab (which is not in ``self.tab_range``)."""
            return self.tab_range.get(pane)

        def _now_ms(self) -> int:
            """The current time in epoch milliseconds, via the injectable clock."""
            return int(self._clock() * 1000)

        def _bounds_for(self, pane):
            """``(start_ms, end_ms)`` for a pane's **own** window, from now.

            Never cached: every refresh derives the window from the current
            time, so a long-running app does not drift. ``None`` as the start
            means all-time (the Plugins tab).
            """
            rng = self._range_of(pane)
            now = self._now_ms()
            if rng is None:
                return None, now
            return db.window_bounds(rng, now)

        def _sync_range_widget(self) -> None:
            """Point the shared range Select at the active tab's own window.

            Every tab keeps its own range (``self.tab_range``), so switching tabs
            shows that tab's range, not the one you last picked elsewhere. The
            Plugins tab has no window, so the Select is hidden there. Setting
            ``Select.value`` posts a ``Changed`` message, which
            ``on_select_changed`` ignores when it already matches the active
            tab's range (so this never re-triggers a refresh loop).
            """
            try:
                sel = self.query_one("#range", Select)
            except Exception:
                return
            pane = self.query_one(TabbedContent).active
            if pane == "tab-plugins":
                sel.display = False
                return
            sel.display = True
            value = self._range_of(pane)
            if value is not None and sel.value != value:
                sel.value = value

        def _render_usage_window(self) -> None:
            # Dates only: the window is a set of whole local calendar days, so
            # a clock time here would be noise.
            start, end = self._bounds_for("tab-usage")
            label = db.USAGE_RANGE_LABELS[self.tab_range["tab-usage"]]
            end_date = datetime.fromtimestamp(end / 1000).strftime("%Y-%m-%d")
            if start is None:
                self.query_one("#usage-window", Static).update(
                    f"{label}\n{end_date}")
            else:
                start_date = datetime.fromtimestamp(
                    start / 1000).strftime("%Y-%m-%d")
                # "Today" is one calendar day: printing it as a range of the same
                # date twice read like a bug, and every 1d window hit it.
                span = (start_date if start_date == end_date
                        else f"{start_date} — {end_date}")
                self.query_one("#usage-window", Static).update(f"{label}\n{span}")

        def _refresh_usage(self, conn=None) -> None:
            # Recompute the window from the CURRENT time on every refresh — never
            # a cached/opening-time range.
            if not self._usage_ready:
                return
            now_ms = self._now_ms()
            start, end = self._bounds_for("tab-usage")
            self._last_refresh = now_ms
            self._render_usage_window()
            self._render_usage_status()
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as e:
                    self.query_one("#usage-status", Static).update(
                        f"usage unavailable: {e}")
                    self._report_query_error(e)
                    return
            try:
                s = db.q_usage_summary(conn, start, end)
                self._render_summary_panels(s)
                self._fill_usage_logs(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#usage-status", Static).update(
                    f"usage query failed ({e}); run: cbut sync")
                # The page note is local; the bar is what a user reads from
                # another tab, so the failure has to reach both.
                self._report_query_error(e)
            finally:
                if own:
                    conn.close()
            self._apply_responsive_layout()

        def _render_summary_panels(self, s: dict) -> None:
            def put(wid: str, label: str, val: str) -> None:
                self.query_one(wid, Static).update(self._panel_line(label, val))

            req = s["requests"] or 0
            put("#sum-requests", "Requests", f"{req:,}")
            # Usage Total = Input + Output + cache hit.
            put("#sum-usage-total", "Usage Total",
                fmt_n(s["usage_total_tokens"]))
            put("#sum-input", "Input", fmt_n(s["prompt_tokens"]))
            put("#sum-output", "Output", fmt_n(s["completion_tokens"]))
            put("#sum-total", "API Total", fmt_n(s["total_tokens"]))
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
                missing.append(f"{s['missing_prompt']:,} missing input")
            if s["missing_completion"]:
                missing.append(f"{s['missing_completion']:,} missing output")
            if req == 0:
                note = "no data in window"
            elif missing:
                note = ", ".join(missing)
            else:
                note = "complete"
            # The panel reports completeness either way; naming it "Incomplete"
            # made the good case read "Incomplete: complete".
            put("#sum-missing", "Completeness", note)

        # --- Dashboard page ------------------------------------------------

        def _fill_dashboard(self, conn, start=None, end=None) -> None:
            """Render every Dashboard panel for this tab's own window.

            All panels reflect the Dashboard's window only — the other tabs are
            untouched. Every query takes the same ``(start, end)`` so the tab
            always agrees with itself.
            """
            try:
                kpi = db.q_usage_kpi(conn, start, end)
                s = db.q_usage_summary(conn, start, end)
                act = db.q_usage_activity(conn, start, end)
                models = db.q_usage_model_stats(conn, start, end)
                tools = db.q_tools(conn, limit=self.DASH_TOP,
                                   start_ts=start, end_ts=end)
                daily = db.q_usage_daily(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#dash-note", Static).update(
                    f"dashboard query failed ({e}); run: cbut sync")
                self._report_query_error(e)
                return
            self._render_dashboard_kpi(kpi)
            self._render_dashboard_tokens(s)
            self._render_dashboard_cache(s)
            self._render_dashboard_runtime(act)
            self._render_dashboard_models(models)
            self._render_dashboard_tools(tools)
            self._render_dashboard_activity(daily, start, end)
            if not any(kpi.values()) and not (s["requests"] or 0):
                self.query_one("#dash-note", Static).update(
                    "No data in this window — widen the range above.")
            else:
                self.query_one("#dash-note", Static).update(
                    "Range: the top bar · this tab keeps its own window")
            self._apply_responsive_layout()

        def _put_panel(self, wid: str, label: str, val: str) -> None:
            self.query_one(wid, Static).update(self._panel_line(label, val))

        def _render_dashboard_kpi(self, k: dict) -> None:
            # COUNT(*) is authoritative -> a real 0, never a dash.
            self._put_panel("#dash-kpi-tools", "Tool calls", f"{k['tool_calls']:,}")
            self._put_panel("#dash-kpi-skills", "Skills", f"{k['skills']:,}")
            self._put_panel("#dash-kpi-agents", "Agents", f"{k['agents']:,}")
            self._put_panel("#dash-kpi-mcp", "MCP", f"{k['mcp']:,}")
            self._put_panel("#dash-kpi-plugins", "Plugins", f"{k['plugins']:,}")

        def _render_dashboard_tokens(self, s: dict) -> None:
            self._put_panel("#dash-tok-requests", "Requests", f"{s['requests'] or 0:,}")
            self._put_panel("#dash-tok-input", "Input", fmt_n(s["prompt_tokens"]))
            self._put_panel("#dash-tok-output", "Output", fmt_n(s["completion_tokens"]))
            self._put_panel("#dash-tok-api", "API Total", fmt_n(s["total_tokens"]))
            self._put_panel("#dash-tok-usage", "Usage Total", fmt_n(s["usage_total_tokens"]))
            self._put_panel("#dash-tok-hit-rate", "Hit rate",
                            self._hit_rate(s["cache_hit"], s["cache_miss"],
                                           s["cache_write"]))

        def _render_dashboard_cache(self, s: dict) -> None:
            self._put_panel("#dash-cache-hit", "Cache hit", fmt_n(s["cache_hit"]))
            self._put_panel("#dash-cache-miss", "Cache miss", fmt_n(s["cache_miss"]))
            self._put_panel("#dash-cache-write", "Cache write", fmt_n(s["cache_write"]))

        def _render_dashboard_runtime(self, a: dict) -> None:
            # counts are COUNT-based -> a real 0; avg_ms is NULL when nothing ran.
            self._put_panel("#dash-rt-completed", "Completed", f"{a['completed'] or 0:,}")
            self._put_panel("#dash-rt-incomplete", "Incomplete", f"{a['incomplete'] or 0:,}")
            self._put_panel("#dash-rt-avg", "Avg tool time", ms(a["avg_ms"]))
            self._put_panel("#dash-rt-sessions", "Sessions", f"{a['sessions'] or 0:,}")
            self._put_panel("#dash-rt-projects", "Projects", f"{a['projects'] or 0:,}")

        def _render_dashboard_models(self, rows) -> None:
            top = rows[: self.DASH_TOP]
            if not top:
                self.query_one("#dash-models", Static).update("nothing in this window")
                return
            lines = [f"{i}. {clip(r['model'], 26):<26}{fmt_n(r['total_tokens']):>12}"
                     f"  {r['requests']:>5} req"
                     for i, r in enumerate(top, 1)]
            self.query_one("#dash-models", Static).update("\n".join(lines))

        def _render_dashboard_tools(self, rows) -> None:
            # The name-keyed list keeps 0-call tools under a window; drop them.
            top = [r for r in rows if r["calls"]][: self.DASH_TOP]
            if not top:
                self.query_one("#dash-tools", Static).update("nothing in this window")
                return
            lines = [f"{i}. {clip(r['tool_name'], 22):<22}"
                     f"{count_n(r['calls'], 'call'):>12}"
                     f" · {ms(r['avg_ms'])}"
                     for i, r in enumerate(top, 1)]
            self.query_one("#dash-tools", Static).update("\n".join(lines))

        # Block characters for the per-day sparkline (index 0 = no activity).
        _SPARK = " ▁▂▃▄▅▆▇█"

        def _sparkline(self, values) -> str:
            peak = max(values) if values else 0
            if peak <= 0:
                # A quiet window still draws something: an empty string left a
                # blank region on the Dashboard, which reads as "widget broken"
                # rather than "no calls". A dotted baseline says the same thing
                # as the zero-height bars it replaces, without inventing a bar.
                return "·" * len(values)
            out = []
            for v in values:
                level = 0 if v <= 0 else min(8, max(1, round(v / peak * 8)))
                out.append(self._SPARK[level])
            return "".join(out)

        def _render_dashboard_activity(self, daily, start, end) -> None:
            """A one-line sparkline of calls per local day across the window.

            ``daily`` only carries days that had calls, so the gaps are filled
            from the window bounds. For the all-time range the series starts at
            the first day with data (never from the epoch).
            """
            counts = {r["day"]: r["calls"] for r in daily}
            now = self._now_ms() if end is None else end
            end_day = datetime.fromtimestamp(now / 1000).date()
            if start is not None:
                start_day = datetime.fromtimestamp(start / 1000).date()
            elif daily:
                start_day = datetime.strptime(daily[0]["day"], "%Y-%m-%d").date()
            else:
                start_day = end_day
            days, d = [], start_day
            while d <= end_day:
                days.append(d)
                d += timedelta(days=1)
            series = [counts.get(day.isoformat(), 0) for day in days]
            self.query_one("#dash-activity", Static).update(self._sparkline(series))
            # The day list is never empty (a window always contains at least
            # today), so the branch that matters is "no calls in it", not
            # "no days" — the old `if series:` test was always true.
            if any(series):
                self.query_one("#dash-activity-axis", Static).update(
                    f"{days[0]:%m-%d} → {days[-1]:%m-%d} · peak {max(series):,}/day")
            else:
                self.query_one("#dash-activity-axis", Static).update(
                    f"{days[0]:%m-%d} → {days[-1]:%m-%d} · no activity")

        @staticmethod
        def _hit_rate(hit, miss, write) -> str:
            frac = db.cache_hit_rate(hit, miss, write)
            return "-" if frac is None else f"{frac * 100:.1f}%"

        def _fill_usage_logs(self, conn, start, end) -> None:
            rows = db.q_usage_request_logs(conn, start, end,
                                           limit=self.USAGE_LOG_LIMIT)
            self.query_one("#usage-note", Static).update(
                "Newest first · cache hit=read, write=create · "
                "no cost/duration/status" if rows else "")
            out = [[
                local_time(r["ts"]), r["model"] or "-",
                fmt_n(r["usage_total_tokens"]),
                fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                fmt_n(r["total_tokens"]),
                fmt_n(r["prompt_cache_hit_tokens"]),
                fmt_n(r["prompt_cache_miss_tokens"]),
                fmt_n(r["prompt_cache_write_tokens"]),
                self._hit_rate(r["prompt_cache_hit_tokens"],
                               r["prompt_cache_miss_tokens"],
                               r["prompt_cache_write_tokens"]),
            ] for r in rows]
            # The shared empty state, like the other seven tables: a note above an
            # untouched table reads as a page that failed to load.
            self._fill_wide("t-usage", out,
                            "No requests in this window — widen the range above.")

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id != "range":
                return
            pane = self.query_one(TabbedContent).active
            if pane == "tab-plugins":
                return                    # Plugins has no window
            if event.value == self._range_of(pane):
                return                    # programmatic sync, not a user change
            self.tab_range[pane] = event.value
            if pane == "tab-usage":
                self._render_usage_window()   # keep the Usage Window panel live
            self._refresh_active_tab()    # re-query only the visible tab

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
            # The line itself carries the state ("· auto-sync OFF"); a bespoke
            # message here used to wipe the counts off the bar until an unrelated
            # refresh happened to put them back.
            self._set_status(self._status_line())
            self.notify(f"Auto sync: {'ON' if self._auto_sync else 'OFF'}")
            if self._usage_ready:
                self._render_usage_status()

        # --- automatic sync / refresh timers --------------------------------

        def _auto_sync_tick(self) -> None:
            # The same shutdown guard as _auto_refresh_tick (D-003). A timer can
            # fire once more while the app is closing; _request_sync then reaches
            # for an event loop that is already gone and raises
            # "RuntimeError: no running event loop", leaving a worker coroutine
            # that is never awaited.
            if not self.is_running:
                return
            if self._auto_sync:
                self._request_sync()

        def _auto_refresh_tick(self) -> None:
            # Only the visible tab is re-queried; hidden tabs refresh when the
            # user opens them. The is_running guard matters because a timer can
            # fire once more while the app is shutting down (e.g. run_test's
            # teardown), after the widgets are gone — query_one then raises
            # NoMatches from inside the timer and fails the test run.
            if not self.is_running:
                return
            self._refresh_active_tab()

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
                    self.sync_mod = db.load_sync()
                stats = self.sync_mod.run(quiet=True, db_path=self.db_path)
                self.call_from_thread(self._on_sync_done, True, stats, None)
            except Exception as e:  # report, never swallow
                self.call_from_thread(self._on_sync_done, False, None, str(e))

        def _on_sync_done(self, ok: bool, stats, err) -> None:
            self._sync_running = False
            if ok:
                self._last_sync = self._now_ms()
                if (stats or {}).get("files_indexed", 0) > 0:
                    self.refresh_data()          # new rows can touch any tab
                else:
                    self._refresh_active_tab()   # nothing changed -> light refresh
                    # A light refresh does not rewrite the bar, so without this
                    # the transient "Syncing…" from `r` survives the sync that
                    # already finished.
                    self._set_status(self._status_line())
                self.notify(f"Synced {count_n(stats.get('files_indexed', 0), 'file')}")
            else:
                # The same composer as the data line: a failed sync is a fact
                # *about* the numbers, not a replacement for them.
                self._status_note = "Sync failed: " + (err or "unknown error")[:200]
                self._set_status(self._status_line())
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
    try:
        app = TrackerApp(args.db, config=db.load_config())
    except db.ConfigError as exc:
        # A broken settings file is the user's to fix, and starting with defaults
        # silently would leave them editing a file that is not being read.
        print(f"your settings are not usable: {exc}\n"
              f"  fix or remove that file, or point CBUT_CONFIG somewhere else",
              file=sys.stderr)
        return 2
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

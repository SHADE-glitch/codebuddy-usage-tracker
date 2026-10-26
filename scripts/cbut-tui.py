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
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402

try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Grid, Horizontal, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button, Checkbox, DataTable, Footer, Input, Select, Static,
        TabbedContent, TabPane,
    )

    class Table(DataTable):
        """A DataTable whose enter key is in the footer.

        Textual binds `enter` on DataTable with `show=False`, so the one key
        that opens a row's detail was the one key the footer hid — and an
        app-level `enter` binding cannot fix it, because the focused table's
        own binding wins. Same action, visible name. Only the tables whose rows
        actually open something use it; a table with no detail stays plain, so
        the footer never advertises a dead key.
        """

        BINDINGS = [Binding("enter", "select_cursor", "Open")]

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover - exercised only without the dep
    HAVE_TEXTUAL = False


def state_cell(row, calls):
    """The ``state`` cell for one entity row, dimmed when the entity is gone.

    ``calls`` is the row's count in this tab's window, so the label agrees with
    the row it sits on: 0 calls reads ``installed``, not ``in use``. DataTable
    has no per-row style hook — ``add_row`` takes cells, and the only row-level
    classes it renders are its own header/fixed/zebra ones — so the dimming
    lands on the state cell itself. That is where the information is anyway: a
    whole row greyed out would be a mood, and "removed" written in plain text is
    a fact you can read in a screenshot.
    """
    label = db.entity_state(row["installed"], row["ever_used"], calls)
    return Text(label, style="dim") if label == db.ENTITY_STATE_REMOVED else label


def ts(v):
    if not v:
        return "-"
    return datetime.fromtimestamp(v / 1000).strftime("%m-%d %H:%M")


# A negative duration is not a short call: it is the two records disagreeing
# about their order, and ingest stores NULL for those (plus a migration that
# repairs older databases). This guard is for a read-only headless run against a
# database nobody has migrated yet — "-" is the honest answer, and "-4611ms" is
# a number a reader would believe.
def ms(v):
    if v is None or v < 0:
        return "-"
    return f"{v}ms" if v < 1000 else f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count, showing ``-`` for NULL (never a fabricated 0)."""
    if v is None:
        return "-"
    return f"{v:,}"


def fmt_compact_n(v):
    """A token count shortened for a panel line, never for a table.

    The dashboard leaderboard is half the terminal, so a 13-character exact
    count ("7,917,931,247") pushed the request count onto a row of its own and
    the ranking read as broken. The tables keep the exact grouped value; a
    panel only needs the magnitude, and this keeps the field at seven cells as
    the numbers grow.
    """
    if v is None:
        return "-"
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= scale:
            # Two decimals at every scale, so the column's points line up.
            return f"{v / scale:.2f}{unit}"
    return f"{v:,}"


def fmt_credit(v):
    """One provider-reported credit; ``-`` when this row carried none."""
    if v is None:
        return "-"
    if not v:
        return "0"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def fmt_credit_total(value, rows):
    """An aggregate of credits: ``-`` when no row inside it reported one.

    A route that writes no ``credit`` leaves NULL, so a sum is only a money
    statement when at least one contributing row carried the field. The guard
    also reads the value, so a real non-zero can never be hidden by a bad count.
    """
    if value is None or (not rows and not value):
        return "-"
    return fmt_credit(value)


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
    s = db.clean_cell(text)
    return s if len(s) <= width else s[: width - 1] + "…"


def _row_calls(row):
    """The row's count in its tab's window, for the used-only default view.

    Plugins rows call it `uses` and Tokens rows `responses`; a row
    carrying none of those counts is never "unused" — the Tokens tab is
    usage-only by construction and always answers.
    """
    for key in ("calls", "uses", "responses"):
        try:
            return row[key]
        except (IndexError, KeyError):
            continue
    return 1


def model_leader_labels(rows):
    """Display labels for the Dashboard's Top models rows, in row order.

    `q_usage_model_stats` groups by (model, provider), so one model name
    can appear twice in the list — two routes serving one name. Without the
    provider the two rows read as a duplicate; a model with a single route
    keeps its bare name.
    """
    seen = {}
    for r in rows:
        seen[r["model"]] = seen.get(r["model"], 0) + 1
    return [f"{r['model']} · {db.provider_label(r['provider'])}"
            if seen[r["model"]] > 1 else r["model"] for r in rows]


def local_time(ms):
    """Local-time render of an epoch-ms value (query bounds stay in Unix ms)."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# Usage-page option list (values mirror cbut_db so the two stay in sync).
# Windows are whole local calendar days ending now ("Today", "2 days", …).
# "Custom…" is the escape hatch to the date picker; it is never stored as a
# tab's range (a picked range is a db.CustomWindow instead).
RANGE_OPTIONS = [(db.USAGE_RANGE_LABELS[k], k)
                 for k in ("1d", "2d", "3d", "7d", "14d", "30d", "all")] + [
    ("Custom…", "custom")]
CUSTOM_RANGE = "custom"
# The Usage page is a single Request Logs list, in the family's order: time ·
# model · provider · usage total · credits · the token family · cache detail.
# cc-switch's cost / duration / HTTP-status columns have no transcript source,
# so they are deliberately absent rather than estimated; the provider has one
# (providerData.requestModelId), and credits do too (rawUsage.credit).
USAGE_COLUMNS = ("Time", "Model", "Provider", "Usage Total", "Credits",
                 "Input", "Output", "Cache hit", "Cache miss",
                 "Cache write", "Cache hit rate")

# Tokens-tab column order mirrors the family: Provider right after the model,
# then Usage Total and Credits, the request count, the token parts, the cache
# detail and last use. The near-always-100% "With usage" and "Coverage" columns
# were dropped; the rest are measurements the transcripts really carry.
TOKEN_COLUMNS = ("Model", "Provider", "Usage Total", "Credits", "Requests",
                 "Input", "Output", "Cache hit", "Cache miss",
                 "Cache write", "Cache hit rate", "Last used")

# Entity tabs and the two detail screens. Declared here rather than inline at
# `add_columns` because the column plan needs the full list to compute a prefix from,
# and a plan that cannot name the columns it dropped is a silent truncation wearing a
# guard's clothes.
#
# ``state`` (see `db.entity_state`) is last, and the narrow-terminal plan protects
# it: the tail drops first, so a narrow terminal loses "last used" or "avg" before
# it loses the answer to "is this thing still installed". Column 0 is protected for
# the same reason — a table without its name column cannot be read at all.
# The header is the constant the label helper and the tests use, not a retyped
# string: a renamed column would otherwise keep rendering under the old name here
# while everything that agrees on the name moved.
_STATE = db.ENTITY_STATE_COLUMN
# ``pend`` sits between ``fail`` and ``avg`` because it is the third answer to
# "what happened to this call": completed, incomplete, or no result record at all
# (a session killed mid-call). Without it ``ok`` + ``fail`` silently fell short of
# ``calls``, and the headless ``cbut tools`` had shown the column all along.
TOOLS_COLUMNS = ("tool", "calls", "ok", "fail", "pend", "avg", "last used", _STATE)
SKILLS_COLUMNS = ("skill", "calls", "ok", "last used", _STATE)
AGENTS_COLUMNS = ("agent", "calls", "last used", _STATE)
PLUGINS_COLUMNS = ("plugin", "uses", "skills", "agents", "cmds", _STATE)
MCP_COLUMNS = ("server", "tool", "calls", "ok", "last used", _STATE)
HISTORY_COLUMNS = ("when", "project", "session", "status", "duration")
# A plugin's "calls" are attributed uses (plugin_usage), which carry no
# status/duration_ms — so the plugin history screen has its own columns instead
# of reusing HISTORY_COLUMNS.
PLUGIN_HISTORY_COLUMNS = ("when", "kind", "target", "marketplace", "session",
                          "project")
# Twelve columns need 170 cells: on an 80-wide terminal more than half of this table
# used to sit past the right edge with no key that would bring it back.
RESPONSE_COLUMNS = ("when", "model", "provider", "usage total", "credits",
                    "prompt", "completion", "cache hit", "cache miss",
                    "cache write", "cache r", "cache w", "usage", "message_id")
# The Dashboard board is a money/reach summary, not a session log: sessions sit
# after the credits so the longest paths cannot push `last used` under the bar.
PROJECT_COLUMNS = ("project", "requests", "usage total", "credits", "sessions",
                   "last used")

# The single-call detail screen renders every column of the row. Labels live here
# rather than inline so the DB column name and what the screen calls it stay one
# mapping, and a column with no entry still shows (it falls back to its name).
CALL_DETAIL_LABELS = {
    "call_id": "call id", "id": "id", "tool_name": "tool", "tool": "tool",
    "agent_type": "agent", "skill": "skill", "plugin": "plugin",
    "category": "category", "model": "model", "provider": "provider",
    "status": "status", "kind": "kind", "source": "source",
    "marketplace": "marketplace", "target": "target", "server": "server",
    "has_args": "has args", "ts": "when", "duration_ms": "duration",
    "session_id": "session", "project": "project", "message_id": "message id",
    "conversation_request_id": "conversation request id",
    "prompt_tokens": "input tokens", "completion_tokens": "output tokens",
    "provider_total_tokens": "provider total",
    "credit": "credits",
    "cache_read_input_tokens": "cache read",
    "cache_creation_input_tokens": "cache creation",
    "prompt_cache_hit_tokens": "cache hit",
    "prompt_cache_miss_tokens": "cache miss",
    "prompt_cache_write_tokens": "cache write",
    # The stored per-row total, named for where it came from: the panels print one
    # token total and it is `usage total`, so a second "api total" beside it read as
    # a rival answer to the same question.
    "total_tokens": "reported total", "total_tokens_source": "total source",
    "usage_available": "usage available", "missing": "missing fields",
}
_DETAIL_TIME_COLS = {"ts"}
_DETAIL_DURATION_COLS = {"duration_ms"}
_DETAIL_BOOL_COLS = {"has_args", "usage_available"}
_DETAIL_TOKEN_COLS = {
    "prompt_tokens", "completion_tokens", "provider_total_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "prompt_cache_write_tokens", "total_tokens",
}


# --- entity-tab registry ---------------------------------------------------
#
# Six tabs differ only in their table id, columns, query, row builder and empty
# message. Declaring that once is what keeps a new entity kind from needing six
# separate edits (columns, pane, range, fill method, WIDE_TABLES entry and the
# refresh list) — the shape that let the Plugins tab miss its window for a whole
# round. Dashboard and Usage are deliberately not here: they draw panels, not a
# table, and their layout is genuinely different.
# The sort cycle every tab shares: the query's own order (mode 0), then these
# fields. ``None`` as the field means "sort by the tab's name key"; descending
# flags are per field, so a count ranking and an A-Z name list can coexist.
_SORT_CALLS = (("calls", "calls", True), ("recent", "last_used", True),
               ("name", None, False), ("fails", "failed", True))
_SORT_SIMPLE = (("calls", "calls", True), ("recent", "last_used", True),
                ("name", None, False))
_SORT_USES = (("uses", "uses", True), ("recent", "last_used", True),
              ("name", None, False))
_SORT_TOKENS = (("requests", "responses", True),
                ("tokens", "usage_total_tokens", True), ("name", None, False))
# The Requests page is newest-first; `s` keeps cycling through those three.
_USAGE_SORT_FIELDS = (("time", "ts", True), ("tokens", "usage_total_tokens", True),
                      ("model", "model", False))


class EntityTab(NamedTuple):
    pane: str
    title: str
    table_id: str
    note_id: str
    columns: tuple
    query: str          # function name on ``cbut_db`` (resolved per call)
    cells: object       # row -> list of cells
    empty: str
    key_kind: str
    key_index: object = 0
    default_range: str = "7d"
    # DB columns a composite key is built from, in the same order as
    # ``key_index``. Empty means "key the visible cells". Tokens needs it: its
    # provider cell is a display label (Built-in), while the drill-down has to
    # filter on the stored value, and the name may be capped on a narrow screen.
    key_columns: tuple = ()
    # (label, raw field, descending) cycled by `s`; the query order is mode 0.
    sort_fields: tuple = _SORT_CALLS
    # The raw row field that names the row for the `name` sort mode.
    name_key: str = "tool_name"


def _tool_row(r):
    return [r["tool_name"], f"{r['calls']:,}", f"{r['completed']:,}",
            f"{r['failed']:,}", f"{r['pending']:,}", ms(r["avg_ms"]),
            ts(r["last_used"]), state_cell(r, r["calls"])]


def _skill_row(r):
    return [r["skill"], f"{r['calls']:,}", f"{r['completed']:,}",
            ts(r["last_used"]), state_cell(r, r["calls"])]


def _agent_row(r):
    return [r["agent_type"], f"{r['calls']:,}", ts(r["last_used"]),
            state_cell(r, r["calls"])]


def _plugin_row(r):
    return [r["plugin"], f"{r['uses']:,}", f"{r['skills']:,}",
            f"{r['agents']:,}", f"{r['commands']:,}",
            state_cell(r, r["uses"])]


def _mcp_row(r):
    return [r["server"], r["tool"], f"{r['calls']:,}", f"{r['completed']:,}",
            ts(r["last_used"]), state_cell(r, r["calls"])]


def _token_row(r):
    hit = db.cache_hit_rate(r["prompt_cache_hit_tokens"],
                            r["prompt_cache_miss_tokens"],
                            r["prompt_cache_write_tokens"])
    return [r["model"], db.provider_label(r["provider"]),
            fmt_n(r["usage_total_tokens"]),
            fmt_credit_total(r["credits"], r["credit_rows"]),
            f"{r['responses']:,}",
            fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
            fmt_n(r["prompt_cache_hit_tokens"]),
            fmt_n(r["prompt_cache_miss_tokens"]),
            fmt_n(r["prompt_cache_write_tokens"]),
            "-" if hit is None else f"{hit * 100:.1f}%",
            ts(r["last_used"])]


ENTITY_TABS = (
    EntityTab("tab-tools", "Tools", "t-tools", "colnote-tools",
              TOOLS_COLUMNS, "q_tools", _tool_row, "No tool calls yet", "tool",
              name_key="tool_name"),
    EntityTab("tab-skills", "Skills", "t-skills", "colnote-skills",
              SKILLS_COLUMNS, "q_skills", _skill_row, "No skills used yet",
              "skill", name_key="skill", sort_fields=_SORT_SIMPLE),
    EntityTab("tab-agents", "Agents", "t-agents", "colnote-agents",
              AGENTS_COLUMNS, "q_agents", _agent_row, "No agents used yet",
              "agent", name_key="agent_type", sort_fields=_SORT_SIMPLE),
    EntityTab("tab-plugins", "Plugins", "t-plugins", "colnote-plugins",
              PLUGINS_COLUMNS, "q_plugins", _plugin_row, "No plugins installed",
              "plugin", name_key="plugin", sort_fields=_SORT_USES),
    EntityTab("tab-mcp", "MCP", "t-mcp", "colnote-mcp",
              MCP_COLUMNS, "q_mcp", _mcp_row, "No MCP calls yet", "mcp",
              key_index=(0, 1), name_key="server", sort_fields=_SORT_SIMPLE),
    EntityTab("tab-tokens", "Tokens", "t-tokens", "colnote-tokens",
              TOKEN_COLUMNS, "q_model_tokens", _token_row,
              "No model responses yet", "model", key_index=(0, 1),
              key_columns=("model", "provider"), name_key="model",
              sort_fields=_SORT_TOKENS),
)


def _call_detail_rows(row):
    """``(label, text)`` for every field of one call row, in row order.

    NULL renders as "-" (never a fabricated 0); a timestamp, a duration and the
    yes/no flags are formatted, tokens are grouped. An unknown column falls back
    to its name, so a column added to the query still appears.
    """
    out = []
    for col, value in row.items():
        label = CALL_DETAIL_LABELS.get(col, col.replace("_", " "))
        if value is None:
            text = "-"
        elif col == "provider":
            # The stored sentinel (and a NULL route) has one display form, and
            # every other screen already uses it: one route, one name.
            text = db.provider_label(value)
        elif col in _DETAIL_TIME_COLS:
            text = local_time(value)
        elif col in _DETAIL_DURATION_COLS:
            text = ms(value)
        elif col in _DETAIL_BOOL_COLS:
            text = "yes" if value else "no"
        elif col in _DETAIL_TOKEN_COLS:
            text = fmt_n(value)
        else:
            text = str(value)
        out.append((label, db.clean_cell(text)))
    return out


if HAVE_TEXTUAL:

    class TopBar(Horizontal):
        """标题栏 + 共享时间范围选择器（取代 textual 的 Header）。

        去掉两个内置行为：点击不再切换 'tall' 高度；没有左上角图标，不会打开命令面板。
        只有主界面带选择器；详情屏用 with_range=False。
        """
        DEFAULT_CSS = """
        TopBar { dock: top; width: 100%; height: 1; background: $panel; color: $foreground; }
        TopBar > #topbar-title { width: 1fr; padding: 0 1; content-align: left middle;
                                 text-wrap: nowrap; text-overflow: ellipsis; }
        TopBar > #date-range { width: auto; min-width: 0; height: 1; border: none;
                               padding: 0 1; background: $panel; color: $accent;
                               text-style: bold; }
        TopBar > #date-range:hover { background: $boost; }
        TopBar > #range { width: 30; }
        TopBar > #range SelectCurrent { border: none; padding: 0 1;
                                        background: $panel; color: $accent; }
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
                # Two controls, two jobs: the Select picks a fixed preset, and the
                # button opens the calendar for a custom span. The button offers
                # "Custom…" while a preset is active and names the span once a
                # custom window is in force.
                yield Button("📅 Custom…", id="date-range")
                yield Select(RANGE_OPTIONS, value=self._range_value,
                             allow_blank=False, id="range")

        def set_range_label(self, text: str, custom: bool = False) -> None:
            """Name the custom span, or offer the picker when a preset is active."""
            try:
                btn = self.query_one("#date-range", Button)
            except Exception:
                return
            btn.label = f"📅 {text} ▾" if custom and text else "📅 Custom…"
            btn.tooltip = text or None
            btn.display = bool(text)
            # A Button does not re-lay-out when its label grows, so the control
            # kept the width it was built with and cut the longer span down until
            # the next full relayout.
            btn.refresh(layout=True)

    class FilterInput(Input):
        """The one-line filter for the active tab.

        Typing filters live (the app re-renders the active tab from its cached
        rows; the Requests page re-queries in SQL); Enter hands the keys back
        to the table but keeps the bar visible, and Esc clears the filter and
        hides the bar. Its own binding runs before the app's, so every
        printable key — `a` included — inserts into the field instead of
        reaching the tab's toggle.
        """

        BINDINGS = [Binding("escape", "close_filter", "Clear filter",
                            show=False)]

        def action_close_filter(self) -> None:
            # Widget-level bindings resolve their action on the widget,
            # not on the app, so this shim forwards Esc to the handler.
            self.app.action_close_filter()

    class TimeInput(Input):
        """A clock field you can scrub as well as type.

        Typing is the fast path (``00:00`` and ``9:5 PM`` are both understood);
        ↑/↓ and the mouse wheel step the value so it can be nudged without the
        keyboard. The step is a per-instance count of minutes, set by the
        picker's step selector, so one key press can move a minute, ten, or an
        hour. The value is re-rendered through ``db.format_time``, so what stays
        in the field is always the canonical 24-hour ``HH:MM``.
        """

        BINDINGS = [
            Binding("up", "nudge_up", "Later", show=False),
            Binding("down", "nudge_down", "Earlier", show=False),
        ]
        STEP = 1  # minutes per nudge, until the picker's step selector says otherwise

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.step = self.STEP

        def action_nudge_up(self) -> None:
            self._nudge(self.step)

        def action_nudge_down(self) -> None:
            self._nudge(-self.step)

        def on_mouse_scroll_up(self, event) -> None:
            self._nudge(self.step)
            event.stop()

        def on_mouse_scroll_down(self, event) -> None:
            self._nudge(-self.step)
            event.stop()

        def _nudge(self, minutes: int) -> None:
            try:
                hour, minute = db.parse_time(self.value)
            except ValueError:
                # An empty or half-typed field nudges from *now*, so the first
                # ↑/↓ yields a valid time instead of an error.
                now = datetime.now()
                hour, minute = now.hour, now.minute
            total = (hour * 60 + minute + minutes) % (24 * 60)
            self.value = db.format_time(total // 60, total % 60)
            self.cursor_position = len(self.value)

    class DateRangeModal(ModalScreen):
        """The custom date-range picker: preset chips, two cards, two grids.

        Left column: the shortcut chips, an editable START TIME and END TIME (a
        date plus a 24-hour time each), a quick-time row under each card, a
        nudge-step selector and the "end follows now" switch. Right column: a
        month calendar whose ‹ › step the month (and, at the year's ends, the
        year), with a 24-cell hour grid below it. Clicking a card makes it
        active; a day or an hour then writes into the active card — start and
        end are picked the same way, and the calendar and the grid are the two
        halves of one clock.

        Opened from the top bar's 📅 control or the Select's "Custom…". Confirm
        dismisses with a ``db.CustomWindow``; Cancel or Esc dismisses with
        ``None``. A blank start date means all time.
        """

        BINDINGS = [Binding("escape", "cancel", "Cancel")]
        CELLS = 42                       # 6 rows × 7 columns covers any month
        DOW = ("S", "M", "T", "W", "T", "F", "S")
        # (chip label, widget id, preset key). "Today" and "1d" both name the
        # single whole calendar day that our window model calls "1d".
        PRESETS = (
            ("Today", "dr-today", "1d"),
            ("1d", "dr-1d", "1d"),
            ("7d", "dr-7d", "7d"),
            ("14d", "dr-14d", "14d"),
            ("30d", "dr-30d", "30d"),
            ("All time", "dr-all", "all"),
        )
        # The quick-time row under each card. The start card offers the day's
        # edges and the common halves; the end card also offers ``now``, which
        # is the same thing as ticking "follows current time" — the two ends of
        # the same choice, one click each.
        QUICK_START = ("00:00", "06:00", "12:00", "18:00", "23:59")
        QUICK_END = ("now", "00:00", "12:00", "23:59")
        # The nudge selector: (label, minutes). ↑/↓ and the wheel move by the
        # selected step, so a day's edge is reachable without typing.
        STEPS = (("1m", 1), ("10m", 10), ("1h", 60))
        HOURS = 24

        DEFAULT_CSS = """
        DateRangeModal { align: center middle; overflow: auto; }
        /* Width budget, so the whole picker fits an 80-column terminal:
           1 border + 1 padding + 53 (left column, of which the six preset chips
           use 52) + 1 margin + 21 (7 day cells of 3, and 6 hour cells of 3) +
           1 padding + 1 border = 79. The day and hour cells are Statics, not
           Buttons: a Button renders a two-digit label as one digit at width 3. */
        #dr-box { width: auto; height: auto; border: round $primary;
                  background: $surface; padding: 1 1; }
        #dr-left { width: 53; height: auto; }
        #dr-presets { height: auto; }
        .dr-card { border: round $panel; height: auto; padding: 0 1; margin-top: 1; }
        .dr-card.-active { border: round $accent; background: $boost; }
        .dr-card-title { color: $text-muted; text-style: bold; }
        .dr-fields { height: auto; }
        #dr-start-date, #dr-end-date { width: 1fr; margin: 0 1 0 0; }
        /* An Input spends 6 cells on its border and its 0-2 padding, so 16 is
           what a 5-cell "23:59" needs with room to type over it. */
        #dr-start-time, #dr-end-time { width: 16; }
        /* The quick-time rows: one cell tall, so five chips + four chips cost
           two rows rather than six. 5 × (5 + 2 padding + 1 margin) = 40 ≤ 53. */
        #dr-start-quick, #dr-end-quick { height: auto; }
        #dr-follow { height: auto; }
        #dr-nudge { height: auto; }
        /* A bare Static in a Horizontal takes the whole row and pushes the
           buttons past the column (and off the modal), so the label is pinned
           to its own cells. */
        #dr-nudge .dr-card-title { width: 7; }
        #dr-actions { height: auto; align-horizontal: right; }
        #dr-cal { width: 21; height: auto; margin-left: 1; }
        #dr-cal-head { height: auto; align: center middle; }
        #dr-cal-month { width: 1fr; text-align: center; text-style: bold; }
        #dr-cal-grid { grid-size: 7; grid-gutter: 0; height: auto; }
        /* 6 columns of 3 = 18 of the 21 available, so the 24 hours land in 4
           rows under the month. */
        #dr-hour-grid { grid-size: 6; grid-gutter: 0; height: auto; margin-top: 1; }
        /* Every rule that touches a Button is scoped by an id: Button's own
           `Button:ansi.-style-default` selector outranks a bare class, so
           `.dr-day { border: none }` silently lost and the calendar rendered
           with tall borders and no room for the number. */
        #dr-presets Button { width: auto; min-width: 0; height: 3; border: none;
                             padding: 0 1; margin: 0 1 0 0; background: $boost;
                             color: $foreground; }
        #dr-start-quick Button, #dr-end-quick Button {
                             width: auto; min-width: 0; height: 1; border: none;
                             padding: 0 1; margin: 0 1 0 0; background: $boost;
                             color: $foreground; }
        #dr-nudge Button { width: auto; min-width: 0; height: 1; border: none;
                           padding: 0 1; margin: 0 1 0 0; background: $boost;
                           color: $foreground; }
        /* -selected, not -active: Button uses -active for its own 0.2s pressed
           effect and strips it on a timer, so the chosen step flickered off. */
        #dr-nudge Button.-selected { background: $accent; color: $text;
                                     text-style: bold; }
        #dr-actions Button { width: auto; min-width: 0; margin-left: 1; }
        #dr-cal-head Button { width: auto; min-width: 0; border: none;
                              background: $surface; color: $foreground; }
        #dr-cal-grid .dr-dow { text-align: center; color: $text-muted; }
        /* 3, not 4: a Static renders "30" at width 3, where a Button shows " 3 ".
           The clicks are read from the cell id in on_click. */
        #dr-cal-grid .dr-day { width: 3; height: 1; text-align: center;
                               background: $surface; color: $foreground; }
        #dr-cal-grid .dr-day.-other { color: $text-disabled; }
        #dr-cal-grid .dr-day.-today { color: $accent; text-style: bold; }
        #dr-cal-grid .dr-day.-selected { background: $accent; color: $text;
                                         text-style: bold; }
        #dr-hour-grid .dr-hour { width: 3; height: 1; text-align: center;
                                 background: $surface; color: $foreground; }
        #dr-hour-grid .dr-hour.-selected { background: $accent; color: $text;
                                           text-style: bold; }
        """

        def __init__(self, current, now_ms: int, id: str | None = None):
            super().__init__(id=id)
            self._current = current
            self._now = now_ms
            self._active = "start"       # which card the calendar writes into
            self._step = self.STEPS[0][1]   # minutes per ↑/↓ nudge
            self._view = datetime.fromtimestamp(now_ms / 1000).date().replace(day=1)
            # What we last wrote into each field, so a *user* edit can be told
            # from our own prefill/preset write (both post Input.Changed).
            self._written = {}

        def compose(self) -> ComposeResult:
            with Horizontal(id="dr-box"):
                with Vertical(id="dr-left"):
                    with Horizontal(id="dr-presets"):
                        for label, wid, _key in self.PRESETS:
                            yield Button(label, id=wid)
                    with Vertical(id="dr-start-card", classes="dr-card"):
                        yield Static("START TIME", classes="dr-card-title")
                        with Horizontal(classes="dr-fields"):
                            yield Input(placeholder="MM/DD/YYYY", id="dr-start-date")
                            yield TimeInput(placeholder="00:00", id="dr-start-time")
                        with Horizontal(id="dr-start-quick"):
                            for i, label in enumerate(self.QUICK_START):
                                yield Button(label, id=f"dr-start-quick-{i}")
                    with Vertical(id="dr-end-card", classes="dr-card"):
                        yield Static("END TIME", classes="dr-card-title")
                        with Horizontal(classes="dr-fields"):
                            yield Input(placeholder="MM/DD/YYYY", id="dr-end-date")
                            yield TimeInput(placeholder="00:00", id="dr-end-time")
                        with Horizontal(id="dr-end-quick"):
                            for i, label in enumerate(self.QUICK_END):
                                yield Button(label, id=f"dr-end-quick-{i}")
                    yield Checkbox("End time follows current time", True, id="dr-follow")
                    with Horizontal(id="dr-nudge"):
                        yield Static("step", classes="dr-card-title")
                        for label, minutes in self.STEPS:
                            yield Button(label, id=f"dr-step-{minutes}")
                    with Horizontal(id="dr-actions"):
                        yield Button("Cancel", id="dr-cancel")
                        yield Button("Confirm", variant="primary", id="dr-ok")
                with Vertical(id="dr-cal"):
                    with Horizontal(id="dr-cal-head"):
                        yield Button("‹", id="dr-prev")
                        yield Static("", id="dr-cal-month")
                        yield Button("›", id="dr-next")
                    with Grid(id="dr-cal-grid"):
                        for dow in self.DOW:
                            yield Static(dow, classes="dr-dow")
                        for i in range(self.CELLS):
                            # A Static, not a Button: a Button renders the
                            # two-digit label "30" as " 3 " at a 3-cell width,
                            # and widening every cell to 4 would not fit the
                            # calendar beside the cards. The day is read from
                            # the cell id in on_click instead.
                            yield Static("", id=f"dr-day-{i}", classes="dr-day")
                    with Grid(id="dr-hour-grid"):
                        for h in range(self.HOURS):
                            yield Static(f"{h:02d}", id=f"dr-hour-{h}",
                                         classes="dr-hour")

        def on_mount(self) -> None:
            self._prefill()
            # The reference popup's "Supports both date and time" note, on the
            # box's bottom border so it costs no extra row.
            self.query_one("#dr-box").border_subtitle = "Supports both date and time"
            self._set_active("start")
            self._render_steps()

        # -- prefill from the tab's current window --------------------------
        def _prefill(self) -> None:
            cur = self._current
            follow = True
            if isinstance(cur, db.CustomWindow):
                start, end, follow = cur.start_ms, cur.end_ms, cur.follow_now
            else:
                start, end = db.window_bounds(cur, self._now)
            self._set_field("#dr-start", start)
            # A following end shows *now*; a fixed one shows the stored bound.
            self._set_field("#dr-end", self._now if follow else end)
            self.query_one("#dr-follow", Checkbox).value = follow
            anchor = start if start is not None else self._now
            self._view = datetime.fromtimestamp(anchor / 1000).date().replace(day=1)

        def _set_field(self, prefix: str, ms) -> None:
            date_in = self.query_one(f"{prefix}-date", Input)
            time_in = self.query_one(f"{prefix}-time", Input)
            if ms is None:
                date_in.value = time_in.value = ""
            else:
                dt = datetime.fromtimestamp(ms / 1000)
                date_in.value = db.format_date(dt)
                time_in.value = db.format_time(dt.hour, dt.minute)
            self._written[date_in.id] = date_in.value
            self._written[time_in.id] = time_in.value
            self._render_hours()

        def _follows_now(self) -> bool:
            return self.query_one("#dr-follow", Checkbox).value

        # -- the calendar ----------------------------------------------------
        def _grid_start(self) -> date:
            """The date in cell 0: the first day of the view, back to Sunday."""
            lead = (self._view.weekday() + 1) % 7      # Sunday-first column
            return self._view - timedelta(days=lead)

        def _day_at(self, index: int) -> date:
            return self._grid_start() + timedelta(days=index)

        def _active_date(self):
            """The active card's typed date, or ``None`` if it is blank/bad."""
            try:
                return db.parse_date(
                    self.query_one(f"#dr-{self._active}-date", Input).value,
                    self._view.year).date()
            except ValueError:
                return None

        def _render_calendar(self) -> None:
            today = datetime.fromtimestamp(self._now / 1000).date()
            chosen = self._active_date()
            self.query_one("#dr-cal-month", Static).update(
                self._view.strftime("%B %Y"))
            for i in range(self.CELLS):
                day = self._day_at(i)
                cell = self.query_one(f"#dr-day-{i}", Static)
                cell.update(str(day.day))
                cell.set_class(day.month != self._view.month, "-other")
                cell.set_class(day == today, "-today")
                cell.set_class(day == chosen, "-selected")

        def _step_month(self, delta: int) -> None:
            month = self._view.month + delta
            year = self._view.year + (month - 1) // 12
            self._view = date(year, (month - 1) % 12 + 1, 1)
            self._render_calendar()

        # -- the hour grid ---------------------------------------------------
        def _active_time(self):
            """The active card's typed time as ``(hour, minute)``, or ``None``."""
            try:
                return db.parse_time(
                    self.query_one(f"#dr-{self._active}-time", Input).value)
            except ValueError:
                return None

        def _render_hours(self) -> None:
            """Mark the active card's hour in the 24-cell grid."""
            parsed = self._active_time()
            hour = parsed[0] if parsed else None
            for h in range(self.HOURS):
                self.query_one(f"#dr-hour-{h}", Static).set_class(
                    h == hour, "-selected")

        def _pick_hour(self, hour: int) -> None:
            """Write a clicked hour into the active card, keeping its minute.

            The grid is the calendar's other half: the day cells set the date,
            these set the hour, and both land in whichever card is active.
            """
            parsed = self._active_time()
            minute = parsed[1] if parsed else 0
            self._write_time(self._active, db.format_time(hour, minute))

        def _write_time(self, card: str, value: str) -> None:
            """Set one card's time field, and fix the end if that is the card."""
            if card == "end":
                # A chosen end time means a fixed end, not "now".
                self.query_one("#dr-follow", Checkbox).value = False
            self.query_one(f"#dr-{card}-time", Input).value = value
            self._written[f"dr-{card}-time"] = value
            self._render_hours()

        def _pick_day(self, index: int) -> None:
            """Write a clicked day into the active card."""
            day = self._day_at(index)
            if self._active == "end":
                # Choosing an end date means a fixed end, not "now".
                self.query_one("#dr-follow", Checkbox).value = False
            self.query_one(f"#dr-{self._active}-date", Input).value = db.format_date(day)
            self._written[f"dr-{self._active}-date"] = db.format_date(day)
            if (day.year, day.month) != (self._view.year, self._view.month):
                self._view = day.replace(day=1)
            self._render_calendar()

        def _set_active(self, which: str) -> None:
            self._active = which
            self.query_one("#dr-start-card").set_class(which == "start", "-active")
            self.query_one("#dr-end-card").set_class(which == "end", "-active")
            self._render_calendar()
            self._render_hours()

        # -- presets ---------------------------------------------------------
        def _apply_preset(self, key: str) -> None:
            start, _ = db.window_bounds(key, self._now)
            self._set_field("#dr-start", start)
            self.query_one("#dr-follow", Checkbox).value = True
            if start is not None:
                self._view = datetime.fromtimestamp(start / 1000).date().replace(day=1)
            self._render_calendar()

        def _apply_quick(self, card: str, label: str) -> None:
            """A quick-time chip: ``now`` re-follows the clock, a time fixes it."""
            if label == "now":
                self._set_field("#dr-end", self._now)
                self.query_one("#dr-follow", Checkbox).value = True
                self._render_hours()
                return
            hour, minute = db.parse_time(label)
            self._write_time(card, db.format_time(hour, minute))

        # -- nudge step ------------------------------------------------------
        def _set_step(self, minutes: int) -> None:
            self._step = minutes
            for wid in ("#dr-start-time", "#dr-end-time"):
                self.query_one(wid, TimeInput).step = minutes
            self._render_steps()

        def _render_steps(self) -> None:
            for _label, minutes in self.STEPS:
                self.query_one(f"#dr-step-{minutes}", Button).set_class(
                    minutes == self._step, "-selected")

        # -- confirm ---------------------------------------------------------
        def _confirm(self) -> None:
            follow = self._follows_now()
            year = self._view.year
            try:
                start = db.parse_datetime(
                    self.query_one("#dr-start-date", Input).value,
                    self.query_one("#dr-start-time", Input).value, year)
                end = None if follow else db.parse_datetime(
                    self.query_one("#dr-end-date", Input).value,
                    self.query_one("#dr-end-time", Input).value, year)
            except ValueError:
                self.notify("Dates are MM/DD/YYYY, times HH:MM (24-hour)",
                            severity="error")
                return
            if not follow and end is None:
                self.notify("Set an end date, or tick 'End follows current time'",
                            severity="error")
                return
            if start is not None and end is not None and end < start:
                self.notify("End is before start", severity="error")
                return
            self.dismiss(db.CustomWindow(start, end, follow))

        def action_cancel(self) -> None:
            self.dismiss(None)

        # -- events ----------------------------------------------------------
        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            if event.checkbox.id == "dr-follow" and event.value:
                # Ticking "follows current time" puts the end back on now.
                self._set_field("#dr-end", self._now)

        def on_input_changed(self, event: Input.Changed) -> None:
            # A *user* edit of an end field means a fixed end, so drop the
            # follow switch. Our own prefill/preset writes are ignored by
            # matching the value we recorded.
            wid = event.input.id or ""
            if wid.endswith("-time"):
                self._render_hours()      # a typed hour re-marks the grid
            if not wid.startswith("dr-end") or not self._follows_now():
                return
            if self._written.get(wid) == event.value:
                return
            self.query_one("#dr-follow", Checkbox).value = False

        def on_click(self, event) -> None:
            # A calendar day and an hour cell are Statics, so their clicks
            # arrive here rather than as a Button.Pressed — read the index from
            # the cell's id.
            wid = getattr(event.widget, "id", "") or ""
            if wid.startswith("dr-day-"):
                self._pick_day(int(wid.rsplit("-", 1)[1]))
                return
            if wid.startswith("dr-hour-"):
                self._pick_hour(int(wid.rsplit("-", 1)[1]))
                return
            # Clicking anywhere on a card makes it the calendar's target.
            node = event.widget
            while node is not None:
                if getattr(node, "has_class", None) and node.has_class("dr-card"):
                    self._set_active(
                        "start" if node.id == "dr-start-card" else "end")
                    return
                node = getattr(node, "parent", None)

        def on_descendant_focus(self, event) -> None:
            wid = getattr(event.control, "id", "") or ""
            if wid.startswith("dr-start"):
                self._set_active("start")
            elif wid.startswith("dr-end"):
                self._set_active("end")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id or ""
            preset = next((key for _l, wid, key in self.PRESETS if wid == bid), None)
            if preset is not None:
                self._apply_preset(preset)
            elif bid.startswith("dr-start-quick-"):
                self._apply_quick("start", self.QUICK_START[int(bid.rsplit("-", 1)[1])])
            elif bid.startswith("dr-end-quick-"):
                self._apply_quick("end", self.QUICK_END[int(bid.rsplit("-", 1)[1])])
            elif bid.startswith("dr-step-"):
                self._set_step(int(bid.rsplit("-", 1)[1]))
            elif bid == "dr-prev":
                self._step_month(-1)
            elif bid == "dr-next":
                self._step_month(1)
            elif bid == "dr-cancel":
                self.dismiss(None)
            elif bid == "dr-ok":
                self._confirm()


    class WideTableMixin:
        """The narrow-terminal column plan, shared by every host that owns a table.

        A rule that covers two of the ten tables is not a rule, so the mechanism
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

        def _cap_cell(self, text):
            """Fit one cell to ``name_cap``, keeping the half that carries meaning.

            A path's beginning is the shared prefix (`/home/user/…` repeated down
            the column) and its end is what distinguishes the rows, so paths are
            capped from the left. Everything else — a tool, a model, a skill — is
            read from the start, so it is capped from the right.

            A styled cell keeps its style across the cap. `state_cell` hands over
            a Text, and ``name_cap = 8`` is inside the documented range, so an
            "installed" can be capped — dropping the style there would make a
            stale row look loud on a narrow terminal and quiet on a wide one.
            """
            cap = self.name_cap
            plain = text.plain if isinstance(text, Text) else text
            if len(plain) <= cap:
                return text
            keep = cap - 1
            clipped = ("…" + plain[-keep:]) if "/" in plain else (plain[:keep] + "…")
            return Text(clipped, style=text.style) if isinstance(text, Text) else clipped

        @staticmethod
        def _clean_cell(cell):
            """One-line text for one cell, keeping a Text cell's style.

            A newline in a transcript-derived name used to render as a second
            line and double that row's height, shifting the columns after it.
            The stored value is left alone; only what is painted is cleaned.
            """
            if isinstance(cell, Text):
                plain = db.clean_cell(cell.plain)
                return Text(plain, style=cell.style) if plain != cell.plain else cell
            return db.clean_cell(cell)

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
                # NULL renders as "-", matching fmt_n (never a blank cell). A cell
                # that is already text is handed over as it is: the state column
                # passes a styled Text, and str() on one would drop the style and
                # leave a row saying "removed" as loudly as the live ones.
                cells = [self._clean_cell(c) for c in r]
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
                       width=None, key_rows=None):
            """Fill a wide table with the longest prefix of columns that fits.

            Prefix, not priority re-ordering: both tables are already ordered most
            essential first (name, total, then the cache detail), so dropping the
            tail is the plan. The cells past the right edge used to be cut
            mid-label with no keyboard way to reach them.

            ``key_rows`` carries the un-truncated values a key is built from when
            the key is not among the visible cells — the drill-down's ``call_id``
            rides there, so a row can open its own detail without spending a
            column on it. It defaults to the visible rows.
            """
            table = self.query_one(f"#{table_id}", DataTable)
            labels, note_id, chrome = self.WIDE_TABLES[table_id]
            self._wide_rows[table_id] = (rows, empty_msg, key_index, key_kind,
                                         key_rows)
            note = self.query_one(f"#{note_id}", Static)
            width = width or self.size.width or table.region.width
            if not width or not rows:
                # Either the first layout has not happened yet (both are 0, and
                # planning against 0 would drop everything) or there is nothing to
                # protect: an empty-state row spans the whole label set.
                self._set_columns(table, labels)
                self._fill_or_empty(table, rows, empty_msg, key_index, key_kind,
                                    key_rows=key_rows)
                note.update("")
                note.display = False
                if not width:
                    self.call_after_refresh(self._plan_wide_tables)
                return
            # Styled cells (the state column) survive to _fill; everything else
            # is flattened here, so width planning sees the text a cell prints.
            raw = [[self._clean_cell(c) for c in r] for r in rows]
            shown = [[self._cap_cell(c) for c in rr] for rr in raw]
            capped = any(a != b for rr, ss in zip(raw, shown) for a, b in zip(rr, ss))
            full = self._column_widths(labels, raw)
            budget = width - chrome
            if sum(full) + self.CELL_COST * len(labels) <= budget:
                # Everything fits at its real length, so the cap stays out: a wide
                # terminal has no reason to lose characters, and "widen to N" would
                # otherwise promise a number that buys nothing.
                self._set_columns(table, labels)
                self._fill_or_empty(table, raw, empty_msg, key_index, key_kind,
                                    key_rows=key_rows)
                note.update("")
                note.display = False
                return
            widths = self._column_widths(labels, shown)
            indices = list(range(len(labels)))
            # The first column names the row, and the state column answers "is
            # this still here". Dropping either leaves a table that cannot be
            # read, so a narrow terminal loses `last used`/`avg` first instead of
            # silently losing the one column the union exists for. Other tables
            # protect only column 0, which makes this the same tail-drop (prefix)
            # plan they already had.
            state_at = (labels.index(db.ENTITY_STATE_COLUMN)
                        if db.ENTITY_STATE_COLUMN in labels else None)
            # The row key's columns are protected too: the MCP key is
            # (server, tool), and dropping `tool` would leave two rows that
            # render the same and route to one history.
            key_ids = (key_index if isinstance(key_index, (tuple, list))
                       else (key_index,))
            protected = ({i for i in key_ids if 0 <= i < len(labels)}
                         | ({state_at} if state_at is not None else set()))

            def used_width(idxs):
                return sum(widths[i] for i in idxs) + self.CELL_COST * len(idxs)

            while len(indices) > self.MIN_SHOWN and used_width(indices) > budget:
                drop = next((pos for pos in range(len(indices) - 1, -1, -1)
                             if indices[pos] not in protected), None)
                if drop is None:
                    break
                del indices[drop]
            hidden = [labels[i] for i in range(len(labels)) if i not in indices]
            self._set_columns(table, [labels[i] for i in indices])
            self._fill_or_empty(table, [[r[i] for i in indices] for r in shown],
                                empty_msg, key_index, key_kind,
                                key_rows=key_rows if key_rows is not None else rows)
            cap_note = f" · cells capped at {self.name_cap}" if capped else ""
            if not hidden:
                # Nothing is hidden, so the only thing worth saying is that
                # characters were cut — and the full value only comes back at the
                # width named below, which is why it has to be said out loud.
                note.update(f"cells capped at {self.name_cap}" if capped else "")
                note.display = capped
                return
            cuts = used_width(indices) > budget
            note.update(f"{len(indices)} of {len(labels)} columns · hidden: "
                        f"{', '.join(hidden)} · widen to "
                        f"{sum(full) + self.CELL_COST * len(labels) + chrome}"
                        + (" · even these are cut" if cuts else "")
                        + cap_note)
            note.display = True

        def _plan_wide_tables(self, width=None) -> None:
            """Re-plan from the cached rows — a resize must not re-query."""
            for table_id, (rows, empty_msg, key_index, key_kind,
                           key_rows) in list(self._wide_rows.items()):
                self._fill_wide(table_id, rows, empty_msg,
                                key_index=key_index, key_kind=key_kind,
                                width=width, key_rows=key_rows)

    class HistoryScreen(WideTableMixin, Screen):
        """Recent calls for one entity in a window, pushed on row select.

        The header summarises the entity (its counts in the same window); the
        table lists the calls. Selecting a call pushes its full detail.
        """

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        # A pushed screen spans the terminal, so its table loses no cells to its
        # surroundings — measured: region width == terminal width at 60/80/100.
        WIDE_TABLES = {"hist-table": (HISTORY_COLUMNS, "colnote-hist", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, kind: str, name: str, display: str | None = None,
                     start_ts=None, end_ts=None, window_label: str = "all time",
                     server: str | None = None):
            super().__init__()
            self.kind = kind
            # An MCP row is keyed by (server, tool), so the tool alone does not
            # identify it: two servers can export the same name.
            self.server = server
            self.entity = name              # the value queried against the DB
            self.heading = display or name  # what the title shows
            self.start_ts = start_ts        # the tab's window; None = all-time
            self.end_ts = end_ts
            self.window_label = window_label
            # Plugin uses carry no status/duration, so they get their own columns.
            self._columns = (PLUGIN_HISTORY_COLUMNS if kind == "plugin"
                             else HISTORY_COLUMNS)
            self._wide_rows = {}
            # Instance registry: the mixin reads ``self.WIDE_TABLES``, so a
            # per-kind column set overrides the class default. The class attribute
            # stays put for the "every table is registered" guard.
            self.WIDE_TABLES = {"hist-table": (self._columns, "colnote-hist", 0)}

        def compose(self) -> ComposeResult:
            yield TopBar(f"{self.kind} · {self.heading}")
            yield Vertical(
                Static(f"[b]{self.kind}[/b] · {self.heading}", id="hist-title"),
                Static(id="hist-summary", classes="entity-summary"),
                Static(id="colnote-hist", classes="col-note"),
                Table(id="hist-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind}: {self.heading}"
            self.query_one("#hist-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                if self.kind == "plugin":
                    found = db.q_plugin_history(
                        conn, self.entity, self.app.DETAIL_LIMIT,
                        start_ts=self.start_ts, end_ts=self.end_ts)
                    rows = [[ts(r["ts"]), r["kind"] or "-", r["target"] or "-",
                             r["marketplace"] or "-",
                             (r["session_id"] or "")[:8], r["project"] or "-"]
                            for r in found]
                    keys = [[self.kind, r["id"]] for r in found]
                    summary = db.q_plugin_summary(
                        conn, self.entity, start_ts=self.start_ts,
                        end_ts=self.end_ts)
                else:
                    found = db.q_history(
                        conn, self.kind, self.entity, self.app.DETAIL_LIMIT,
                        start_ts=self.start_ts, end_ts=self.end_ts,
                        server=self.server)
                    rows = [[ts(r["ts"]), r["project"] or "-",
                             (r["session_id"] or "")[:8], r["status"] or "-",
                             ms(r["duration_ms"])]
                            for r in found]
                    keys = [[self.kind, r["call_id"]] for r in found]
                    summary = db.q_entity_summary(
                        conn, self.kind, self.entity, start_ts=self.start_ts,
                        end_ts=self.end_ts, server=self.server)
            finally:
                conn.close()
            self.query_one("#hist-summary", Static).update(
                self._summary_line(summary))
            # The call key rides as the row key (index 1), not as a column, so a
            # selected call can open its own detail.
            self._fill_wide("hist-table", rows, "No calls recorded for this name",
                            key_index=(0, 1), key_kind="call", key_rows=keys)

        def _summary_line(self, s) -> str:
            """One line of the entity's windowed counts, for the header."""
            if self.kind == "plugin":
                if not s["uses"]:
                    return f"no uses · {self.window_label}"
                return (f"{count_n(s['uses'], 'use')} · {s['kinds']} kinds · "
                        f"{s['targets']} targets · {s['projects']} projects · "
                        f"{s['sessions']} sessions · "
                        f"{ts(s['first_used'])} → {ts(s['last_used'])} · "
                        f"{self.window_label}")
            if not s["calls"]:
                return f"no calls · {self.window_label}"
            return (f"{count_n(s['calls'], 'call')} · {s['completed']} ok · "
                    f"{s['failed']} fail · {s['pending']} pending · "
                    f"avg {ms(s['avg_ms'])} · {s['projects']} projects · "
                    f"{s['sessions']} sessions · "
                    f"{ts(s['first_used'])} → {ts(s['last_used'])} · "
                    f"{self.window_label}")

    class ModelResponsesScreen(WideTableMixin, Screen):
        """Per-model recent responses in a window, pushed on a Tokens row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        WIDE_TABLES = {"resp-table": (RESPONSE_COLUMNS, "colnote-resp", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, model: str, start_ts=None, end_ts=None,
                     window_label: str = "all time", provider=None):
            super().__init__()
            self.model = model
            self.provider = provider
            self.start_ts = start_ts
            self.end_ts = end_ts
            self.window_label = window_label
            self._wide_rows = {}

        @property
        def scope(self) -> str:
            """The Tokens row this screen was opened from, as it was labelled."""
            return f"{self.model} · {db.provider_label(self.provider)}"

        def compose(self) -> ComposeResult:
            yield TopBar(f"Model responses · {self.scope}")
            yield Vertical(
                Static(f"[b]Model responses[/b] · {self.scope}", id="resp-title"),
                Static(id="resp-summary", classes="entity-summary"),
                Static(id="colnote-resp", classes="col-note"),
                Table(id="resp-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"Model: {self.scope}"
            self.query_one("#resp-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                found = db.q_model_responses(conn, model=self.model,
                                             provider=self.provider,
                                             limit=self.app.DETAIL_LIMIT,
                                             start_ts=self.start_ts,
                                             end_ts=self.end_ts)
                rows = []
                for r in found:
                    mid = r["message_id"] or ""
                    masked = (mid[:8] + "…" + mid[-8:]) if len(mid) > 16 else mid
                    rows.append([
                        ts(r["ts"]), r["model"] or "-",
                        db.provider_label(r["provider"]),
                        fmt_n(r["usage_total_tokens"]),
                        fmt_credit(r["credit"]),
                        fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                        fmt_n(r["prompt_cache_hit_tokens"]),
                        fmt_n(r["prompt_cache_miss_tokens"]),
                        fmt_n(r["prompt_cache_write_tokens"]),
                        fmt_n(r["cache_read_input_tokens"]),
                        fmt_n(r["cache_creation_input_tokens"]),
                        "yes" if r["usage_available"] else "not counted",
                        masked,
                    ])
                summary = db.q_model_summary(conn, self.model,
                                             start_ts=self.start_ts,
                                             end_ts=self.end_ts,
                                             provider=self.provider)
            finally:
                conn.close()
            self.query_one("#resp-summary", Static).update(
                self._summary_line(summary))
            self._fill_wide("resp-table", rows,
                            "No responses recorded for this model",
                            key_index=(0, 1), key_kind="call",
                            key_rows=[["model", r["message_id"]] for r in found])

        def _summary_line(self, s) -> str:
            if not s["responses"]:
                return f"no responses · {self.window_label}"
            return (f"{count_n(s['responses'], 'response')} · "
                    f"{s['with_usage']} with usage · "
                    f"in {fmt_n(s['prompt_tokens'])} · "
                    f"out {fmt_n(s['completion_tokens'])} · "
                    f"credits {fmt_credit_total(s['credits'], s['credit_rows'])}"
                    f" · cache "
                    f"{fmt_n(s['cache_hit'])}/{fmt_n(s['cache_miss'])}/"
                    f"{fmt_n(s['cache_write'])} · {self.window_label}")

    class CallDetailScreen(Screen):
        """Every field of one call, pushed from a drill-down row.

        A plain ``Screen``, not the ``WideTableMixin``: the detail is a
        label/value list, not a wide table, so it needs neither the column plan
        nor a registered ``DataTable``.
        """

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, kind: str, key):
            super().__init__()
            self.kind = kind
            self.key = str(key)

        def compose(self) -> ComposeResult:
            yield TopBar(f"{self.kind} call")
            with Vertical(id="call-body"):
                yield Static(f"[b]{self.kind} call[/b] · {self.key}",
                             id="call-title")
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind} call: {self.key}"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                row = db.q_call_detail(conn, self.kind, self.key)
            finally:
                conn.close()
            body = self.query_one("#call-body")
            if row is None:
                body.mount(Static("record not found", classes="call-value"))
                return
            body.mount_all(
                Horizontal(Static(label, classes="call-label"),
                           Static(value, classes="call-value"))
                for label, value in _call_detail_rows(row))

    class DashBox(Vertical):
        """The Dashboard's scrolling stack.

        It re-fits the project ledger when its own height changes. The app's
        resize event arrives before this box is laid out at the new height, so
        a fit measured there would use the previous window's spare.
        """

        def on_resize(self, event) -> None:  # textual.events.Resize
            try:
                app = self.app
            except Exception:  # not mounted yet
                return
            fit = getattr(app, "_fit_dashboard", None)
            if fit is not None:
                app.call_after_refresh(fit)


    class TrackerApp(WideTableMixin, App):
        CSS = """
        #hist-title, #resp-title, #call-title { padding: 1 2; }
        TabbedContent { height: 1fr; }
        DataTable { height: 1fr; }
        /* One row, always: the line is longer than any terminal, and letting it
           wrap to a second row stole a row from the tab above it. The head of
           the line (db, tool calls) is what survives the ellipsis. */
        #status { padding: 0 2; color: $text-muted; height: 1;
                  text-wrap: nowrap; text-overflow: ellipsis; }
        /* The drill-down header (counts) and the single-call detail. The summary
           is one auto-height line; the detail body takes the rest of the screen
           and scrolls, its label column fixed so long values wrap in 1fr. */
        .entity-summary { padding: 0 2; color: $text-muted; height: auto; }
        #call-body { height: 1fr; overflow-y: auto; padding: 0 1; }
        .call-label { width: 18; color: $text-muted; }
        .call-value { width: 1fr; }
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
        #panel-dash-projects { margin: 0; }
        /* The ledger grows with its rows and the box scrolls once the screen is
           full — a fixed 9 rows silently hid the rest of a long project list. */
        #panel-dash-projects { height: auto; }
        #dash-projects { height: auto; }
        .panel-title { text-style: bold; color: $accent; }
        #usage-status, #usage-window { padding: 0; }
        #usage-note, #dash-note { padding: 0 1; color: $text-muted; }
        /* One line per wide table, only when columns were dropped — an always-on
           note above a table that hides nothing is noise that costs a row. Auto
           height because a note that is itself cut off at the right edge would be
           the same defect it exists to report. */
        .col-note { padding: 0 1; color: $text-muted; height: auto; display: none; }
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
        /* The active tab's filter: one row while it is in use, gone otherwise.
           Hiding is done in Python (widget.display), which outranks CSS. */
        #filter-input { height: 1; border: none; padding: 0 1;
                        background: $panel; }
        """
        # Pane ids in display order. Used for wrap-around tab cycling. Derived
        # from the same registry that builds the panes, columns and fill calls.
        TAB_IDS = ["tab-dashboard"] + [t.pane for t in ENTITY_TABS] + ["tab-usage"]
        # The tab shown on launch. The TopBar Select must start on this tab's
        # own range (see compose), or its mount-time Changed would overwrite
        # that tab's default before the user touches anything.
        INITIAL_TAB = "tab-dashboard"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Sync now"),
            Binding("s", "cycle_sort", "Sort"),
            Binding("a", "toggle_unused", "All rows"),
            Binding("slash", "open_filter", "Filter"),
            Binding("d", "date_range", "Date"),
            Binding("escape", "close_filter", "Clear filter", show=False),
            Binding("tab", "next_tab", "Next tab", priority=True),
            Binding("shift+tab", "previous_tab", "Prev tab", priority=True),
        ]
        TITLE = "CodeBuddy CLI usage tracker"
        # No built-in Header means no ctrl+p palette; disable it outright too.
        ENABLE_COMMAND_PALETTE = False
        # No auto-focus: the first focusable widget used to be the TopBar's
        # Select, which passes letters up; the filter Input does not (it types
        # them), so leaving AUTO_FOCUS alone would eat r/s/a at launch. The
        # tables are focused explicitly where a test or a click needs one.
        AUTO_FOCUS = None

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
            self._sync_running = False
            self._sync_pending = False
            self.sync_mod = None
            # Per-tab range state: each tab keeps its own window, so changing
            # one tab's range never moves another's. Entity tabs default to 7
            # days; the Dashboard and the Usage page default to Today. Plugins
            # is windowed too — its list stays all-time, but `uses`/`last used`
            # count inside the range like every other tab's.
            self.tab_range = {
                "tab-dashboard": "1d",
                **{t.pane: t.default_range for t in ENTITY_TABS},
                "tab-usage": "1d",
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
            # Per-tab view state. The default is used-only: a row with 0 calls
            # in the tab's window is catalogue noise (installed but never
            # touched), and hiding it is what keeps the panel reading as
            # usage. The `a` key includes them again — per tab, like the
            # ranges; the state column stays either way, so a used-then-
            # uninstalled tool still reads "removed" while hidden rows do not
            # bury it. The `/` filter is per tab too. Rows are cached
            # unfiltered so the toggle and the filter re-render without a query.
            self._show_unused = {t.pane: False for t in ENTITY_TABS}
            # Live filter text, per tab. It matches rendered cells too, so a
            # category or a state narrows the list just like a name does.
            self._name_filter = {"tab-usage": "",
                                 **{t.pane: "" for t in ENTITY_TABS}}
            # Per tab sort mode: 0 is the query's own order, then the tab's
            # declared fields. Kept per tab so one page never reorders another.
            self._sort_mode = {}
            self._entity_rows = {}

        def compose(self) -> ComposeResult:
            yield TopBar(self.TITLE, with_range=True,
                         range_value=self.tab_range[self.INITIAL_TAB],
                         id="topbar")
            yield FilterInput(
                placeholder="filter rows — Enter keeps, Esc clears",
                id="filter-input")
            with TabbedContent(initial=self.INITIAL_TAB):
                with TabPane("Dashboard", id="tab-dashboard"):
                    yield DashBox(
                        # Row 1: headline counts, tokens, cache totals.
                        Horizontal(
                            Vertical(
                                Static("Usage", classes="panel-title"),
                                Static(id="dash-sum-requests"),
                                Static(id="dash-sum-credits"),
                                Static(id="dash-sum-sessions"),
                                Static(id="dash-sum-projects"),
                                Static(id="dash-kpi-tools"),
                                Static(id="dash-kpi-skills"),
                                Static(id="dash-kpi-agents"),
                                Static(id="dash-kpi-mcp"),
                                Static(id="dash-kpi-plugins"),
                                classes="usage-panel", id="panel-dash-kpi",
                            ),
                            Vertical(
                                Static("Tokens", classes="panel-title"),
                                Static(id="dash-tok-usage"),
                                Static(id="dash-tok-input"),
                                Static(id="dash-tok-output"),
                                Static(id="dash-tok-credits"),
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
                        # Row 3: the project ledger, and the only panel that grows
                        # past its own content: it takes whatever height is left.
                        Horizontal(
                            Vertical(
                                Static("Projects", classes="panel-title"),
                                Static("", id="colnote-projects",
                                       classes="colnote"),
                                DataTable(id="dash-projects",
                                          zebra_stripes=True),
                                classes="usage-panel",
                                id="panel-dash-projects",
                            ),
                            classes="dash-row", id="dash-row-3",
                        ),
                        Static(id="dash-note"),
                        id="dash-box",
                    )
                for tab in ENTITY_TABS:
                    with TabPane(tab.title, id=tab.pane):
                        # The page's own summary line, directly above its table as
                        # on the sibling trackers — not a shared row under the tabs.
                        yield Static(id=f"note-{tab.pane}", classes="col-note")
                        yield Static(id=tab.note_id, classes="col-note")
                        yield Table(id=tab.table_id, zebra_stripes=True)
                with TabPane("Usage", id="tab-usage"):
                    yield Vertical(
                        Horizontal(
                            Vertical(
                                Static("Window / Requests", classes="panel-title"),
                                Static(id="usage-window"),
                                Static(id="sum-requests"),
                                Static(id="sum-sessions"),
                                Static(id="sum-projects"),
                                Static(id="sum-missing"),
                                classes="usage-panel", id="panel-window",
                            ),
                            Vertical(
                                Static("Model tokens", classes="panel-title"),
                                Static(id="sum-usage-total"),
                                Static(id="sum-credits"),
                                Static(id="sum-input"),
                                Static(id="sum-output"),
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
            self.query_one("#filter-input", Input).display = False
            note = db.migrate(self.db_path)
            self._schema_note = "" if note in ("current", "migrated") else note
            self._usage_ready = True
            self.refresh_data()
            # One incremental sync on launch, then the screen stays still. New
            # rows arrive only through `r` or by reopening the TUI.
            self._request_sync()

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





        def _fill_entity(self, tab: EntityTab, conn, start=None, end=None) -> None:
            """Fill one registry-declared entity table.

            The query is looked up through the module on every call (never bound
            at class-definition time), so a test can monkeypatch ``db.q_tools``
            and this path sees the patched function. The query result is cached
            unfiltered, so the `a` toggle and the `/` bar re-render
            without re-querying.
            """
            rows = getattr(db, tab.query)(conn, start_ts=start, end_ts=end)
            # A NULL in a key column is keyed as the empty string: the composite
            # key must stay one string, and None -> "None" would be a value a
            # provider could legitimately be named.
            key_rows = None
            if tab.key_columns:
                key_rows = [["" if r[c] is None else r[c]
                             for c in tab.key_columns] for r in rows]
            self._entity_rows[tab.pane] = (rows, key_rows)
            self._render_entity_rows(tab)

        def _render_entity_rows(self, tab: EntityTab) -> None:
            """Re-render one entity table from its cached rows (no re-query).

            Sort, used-only and the text filter all run here. The filter
            matches rendered cells, not just the name, so a category or a state
            narrows the page too. The row key always rides on the UNCAPPED key
            value, and every step slices rows and keys with the same indices so
            they can never drift apart.
            """
            rows, key_rows = self._entity_rows.get(tab.pane, ([], None))
            query = self._name_filter.get(tab.pane, "").strip().lower()
            keep, cells = [], []
            for i in self._sorted_indices(
                    rows, tab, self._sort_mode.get(tab.pane, 0)):
                r = rows[i]
                if not self._show_unused.get(tab.pane) and not _row_calls(r):
                    continue
                row_cells = tab.cells(r)
                if not self._matches(row_cells, query):
                    continue
                keep.append(i)
                cells.append(row_cells)
            shown_keys = ([key_rows[i] for i in keep]
                          if key_rows is not None else None)
            self._fill_wide(
                tab.table_id, cells, tab.empty,
                key_index=tab.key_index, key_kind=tab.key_kind,
                key_rows=shown_keys)
            self._render_filter_note(tab, len(cells), len(rows))

        def _sort_fields_for(self, pane):
            if pane == "tab-usage":
                return _USAGE_SORT_FIELDS
            tab = self._entity_tab(pane)
            return tab.sort_fields if tab else ()

        def _sorted_indices(self, rows, tab: EntityTab, mode: int) -> list:
            """Indices of ``rows`` in the selected order; None-valued last.

            Returns indices rather than rows so the caller can slice the key
            rows with the same positions.
            """
            order = list(range(len(rows)))
            fields = tab.sort_fields
            if not mode or not fields:
                return order
            _label, key, descending = fields[(mode - 1) % len(fields)]
            key = key or tab.name_key
            if not key:
                return order
            present = [i for i in order if rows[i][key] is not None]
            missing = [i for i in order if rows[i][key] is None]
            present.sort(
                key=lambda i: (str(rows[i][key]).lower()
                               if isinstance(rows[i][key], str) else rows[i][key]),
                reverse=descending)
            return present + missing

        @staticmethod
        def _matches(cells, query: str) -> bool:
            if not query:
                return True
            for cell in cells:
                plain = cell.plain if hasattr(cell, "plain") else cell
                if query in str(plain).lower():
                    return True
            return False

        def _sort_label(self, pane) -> str:
            if pane == "tab-usage":
                mode = self._sort_mode.get(pane, 0)
                return _USAGE_SORT_FIELDS[mode % len(_USAGE_SORT_FIELDS)][0]
            tab = self._entity_tab(pane)
            if tab is None:
                return ""
            mode = self._sort_mode.get(pane, 0)
            fields = tab.sort_fields
            if not mode or not fields:
                return "tab order"
            return fields[(mode - 1) % len(fields)][0]

        def _render_filter_note(self, tab: EntityTab, shown: int,
                                total: int) -> None:
            """The one-line summary above the table: rows, filter and sort.

            Always visible because the active sort is state the page must name;
            a silent reorder is indistinguishable from data that changed. When
            the used-only default hid every row, the note is also how the
            `a` toggle is discovered.
            """
            note = self.query_one(f"#note-{tab.pane}", Static)
            parts = []
            needle = self._name_filter.get(tab.pane, "").strip()
            if needle:
                parts.append(f'filter "{needle}" · {shown:,} of {total:,}')
            elif self._show_unused.get(tab.pane):
                parts.append(f"{shown:,} of {count_n(total, 'row')}")
            else:
                parts.append(count_n(shown, "row"))
            if self._show_unused.get(tab.pane):
                parts.append("incl. installed-unused")
            elif shown == 0 and total:
                parts.append("installed but unused in this window — press a")
            parts.append(f"sort: {self._sort_label(tab.pane)}")
            note.update(" · ".join(parts))
            note.display = True

        @staticmethod
        def _entity_tab(pane: str):
            return next((t for t in ENTITY_TABS if t.pane == pane), None)

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
                self._sync_range_widget()      # initial 📅 label + Select value
                # Each tab is filled with its own window, so one tab's range
                # never moves another's.
                self._fill_dashboard(conn, *self._bounds_for("tab-dashboard"))
                for tab in ENTITY_TABS:
                    self._fill_entity(tab, conn, *self._bounds_for(tab.pane))
                o = db.overview(conn)          # status bar stays all-time
                # Split the unparsed tally on the connection that is already open,
                # so the status line can tell a reason this build declines on
                # purpose (a permanent 30,000 on a real database) from one it has
                # never seen (the format moving). It reads the reason rows, not the
                # total: the total cannot tell those apart, which is exactly why
                # this is computed here and not in `overview`.
                _, o["unparsed_unexpected"], o["unparsed_unexpected_names"] = \
                    db.load_sync().split_unparsed(db.q_unparsed(conn))
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
            if o["unparsed_records"]:
                # Keep the status line honest: panels showing 0 while records went
                # unclaimed means the format moved, not that usage stopped. The "!"
                # is reserved for a reason this build has never seen, though — the
                # three prose types it declines are tens of thousands of records on
                # any real database, and a bar that shouts about them every day is a
                # bar nobody reads.
                if o.get("unparsed_unexpected"):
                    status += (f" · ! {count_n(o['unparsed_unexpected'], 'record')} "
                               "unparsed under a new reason: "
                               + ", ".join(o["unparsed_unexpected_names"]))
                else:
                    status += (f" · {count_n(o['unparsed_records'], 'record')} "
                               f"unparsed ({o['unparsed_kinds']} known)")
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
            """Refresh only the visible tab (tab activation / sort or filter change).

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
                elif pane == "tab-dashboard":
                    self._fill_dashboard(conn, *self._bounds_for(pane))
                else:
                    tab = self._entity_tab(pane)
                    if tab is not None:
                        self._fill_entity(tab, conn, *self._bounds_for(pane))
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
            # polled), point the range Select at its window, and show that tab's
            # own filter instead of the previous tab's.
            if self._usage_ready:
                self._sync_range_widget()
                self._sync_filter_bar()
                self._refresh_active_tab()
                if self._name_filter.get(self._active_pane(), "").strip():
                    self.call_after_refresh(self._anchor_active_table_top)

        # --- Usage Statistics page -----------------------------------------

        def _render_usage_status(self) -> None:
            self.query_one("#usage-status", Static).update(
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
            **{t.table_id: (t.columns, t.note_id, 2) for t in ENTITY_TABS},
            "t-usage": (USAGE_COLUMNS, "colnote-usage", 4),
            "dash-projects": (PROJECT_COLUMNS, "colnote-projects", 6),
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
            # The ledger is re-fitted by DashBox.on_resize: this box is not laid
            # out at its new height yet, so a fit measured here would use the
            # old one.

        def _range_of(self, pane):
            """The window key for a pane (``None`` only if a pane has no range)."""
            return self.tab_range.get(pane)

        def _now_ms(self) -> int:
            """The current time in epoch milliseconds, via the injectable clock."""
            return int(self._clock() * 1000)

        def _bounds_for(self, pane):
            """``(start_ms, end_ms)`` for a pane's **own** window, from now.

            Never cached: every refresh derives the window from the current
            time, so a long-running app does not drift. ``None`` as the start
            means all-time.
            """
            rng = self._range_of(pane)
            now = self._now_ms()
            if rng is None:
                return None, now
            if isinstance(rng, db.CustomWindow):
                return rng.start_ms, (now if rng.follow_now else rng.end_ms)
            return db.window_bounds(rng, now)

        def _sync_range_widget(self) -> None:
            """Point the shared range Select at the active tab's own window.

            Every tab keeps its own range (``self.tab_range``), so switching tabs
            shows that tab's range, not the one you last picked elsewhere. Setting
            ``Select.value`` posts a ``Changed`` message, which
            ``on_select_changed`` ignores when it already matches the active
            tab's range (so this never re-triggers a refresh loop).
            """
            try:
                sel = self.query_one("#range", Select)
            except Exception:
                return
            pane = self.query_one(TabbedContent).active
            sel.display = True
            value = self._range_of(pane)
            # A custom window is shown as "Custom…" in the Select, with the span
            # itself on the 📅 control next to it.
            target = CUSTOM_RANGE if isinstance(value, db.CustomWindow) else value
            if target is not None and sel.value != target:
                sel.value = target
            self._set_date_label(db.window_label(value),
                                 isinstance(value, db.CustomWindow))

        def _set_date_label(self, text: str, custom: bool = False) -> None:
            try:
                self.query_one("#topbar", TopBar).set_range_label(
                    text, custom=custom)
            except Exception:
                pass

        def _render_usage_window(self) -> None:
            value = self.tab_range["tab-usage"]
            start, end = self._bounds_for("tab-usage")
            end_dt = datetime.fromtimestamp(end / 1000)
            if isinstance(value, db.CustomWindow):
                # A custom window can carry minute precision, so the panel shows
                # the full span with times.
                start_s = ("all" if start is None else datetime.fromtimestamp(
                    start / 1000).strftime("%Y-%m-%d %H:%M"))
                span = f"{start_s} → {end_dt:%Y-%m-%d %H:%M}"
                label = "Custom range"
            else:
                # Presets are whole local calendar days: a clock time would be
                # noise. "Today" is one day, so printing the same date twice read
                # like a bug — every 1d window hit it.
                label = db.USAGE_RANGE_LABELS[value]
                end_date = end_dt.strftime("%Y-%m-%d")
                if start is None:
                    span = end_date
                else:
                    start_date = datetime.fromtimestamp(
                        start / 1000).strftime("%Y-%m-%d")
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
                kpi = db.q_usage_kpi(conn, start, end)
                self._render_summary_panels(s, kpi)
                self._fill_usage_logs(conn, start, end, s["requests"])
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

        def _render_summary_panels(self, s: dict, kpi: dict) -> None:
            def put(wid: str, label: str, val: str) -> None:
                self.query_one(wid, Static).update(self._panel_line(label, val))

            req = s["requests"] or 0
            put("#sum-requests", "Requests", f"{req:,}")
            put("#sum-sessions", "Sessions", f"{kpi['model_sessions'] or 0:,}")
            put("#sum-projects", "Projects", f"{kpi['model_projects'] or 0:,}")
            # Usage Total = Input + Output + cache hit.
            put("#sum-usage-total", "Usage Total",
                fmt_n(s["usage_total_tokens"]))
            put("#sum-credits", "Credits",
                fmt_credit_total(s["credits"], s["credit_rows"]))
            put("#sum-input", "Input", fmt_n(s["prompt_tokens"]))
            put("#sum-output", "Output", fmt_n(s["completion_tokens"]))
            put("#sum-cache-hit", "Cache hit", fmt_n(s["cache_hit"]))
            put("#sum-cache-miss", "Cache miss", fmt_n(s["cache_miss"]))
            put("#sum-cache-write", "Cache write", fmt_n(s["cache_write"]))
            # hit / (hit + miss + write) — the cacheable input.
            frac = db.cache_hit_rate(s["cache_hit"], s["cache_miss"],
                                     s["cache_write"])
            put("#sum-hit-rate", "Cache hit rate",
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
                models = db.q_usage_model_stats(conn, start, end)
                tools = db.q_tools(conn, limit=self.DASH_TOP,
                                   start_ts=start, end_ts=end)
                projects = db.q_project_board(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#dash-note", Static).update(
                    f"dashboard query failed ({e}); run: cbut sync")
                self._report_query_error(e)
                return
            self._render_dashboard_usage(kpi, s)
            self._render_dashboard_tokens(s)
            self._render_dashboard_cache(s)
            self._render_dashboard_models(models)
            self._render_dashboard_tools(tools)
            self._render_dashboard_projects(projects)
            if not any(kpi.values()) and not (s["requests"] or 0):
                self.query_one("#dash-note", Static).update(
                    "No data in this window — widen the range above.")
            else:
                self.query_one("#dash-note", Static).update(
                    "Range: the top bar · this tab keeps its own window")
            self._apply_responsive_layout()
            self.call_after_refresh(self._fit_dashboard)

        #: Each .dash-row carries one row of bottom margin.
        DASH_MARGIN = 1

        def _fit_dashboard(self, settle: bool = True) -> None:
            """Let the project ledger take the height the rows above it leave.

            Every row is content-sized, so on a tall terminal the box ended a
            dozen rows above the status line. The ledger is the only panel that
            can *use* extra rows — it shows one per project — so it grows into
            them, and only into them: when the rows above already fill the box
            the spare is zero and the ledger keeps its natural height, which is
            what keeps a long list scrolling instead of clipped.

            The spare is measured from the rows above and the table's own row
            count, never from the table's current height, so calling this twice
            gives the same answer.
            """
            try:
                box = self.query_one("#dash-box")
                note = self.query_one("#dash-note", Static)
                rows = [self.query_one(f"#dash-row-{i}") for i in (1, 2, 3)]
                table = self.query_one("#dash-projects", DataTable)
            except Exception:  # the Dashboard is not mounted yet
                return
            natural = 1 + table.row_count          # the header, then one row each
            above = (note.size.height + rows[0].size.height + rows[1].size.height
                     + 3 * self.DASH_MARGIN)
            spare = box.size.height - above - (natural + 3)  # + title + border
            table.styles.min_height = natural + max(0, spare)
            # One correction pass: the sizes above are the ones the *previous*
            # layout left, so a window switch can make the first answer too
            # generous. Re-running after this refresh lands on the settled ones.
            if settle:
                self.call_after_refresh(lambda: self._fit_dashboard(False))

        def _put_panel(self, wid: str, label: str, val: str) -> None:
            self.query_one(wid, Static).update(self._panel_line(label, val))

        def _render_dashboard_usage(self, k: dict, s: dict) -> None:
            # COUNT(*) is authoritative -> a real 0, never a dash.
            self._put_panel("#dash-sum-requests", "Requests",
                            f"{s['requests'] or 0:,}")
            self._put_panel("#dash-sum-credits", "Credits",
                            fmt_credit_total(s["credits"], s["credit_rows"]))
            self._put_panel("#dash-sum-sessions", "Sessions",
                            f"{k['model_sessions'] or 0:,}")
            self._put_panel("#dash-sum-projects", "Projects",
                            f"{k['model_projects'] or 0:,}")
            self._put_panel("#dash-kpi-tools", "Tool calls", f"{k['tool_calls']:,}")
            self._put_panel("#dash-kpi-skills", "Skills", f"{k['skills']:,}")
            self._put_panel("#dash-kpi-agents", "Agents", f"{k['agents']:,}")
            self._put_panel("#dash-kpi-mcp", "MCP", f"{k['mcp']:,}")
            self._put_panel("#dash-kpi-plugins", "Plugins", f"{k['plugins']:,}")

        def _render_dashboard_tokens(self, s: dict) -> None:
            self._put_panel("#dash-tok-usage", "Usage Total",
                            fmt_n(s["usage_total_tokens"]))
            self._put_panel("#dash-tok-input", "Input", fmt_n(s["prompt_tokens"]))
            self._put_panel("#dash-tok-output", "Output", fmt_n(s["completion_tokens"]))
            self._put_panel("#dash-tok-credits", "Credits",
                            fmt_credit_total(s["credits"], s["credit_rows"]))
            self._put_panel("#dash-tok-hit-rate", "Cache hit rate",
                            self._hit_rate(s["cache_hit"], s["cache_miss"],
                                           s["cache_write"]))

        def _render_dashboard_cache(self, s: dict) -> None:
            self._put_panel("#dash-cache-hit", "Cache hit", fmt_n(s["cache_hit"]))
            self._put_panel("#dash-cache-miss", "Cache miss", fmt_n(s["cache_miss"]))
            self._put_panel("#dash-cache-write", "Cache write", fmt_n(s["cache_write"]))

        def _render_dashboard_models(self, rows) -> None:
            top = rows[: self.DASH_TOP]
            if not top:
                self.query_one("#dash-models", Static).update("nothing in this window")
                return
            labels = model_leader_labels(top)
            # The panel's own width is the budget, and every field is sized from
            # the data: an exact 13-character count in a 12-cell field made this
            # line 56 cells in a 53-cell panel at 120 columns, which wrapped the
            # request count onto a row of its own and read as a broken ranking.
            panel = self.query_one("#dash-models", Static)
            # One column is held back: the panel is measured before the pane's
            # scrollbar takes its column, and a line that fits the measurement
            # does not fit the render.
            budget = (panel.size.width or 54) - 1
            toks = [fmt_compact_n(r["usage_total_tokens"]) for r in top]
            creds = [fmt_credit_total(r["credits"], r["credit_rows"]) for r in top]
            reqs = [f"{r['requests']:,}" for r in top]
            tok_w = max(len(t) for t in toks)
            cred_w = max(len(c) for c in creds)
            req_w = max(len(q) for q in reqs)
            # "1. " + label + tokens + credits + " cr " + requests + " req"
            label_w = max(8, budget - (tok_w + cred_w + req_w + 13))
            lines = [f"{i}. {clip(label, label_w):<{label_w}} {t:>{tok_w}}"
                     f" {c:>{cred_w}} cr {q:>{req_w}} req"
                     for i, (label, t, c, q) in
                     enumerate(zip(labels, toks, creds, reqs), 1)]
            self.query_one("#dash-models", Static).update("\n".join(lines))

        def _render_dashboard_tools(self, rows) -> None:
            # The name-keyed list keeps 0-call tools under a window; drop them.
            top = [r for r in rows if r["calls"]][: self.DASH_TOP]
            if not top:
                self.query_one("#dash-tools", Static).update("nothing in this window")
                return
            # Sized like the model list: the panel is the budget, and the call
            # count grows with use ("100,000 calls" is 13 cells, not 12).
            panel = self.query_one("#dash-tools", Static)
            budget = (panel.size.width or 56) - 1
            calls = [count_n(r["calls"], "call") for r in top]
            spans = [ms(r["avg_ms"]) for r in top]
            call_w = max(len(c) for c in calls)
            span_w = max(len(s) for s in spans)
            name_w = max(8, budget - (call_w + span_w + 6))
            lines = [f"{i}. {clip(r['tool_name'], name_w):<{name_w}}"
                     f"{c:>{call_w}} · {s:>{span_w}}"
                     for i, (r, c, s) in enumerate(zip(top, calls, spans), 1)]
            self.query_one("#dash-tools", Static).update("\n".join(lines))

        def _render_dashboard_projects(self, rows) -> None:
            """The project ledger: requests, usage, credits, reach and freshness.

            The project column is the decoded cwd; it is capped like every other
            dashboard name, and the full path stays on the Usage page's rows.
            """
            out = [[
                clip(r["project"] or "(no project)", 26),
                f"{r['requests']:,}",
                fmt_n(r["usage_total_tokens"]),
                fmt_credit_total(r["credits"], r["credit_rows"]),
                f"{r['sessions']:,}",
                ts(r["last_used"]),
            ] for r in rows]
            self._fill_wide("dash-projects", out,
                            "No projects in this window")

        @staticmethod
        def _hit_rate(hit, miss, write) -> str:
            frac = db.cache_hit_rate(hit, miss, write)
            return "-" if frac is None else f"{frac * 100:.1f}%"

        def _fill_usage_logs(self, conn, start, end, total) -> None:
            """Fill the request log, and say so when the list is cut short.

            ``total`` is the window's request count (``q_usage_summary`` already
            counted it). The table holds ``USAGE_LOG_LIMIT`` rows, and a silently
            shortened list is the row-level version of the half column the wide
            plan refuses to render: the note names the cut instead of leaving a
            reader to assume they are seeing everything. The filter runs in SQL
            over the whole window, not over the visible page.
            """
            query = self._name_filter.get("tab-usage", "").strip()
            mode = self._sort_mode.get("tab-usage", 0)
            label = _USAGE_SORT_FIELDS[mode % len(_USAGE_SORT_FIELDS)][0]
            rows = db.q_usage_request_logs(
                conn, start, end, limit=self.USAGE_LOG_LIMIT,
                text=query or None, order_by=label)
            parts = []
            if rows:
                parts.append("cache hit=read, write=create · no cost/duration/status")
                parts.append(f"sort: {label}")
                if query:
                    # Counted over the whole window, never the loaded page: the
                    # list is capped, and the length of a capped list answers a
                    # different question from "how many match".
                    matched = db.q_usage_request_log_count(
                        conn, start, end, text=query)
                    parts.append(f'filter "{query}" · {matched:,} of {total:,}')
                elif total > len(rows):
                    parts.append(f"showing {len(rows):,} of {total:,}")
            note = " · ".join(parts)
            self.query_one("#usage-note", Static).update(note)
            out = [[
                local_time(r["ts"]), r["model"] or "-",
                db.provider_label(r["provider"]),
                fmt_n(r["usage_total_tokens"]),
                fmt_credit(r["credit"]),
                fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                fmt_n(r["prompt_cache_hit_tokens"]),
                fmt_n(r["prompt_cache_miss_tokens"]),
                fmt_n(r["prompt_cache_write_tokens"]),
                self._hit_rate(r["prompt_cache_hit_tokens"],
                               r["prompt_cache_miss_tokens"],
                               r["prompt_cache_write_tokens"]),
            ] for r in rows]
            # The shared empty state, like the other seven tables: a note above an
            # untouched table reads as a page that failed to load.
            empty = (f'No requests match "{query}" in this window' if query
                     else "No requests in this window — widen the range above.")
            self._fill_wide("t-usage", out, empty)

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id != "range":
                return
            pane = self.query_one(TabbedContent).active
            if event.value == CUSTOM_RANGE:
                # "Custom…" opens the picker — unless this is the programmatic
                # sync that points the Select at an already-custom window.
                if not isinstance(self._range_of(pane), db.CustomWindow):
                    self._open_date_range(pane)
                return
            if event.value == self._range_of(pane):
                return                    # programmatic sync, not a user change
            self.tab_range[pane] = event.value
            self._sync_range_widget()     # refresh the 📅 label
            if pane == "tab-usage":
                self._render_usage_window()   # keep the Usage Window panel live
            self._refresh_active_tab()    # re-query only the visible tab

        def _open_date_range(self, pane) -> None:
            """Open the date picker for a pane and apply the picked window to it."""
            modal = DateRangeModal(self._range_of(pane), self._now_ms())

            def done(result) -> None:
                if result is None:
                    self._sync_range_widget()   # cancel: revert the Select
                    return
                self.tab_range[pane] = result
                self._sync_range_widget()
                if pane == "tab-usage":
                    self._render_usage_window()
                self._refresh_active_tab()

            self.push_screen(modal, done)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            # Only the top bar's 📅 control reaches the app; the modal handles its
            # own buttons (it is the active screen).
            if event.button.id == "date-range":
                pane = self.query_one(TabbedContent).active
                self._open_date_range(pane)

        # --- row select: route to detail screens ---------------------------

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            key = event.row_key.value
            if not key or "\t" not in key:
                return
            kind, _, rest = key.partition("\t")
            if kind == "call":
                # A drill-down row: call\t<kind>\t<key> opens that call's detail.
                call_kind, _, call_key = rest.partition("\t")
                self.push_screen(CallDetailScreen(call_kind, call_key))
                return
            # The entity tabs' drill-downs follow the tab's own window, so the
            # summary and the list agree with the row that was opened.
            pane = self.query_one(TabbedContent).active
            rng = self._range_of(pane)
            if rng is None:
                start = end = None
                label = "all time"
            else:
                start, end = self._bounds_for(pane)
                label = db.window_label(rng)
            if kind == "model":
                # The key is model\t<model>\t<provider>: both halves travel, or a
                # model served through two routes would open the wrong responses.
                model, _, provider = rest.partition("\t")
                self.push_screen(ModelResponsesScreen(
                    model, start, end, label, provider=provider))
            elif kind == "mcp":
                # Key is mcp\t<server>\t<tool>. Both halves travel: the tool name
                # alone is not unique across servers, so querying on it listed
                # another server's calls under a "server · tool" title.
                server, _, tool = rest.partition("\t")
                self.push_screen(HistoryScreen(
                    "mcp", tool, display=f"{server} · {tool}", server=server,
                    start_ts=start, end_ts=end, window_label=label))
            else:
                self.push_screen(HistoryScreen(kind, rest, start_ts=start,
                                               end_ts=end, window_label=label))

        # --- shortcuts, filter and per-tab sort -----------------------------

        APP_SHORTCUT_ACTIONS = frozenset({
            "quit", "refresh", "cycle_sort", "toggle_unused", "open_filter",
            "close_filter", "date_range", "next_tab", "previous_tab",
        })

        def _active_pane(self) -> str:
            return self.query_one(TabbedContent).active

        def check_action(self, action: str, parameters) -> bool | None:
            """Hide a key where pressing it could only do nothing.

            A pushed detail screen owns its keys: this app's shortcuts are
            disabled there, while the screen's own Back binding stays alive.
            On the main screen, sort/filter/expand disappear where no table or
            no expansion exists instead of leaving dead keys in the footer.
            """
            if len(self.screen_stack) > 1:
                return action not in self.APP_SHORTCUT_ACTIONS
            pane = self._active_pane()
            if action in ("next_tab", "previous_tab"):
                return True
            if action == "toggle_unused":
                return self._entity_tab(pane) is not None
            if action == "cycle_sort":
                return bool(self._sort_fields_for(pane))
            if action in ("open_filter", "close_filter"):
                return pane == "tab-usage" or self._entity_tab(pane) is not None
            if action == "date_range":
                return self._range_of(pane) is not None
            return True

        def action_refresh(self) -> None:
            # R = incremental sync + reload DB + refresh the UI (not just a
            # re-render of already-loaded data).
            self._set_status("Syncing…")
            self._request_sync()

        def action_toggle_unused(self) -> None:
            # a = include the installed-but-unused half of the union, per tab.
            # Rows re-render from the cache; no query runs.
            pane = self._active_pane()
            tab = self._entity_tab(pane)
            if tab is None:
                return
            self._show_unused[pane] = not self._show_unused[pane]
            self._render_entity_rows(tab)

        def action_cycle_sort(self) -> None:
            pane = self._active_pane()
            fields = self._sort_fields_for(pane)
            if not fields:
                return
            if pane == "tab-usage":
                self._sort_mode[pane] = ((self._sort_mode.get(pane, 0) + 1)
                                         % len(fields))
                self._refresh_usage()
            else:
                tab = self._entity_tab(pane)
                if tab is None:
                    return
                self._sort_mode[pane] = ((self._sort_mode.get(pane, 0) + 1)
                                         % (len(fields) + 1))
                self._render_entity_rows(tab)

        def action_date_range(self) -> None:
            pane = self._active_pane()
            if self._range_of(pane) is None:
                return
            self._open_date_range(pane)

        def action_open_filter(self) -> None:
            pane = self._active_pane()
            if pane != "tab-usage" and self._entity_tab(pane) is None:
                return
            inp = self.query_one("#filter-input", Input)
            inp.display = True
            inp.value = self._name_filter.get(pane, "")
            inp.focus()
            inp.cursor_position = len(inp.value)
            self.call_after_refresh(self._anchor_active_table_top)

        def action_close_filter(self) -> None:
            # Esc = cancel: the needle goes away with the bar.
            self._clear_filter()

        def _clear_filter(self) -> None:
            pane = self._active_pane()
            inp = self.query_one("#filter-input", Input)
            inp.value = ""
            self._name_filter[pane] = ""
            inp.display = False
            if pane == "tab-usage":
                self._refresh_usage()
            else:
                tab = self._entity_tab(pane)
                if tab is not None:
                    self._render_entity_rows(tab)
            self._focus_active_table()
            self.call_after_refresh(self._anchor_active_table_top)

        def _sync_filter_bar(self) -> None:
            """Show the active tab's filter, or hide the row when it has none."""
            pane = self._active_pane()
            inp = self.query_one("#filter-input", Input)
            value = self._name_filter.get(pane, "")
            inp.value = value
            inp.display = bool(value.strip())

        def _focus_active_table(self) -> None:
            pane = self._active_pane()
            table_id = "t-usage" if pane == "tab-usage" else None
            if table_id is None:
                tab = self._entity_tab(pane)
                table_id = tab.table_id if tab else None
            if table_id is None:
                return
            try:
                self.query_one(f"#{table_id}", DataTable).focus()
            except Exception:
                pass

        def _anchor_active_table_top(self) -> None:
            """Filter edits show the first match instead of pinning a deep row.

            `_fill` restores the cursor by row key on refreshes and sorts. Under
            a live filter that would keep a deep selection and make the list
            look as if it scrolls by itself, hiding the first matches.
            """
            pane = self._active_pane()
            table_id = "t-usage" if pane == "tab-usage" else None
            if table_id is None:
                tab = self._entity_tab(pane)
                table_id = tab.table_id if tab else None
            if table_id is None:
                return
            try:
                table = self.query_one(f"#{table_id}", DataTable)
            except Exception:
                return
            if table.row_count:
                table.move_cursor(row=0)
                table.scroll_home(animate=False)

        def on_input_changed(self, event: Input.Changed) -> None:
            # The modal's own inputs are handled on DateRangeModal; anything
            # that bubbles here with another id is not the filter bar.
            if event.input.id != "filter-input":
                return
            pane = self._active_pane()
            if pane != "tab-usage" and self._entity_tab(pane) is None:
                return
            self._name_filter[pane] = event.value.strip()
            if pane == "tab-usage":
                self._refresh_usage()
            else:
                self._render_entity_rows(self._entity_tab(pane))
            self.call_after_refresh(self._anchor_active_table_top)

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "filter-input":
                # Enter hands the keys back to the table but keeps the bar
                # visible: an applied filter is state the page must show.
                pane = self._active_pane()
                event.input.display = bool(
                    self._name_filter.get(pane, "").strip())
                self._focus_active_table()

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

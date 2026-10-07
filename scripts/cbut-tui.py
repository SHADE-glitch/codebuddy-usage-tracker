#!/usr/bin/env python3
"""cbut-tui — interactive five-tab viewer for the CodeBuddy usage tracker.

Tabs: Tools · Skills · Agents · Plugins · MCP.

Requires ``textual`` (see requirements.txt). The headless reports in
cbut-stats.py need no third-party packages; this file is the only place that
imports textual, so importing it without textual fails with a clear message
instead of a traceback.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import Screen
    from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

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


if HAVE_TEXTUAL:

    class HistoryScreen(Screen):
        """Recent calls for one entity, pushed on row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, kind: str, name: str):
            super().__init__()
            self.kind = kind
            self.entity = name

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Vertical(
                Static(f"[b]{self.kind}[/b] · {self.entity}", id="hist-title"),
                DataTable(id="hist-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind}: {self.entity}"
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

    class TrackerApp(App):
        CSS = """
        #hist-title { padding: 1 2; }
        DataTable { height: 1fr; }
        #status { padding: 0 2; color: $text-muted; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("s", "sync", "Sync"),
            Binding("tab", "next_tab", "Next tab", priority=True),
        ]
        TITLE = "CodeBuddy usage tracker"

        def __init__(self, db_path):
            super().__init__()
            self.db_path = str(db_path)

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
            self.refresh_data()

        def _fill(self, table, rows, key_index=0, key_kind=None):
            table.clear()
            for r in rows:
                cells = [("" if c is None else str(c)) for c in r]
                key = None
                if key_kind:
                    key = f"{key_kind}\t{r[key_index]}"
                table.add_row(*cells, key=key)

        def refresh_data(self) -> None:
            conn = db.open_db(self.db_path, readonly=True)
            try:
                self._fill(
                    self.query_one("#t-tools", DataTable),
                    [[r["tool_name"], r["calls"], r["completed"], r["failed"],
                      ms(r["avg_ms"]), ts(r["last_used"])] for r in db.q_tools(conn)],
                    key_kind="tool")
                self._fill(
                    self.query_one("#t-skills", DataTable),
                    [[r["skill"], r["plugin"] or "-", r["calls"], r["completed"],
                      ts(r["last_used"])] for r in db.q_skills(conn)],
                    key_kind="skill")
                self._fill(
                    self.query_one("#t-agents", DataTable),
                    [[r["agent_type"], r["kind"], r["calls"], ts(r["last_used"])]
                     for r in db.q_agents(conn)],
                    key_kind="agent")
                self._fill(
                    self.query_one("#t-plugins", DataTable),
                    [[r["plugin"], r["version"] or "-", r["uses"], r["skills"],
                      r["agents"], r["commands"]] for r in db.q_plugins(conn)])
                self._fill(
                    self.query_one("#t-mcp", DataTable),
                    [[r["server"], r["tool"], r["calls"], r["completed"], ts(r["last_used"])]
                     for r in db.q_mcp(conn)],
                    key_kind="mcp")
                o = db.overview(conn)
            finally:
                conn.close()
            self.query_one("#status", Static).update(
                f"db={self.db_path} · {o['tool_calls']} tool calls · "
                f"{o['skills']} skills · {o['agents']} agents · "
                f"{o['plugins_used']} plugins · {o['mcp']} mcp · "
                f"last {ts(o['last_ts'])}"
            )

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            key = event.row_key.value
            if not key or "\t" not in key:
                return
            kind, name = key.split("\t", 1)
            self.push_screen(HistoryScreen(kind, name))

        def action_refresh(self) -> None:
            self.refresh_data()
            self.notify("refreshed")

        def action_sync(self) -> None:
            self.notify("syncing…")
            self.run_worker(self._do_sync, thread=True)

        def _do_sync(self) -> None:
            # cbut-sync.py has a hyphen (script naming, like skillt), so it is
            # invoked as a subprocess rather than imported.
            import subprocess

            sync = Path(__file__).with_name("cbut-sync.py")
            subprocess.run(
                [sys.executable, str(sync), "--quiet", "--db", self.db_path],
                check=False,
            )
            self.call_from_thread(self.refresh_data)
            self.call_from_thread(self.notify, "sync complete")

        def action_next_tab(self) -> None:
            tabs = self.query_one(TabbedContent)
            tabs.action_next_tab()


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

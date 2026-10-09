#!/usr/bin/env python3
"""cbut-stats — headless reports over the tracker database.

Standard library only (no textual needed). Subcommands:

    stats                 overview of all five categories
    tools|skills|agents|mcp|plugins   per-category tables
    models                per-model token totals from transcript rawUsage
    show <kind> <name>    recent calls for one entity
    recent                most recent tool calls
    inventory             what is installed / available, used vs unused
    export                dump every table as JSON
    health                DB + data-source health check

The interactive view lives in cbut-tui.py; this is for scripting and CI.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402


# --- formatting ------------------------------------------------------------


def ts(v):
    if not v:
        return "-"
    return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")


def ms(v):
    if v is None:
        return "-"
    if v < 1000:
        return f"{v}ms"
    return f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count: ``-`` for NULL (never a fabricated 0), else grouped."""
    if v is None:
        return "-"
    return f"{v:,}"


def _rendered_width(headers, rows) -> int:
    """Width (chars) of the table ``table()`` would print, including separators."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len("" if c is None else str(c)))
    return sum(widths) + 2 * (len(headers) - 1)


def table(headers, rows, aligns=None):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    aligns = aligns or ["<"] * len(headers)

    def line(cells):
        return "  ".join(
            f"{c:{aligns[i]}{widths[i]}}" for i, c in enumerate(cells)
        )

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _rows(cur, keys):
    return [[r[k] for k in keys] for r in cur]


# --- subcommands -----------------------------------------------------------


def cmd_stats(conn, args):
    o = db.overview(conn)
    print("CodeBuddy usage — overview")
    print("=" * 60)
    print(f"  sessions           {o['sessions']}")
    print(f"  projects           {o['projects']}")
    print(f"  tool calls         {o['tool_calls']}  ({o['distinct_tools']} distinct tools)")
    print(f"  skill invocations  {o['skills']}")
    print(f"  agent invocations  {o['agents']}")
    print(f"  MCP invocations    {o['mcp']}")
    print(f"  plugins used       {o['plugins_used']}")
    print(f"  slash commands     {o['commands']}")
    print(f"  model responses    {o['model_responses']} "
          f"({o['model_responses_with_usage']} with usage)")
    print(f"  model tokens       in {o['model_prompt_tokens']:,}  "
          f"out {o['model_completion_tokens']:,}  "
          f"total {o['model_total_tokens']:,}")
    print(f"  model cache        hit {o['model_prompt_cache_hit_tokens']:,}  "
          f"miss {o['model_prompt_cache_miss_tokens']:,}  "
          f"write {o['model_prompt_cache_write_tokens']:,}  (not in total)")
    # Kept visually separate from model tokens on purpose: sessions.tokens is the
    # turn-metrics.tokenDelta context metric, not prompt/completion tokens.
    print(f"  context tokens     {o['total_tokens']:,}  (turn-metrics tokenDelta)")
    print(f"  range              {ts(o['first_ts'])} .. {ts(o['last_ts'])}")
    print()

    print("Top tools")
    rows = _rows(db.q_tools(conn, 10), ["tool_name", "calls", "completed", "failed", "avg_ms", "last_used"])
    print(table(["tool", "calls", "ok", "fail", "avg", "last used"],
                [[r[0], r[1], r[2], r[3], ms(r[4]), ts(r[5])] for r in rows]))
    print()

    print("Skills")
    rows = _rows(db.q_skills(conn), ["skill", "calls", "last_used"])
    print(table(["skill", "calls", "last used"],
                [[r[0], r[1], ts(r[2])] for r in rows]) or "  (none)")
    print()

    print("Agents")
    rows = _rows(db.q_agents(conn), ["agent_type", "calls", "last_used"])
    print(table(["agent", "calls", "last used"],
                [[r[0], r[1], ts(r[2])] for r in rows]) or "  (none)")
    print()

    print("Plugins (installed ∪ used, by attributed usage)")
    rows = _rows(db.q_plugins(conn), ["plugin", "uses", "skills", "agents", "commands"])
    print(table(["plugin", "uses", "skills", "agents", "cmds"],
                [[r[0], r[1], r[2], r[3], r[4]] for r in rows]))
    print()

    print("MCP")
    rows = _rows(db.q_mcp(conn), ["server", "tool", "calls", "last_used"])
    if rows:
        print(table(["server", "tool", "calls", "last used"],
                    [[r[0], r[1], r[2], ts(r[3])] for r in rows]))
    else:
        print("  (no MCP tool invocations recorded)")


def cmd_tools(conn, args):
    rows = _rows(db.q_tools(conn, args.limit),
                 ["tool_name", "calls", "completed", "failed", "pending", "avg_ms", "last_used", "projects"])
    print(table(["tool", "calls", "ok", "fail", "pend", "avg", "last used", "proj"],
                [[r[0], r[1], r[2], r[3], r[4], ms(r[5]), ts(r[6]), r[7]] for r in rows]))


def cmd_skills(conn, args):
    rows = _rows(db.q_skills(conn, args.limit),
                 ["skill", "calls", "completed", "last_used", "projects"])
    print(table(["skill", "calls", "ok", "last used", "proj"],
                [[r[0], r[1], r[2], ts(r[3]), r[4]] for r in rows]) or "(none)")


def cmd_agents(conn, args):
    rows = _rows(db.q_agents(conn, args.limit),
                 ["agent_type", "calls", "last_used", "projects"])
    print(table(["agent", "calls", "last used", "proj"],
                [[r[0], r[1], ts(r[2]), r[3]] for r in rows]) or "(none)")


def cmd_mcp(conn, args):
    rows = _rows(db.q_mcp(conn, args.limit),
                 ["server", "tool", "calls", "completed", "last_used"])
    print(table(["server", "tool", "calls", "ok", "last used"],
                [[r[0], r[1], r[2], r[3], ts(r[4])] for r in rows]) or "(none)")


# Per-model table: (header, q_model_tokens key). Order matters — the trailing
# cache columns are the first dropped when the terminal is narrow.
MODEL_TABLE_COLUMNS = (
    ("model", "model"),
    ("resp", "responses"),
    ("in", "prompt_tokens"),
    ("out", "completion_tokens"),
    ("total", "total_tokens"),
    ("cache hit", "prompt_cache_hit_tokens"),
    ("cache miss", "prompt_cache_miss_tokens"),
    ("cache write", "prompt_cache_write_tokens"),
)
# Dropped left-to-right until the table fits; model/resp/in/out always survive.
_MODEL_DROP_ORDER = ("cache write", "cache miss", "cache hit", "resp")
_MODEL_ALWAYS_KEEP = 4


def _fit_model_columns(width, headers, rows):
    """Reduce (headers, rows) to fit ``width`` by dropping cache, then resp/usage.

    Never drops below the first four columns (model / resp / in / out).
    """
    headers = list(headers)
    rows = [list(r) for r in rows]
    for name in _MODEL_DROP_ORDER:
        if _rendered_width(headers, rows) <= width or len(headers) <= _MODEL_ALWAYS_KEEP:
            break
        if name in headers:
            j = headers.index(name)
            headers.pop(j)
            for r in rows:
                r.pop(j)
    return headers, rows


def cmd_models(conn, args):
    """Per-model token totals from real transcript usage (rawUsage)."""
    data = _rows(db.q_model_tokens(conn), [k for _, k in MODEL_TABLE_COLUMNS])
    rows = [[r[0] or "?"] + [fmt_n(c) for c in r[1:]] for r in data]
    headers = [h for h, _ in MODEL_TABLE_COLUMNS]
    width = shutil.get_terminal_size((100, 24)).columns
    headers, rows = _fit_model_columns(width, headers, rows)
    print("Model token usage (source: transcript providerData.rawUsage)")
    print("Total = provider total when present, else Input + Output. "
          "Cache hit/miss/write are separate, not in Total.")
    if rows:
        print(table(headers, rows))
    else:
        print("  (no model responses indexed yet)")
    print()
    print("Recent model responses")
    recent = db.q_model_responses(conn, args.limit)
    print(table(
        ["when", "model", "in", "out", "total", "cache hit", "cache miss",
         "cache write", "usage", "session"],
        [[ts(r["ts"]), r["model"] or "?", fmt_n(r["prompt_tokens"]),
          fmt_n(r["completion_tokens"]), fmt_n(r["total_tokens"]),
          fmt_n(r["prompt_cache_hit_tokens"]),
          fmt_n(r["prompt_cache_miss_tokens"]),
          fmt_n(r["prompt_cache_write_tokens"]),
          "yes" if r["usage_available"] else "no",
          (r["session_id"] or "")[:8]] for r in recent]) or "  (none)")


def cmd_plugins(conn, args):
    rows = _rows(db.q_plugins(conn),
                 ["plugin", "uses", "skills", "agents", "commands", "last_used"])
    print(table(["plugin", "uses", "skills", "agents", "cmds", "last used"],
                [[r[0], r[1], r[2], r[3], r[4], ts(r[5])] for r in rows]))


def cmd_show(conn, args):
    rows = _rows(db.q_history(conn, args.kind, args.name, args.limit),
                 ["ts", "project", "session_id", "status", "duration_ms"])
    if not rows:
        print(f"no recorded calls for {args.kind} {args.name!r}")
        return
    print(f"{args.kind} {args.name} — {len(rows)} recent calls")
    print(table(["when", "project", "session", "status", "dur"],
                [[ts(r[0]), r[1] or "-", (r[2] or "")[:8], r[3] or "-", ms(r[4])] for r in rows]))


def cmd_recent(conn, args):
    rows = _rows(db.q_recent(conn, args.limit),
                 ["ts", "tool_name", "category", "status", "duration_ms", "project"])
    print(table(["when", "tool", "category", "status", "dur", "project"],
                [[ts(r[0]), r[1], r[2], r[3] or "-", ms(r[4]), r[5] or "-"] for r in rows]))


def cmd_inventory(conn, args):
    rows = _rows(db.q_inventory(conn, args.kind),
                 ["kind", "name", "owner_plugin", "source", "version"])
    print(table(["kind", "name", "owner", "source", "version"],
                [[r[0], r[1], r[2] or "-", r[3], r[4] or "-"] for r in rows]))


def cmd_export(conn, args):
    out = {}
    for view in ("v_tools", "v_skills", "v_agents", "v_mcp", "v_plugins"):
        out[view] = [dict(r) for r in conn.execute(f"SELECT * FROM {view}")]
    out["inventory"] = [dict(r) for r in conn.execute("SELECT * FROM inventory")]
    out["model_tokens"] = [dict(r) for r in db.q_model_tokens(conn)]
    out["model_responses"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM model_responses ORDER BY ts DESC")
    ]
    out["sessions"] = [
        dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY ended_at DESC")
    ]
    out["overview"] = db.overview(conn)
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_health(conn, args):
    print("cbut health")
    print("=" * 60)
    print(f"  db path        {db.DB_PATH}")
    print(f"  codebuddy dir  {db.CODEBUDDY_DIR} "
          f"({'ok' if db.CODEBUDDY_DIR.is_dir() else 'MISSING'})")
    try:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        print(f"  schema version {ver[0] if ver else '?'}")
    except sqlite3.Error:
        print("  schema version (missing)")
    projects = db.CODEBUDDY_DIR / "projects"
    n_files = len(list(projects.glob("**/*.jsonl"))) if projects.is_dir() else 0
    tracked = conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    print(f"  transcript files {n_files} on disk, {tracked} tracked")
    o = db.overview(conn)
    print(f"  indexed        {o['tool_calls']} tool calls, {o['sessions']} sessions, "
          f"{o['model_responses']} model responses "
          f"({o['model_responses_with_usage']} with usage)")
    print(f"  last activity  {ts(o['last_ts'])}")
    # The format-change signal. Printing the "0" case too, so the line itself
    # proves the check ran rather than merely being absent when nothing broke.
    if o["unparsed_records"]:
        print(f"  UNPARSED       {o['unparsed_records']} records "
              f"across {o['unparsed_kinds']} reason(s) no handler claims:")
        for row in db.q_unparsed(conn)[:8]:
            print(f"                 {row['count']:>8}  {row['reason']}")
        print("                 (a jump here right after a CodeBuddy upgrade "
              "means its log format changed)")
    else:
        print("  unparsed       0 — every record type was claimed")


# --- snapshots -------------------------------------------------------------
#
# These two do not follow the ``cmd_x(conn, args)`` shape the reports use: they
# work on the database *file*, and ``restore`` has to run when the database is
# missing or unreadable — which is exactly when the read-only connection the
# reports open would refuse to start.


def cmd_backup(args) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path} — run: cbut sync", file=sys.stderr)
        return 1
    conn = db.open_db(path)
    try:
        dest = db.snapshot(conn, reason="manual")
    finally:
        conn.close()
    print(f"snapshot  {dest}  ({dest.stat().st_size:,} bytes)")
    print(f"          keeping the newest {db.BACKUP_KEEP} under {dest.parent}")
    return 0


def cmd_restore(args) -> int:
    path = Path(args.db)
    found = db.list_backups(path)
    if not args.name:
        if not found:
            print(f"no backups under {path.parent}", file=sys.stderr)
            return 1
        print(f"backups for {path}")
        for item in found:
            kind = "managed" if item.parent.name == "backups" else "hand-made"
            print(f"  {item.name}  {item.stat().st_size:>12,} B  {kind}")
        print("\n  restore one:  cbut restore NAME     (close the TUI first)")
        return 0
    candidate = Path(args.name)
    target = candidate if candidate.is_file() else next(
        (p for p in found if p.name == args.name), None)
    if target is None:
        print(f"no backup named {args.name!r} — list them with: cbut restore",
              file=sys.stderr)
        return 1
    try:
        db.restore(target, db_path=path)
    except sqlite3.Error as exc:
        # Almost always the TUI holding the file open.
        print(f"could not restore over {path}: {exc}\n"
              f"  close the TUI (and stop cbut-sync.timer) and retry",
              file=sys.stderr)
        return 1
    print(f"restored  {path}\n  from      {target}")
    print(f"  the database that was there is kept in {db.backup_dir(path)}")
    return 0


FORMAT_BEGIN = "<!-- BEGIN generated: cbut format -->"
FORMAT_END = "<!-- END generated -->"


def _replace_generated_block(doc: Path, body: str) -> bool:
    """Rewrite the marked block in ``doc``; False when the markers are gone.

    Refusing beats appending: a generated section dropped into hand-written prose
    is how a document ends up stating two things at once.
    """
    text = doc.read_text(encoding="utf-8") if doc.is_file() else ""
    if FORMAT_BEGIN not in text or FORMAT_END not in text:
        return False
    head, _, rest = text.partition(FORMAT_BEGIN + "\n")
    _, tail = rest.split(FORMAT_END, 1)
    doc.write_text(f"{head}{FORMAT_BEGIN}\n{body}{FORMAT_END}{tail}",
                   encoding="utf-8")
    return True


def cmd_format(args) -> int:
    """Print or refresh the CodeBuddy format-dependency surface.

    Generated from the parser's registry instead of written by hand, because a
    hand-copied list is how `schema_version` became a value nothing read: a
    description that stopped matching the code and stayed checked in anyway.
    """
    body = db.load_sync().format_doc()
    if not args.write:
        print(body, end="")
        return 0
    doc = (Path(__file__).resolve().parents[1]
           / "docs" / "maintenance" / "codebuddy-format.md")
    if not _replace_generated_block(doc, body):
        print(f"{doc} has no generated block markers — refusing to rewrite it",
              file=sys.stderr)
        return 1
    print(f"updated   {doc}")
    return 0


# --- driver ----------------------------------------------------------------

COMMANDS = {
    "stats": cmd_stats, "tools": cmd_tools, "skills": cmd_skills,
    "agents": cmd_agents, "mcp": cmd_mcp, "plugins": cmd_plugins,
    "models": cmd_models,
    "show": cmd_show, "recent": cmd_recent, "inventory": cmd_inventory,
    "export": cmd_export, "health": cmd_health,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cbut", description="CodeBuddy usage reports.")
    ap.add_argument("--db", default=str(db.DB_PATH))
    sub = ap.add_subparsers(dest="cmd")

    for name in ("stats", "health", "plugins", "inventory", "export"):
        p = sub.add_parser(name)
        p.add_argument("--kind", default=None, help="inventory only: filter by kind")
    for name in ("tools", "skills", "agents", "mcp", "recent", "models"):
        sub.add_parser(name).add_argument("--limit", type=int, default=30)
    sp = sub.add_parser("show")
    sp.add_argument("kind", choices=["tool", "skill", "agent", "mcp"])
    sp.add_argument("name")
    sp.add_argument("--limit", type=int, default=50)
    sub.add_parser("backup", help="snapshot the database before a risky step")
    rp = sub.add_parser("restore", help="restore a snapshot (omit NAME to list)")
    rp.add_argument("name", nargs="?", default=None)
    fp = sub.add_parser("format",
                        help="print the CodeBuddy format-dependency surface")
    fp.add_argument("--write", action="store_true",
                    help="refresh the generated block in docs/maintenance/")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "stats"

    # Not through the report connection: restore has to work when the database
    # is missing or corrupt, which is when opening it read-only would fail.
    if args.cmd == "backup":
        return cmd_backup(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "format":
        # Reads no database at all — the surface is in the code.
        return cmd_format(args)

    if not Path(args.db).exists():
        print(f"no database at {args.db} — run: cbut sync", file=sys.stderr)
        return 1
    conn = db.open_db(args.db, readonly=True)
    try:
        COMMANDS[args.cmd](conn, args)
    except sqlite3.OperationalError as exc:
        # A database written by an older build of this tool has no table or
        # column the current queries expect, and this CLI opens it read-only — it
        # cannot repair that itself. A traceback hides an actionable one-liner.
        print(f"cbut: this database is older than this build ({exc})\n"
              f"      run: cbut sync", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

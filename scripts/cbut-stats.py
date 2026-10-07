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
          f"out {o['model_completion_tokens']:,}")
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
    rows = _rows(db.q_skills(conn), ["skill", "plugin", "calls", "last_used"])
    print(table(["skill", "plugin", "calls", "last used"],
                [[r[0], r[1] or "-", r[2], ts(r[3])] for r in rows]) or "  (none)")
    print()

    print("Agents")
    rows = _rows(db.q_agents(conn), ["agent_type", "kind", "calls", "last_used"])
    print(table(["agent", "kind", "calls", "last used"],
                [[r[0], r[1], r[2], ts(r[3])] for r in rows]) or "  (none)")
    print()

    print("Plugins (installed, by attributed usage)")
    rows = _rows(db.q_plugins(conn), ["plugin", "version", "uses", "skills", "agents", "commands"])
    print(table(["plugin", "version", "uses", "skills", "agents", "cmds"],
                [[r[0], r[1] or "-", r[2], r[3], r[4], r[5]] for r in rows]))
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
                 ["skill", "plugin", "calls", "completed", "last_used", "projects"])
    print(table(["skill", "plugin", "calls", "ok", "last used", "proj"],
                [[r[0], r[1] or "-", r[2], r[3], ts(r[4]), r[5]] for r in rows]) or "(none)")


def cmd_agents(conn, args):
    rows = _rows(db.q_agents(conn, args.limit),
                 ["agent_type", "kind", "calls", "last_used", "projects"])
    print(table(["agent", "kind", "calls", "last used", "proj"],
                [[r[0], r[1], r[2], ts(r[3]), r[4]] for r in rows]) or "(none)")


def cmd_mcp(conn, args):
    rows = _rows(db.q_mcp(conn, args.limit),
                 ["server", "tool", "calls", "completed", "last_used"])
    print(table(["server", "tool", "calls", "ok", "last used"],
                [[r[0], r[1], r[2], r[3], ts(r[4])] for r in rows]) or "(none)")


def cmd_models(conn, args):
    """Per-model token totals from real transcript usage (rawUsage)."""
    rows = _rows(db.q_model_tokens(conn),
                 ["model", "responses", "with_usage", "prompt_tokens",
                  "completion_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"])
    print("Model token usage (source: transcript providerData.rawUsage)")
    print(table(["model", "resp", "usage", "in", "out", "cache_r", "cache_w"],
                [[r[0] or "?", r[1], r[2], r[3] or 0, r[4] or 0, r[5] or 0, r[6] or 0]
                 for r in rows]) or "  (no model responses indexed yet)")
    print()
    print("Recent model responses")
    recent = db.q_model_responses(conn, args.limit)
    print(table(["when", "model", "in", "out", "cache_r", "cache_w", "usage", "session"],
                [[ts(r["ts"]), r["model"] or "?", r["prompt_tokens"], r["completion_tokens"],
                  r["cache_read_input_tokens"], r["cache_creation_input_tokens"],
                  "yes" if r["usage_available"] else "no",
                  (r["session_id"] or "")[:8]] for r in recent]) or "  (none)")


def cmd_plugins(conn, args):
    rows = _rows(db.q_plugins(conn),
                 ["plugin", "version", "uses", "skills", "agents", "commands", "last_used"])
    print(table(["plugin", "version", "uses", "skills", "agents", "cmds", "last used"],
                [[r[0], r[1] or "-", r[2], r[3], r[4], r[5], ts(r[6])] for r in rows]))


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

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "stats"
    if not Path(args.db).exists():
        print(f"no database at {args.db} — run: cbut sync", file=sys.stderr)
        return 1
    conn = db.open_db(args.db, readonly=True)
    try:
        COMMANDS[args.cmd](conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

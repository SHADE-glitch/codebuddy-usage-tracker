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
    # Negative: the call and result records disagree about their order (see
    # db._null_negative_durations); "-" rather than a believable "-4611ms".
    if v is None or v < 0:
        return "-"
    if v < 1000:
        return f"{v}ms"
    return f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count: ``-`` for NULL (never a fabricated 0), else grouped."""
    if v is None:
        return "-"
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


def _rendered_width(headers, rows) -> int:
    """Width (chars) of the table ``table()`` would print, including separators."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i],
                            len("" if c is None else db.clean_cell(c)))
    return sum(widths) + 2 * (len(headers) - 1)


def table(headers, rows, aligns=None):
    # A transcript-derived name can carry a newline or a terminal escape; the
    # headless report is rendered by the same cleaner the TUI uses, so one bad
    # name cannot break either surface.
    rows = [[("" if c is None else db.clean_cell(c)) for c in r] for r in rows]
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


def table_or(headers, rows, aligns=None, empty="(none)"):
    """``table(...)`` when there is data, else the ``empty`` notice.

    ``table`` always returns at least a header and a separator line, so the
    ``table(...) or "(none)"`` idiom this replaces never reached its right-hand
    side: an empty report printed a bare header with nothing under it. Branch on
    the rows, which is what the fallback was always trying to say.
    """
    return table(headers, rows, aligns) if rows else empty


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
    rows = _rows(db.q_tools(conn, 10),
                 ["tool_name", "calls", "completed", "failed", "avg_ms",
                  "last_used", "installed", "ever_used"])
    print(table(["tool", "calls", "ok", "fail", "avg", "last used", "state"],
                [[r[0], r[1], r[2], r[3], ms(r[4]), ts(r[5]),
                  db.entity_state(r[6], r[7], r[1])] for r in rows]))
    print()

    print("Skills")
    rows = _rows(db.q_skills(conn),
                 ["skill", "calls", "last_used", "installed", "ever_used"])
    print(table_or(["skill", "calls", "last used", "state"],
                   [[r[0], r[1], ts(r[2]), db.entity_state(r[3], r[4], r[1])]
                    for r in rows], empty="  (none)"))
    print()

    print("Agents")
    rows = _rows(db.q_agents(conn),
                 ["agent_type", "calls", "last_used", "installed", "ever_used"])
    print(table_or(["agent", "calls", "last used", "state"],
                   [[r[0], r[1], ts(r[2]), db.entity_state(r[3], r[4], r[1])]
                    for r in rows], empty="  (none)"))
    print()

    print("Plugins (installed ∪ used, by attributed usage)")
    rows = _rows(db.q_plugins(conn),
                 ["plugin", "uses", "skills", "agents", "commands",
                  "installed", "ever_used"])
    print(table(["plugin", "uses", "skills", "agents", "cmds", "state"],
                [[r[0], r[1], r[2], r[3], r[4],
                  db.entity_state(r[5], r[6], r[1])] for r in rows]))
    print()

    print("MCP")
    rows = _rows(db.q_mcp(conn),
                 ["server", "tool", "calls", "last_used", "installed", "ever_used"])
    if rows:
        print(table(["server", "tool", "calls", "last used", "state"],
                    [[r[0], r[1], r[2], ts(r[3]),
                      db.entity_state(r[4], r[5], r[2])] for r in rows]))
    else:
        print("  (no MCP tool invocations recorded)")


def cmd_tools(conn, args):
    rows = _rows(db.q_tools(conn, args.limit),
                 ["tool_name", "calls", "completed", "failed", "pending",
                  "avg_ms", "last_used", "projects", "installed", "ever_used"])
    print(table(["tool", "calls", "ok", "fail", "pend", "avg", "last used",
                 "proj", "state"],
                [[r[0], r[1], r[2], r[3], r[4], ms(r[5]), ts(r[6]), r[7],
                  db.entity_state(r[8], r[9], r[1])] for r in rows]))


def cmd_skills(conn, args):
    rows = _rows(db.q_skills(conn, args.limit),
                 ["skill", "calls", "completed", "last_used", "projects",
                  "installed", "ever_used"])
    print(table_or(["skill", "calls", "ok", "last used", "proj", "state"],
                   [[r[0], r[1], r[2], ts(r[3]), r[4],
                     db.entity_state(r[5], r[6], r[1])] for r in rows]))


def cmd_agents(conn, args):
    rows = _rows(db.q_agents(conn, args.limit),
                 ["agent_type", "calls", "last_used", "projects",
                  "installed", "ever_used"])
    print(table_or(["agent", "calls", "last used", "proj", "state"],
                   [[r[0], r[1], ts(r[2]), r[3],
                     db.entity_state(r[4], r[5], r[1])] for r in rows]))


def cmd_mcp(conn, args):
    rows = _rows(db.q_mcp(conn, args.limit),
                 ["server", "tool", "calls", "completed", "last_used",
                  "installed", "ever_used"])
    print(table_or(["server", "tool", "calls", "ok", "last used", "state"],
                   [[r[0], r[1], r[2], r[3], ts(r[4]),
                     db.entity_state(r[5], r[6], r[2])] for r in rows]))


# Per-model table: (header, q_model_tokens key). Order matters — the trailing
# cache columns are the first dropped when the terminal is narrow.
MODEL_TABLE_COLUMNS = (
    ("model", "model"),
    ("provider", "provider"),
    ("resp", "responses"),
    ("in", "prompt_tokens"),
    ("out", "completion_tokens"),
    ("total", "total_tokens"),
    ("credits", "credits"),
    ("cache hit", "prompt_cache_hit_tokens"),
    ("cache miss", "prompt_cache_miss_tokens"),
    ("cache write", "prompt_cache_write_tokens"),
)
# Dropped left-to-right until the table fits; the identity, size and money
# columns (model / provider / resp / in / out / total / credits) survive.
_MODEL_DROP_ORDER = ("cache write", "cache miss", "cache hit")
_MODEL_ALWAYS_KEEP = 7


def _fit_model_columns(width, headers, rows):
    """Reduce (headers, rows) to fit ``width`` by dropping the cache columns.

    Never drops below the first five columns (model / provider / resp / in / out).
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
    # model and provider are text; the rest are counts.
    rows = [[r[0] or "?", db.provider_label(r[1])]
            + [fmt_n(c) for c in r[2:]] for r in data]
    headers = [h for h, _ in MODEL_TABLE_COLUMNS]
    width = shutil.get_terminal_size((100, 24)).columns
    headers, rows = _fit_model_columns(width, headers, rows)
    print("Model token usage (source: transcript providerData.rawUsage)")
    print("Total = provider total when present, else Input + Output. "
          "Credits are billing, kept beside the tokens. "
          "Cache hit/miss/write are separate, not in Total.")
    if rows:
        print(table(headers, rows))
    else:
        print("  (no model responses indexed yet)")
    print()
    print("Recent model responses")
    recent = db.q_model_responses(conn, args.limit)
    print(table_or(
        ["when", "model", "in", "out", "total", "credits", "cache hit",
         "cache miss", "cache write", "usage", "session"],
        [[ts(r["ts"]), r["model"] or "?", fmt_n(r["prompt_tokens"]),
          fmt_n(r["completion_tokens"]), fmt_n(r["total_tokens"]),
          fmt_credit(r["credit"]),
          fmt_n(r["prompt_cache_hit_tokens"]),
          fmt_n(r["prompt_cache_miss_tokens"]),
          fmt_n(r["prompt_cache_write_tokens"]),
          "yes" if r["usage_available"] else "no",
          (r["session_id"] or "")[:8]] for r in recent], empty="  (none)"))


def cmd_plugins(conn, args):
    rows = _rows(db.q_plugins(conn),
                 ["plugin", "uses", "skills", "agents", "commands", "last_used",
                  "installed", "ever_used"])
    print(table(["plugin", "uses", "skills", "agents", "cmds", "last used",
                 "state"],
                [[r[0], r[1], r[2], r[3], r[4], ts(r[5]),
                  db.entity_state(r[6], r[7], r[1])] for r in rows]))


def cmd_show(conn, args):
    if args.kind == "model":
        # The model drill-down is a response list, not a call list: the row
        # key is the model name and the rows are its (model, provider)
        # responses, both routes shown so a name served two ways is not
        # silently merged.
        found = db.q_model_responses(conn, model=args.name, limit=args.limit)
        if not found:
            print(f"no recorded responses for model {args.name!r}")
            return
        print(f"model {args.name} — {len(found)} recent responses")
        print(table(["when", "provider", "in", "out", "total", "credits",
                     "cache hit", "cache miss", "cache write", "session"],
                    [[ts(r["ts"]), db.provider_label(r["provider"]),
                      fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                      fmt_n(r["total_tokens"]), fmt_credit(r["credit"]),
                      fmt_n(r["prompt_cache_hit_tokens"]),
                      fmt_n(r["prompt_cache_miss_tokens"]),
                      fmt_n(r["prompt_cache_write_tokens"]),
                      (r["session_id"] or "")[:8]] for r in found]))
        return
    rows = _rows(db.q_history(conn, args.kind, args.name, args.limit),
                 ["ts", "project", "session_id", "status", "duration_ms"])
    if not rows:
        print(f"no recorded calls for {args.kind} {args.name!r}")
        return
    print(f"{args.kind} {args.name} — {len(rows)} recent calls")
    print(table(["when", "project", "session", "status", "dur"],
                [[ts(r[0]), r[1] or "-", (r[2] or "")[:8], r[3] or "-", ms(r[4])] for r in rows]))


def cmd_sessions(conn, args):
    """Recent sessions, newest first. tokens is the context metric."""
    rows = db.q_sessions(conn, limit=args.limit)
    print(table_or(
        ["started", "ended", "project", "model", "ctx tok", "active"],
        [[ts(r["started_at"]), ts(r["ended_at"]), r["project"] or "-",
          r["model"] or "-", fmt_n(r["tokens"]), ms(r["duration_ms"])]
         for r in rows],
        empty="  (no sessions indexed yet)"))


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
    print(f"  db path        {args.db}")
    print(f"  codebuddy dir  {db.CODEBUDDY_DIR} "
          f"({'ok' if db.CODEBUDDY_DIR.is_dir() else 'MISSING'})")
    # The settings surface, named so it can be found without reading the README.
    # A broken file is reported as one, in place, rather than being quietly skipped.
    try:
        cfg = db.load_config()
        state = "" if db.CONFIG_PATH.is_file() else " (absent — defaults)"
        # Every key the loader knows, spelled the way the settings file spells it.
        # A hand-listed line here goes stale one key at a time, and it silently
        # renames what it does print — both are worse than printing nothing at all.
        values = " ".join(f"{k}={v}{'s' if k.endswith('_secs') else ''}"
                          for k, v in sorted(cfg.items()))
        print(f"  settings       {db.CONFIG_PATH}{state} — {values}")
    except db.ConfigError as exc:
        print(f"  settings       NOT READ — {exc}")
    try:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        print(f"  schema version {ver[0] if ver else '?'}")
    except sqlite3.Error:
        print("  schema version (missing)")
    # Which builtin catalogue this machine resolves to. The fallback constants look
    # exactly like the real list in every table, and the two disagree about names
    # upstream actually ships, so the only place a stale fallback can be seen is a
    # line that says which of the two was read. Resolved again here rather than
    # remembered: health reports the state of the machine now, not of the last sync.
    sync = db.load_sync()
    catalog = sync.product_catalog()
    if catalog.path is not None:
        catalog_line = f"product.json @ {catalog.path}"
    else:
        found = sync.resolve_product_json()
        catalog_line = "fallback constants (" + (
            f"{found} carries no catalogue" if found is not None
            else "product.json unresolved") + ")"
    print(f"  {'builtin catalog':<15} {catalog_line} — "
          f"{len(catalog.tools)} tools, {len(catalog.agents)} agents, "
          f"{len(catalog.models)} models, {len(catalog.commands)} commands")
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
    rows = db.q_unparsed(conn)
    if rows:
        # Split the tally, because a total that mixes the three prose types this
        # build declines (tens of thousands on any real database) with genuine
        # drift cannot answer the question it is printed for. Each reason is
        # marked, and the line under the table only calls out the drift.
        expected, unexpected, unexpected_names = sync.split_unparsed(rows)
        print(f"  UNPARSED       {o['unparsed_records']} records "
              f"across {o['unparsed_kinds']} reason(s) no handler claims "
              f"({expected} known, {unexpected} not):")
        for row in rows[:8]:
            mark = "NEW" if not sync.unparsed_is_expected(row["reason"]) else "   "
            print(f"                 {mark} {row['count']:>8}  {row['reason']}")
        if unexpected:
            print(f"                 ({unexpected} record(s) under a reason this build "
                  "has never seen: " + ", ".join(unexpected_names) + " — that is "
                  "the format moving. A known reason growing is not.)")
        else:
            print("                 (every reason here is one this build already "
                  "declines on purpose, so nothing has moved)")
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


def _file_state(conn, path) -> dict:
    """What the file looked like, for comparing one rewrite against another."""
    return {
        "bytes": path.stat().st_size,
        "pages": conn.execute("PRAGMA page_count").fetchone()[0],
        "page_size": conn.execute("PRAGMA page_size").fetchone()[0],
        "free": conn.execute("PRAGMA freelist_count").fetchone()[0],
        "check": conn.execute("PRAGMA quick_check").fetchone()[0],
        # Every count `cbut health` prints, so a rewrite that lost a row says so here
        # instead of being found by a panel showing a number that moved for no reason.
        "totals": db.overview(conn),
    }


def cmd_vacuum(args) -> int:
    """Compact the database file, taking a snapshot first.

    `VACUUM` rewrites the whole file, so it needs room for a second copy — and it is the
    only way to get back the space a dropped index leaves behind: dropping one *parks*
    its pages on the freelist, it does not release them. It shipped as a subcommand
    rather than staying a recipe in `docs/maintenance/measurements.md` because the
    step easiest to forget in that recipe is the snapshot in front of it.

    The reading it came from, on the real store 2026-10-17: 19,636,224 B -> 18,284,544 B,
    4,794 pages to 4,465 of 4 KiB, with `freelist_count` at 0 *before* it — the space was
    slack inside the pages, so no freelist reading would have predicted it. Nothing is
    re-indexed and nothing is re-read: the rows come out as they went in, which is what
    the row counts and `quick_check` below are for.
    """
    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path} — run: cbut sync", file=sys.stderr)
        return 1
    conn = db.open_db(path)
    try:
        # Fold the WAL back into the main file *before* anything is measured: the file
        # size of a database whose newest pages are still in `-wal` is not the file this
        # command is about, and reading it there is how a compaction looks like it grew.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = _file_state(conn, path)
        snapshot = db.snapshot(conn, reason="pre-vacuum")
        conn.commit()          # VACUUM cannot run inside a transaction
        conn.execute("VACUUM")
        after = _file_state(conn, path)
    except sqlite3.Error as exc:
        print(f"could not compact {path}: {exc}", file=sys.stderr)
        if "lock" in str(exc).lower() or "busy" in str(exc).lower():
            print("  close the TUI (and stop the cbut-sync.timer) and retry",
                  file=sys.stderr)
        return 1
    finally:
        conn.close()
    # The file is only truncated to its new page count when the last connection lets go
    # of it, so the final size is read after the close. Reading it before reports a
    # compaction that reclaimed nothing while dropping pages.
    after["bytes"] = path.stat().st_size

    freed = before["bytes"] - after["bytes"]
    print(f"compacted  {path}")
    print(f"  before     {before['bytes']:,} B ({before['pages']:,} pages of "
          f"{before['page_size']:,} B, {before['free']:,} on the freelist)")
    print(f"  after      {after['bytes']:,} B ({after['pages']:,} pages, "
          f"{after['free']:,} free)")
    if freed:
        print(f"  reclaimed  {freed:,} B ({freed / before['bytes']:.1%} of the file)")
    else:
        print("  reclaimed  nothing — the file was already compact")
    print(f"  snapshot   {snapshot}")
    if after["check"] != "ok" or after["totals"] != before["totals"]:
        print(f"  WARNING    the file changed shape — quick_check reads "
              f"{after['check']!r} and the row totals "
              f"{'differ' if after['totals'] != before['totals'] else 'match'}\n"
              f"             restore it:  cbut restore {snapshot.name}",
              file=sys.stderr)
        return 1
    print("  verified   quick_check ok, and every row count is what it was")
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
    "show": cmd_show, "recent": cmd_recent, "sessions": cmd_sessions,
    "inventory": cmd_inventory, "export": cmd_export, "health": cmd_health,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cbut", description="CodeBuddy usage reports.")
    ap.add_argument("--db", default=str(db.DB_PATH))
    sub = ap.add_subparsers(dest="cmd")

    for name in ("stats", "health", "plugins", "inventory", "export"):
        p = sub.add_parser(name)
        p.add_argument("--kind", default=None, help="inventory only: filter by kind")
    for name in ("tools", "skills", "agents", "mcp", "recent", "models",
                 "sessions"):
        sub.add_parser(name).add_argument("--limit", type=int, default=30)
    sp = sub.add_parser("show")
    sp.add_argument("kind", choices=["tool", "skill", "agent", "mcp", "model"])
    sp.add_argument("name")
    sp.add_argument("--limit", type=int, default=50)
    sub.add_parser("backup", help="snapshot the database before a risky step")
    sub.add_parser("vacuum", help="compact the database file (snapshots first)")
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
    if args.cmd == "vacuum":
        return cmd_vacuum(args)
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

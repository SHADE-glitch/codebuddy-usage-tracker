#!/usr/bin/env python3
"""cbut-sync — index CodeBuddy's own logs into the tracker database.

Passive reader: walks ``~/.codebuddy/projects/**/*.jsonl`` (session
transcripts), classifies every tool call into one of the five tracked
categories, and upserts it into SQLite. Incremental: each file remembers how
many bytes were already parsed, so re-runs only read the tail.

    python3 scripts/cbut-sync.py            # incremental
    python3 scripts/cbut-sync.py --full     # wipe tracker tables, re-index all
    python3 scripts/cbut-sync.py --quiet

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbut_db as db  # noqa: E402

# --- classification tables -------------------------------------------------

# Tools shipped with CodeBuddy. Anything not Skill/Agent/mcp/meta is treated as
# a builtin tool even if absent here (the set only powers the "available" view).
BUILTIN_TOOLS = {
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "LSP", "NotebookEdit",
    "WebFetch", "WebSearch", "TaskCreate", "TaskGet", "TaskUpdate", "TaskList",
    "TaskOutput", "TaskStop", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    "SendMessage", "Skill", "Agent", "TeamCreate", "TeamDelete", "EnterWorktree",
    "LeaveWorktree", "Monitor", "PushNotification", "ReportFindings", "VideoGen",
    "ImageGen", "Workflow", "CronCreate", "CronDelete", "CronList",
}

# Meta tools that expose deferred/MCP tools rather than doing work themselves.
META_TOOLS = {"ToolSearch", "DeferExecuteTool", "WaitForMcpServers",
              "ListMcpResources", "ReadMcpResource"}

# Built-in agents (name -> kind). "internal" = background/auxiliary.
BUILTIN_AGENTS = {
    "general-purpose": "active", "Explore": "active", "Plan": "active",
    "statusline-setup": "active", "fork": "active",
    "compact": "internal", "contextSummary": "internal",
    "contentAnalyzer": "internal", "terminalTitleGenerator": "internal",
    "promptSuggestion": "internal", "summaryGenerator": "internal",
    "promptHookEvaluator": "internal", "insightsAnalyzer": "internal",
    "memorySelector": "internal", "agentInstructions": "internal",
    "autoModeClassifier": "internal", "securityReviewer": "internal",
}

CMD_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
BAGGAGE_SID_RE = re.compile(r"codebuddy\.session_id=([^,\s]+)")

# Transcript record types that represent one model response. All carry a
# ``providerData.messageId`` that is globally unique *when the record also
# carries ``rawUsage``* — that uniqueness is what we dedup on.
MODEL_RESPONSE_TYPES = ("function_call", "message", "model-usage")

# Token fields read from ``providerData.rawUsage``, in persistence order.
# Deliberately excluded: ``credit`` (billing, not tokens), ``total_tokens``
# (derivable), and everything derived from ``turn-metrics.tokenDelta``.
USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _session_id(rec) -> str | None:
    """Session id from the record, falling back to _meta.baggage.

    ``turn-metrics`` and other auxiliary records omit ``sessionId`` but carry it
    in the W3C baggage string instead.
    """
    sid = rec.get("sessionId")
    if sid:
        return sid
    meta = rec.get("_meta") or {}
    m = BAGGAGE_SID_RE.search(meta.get("baggage") or "")
    return m.group(1) if m else None


# --- inventory (what is installed / available) -----------------------------


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def scan_inventory(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """Populate the inventory table; return skill/agent/command -> plugin maps."""
    root = db.CODEBUDDY_DIR
    rows: list[tuple] = []
    skill_owner: dict[str, str] = {}
    agent_owner: dict[str, str] = {}
    command_owner: dict[str, str] = {}

    # builtin tools + agents. owner_plugin is part of the primary key, so use an
    # empty string (not NULL) for "no owner" — SQLite treats NULLs as distinct
    # in a unique index, which would let duplicates accumulate across runs.
    rows += [("tool", n, "", None, None, "builtin") for n in sorted(BUILTIN_TOOLS)]
    rows += [("agent", n, "", None, None, "builtin") for n in sorted(BUILTIN_AGENTS)]

    # MCP servers
    mcp = _load_json(root / "mcp.json") or {}
    for server in (mcp.get("mcpServers") or {}):
        rows.append(("mcp", server, "", None, str(root / "mcp.json"), "user"))

    # user skills
    user_skills = root / "skills"
    if user_skills.is_dir():
        for d in sorted(user_skills.iterdir()):
            if d.is_dir():
                rows.append(("skill", d.name, "", None, str(d), "user"))

    # plugins (installed_plugins.json is the authoritative list)
    installed = _load_json(root / "plugins" / "installed_plugins.json") or {}
    for key, installs in (installed.get("plugins") or {}).items():
        name, _, marketplace = key.partition("@")
        for inst in installs:
            ipath = Path(inst.get("installPath", ""))
            version = inst.get("version")
            rows.append(("plugin", name, name, version, str(ipath), "plugin"))

            for sub, kind, table in (
                ("skills", "skill", skill_owner),
                ("agents", "agent", agent_owner),
                ("commands", "command", command_owner),
            ):
                sdir = ipath / sub
                if not sdir.is_dir():
                    continue
                for entry in sorted(sdir.iterdir()):
                    if entry.name.startswith("."):
                        continue
                    item = entry.stem if entry.is_file() else entry.name
                    rows.append((kind, item, name, version, str(entry), "plugin"))
                    if kind == "skill":
                        table[item] = name
                    elif kind == "agent":
                        table[item] = name
                    else:
                        table[item] = name

    conn.executemany(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version, path, source)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return skill_owner, agent_owner, command_owner


# --- transcript indexing ---------------------------------------------------


def _mcp_from_name(name: str):
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[1], "__".join(parts[2:])
    return None


def _classify(name: str, args: dict):
    """Return (category, mcp_pair). mcp_pair is (server, tool) or None."""
    if name == "Skill":
        return "skill", None
    if name == "Agent":
        return "agent", None
    pair = _mcp_from_name(name)
    if pair:
        return "mcp", pair
    if name in META_TOOLS:
        # DeferExecuteTool wraps the real (often MCP) tool.
        inner = args.get("toolName") or args.get("tool_name")
        if isinstance(inner, str):
            pair = _mcp_from_name(inner)
            if pair:
                return "mcp", pair
        return "meta", None
    return "builtin", None


def _args_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


def index_file(conn: sqlite3.Connection, path: Path, offset: int,
               skill_owner: dict, agent_owner: dict, command_owner: dict) -> int:
    """Index appended bytes of one transcript. Returns the new offset."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return offset

    # Only process complete lines; leave a trailing partial line for next time.
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return offset
    complete = chunk[: last_nl + 1]
    new_offset = offset + last_nl + 1

    for raw in complete.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        _handle_record(conn, rec, skill_owner, agent_owner, command_owner)
    return new_offset


def _record_model_response(conn, rec, sid, project, ts) -> None:
    """Persist one model response's token usage, deduped by messageId.

    Source of truth is ``providerData.rawUsage`` only. The same response can be
    re-emitted several times (a usage-bearing record plus usage-less copies that
    share its ``messageId``), so:

    * a record *with* ``rawUsage`` upserts the token columns;
    * a record *without* ``rawUsage`` only inserts a placeholder if the response
      is not known yet (``INSERT OR IGNORE``), never fabricating tokens.

    Missing token fields are stored as NULL and named in ``missing``. ``credit``
    and ``turn-metrics.tokenDelta`` are never read here.
    """
    if rec.get("type") not in MODEL_RESPONSE_TYPES:
        return
    provider = rec.get("providerData") or {}
    message_id = provider.get("messageId")
    if not message_id:
        return                       # no stable response id -> do not guess one
    model = provider.get("model")
    conv = provider.get("conversationRequestId")
    raw = provider.get("rawUsage")

    if isinstance(raw, dict):
        vals = {k: raw.get(k) for k in USAGE_FIELDS}
        missing = [k for k in USAGE_FIELDS if raw.get(k) is None]
        if model is None:
            missing.append("model")
        conn.execute(
            "INSERT INTO model_responses"
            "(message_id, session_id, conversation_request_id, model,"
            " prompt_tokens, completion_tokens,"
            " cache_read_input_tokens, cache_creation_input_tokens,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,'transcript',1,?)"
            " ON CONFLICT(message_id) DO UPDATE SET"
            "   session_id = COALESCE(excluded.session_id, session_id),"
            "   conversation_request_id ="
            "     COALESCE(excluded.conversation_request_id, conversation_request_id),"
            "   model = COALESCE(excluded.model, model),"
            "   prompt_tokens = excluded.prompt_tokens,"
            "   completion_tokens = excluded.completion_tokens,"
            "   cache_read_input_tokens = excluded.cache_read_input_tokens,"
            "   cache_creation_input_tokens = excluded.cache_creation_input_tokens,"
            "   ts = COALESCE(excluded.ts, ts),"
            "   project = COALESCE(excluded.project, project),"
            "   usage_available = 1,"
            "   missing = excluded.missing",
            (message_id, sid, conv, model, vals["prompt_tokens"],
             vals["completion_tokens"], vals["cache_read_input_tokens"],
             vals["cache_creation_input_tokens"], ts, project,
             ",".join(missing) or None),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO model_responses"
            "(message_id, session_id, conversation_request_id, model,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,'transcript',0,'rawUsage')",
            (message_id, sid, conv, model, ts, project),
        )


def _handle_record(conn, rec, skill_owner, agent_owner, command_owner) -> None:
    rtype = rec.get("type")
    sid = _session_id(rec)
    project = rec.get("cwd")
    ts = rec.get("timestamp")

    _record_model_response(conn, rec, sid, project, ts)

    if rtype == "function_call":
        name = rec.get("name") or ""
        args = _args_dict(rec.get("arguments"))
        provider = rec.get("providerData") or {}
        model = provider.get("model")
        call_id = rec.get("callId") or rec.get("id")
        category, pair = _classify(name, args)

        conn.execute(
            "INSERT OR IGNORE INTO tool_calls"
            "(call_id, session_id, project, tool_name, category, ts, status, model)"
            " VALUES(?,?,?,?,?,?,NULL,?)",
            (call_id, sid, project, name, category, ts, model),
        )
        if category == "skill":
            skill = args.get("skill") or args.get("command") or "?"
            conn.execute(
                "INSERT OR IGNORE INTO skill_usage"
                "(call_id, skill, has_args, plugin, session_id, project, ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (call_id, skill, 1 if args.get("args") else 0,
                 skill_owner.get(skill), sid, project, ts),
            )
        elif category == "agent":
            atype = args.get("subagent_type") or args.get("agent_type") or "?"
            conn.execute(
                "INSERT OR IGNORE INTO agent_usage"
                "(call_id, agent_type, description, kind, source, session_id, project, ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (call_id, atype, args.get("description"),
                 BUILTIN_AGENTS.get(atype, "active"), "tool", sid, project, ts),
            )
        elif category == "mcp" and pair:
            conn.execute(
                "INSERT OR IGNORE INTO mcp_usage"
                "(call_id, server, tool, session_id, project, ts) VALUES(?,?,?,?,?,?)",
                (call_id, pair[0], pair[1], sid, project, ts),
            )

    elif rtype == "function_call_result":
        call_id = rec.get("callId") or rec.get("id")
        status = rec.get("status")
        rts = rec.get("timestamp")
        conn.execute(
            "UPDATE tool_calls SET status=?,"
            " duration_ms = CASE WHEN ts IS NOT NULL AND ? IS NOT NULL"
            "   THEN ? - ts ELSE duration_ms END"
            " WHERE call_id=?",
            (status, rts, rts, call_id),
        )
        for table in ("skill_usage", "agent_usage", "mcp_usage"):
            conn.execute(
                f"UPDATE {table} SET status=?,"
                " duration_ms = CASE WHEN ts IS NOT NULL AND ? IS NOT NULL"
                "   THEN ? - ts ELSE duration_ms END"
                " WHERE call_id=?",
                (status, rts, rts, call_id),
            )

    elif rtype in ("session-meta", "ai-title"):
        title = None
        if rtype == "ai-title":
            title = rec.get("aiTitle")
        else:
            meta = rec.get("meta") or {}
            titles = meta.get("codebuddy.ai/sessionTitle") or []
            if titles:
                title = titles[0].get("aiTitle")
        if sid and title:
            conn.execute(
                "INSERT INTO sessions(session_id, project, title, started_at, ended_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   title = COALESCE(excluded.title, title),"
                "   project = COALESCE(project, excluded.project)",
                (sid, project, title, ts, ts),
            )

    elif rtype == "turn-metrics":
        # NOTE: tokenDelta is a *context* metric for the whole turn, not the
        # model's prompt/completion tokens. It is kept in sessions.tokens and is
        # never mixed into model_responses (which reads rawUsage only).
        if sid:
            conn.execute(
                "INSERT INTO sessions"
                "(session_id, project, started_at, ended_at, tokens, duration_ms)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   started_at = MIN(started_at, excluded.started_at),"
                "   ended_at   = MAX(ended_at, excluded.ended_at),"
                "   tokens     = tokens + excluded.tokens,"
                "   duration_ms= duration_ms + excluded.duration_ms",
                (sid, project, ts, ts, rec.get("tokenDelta") or 0,
                 rec.get("durationMs") or 0),
            )

    elif rtype == "message":
        provider = rec.get("providerData") or {}
        model = provider.get("model")
        if sid and model:
            conn.execute(
                "INSERT INTO sessions(session_id, project, model)"
                " VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
                "   model = COALESCE(excluded.model, model)",
                (sid, project, model),
            )
        blob = json.dumps(rec.get("content") or "")
        for m in CMD_RE.finditer(blob):
            cmd = m.group(1)
            conn.execute(
                "INSERT OR IGNORE INTO commands(command, session_id, project, ts)"
                " VALUES(?,?,?,?)",
                (cmd, sid, project, ts),
            )
            owner = command_owner.get(cmd)
            if owner:
                conn.execute(
                    "INSERT OR IGNORE INTO plugin_usage"
                    "(plugin, marketplace, kind, target, session_id, project, ts)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (owner, "codebuddy-plugins-official", "command", cmd, sid, project, ts),
                )

    # Track session start/end times from any record carrying them.
    if sid and ts:
        conn.execute(
            "INSERT INTO sessions(session_id, project, started_at, ended_at)"
            " VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            "   started_at = MIN(started_at, excluded.started_at),"
            "   ended_at   = MAX(ended_at, excluded.ended_at),"
            "   project    = COALESCE(project, excluded.project)",
            (sid, project, ts, ts),
        )


# --- plugin usage backfill -------------------------------------------------


def backfill_plugin_usage(conn: sqlite3.Connection) -> int:
    """Derive plugin_usage rows from skills/agents owned by a plugin."""
    n = 0
    for row in conn.execute(
        "SELECT skill, plugin, session_id, project, ts FROM skill_usage"
        " WHERE plugin IS NOT NULL"
    ):
        conn.execute(
            "INSERT OR IGNORE INTO plugin_usage"
            "(plugin, marketplace, kind, target, session_id, project, ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (row["plugin"], "codebuddy-plugins-official", "skill",
             row["skill"], row["session_id"], row["project"], row["ts"]),
        )
        n += 1
    for row in conn.execute(
        "SELECT agent_type, session_id, project, ts FROM agent_usage"
    ):
        plugin = None
        for inv in conn.execute(
            "SELECT owner_plugin FROM inventory WHERE kind='agent' AND name=?",
            (row["agent_type"],),
        ):
            plugin = inv["owner_plugin"]
        if plugin:
            conn.execute(
                "INSERT OR IGNORE INTO plugin_usage"
                "(plugin, marketplace, kind, target, session_id, project, ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (plugin, "codebuddy-plugins-official", "agent",
                 row["agent_type"], row["session_id"], row["project"], row["ts"]),
            )
            n += 1
    conn.commit()
    return n


# --- driver ----------------------------------------------------------------


def run(full: bool = False, quiet: bool = False, db_path=db.DB_PATH) -> dict:
    conn = db.open_db(db_path)
    db.ensure_schema(conn)
    if full:
        db.reset(conn)

    skill_owner, agent_owner, command_owner = scan_inventory(conn)

    projects = db.CODEBUDDY_DIR / "projects"
    files = sorted(projects.glob("**/*.jsonl")) if projects.is_dir() else []

    n_files = n_records = 0
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        row = conn.execute(
            "SELECT size, offset FROM sync_state WHERE file_path=?", (key,)
        ).fetchone()
        offset = 0
        if row is not None:
            offset = row["offset"] or 0
            if st.st_size < offset:      # truncated / rewritten
                offset = 0
        if row is not None and st.st_size == (row["size"] or 0) and offset:
            continue
        new_offset = index_file(conn, path, offset,
                                skill_owner, agent_owner, command_owner)
        conn.execute(
            "INSERT OR REPLACE INTO sync_state(file_path, size, mtime, offset)"
            " VALUES(?,?,?,?)",
            (key, st.st_size, st.st_mtime, new_offset),
        )
        n_files += 1
        conn.commit()

    n_records = backfill_plugin_usage(conn)
    stats = {
        "files_indexed": n_files,
        "files_total": len(files),
        "plugin_rows": n_records,
        "tool_calls": conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0],
        "skills": conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0],
        "agents": conn.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0],
        "mcp": conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0],
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "commands": conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
        "model_responses": conn.execute(
            "SELECT COUNT(*) FROM model_responses").fetchone()[0],
        "model_responses_with_usage": conn.execute(
            "SELECT COUNT(*) FROM model_responses WHERE usage_available=1"
        ).fetchone()[0],
        "model_prompt_tokens": conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) FROM model_responses"
        ).fetchone()[0],
        "model_completion_tokens": conn.execute(
            "SELECT COALESCE(SUM(completion_tokens),0) FROM model_responses"
        ).fetchone()[0],
    }
    conn.close()

    if not quiet:
        print(f"indexed {stats['files_indexed']}/{stats['files_total']} files")
        print(f"  tool_calls={stats['tool_calls']}  skills={stats['skills']}  "
              f"agents={stats['agents']}  mcp={stats['mcp']}  "
              f"sessions={stats['sessions']}  commands={stats['commands']}")
        print(f"  model_responses={stats['model_responses']} "
              f"(with usage {stats['model_responses_with_usage']})  "
              f"prompt_tokens={stats['model_prompt_tokens']:,}  "
              f"completion_tokens={stats['model_completion_tokens']:,}")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Index CodeBuddy logs into the tracker DB.")
    ap.add_argument("--full", action="store_true", help="wipe and re-index everything")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--db", default=str(db.DB_PATH))
    args = ap.parse_args(argv)
    t0 = time.time()
    run(full=args.full, quiet=args.quiet, db_path=args.db)
    if not args.quiet:
        print(f"done in {time.time() - t0:.1f}s -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# --- CodeBuddy format registry ---------------------------------------------
#
# Everything cbut assumes about CodeBuddy's data: on-disk layout, record shape,
# field names, and the tool names that drive classification. This is the block
# to re-verify when CodeBuddy ships a new version; AGENTS.md §Format coupling
# points here.
#
# Two kinds of entry, on purpose:
#   * Values reached through a call (paths, globs, separators, tool names) are
#     named constants used at their sites, so a rename is one edit and a
#     half-applied rename cannot hide.
#   * Record *field names* stay inline in the handlers — a parser is easier to
#     trust when the field it grabs is visible — and are mirrored in the
#     ``*_FIELDS`` sets below.
# ``scripts/tests/test_format_registry.py`` locks both directions: no field may
# be read without being registered, no registered field may go unread, and no
# bare literal may be passed to a layout method. The registry therefore cannot
# turn into a description of the parser that stopped matching it.
#
# Why: a renamed field or record type raises nothing. The panels go empty,
# `cbut sync` prints "done", CI stays green. The unparsed counter in
# ``_handle_record`` is the runtime signal; this registry is the surface it is
# measured against.

# On-disk layout, all relative to ~/.codebuddy (``db.CODEBUDDY_DIR``).
DIR_PROJECTS = "projects"
DIR_SKILLS = "skills"
DIR_AGENTS = "agents"
DIR_PLUGINS = "plugins"
TRANSCRIPT_GLOB = "**/*.jsonl"
# A skill is the *parent of a manifest*: skills/<category>/<skill>/SKILL.md.
# Treating one directory level as the skill listed the categories ("backend",
# "frontend") as skills and hid every real one.
SKILL_MANIFEST_GLOB = "**/SKILL.md"
AGENT_SPEC_GLOB = "*.md"                 # agents/<name>.md
MCP_FILE = "mcp.json"
PLUGIN_INDEX_FILE = "installed_plugins.json"
PLUGIN_MARKETPLACE_SEP = "@"             # index keys are "<name>@<marketplace>"
INVENTORY_HIDDEN_PREFIX = "."            # dotfiles are never an installable item
# (subdirectory, kind) inside a plugin install. The names coincide with the
# user-level directories above because they hold the same things.
PLUGIN_SUBDIRS = ((DIR_SKILLS, "skill"), (DIR_AGENTS, "agent"),
                  ("commands", "command"))

# A transcript is newline-delimited JSON. The same separator decides where an
# incremental offset is allowed to resume: one byte mid-line is a rewritten file.
TRANSCRIPT_LINE_SEP = b"\n"

# Tool names that carry meaning for classification (see ``_classify``).
SKILL_TOOL = "Skill"
AGENT_TOOL = "Agent"
MCP_TOOL_PREFIX = "mcp__"                # mcp__<server>__<tool>
MCP_TOOL_SEP = "__"

CMD_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
BAGGAGE_SID_RE = re.compile(r"codebuddy\.session_id=([^,\s]+)")

# ``type`` values a handler claims in ``_handle_record``. The test derives the
# same set from the dispatch chain, so a branch without a registration — or a
# registration without a branch — fails.
HANDLED_RECORD_TYPES = frozenset({
    "function_call", "function_call_result", "message", "model-usage",
    "turn-metrics", "session-meta", "ai-title",
})
# Types present in real transcripts that claim no handler, measured on this
# machine 2026-10-09: ``reasoning`` 14757, ``file-history-snapshot`` 5786,
# ``summary`` 346. They stay inside the unparsed count deliberately — an
# allowlist quieting them would also hide the day one of them starts carrying
# something we index. Anything new appears beside them in `cbut health`.

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

# The Agent tool's own contract: when the caller omits ``subagent_type`` the
# call runs as ``general-purpose``. A call carrying neither ``subagent_type``
# nor ``agent_type`` is therefore recorded under this name, never a "?" — the
# placeholder used to fragment real general-purpose usage into an unknown row.
DEFAULT_AGENT_TYPE = "general-purpose"

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

# Transcript record types that represent one model response. All carry a
# ``providerData.messageId`` that is globally unique *when the record also
# carries ``rawUsage``* — that uniqueness is what we dedup on.
MODEL_RESPONSE_TYPES = ("function_call", "message", "model-usage")

# Token fields read from ``providerData.rawUsage``, in persistence order.
# Deliberately excluded: ``credit`` (billing, not tokens) and everything derived
# from ``turn-metrics.tokenDelta``. ``total_tokens`` is now persisted separately
# as ``provider_total_tokens`` (the provider's own total, verbatim) but is kept
# out of this tuple so the legacy ``missing`` diagnostic keeps its meaning.
USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Newer rawUsage cache fields (schema v3). In real transcripts the cache numbers
# live here, while ``cache_read/creation_input_tokens`` above are usually 0.
# Read as-is: absent -> NULL, explicit 0 -> 0. Kept separate from USAGE_FIELDS
# so the legacy ``missing`` diagnostic keeps its original meaning.
CACHE_USAGE_FIELDS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_write_tokens",
)

# --- field mirrors: the record shape we read, by object --------------------
# Read dynamically in ``_record_model_response`` (``raw.get(k)``), so they never
# appear as a literal ``.get()`` key; each must still land in a column.
RAW_USAGE_FIELDS = frozenset(USAGE_FIELDS) | frozenset(CACHE_USAGE_FIELDS) | {"total_tokens"}

RECORD_FIELDS = frozenset({
    "type", "sessionId", "cwd", "timestamp", "name", "arguments",
    "callId", "id", "status", "content", "providerData",
    "tokenDelta", "durationMs", "_meta",
})
PROVIDER_FIELDS = frozenset({
    "messageId", "model", "conversationRequestId", "rawUsage",
})
BAGGAGE_FIELDS = frozenset({"baggage"})
CONTENT_BLOCK_FIELDS = frozenset({"text"})
# Block *types* observed in real transcripts (input_text 134, output_text 2,
# measured 2026-10-09) are deliberately NOT among the fields we read: see
# ``_text_blocks`` for what filtering on them cost.
# Tool *argument names*. Reading a name is metadata; reading its value is the
# privacy line, and only these two are ever stored: the skill name and the agent
# type, both of which are catalog entries rather than prose.
TOOL_ARG_FIELDS = frozenset({
    "skill", "command", "args",               # Skill tool
    "subagent_type", "agent_type",            # Agent tool
    "toolName", "tool_name",                  # meta tools that wrap another tool
})
# JSON keys of the inventory files, not of a transcript.
INVENTORY_JSON_FIELDS = frozenset({"mcpServers", "plugins", "installPath", "version"})

CODEBUDDY_FIELDS = (RECORD_FIELDS | PROVIDER_FIELDS | BAGGAGE_FIELDS
                    | CONTENT_BLOCK_FIELDS | TOOL_ARG_FIELDS
                    | INVENTORY_JSON_FIELDS | RAW_USAGE_FIELDS)

# cbut's own vocabulary in the ``unparsed`` table — NOT CodeBuddy fields.
# ``cbut health`` and the TUI status line print these verbatim: "type:<name>"
# for a record no branch claimed, UNPARSEABLE_LINE for a complete line that is
# not JSON, and "unreadable_file:<OSError>" for a file that could not be opened.
UNPARSEABLE_LINE = "unparseable_line"


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
    owner_maps = {"skill": skill_owner, "agent": agent_owner,
                  "command": command_owner}

    # builtin tools + agents. owner_plugin is part of the primary key, so use an
    # empty string (not NULL) for "no owner" — SQLite treats NULLs as distinct
    # in a unique index, which would let duplicates accumulate across runs.
    rows += [("tool", n, "", None, None, "builtin") for n in sorted(BUILTIN_TOOLS)]
    rows += [("agent", n, "", None, None, "builtin") for n in sorted(BUILTIN_AGENTS)]

    # MCP servers
    mcp_path = root / MCP_FILE
    mcp = _load_json(mcp_path) or {}
    for server in (mcp.get("mcpServers") or {}):
        rows.append(("mcp", server, "", None, str(mcp_path), "user"))

    # User skills. The layout on disk is skills/<category>/<skill>/SKILL.md, so a
    # skill is the *parent of a manifest* — the directories directly under
    # skills/ are categories, and listing them as skills put names like "backend"
    # and "frontend" in the Skills panel while the real skills were missing.
    # The scan also covers a skill that sits one level down with no category.
    user_skills = root / DIR_SKILLS
    if user_skills.is_dir():
        for manifest in sorted(user_skills.glob(SKILL_MANIFEST_GLOB)):
            rows.append(("skill", manifest.parent.name, "", None,
                         str(manifest.parent), "user"))

    # User agents: ~/.codebuddy/agents/<name>.md. These were never scanned, so
    # an installed-but-unused agent could not appear in the Agents panel at all.
    user_agents = root / DIR_AGENTS
    if user_agents.is_dir():
        for spec in sorted(user_agents.glob(AGENT_SPEC_GLOB)):
            rows.append(("agent", spec.stem, "", None, str(spec), "user"))

    # plugins (installed_plugins.json is the authoritative list)
    installed = _load_json(root / DIR_PLUGINS / PLUGIN_INDEX_FILE) or {}
    for key, installs in (installed.get("plugins") or {}).items():
        # The key is "<name>@<marketplace>". The marketplace is real but the
        # schema has nowhere to keep it, so plugin_usage names the only one that
        # exists on this machine (verified: all installed plugins resolve to
        # "codebuddy-plugins-official"). A second marketplace would attribute
        # rows to the wrong one; the fix would be a column on inventory, which is
        # not worth adding to store a single observed value.
        name, _, _marketplace = key.partition(PLUGIN_MARKETPLACE_SEP)
        for inst in installs:
            ipath = Path(inst.get("installPath", ""))
            version = inst.get("version")
            rows.append(("plugin", name, name, version, str(ipath), "plugin"))

            for sub, kind in PLUGIN_SUBDIRS:
                sdir = ipath / sub
                if not sdir.is_dir():
                    continue
                for entry in sorted(sdir.iterdir()):
                    if entry.name.startswith(INVENTORY_HIDDEN_PREFIX):
                        continue
                    item = entry.stem if entry.is_file() else entry.name
                    rows.append((kind, item, name, version, str(entry), "plugin"))
                    owner_maps[kind][item] = name

    conn.executemany(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version, path, source)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return skill_owner, agent_owner, command_owner


# --- transcript indexing ---------------------------------------------------


def _mcp_from_name(name: str):
    """Parse ``mcp__<server>__<tool>`` into ``(server, tool)``, else ``None``.

    A well-formed MCP tool name has at least three non-empty ``__``-separated
    parts: the literal ``mcp`` prefix, the server, and the tool. A name with
    fewer parts, an empty server/tool, or a trailing ``__`` is *not* an MCP
    name — return ``None`` rather than guessing a split.
    """
    if not name.startswith(MCP_TOOL_PREFIX) or name.endswith(MCP_TOOL_SEP):
        return None
    parts = name.split(MCP_TOOL_SEP)
    if len(parts) < 3:
        return None
    server, tool = parts[1], MCP_TOOL_SEP.join(parts[2:])
    if not server or not tool:
        return None
    return server, tool


def _classify(name: str, args: dict):
    """Return (category, mcp_pair). mcp_pair is (server, tool) or None."""
    if name == SKILL_TOOL:
        return "skill", None
    if name == AGENT_TOOL:
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


def _plugin_owner(name: str, command_owner: dict, skill_owner: dict,
                  agent_owner: dict):
    """Resolve a slash command / skill / agent name to its owning plugin.

    Evidence-based: a plugin is attributed only when ``inventory`` maps the
    name to one (``scan_inventory`` returns these maps). Commands win over
    skills over agents when a name is registered as more than one kind.
    Returns ``(plugin, kind)`` or ``(None, None)``.
    """
    if name in command_owner:
        return command_owner[name], "command"
    if name in skill_owner:
        return skill_owner[name], "skill"
    if name in agent_owner:
        return agent_owner[name], "agent"
    return None, None


def _bump(counts: dict, key: str, n: int = 1) -> None:
    """Accumulate one of cbut's *own* diagnostic counters.

    Takes the reason as a value so no CodeBuddy field name can be confused with
    a reason key: the two vocabularies never meet in a literal ``.get()``.
    """
    counts[key] = counts.get(key, 0) + n


def index_file(conn: sqlite3.Connection, path: Path, offset: int,
               skill_owner: dict, agent_owner: dict, command_owner: dict,
               unparsed=None) -> int:
    """Index appended bytes of one transcript. Returns the new offset.

    Only *fully-consumed* bytes are reported back to the caller, so the tail
    of a partially-written line is retried next run instead of being skipped
    forever. A final line that lacks a trailing newline but already parses as
    complete JSON is consumed immediately.

    This never touches ``sessions``: one session spans its main transcript
    *plus* every subagent transcript, so a single file's contribution to the
    accumulated ``turn-metrics`` totals cannot be cleared in isolation. A stale
    offset is handled by a whole-DB rebuild in :func:`run` instead.
    """
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return offset

    # Everything up to and including the last newline is complete lines; the
    # remainder is only consumed if it is a complete JSON record on its own.
    last_nl = chunk.rfind(TRANSCRIPT_LINE_SEP)
    if last_nl == -1:
        complete = b""
        tail = chunk
    else:
        complete = chunk[: last_nl + 1]
        tail = chunk[last_nl + 1:]

    recs = []
    bad_lines = 0
    for raw in complete.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            recs.append(json.loads(raw))
        except ValueError:
            # A *complete* line that is not JSON. The tail of a half-written
            # line is not counted here — it stays unconsumed and is retried.
            bad_lines += 1
            continue
    if bad_lines and unparsed is not None:
        _bump(unparsed, UNPARSEABLE_LINE, bad_lines)
    consumed = offset + len(complete)
    if tail.strip():
        try:
            recs.append(json.loads(tail))
            consumed += len(tail)          # the final line was complete
        except ValueError:
            pass                           # partial line -> retry next run

    for rec in recs:
        _handle_record(conn, rec, skill_owner, agent_owner, command_owner,
                       unparsed)
    return consumed


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
        cache = {k: raw.get(k) for k in CACHE_USAGE_FIELDS}
        missing = [k for k in USAGE_FIELDS if raw.get(k) is None]
        if model is None:
            missing.append("model")
        conn.execute(
            "INSERT INTO model_responses"
            "(message_id, session_id, conversation_request_id, model,"
            " prompt_tokens, completion_tokens,"
            " cache_read_input_tokens, cache_creation_input_tokens,"
            " prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
            " prompt_cache_write_tokens, provider_total_tokens,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'transcript',1,?)"
            " ON CONFLICT(message_id) DO UPDATE SET"
            "   session_id = COALESCE(excluded.session_id, session_id),"
            "   conversation_request_id ="
            "     COALESCE(excluded.conversation_request_id, conversation_request_id),"
            "   model = COALESCE(excluded.model, model),"
            "   prompt_tokens = excluded.prompt_tokens,"
            "   completion_tokens = excluded.completion_tokens,"
            "   cache_read_input_tokens = excluded.cache_read_input_tokens,"
            "   cache_creation_input_tokens = excluded.cache_creation_input_tokens,"
            "   prompt_cache_hit_tokens = excluded.prompt_cache_hit_tokens,"
            "   prompt_cache_miss_tokens = excluded.prompt_cache_miss_tokens,"
            "   prompt_cache_write_tokens = excluded.prompt_cache_write_tokens,"
            "   provider_total_tokens = excluded.provider_total_tokens,"
            "   ts = COALESCE(excluded.ts, ts),"
            "   project = COALESCE(excluded.project, project),"
            "   usage_available = 1,"
            "   missing = excluded.missing",
            (message_id, sid, conv, model, vals["prompt_tokens"],
             vals["completion_tokens"], vals["cache_read_input_tokens"],
             vals["cache_creation_input_tokens"],
             cache["prompt_cache_hit_tokens"],
             cache["prompt_cache_miss_tokens"],
             cache["prompt_cache_write_tokens"],
             raw.get("total_tokens"),          # provider's own total, verbatim
             ts, project, ",".join(missing) or None),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO model_responses"
            "(message_id, session_id, conversation_request_id, model,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,'transcript',0,'rawUsage')",
            (message_id, sid, conv, model, ts, project),
        )


def _text_blocks(content) -> list[str]:
    """Every text payload carried by a message's ``content``, block by block.

    Deliberately does *not* filter on the block ``type``. Measured on this
    machine 2026-10-09, command markers live in ``input_text`` (134 blocks) and
    ``output_text`` (2) — an earlier version of this helper required
    ``type == "text"``, matched nothing, and emptied the Commands panel while
    every test stayed green. The type is not needed here: the marker is
    self-delimiting, so this stays correct if upstream renames the block types
    again. Only the name inside the marker reaches the database.
    """
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                out.append(text)
    return out


def _record_commands(conn, content, sid, project, ts,
                     command_owner, skill_owner, agent_owner) -> None:
    """Index the slash commands a message announced, without storing the message.

    A command appears inside a text block as ``<command-name>/name</command-name>``.
    The old code serialised the whole content list with ``json.dumps`` and regexed
    that string, which meant holding the prose; walking the blocks gives the same
    result while the only value that reaches the database is the captured name.
    """
    for text in _text_blocks(content):
        for m in CMD_RE.finditer(text):
            cmd = m.group(1)
            conn.execute(
                "INSERT OR IGNORE INTO commands(command, session_id, project, ts)"
                " VALUES(?,?,?,?)",
                (cmd, sid, project, ts),
            )
            # A slash command can name a plugin command *or* a plugin-owned
            # skill/agent (e.g. /playwright-cli). Attribute it to the plugin
            # only when inventory maps the name; never guess.
            owner, kind = _plugin_owner(
                cmd, command_owner, skill_owner, agent_owner)
            if owner:
                conn.execute(
                    "INSERT OR IGNORE INTO plugin_usage"
                    "(plugin, marketplace, kind, target, session_id, project, ts)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (owner, "codebuddy-plugins-official", kind, cmd,
                     sid, project, ts),
                )


def _handle_record(conn, rec, skill_owner, agent_owner, command_owner,
                   unparsed=None) -> None:
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
            atype = (args.get("subagent_type") or args.get("agent_type")
                     or DEFAULT_AGENT_TYPE)
            # ``description`` is prose the model wrote for itself, so it is not
            # stored — schema v5 dropped the column that used to hold it. Only
            # the agent *type* is metadata.
            conn.execute(
                "INSERT OR IGNORE INTO agent_usage"
                "(call_id, agent_type, kind, source, session_id, project, ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (call_id, atype, BUILTIN_AGENTS.get(atype, "active"),
                 "tool", sid, project, ts),
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
        # Known records whose payload is an AI-generated session title — prose
        # derived from the conversation, so it is deliberately not stored
        # (schema v5 dropped the column that held it). The session row these
        # used to create is made by the shared start/end block below. Claimed
        # here on purpose: dropping them into the `else` would pad the unparsed
        # count with a type we understand perfectly well, and the number would
        # stop meaning "the format changed".
        pass

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
        _record_commands(conn, rec.get("content"), sid, project, ts,
                         command_owner, skill_owner, agent_owner)

    elif rtype == "model-usage":
        # A usage-only echo of a model response. Its tokens are already stored by
        # _record_model_response above, so there is nothing left to do here — but
        # the type must be *claimed*, or the unparsed count reports known rows as
        # if the format had changed. A number that cries wolf is not a signal.
        pass

    else:
        # No handler claimed this record. Counting it is the only defence against
        # a CodeBuddy format change silently zeroing every panel: the run still
        # prints "done" either way, so an unrecognised ``type`` has to become a
        # number someone can see (``cbut health``, the sync summary).
        if unparsed is not None:
            key = f"type:{rtype}" if rtype else "type:<missing>"
            _bump(unparsed, key)

    # Track session start/end times from any record carrying them — but only for
    # a record that says *where* it ran. Metadata-only records (a session-meta, a
    # summary) carry a sessionId and a timestamp and no cwd, and creating a row
    # for each produced 308 sessions with a NULL project and no tool calls and no
    # model responses: invisible in every panel, but counted as sessions. A real
    # session always has at least one cwd-bearing record.
    if sid and ts and project:
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


# --- documentation ---------------------------------------------------------


def format_doc() -> str:
    """Render the format registry as markdown (``cbut format``).

    Every value is read back out of the constants rather than retyped, so the
    checked-in document cannot describe a surface the code no longer reads.
    ``scripts/tests/test_maintenance_docs.py`` holds the file to this output.
    """
    def row(title, values):
        items = ", ".join(f"`{v}`" for v in sorted(values))
        return f"- **{title}**: {items}\n"

    def pairs(title, mapping):
        body = "".join(f"  - `{k}` → `{v}`\n" for k, v in mapping)
        return f"- **{title}**:\n{body}"

    out = [
        row("Record types a handler claims", HANDLED_RECORD_TYPES),
        row("Record types treated as a model response", MODEL_RESPONSE_TYPES),
        row("Transcript record fields", RECORD_FIELDS),
        row("`providerData` fields", PROVIDER_FIELDS),
        row("`providerData.rawUsage` fields", RAW_USAGE_FIELDS),
        row("`_meta` fields", BAGGAGE_FIELDS),
        row("message content-block fields", CONTENT_BLOCK_FIELDS),
        row("tool argument names", TOOL_ARG_FIELDS),
        row("inventory JSON keys", INVENTORY_JSON_FIELDS),
        "- **Tool names that drive classification**: "
        f"`{SKILL_TOOL}`, `{AGENT_TOOL}`, prefix `{MCP_TOOL_PREFIX}` "
        f"with separator `{MCP_TOOL_SEP}`\n",
        row("Meta tools (wrap another tool)", META_TOOLS),
        f"- **Tolerant name tables** (an unknown name still indexes): "
        f"{len(BUILTIN_TOOLS)} builtin tools, {len(BUILTIN_AGENTS)} builtin agents, "
        f"default agent type `{DEFAULT_AGENT_TYPE}`\n",
        "- **Markers parsed out of text**: "
        f"`{CMD_RE.pattern}` · `{BAGGAGE_SID_RE.pattern}`\n",
        "- **On-disk layout, relative to `~/.codebuddy`**:\n"
        f"  - `{DIR_PROJECTS}` / `{TRANSCRIPT_GLOB}` — transcripts, the only "
        "ingested log tree\n"
        f"  - `{DIR_SKILLS}` / `{SKILL_MANIFEST_GLOB}` — a skill is the parent "
        "of a manifest\n"
        f"  - `{DIR_AGENTS}` / `{AGENT_SPEC_GLOB}` — user agents\n"
        f"  - `{MCP_FILE}` — MCP servers\n"
        f"  - `{DIR_PLUGINS}` / `{PLUGIN_INDEX_FILE}`, keys split on "
        f"`{PLUGIN_MARKETPLACE_SEP}`\n"
        f"  - a name starting with `{INVENTORY_HIDDEN_PREFIX}` is never an "
        "installed item\n"
        + pairs("plugin subdirectories (dir → kind)", PLUGIN_SUBDIRS) +
        f"- **Transcript framing**: one JSON record per line, separated by a "
        f"newline byte (`{TRANSCRIPT_LINE_SEP!r}`)\n",
    ]
    return "".join(out)


# --- driver ----------------------------------------------------------------


def _starts_on_line_boundary(path: Path, offset: int) -> bool:
    """True when ``offset`` sits just after a newline.

    An append-only transcript always leaves its offset immediately after a
    ``\\n`` (or at 0). A byte other than ``\\n`` there means the prefix was
    rewritten, so the offset can no longer be trusted.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(offset - 1)
            return fh.read(1) == TRANSCRIPT_LINE_SEP
    except OSError:
        return False


def _needs_reset(path: Path, st, row) -> bool:
    """True when a stored offset can no longer be trusted for this file.

    A transcript is append-only: its offset always sits on a line boundary and
    the file only ever grows. A shrink, a same-or-smaller rewrite (mtime
    changed), or an offset that no longer follows a newline means the prefix was
    rewritten and the stored offset is stale. A file that is untouched and fully
    consumed is *not* stale — that early-out matters because a transcript may
    legitimately end without a trailing newline, which would otherwise trip the
    boundary check. An offset of 0 means nothing was consumed yet, so a re-read
    from zero is always safe and is not a reset.
    """
    offset = row["offset"] or 0
    if not offset:
        return False
    old_size = row["size"] or 0
    old_mtime = row["mtime"]
    if (st.st_mtime == old_mtime and st.st_size == old_size
            and offset == st.st_size):
        return False                   # untouched and fully consumed
    return (
        st.st_size < offset
        or (st.st_mtime != old_mtime and st.st_size <= old_size)
        or not _starts_on_line_boundary(path, offset)
    )


def run(full: bool = False, quiet: bool = False, db_path=db.DB_PATH) -> dict:
    conn = db.open_db(db_path)
    try:
        return _run(conn, full=full, quiet=quiet)
    finally:
        # The TUI calls this from a worker thread every 30 seconds, so an
        # exception that skipped the close would leak a read-write connection on
        # every failed poll rather than once per process.
        conn.close()


def _run(conn, full: bool = False, quiet: bool = False) -> dict:
    db.ensure_schema(conn)
    if full:
        # A snapshot of an empty database is clutter, and every test builds one;
        # only state that exists is worth a way back.
        if (conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
                or conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]):
            db.snapshot(conn, reason="pre-full-rebuild")
        db.reset(conn)

    skill_owner, agent_owner, command_owner = scan_inventory(conn)

    projects = db.CODEBUDDY_DIR / DIR_PROJECTS
    files = sorted(projects.glob(TRANSCRIPT_GLOB)) if projects.is_dir() else []

    # Pre-pass: if ANY file's stored offset is stale (shrunk, rewritten, or no
    # longer on a line boundary), rebuild the whole DB. One session spans its
    # main transcript *and* every subagent transcript, so a single file's
    # contribution cannot be subtracted in isolation; clearing sync_state +
    # sessions makes the main loop re-read every file from zero exactly once, so
    # the accumulated turn-metrics totals are rebuilt (never lost or doubled).
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        row = conn.execute(
            "SELECT size, mtime, offset FROM sync_state WHERE file_path=?",
            (str(path),),
        ).fetchone()
        if row is not None and _needs_reset(path, st, row):
            conn.execute("DELETE FROM sync_state")
            conn.execute("DELETE FROM sessions")
            # Every file is re-read from zero below, so the unparsed counts would
            # otherwise accumulate a second copy of itself — the same inflation
            # that ``sessions`` had to be cleared for.
            conn.execute("DELETE FROM unparsed")
            conn.commit()
            break

    n_files = n_records = n_unparsed = 0
    unparsed = {}
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        row = conn.execute(
            "SELECT size, mtime, offset FROM sync_state WHERE file_path=?", (key,)
        ).fetchone()
        offset = 0
        if row is not None:
            offset = row["offset"] or 0
            old_size = row["size"] or 0
            old_mtime = row["mtime"]
            if (offset and st.st_mtime == old_mtime and st.st_size == old_size
                    and offset == st.st_size):
                continue              # untouched and fully consumed
            # Any stale offset was already resolved by the pre-pass above (it
            # cleared sync_state, so no row survives to this point).
        unparsed.clear()
        try:
            new_offset = index_file(conn, path, offset,
                                    skill_owner, agent_owner, command_owner,
                                    unparsed)
        except OSError as exc:
            # One unreadable transcript must not abort the whole index — but it is
            # counted rather than skipped quietly, and no offset is stored, so the
            # next run retries it.
            db.note_unparsed(conn, f"unreadable_file:{type(exc).__name__}", 1)
            n_unparsed += 1
            continue
        for reason, n in unparsed.items():
            db.note_unparsed(conn, reason, n)
            n_unparsed += n
        # `offset` is what was consumed, so a deferred tail is retried. `size` is
        # the file's real size — the value _needs_reset compares against to notice
        # a transcript rewritten or truncated in place. Storing the offset here
        # understated it, so a rewrite that still left the file larger than the
        # offset was never detected. Rows written by older builds hold the offset;
        # for a fully-consumed untouched file the two are equal, so those rows are
        # already right, and each is corrected the next time its file is read.
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
        # Per-run and cumulative: a format change moves both, while an old
        # unaddressed count only shows in the total.
        "unparsed_this_run": n_unparsed,
        "unparsed": conn.execute(
            "SELECT COALESCE(SUM(count),0) FROM unparsed").fetchone()[0],
        "unparsed_kinds": conn.execute(
            "SELECT COUNT(*) FROM unparsed").fetchone()[0],
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
    # Loud on purpose: an unclaimed record is the only signal that CodeBuddy's
    # format moved, and a run that prints only "done" hides it.
    if not quiet and stats["unparsed_this_run"]:
        reasons = ", ".join(
            f"{r['reason']} x{r['count']}" for r in db.q_unparsed(conn)[:3])
        print(f"  UNPARSED {stats['unparsed_this_run']} records this run "
              f"({stats['unparsed']} total, {reasons}) — cbut health lists them")

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

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
import os
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
# The manifest that makes a directory a skill. It is joined onto each entry
# rather than globbed: ``**/SKILL.md`` does not descend into a *symlinked*
# directory, and ~/.codebuddy/skills is symlinks, so a recursive glob saw none
# of the installed skills while the panels kept listing names from a layout
# that no longer exists. A skill is a direct child of skills/ that holds one.
SKILL_MANIFEST = "SKILL.md"
SKILL_MANIFEST_GLOB = "**/SKILL.md"      # kept: the retired layout is still read (see _skill_dirs)
AGENT_SPEC_GLOB = "*.md"                 # agents/<name>.md
MCP_FILE = "mcp.json"
MODELS_FILE = "models.json"              # custom provider definitions (url per model id)
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

# The builtin catalogue, read from the installed CodeBuddy package rather than
# retyped here. NOT under ~/.codebuddy — it ships inside the npm package, at
# <package-root>/product.json, which is found by resolving the ``codebuddy``
# executable on PATH upwards (see ``resolve_product_json``). The JSON keys below
# are the only thing about that file this parser assumes.
PRODUCT_JSON = "product.json"
PRODUCT_TOOLS_FIELD = "tools"
PRODUCT_AGENTS_FIELD = "agents"
PRODUCT_NAME_FIELD = "name"
# The executable on PATH that leads to the package root.
PRODUCT_CLI_NAME = "codebuddy"

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
# machine 2026-10-09: ``reasoning`` by the tens of thousands,
# ``file-history-snapshot`` by the thousands, ``summary`` by the hundreds —
# `cbut health` prints the exact figures for whoever owns this database.
# They stay inside the unparsed count deliberately — an
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

# One cache number, two spellings. A built-in route writes
# ``rawUsage.prompt_cache_hit_tokens`` *and* repeats the identical value nested at
# ``rawUsage.prompt_tokens_details.cached_tokens``; a ``custom-local`` endpoint sends
# only the nested one. Measured on real transcripts 2026-10-11: built-in 19,358
# records, both spellings summing to exactly 2,614,796,037 tokens each; custom 712
# records, top-level absent, nested 67,664,640.
#
# So the nested copy is a **fallback, never a second source** — adding the two would
# double every built-in cache number, which is the kind of inflation this record has
# already been promised against. Reading only the top level is what left 588 custom
# rows NULL while their number existed the whole time.
NESTED_CACHE_HIT = ("prompt_tokens_details", "cached_tokens")
NESTED_USAGE_FIELDS = frozenset(NESTED_CACHE_HIT)

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
    # The route identity: a bare model id for a built-in route, or
    # "<endpoint>:<model id>" for a custom one. Drives model_responses.provider.
    "requestModelId",
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
# JSON keys of the non-transcript files we read: the inventory files plus
# models.json (whose "models"[].id/url give the custom routes their host).
INVENTORY_JSON_FIELDS = frozenset({
    "mcpServers", "plugins", "installPath", "version", "models", "url",
    # product.json, the builtin catalogue. Read by name, so the field has to be
    # registered or the registry stops describing the parser.
    PRODUCT_TOOLS_FIELD, PRODUCT_AGENTS_FIELD, PRODUCT_NAME_FIELD,
})

CODEBUDDY_FIELDS = (RECORD_FIELDS | PROVIDER_FIELDS | BAGGAGE_FIELDS
                    | CONTENT_BLOCK_FIELDS | TOOL_ARG_FIELDS
                    | INVENTORY_JSON_FIELDS | RAW_USAGE_FIELDS
                    | NESTED_USAGE_FIELDS)

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


# --- builtin catalogue (product.json) ---------------------------------------


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def resolve_product_json() -> Path | None:
    """Locate the installed CodeBuddy package's ``product.json``, or ``None``.

    ``CBUT_PRODUCT_JSON`` wins; otherwise PATH is scanned for the ``codebuddy``
    executable and the package root is found by walking up from the resolved
    target. Pure ``os``/``pathlib`` on purpose — shelling out to
    ``readlink $(which codebuddy)`` would break the invariant that no production
    script spawns a process (``test_privacy``).
    """
    override = os.environ.get("CBUT_PRODUCT_JSON")
    if override:
        pinned = Path(override).expanduser()
        # An override that names nothing is an override that locates nothing:
        # returning the path anyway would report a file nobody can read as the
        # catalogue in force, which is the one claim health exists to refute.
        return pinned if pinned.is_file() else None
    path_env = os.environ.get("PATH")
    if not path_env:
        return None
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        try:
            candidate = Path(entry) / PRODUCT_CLI_NAME
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            # Symlinks into node_modules/.bin are what npm actually installs;
            # resolve() is what makes the package root reachable by walking up.
            for parent in candidate.resolve().parents:
                found = parent / PRODUCT_JSON
                if found.is_file():
                    return found
        except OSError:
            continue
    return None


def builtin_catalog() -> tuple[frozenset, dict, Path | None]:
    """Return ``(tool names, agent name -> kind, product.json path or None)``.

    The installed package decides **membership** — which names exist at all —
    because that is what moves with the install: product.json carries ``cli``
    and not ``fork``, so a name the package dropped stops being inventoried
    instead of outliving it as a permanent 0-call row. Membership it does not
    carry is ``kind``: active-vs-internal is this tool's taxonomy, and
    BUILTIN_AGENTS supplies it, defaulting to "active".

    The checked-in fallback constants exist so a machine without a resolvable
    package still gets a catalogue instead of empty panels. ``cbut health``
    prints which of the two was used, so a stale fallback cannot quietly
    masquerade as the real list.
    """
    path = resolve_product_json()
    if path is not None:
        raw = _load_json(path)
        if isinstance(raw, dict):
            tools = frozenset(_catalog_names(raw.get(PRODUCT_TOOLS_FIELD)))
            names = _catalog_names(raw.get(PRODUCT_AGENTS_FIELD))
            if tools and names:
                agents = {n: BUILTIN_AGENTS.get(n, "active") for n in names}
                return tools, agents, path
    return frozenset(BUILTIN_TOOLS), dict(BUILTIN_AGENTS), None


def _catalog_names(entries) -> list[str]:
    """Names out of a product.json array, whatever shape the entries have."""
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get(PRODUCT_NAME_FIELD), str):
            names.append(entry[PRODUCT_NAME_FIELD])
    return names


# --- inventory (what is installed / available) -----------------------------


def _skill_dirs(base: Path):
    """Yield every directory under ``base`` that holds a manifest.

    Two layouts are real on this machine and both are still read:

    ``skills/<skill>/SKILL.md``
        The entries are symlinks. The manifest is found by *joining* onto the
        entry, which crosses the link.
    ``skills/<category>/<skill>/SKILL.md``
        The retired layout, reached by descending into a real directory.

    Recursion is deliberately blind to symlinks: ``Path.glob("**/…")`` does not
    descend into a symlinked directory at all, which is how a glob for
    ``**/SKILL.md`` found none of the 27 symlinked skills while the panels went
    on listing names from a layout that had been gone for weeks.
    """
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(INVENTORY_HIDDEN_PREFIX):
            continue
        try:
            if (entry / SKILL_MANIFEST).is_file():
                yield entry
            elif entry.is_dir() and not entry.is_symlink():
                yield from _skill_dirs(entry)
        except OSError:
            continue


def collect_inventory() -> tuple[list[tuple], dict, Path | None]:
    """Read every installed entity off disk.

    Returns ``(rows, owner maps, product.json path)`` and writes nothing, so a
    scan that dies half way through cannot leave a half-replaced table behind.
    """
    root = db.CODEBUDDY_DIR
    rows: list[tuple] = []
    skill_owner: dict[str, str] = {}
    agent_owner: dict[str, str] = {}
    command_owner: dict[str, str] = {}
    owner_maps = {"skill": skill_owner, "agent": agent_owner,
                  "command": command_owner}

    # builtin tools + agents, from the installed package when it can be found.
    # owner_plugin is part of the primary key, so use an empty string (not NULL)
    # for "no owner" — SQLite treats NULLs as distinct in a unique index, which
    # would let duplicates accumulate across runs.
    tools, agents, product_path = builtin_catalog()
    rows += [("tool", n, "", None, None, "builtin") for n in sorted(tools)]
    rows += [("agent", n, "", None, None, "builtin") for n in sorted(agents)]

    # MCP servers
    mcp_path = root / MCP_FILE
    mcp = _load_json(mcp_path) or {}
    for server in (mcp.get("mcpServers") or {}):
        rows.append(("mcp", server, "", None, str(mcp_path), "user"))

    # User skills. Dot-prefixed entries are skipped here as well as inside a
    # plugin: ".skill" was inventoried as a skill because the hidden-prefix rule
    # only ran on the plugin side.
    user_skills = root / DIR_SKILLS
    if user_skills.is_dir():
        for skill in _skill_dirs(user_skills):
            rows.append(("skill", skill.name, "", None, str(skill), "user"))

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

            # A plugin can *be* the skill: its manifest sits at the install root
            # instead of under skills/ (verified: docx, pptx and xlsx ship
            # <installPath>/SKILL.md and nothing else CodeBuddy can inventory).
            # Scanned as subdirectories only, those three were never inventoried,
            # so their usage history came back "removed" while the owning plugin
            # read "installed" — and skill_usage got no plugin to attribute to,
            # which is why the Plugins panel did not count their uses either.
            # The name is the plugin's own, not the directory's: installPath ends
            # in the version ("unknown", "1.3.0"), so the directory would be
            # useless. Not read from the frontmatter `name:` — that diverges from
            # the invocable name on a plugin already installed here
            # (skills-sec-audit declares "skills安全审计").
            if (ipath / SKILL_MANIFEST).is_file():
                rows.append(("skill", name, name, version,
                             str(ipath / SKILL_MANIFEST), "plugin"))
                owner_maps["skill"][name] = name

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

    return rows, owner_maps, product_path


def replace_inventory(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Make ``inventory`` a snapshot of what is installed right now.

    One transaction for the delete and the insert, so a reader never sees the
    empty gap, and so a failure mid-write leaves the previous snapshot intact.
    The table used to be written with INSERT OR REPLACE only, which meant an
    uninstalled plugin or a deleted skill file kept its row forever and every
    panel kept listing it — "ever used ∪ installed" read as "installed ∪ ever
    believed installed". "Ever used" is what the usage tables carry; this table
    answers only "is it here now".
    """
    with conn:                                  # BEGIN … COMMIT / ROLLBACK
        conn.execute("DELETE FROM inventory")
        conn.executemany(
            "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version, path, source)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            rows,
        )


def scan_inventory(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """Refresh the inventory snapshot; return skill/agent/command -> plugin maps."""
    rows, owner_maps, _path = collect_inventory()
    replace_inventory(conn, rows)
    return owner_maps["skill"], owner_maps["agent"], owner_maps["command"]


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
               unparsed=None, model_hosts=None) -> int:
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
                       unparsed, model_hosts)
    return consumed


def _nested_cache_hit(raw):
    """``rawUsage.prompt_tokens_details.cached_tokens``, when this record carries it.

    The only cache number a custom endpoint sends. ``None`` means "not reported",
    which is a different fact from an explicit ``0`` — so the caller substitutes this
    only where the top-level field is absent, never where it is zero.
    """
    details = raw.get(NESTED_CACHE_HIT[0])
    if not isinstance(details, dict):
        return None
    value = details.get(NESTED_CACHE_HIT[1])
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# --- model route origin -----------------------------------------------------
#
# providerData.requestModelId carries the route identity: a bare model id for a
# built-in route ("deepseek-v4.1-flash"), or "<endpoint>:<model id>" for a custom
# one ("custom-local:ark-code-latest"). That shape — never the model *name* — is
# what separates a built-in response from a third-party one; a custom endpoint's
# alias can look built-in, so guessing from the name is exactly what this avoids.
PROVIDER_BUILTIN = db.PROVIDER_BUILTIN
# Separates the endpoint tag from the model id inside requestModelId
# ("custom-local:ark-code-latest"). A bare ":" at the call site would be an
# inlined format claim, so it lives here.
PROVIDER_SEP = ":"

# Third-party url host -> the vendor a person would recognise. Matched on the
# registrable suffix, so "ark.cn-beijing.volces.com" hits "volces.com". A host
# that is not listed is stored verbatim rather than guessed at.
_VENDOR_BY_HOST = {
    "volces.com": "Volcano Ark",
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "deepseek.com": "DeepSeek",
    "moonshot.cn": "Moonshot",
    "bigmodel.cn": "Zhipu",
    "aliyuncs.com": "Alibaba Cloud",
    "siliconflow.cn": "SiliconFlow",
}


# The host of a provider url, taken with a regex rather than ``urllib.parse``:
# this project forbids the ``urllib`` import root (it can reach the network) and
# only the authority is needed. Any userinfo is skipped and the match stops
# before a port or path.
_PROVIDER_HOST_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/?#]*@)?([^:/?#]+)")


def url_host(url) -> str:
    """The lower-cased host of ``url``, or ``""`` when it has no authority."""
    m = _PROVIDER_HOST_RE.match(str(url or ""))
    return m.group(1).lower() if m else ""


def load_model_hosts(codebuddy_dir) -> dict:
    """{model id -> url host} from ``<codebuddy_dir>/models.json``, else ``{}``.

    The file also holds each provider's ``apiKey``; only ``id``/``name``/``url``
    are ever read (test_privacy pins that). Any failure — absent file, bad JSON,
    missing url — degrades to ``{}`` so a sync never breaks over config it does
    not need.
    """
    try:
        with open(Path(codebuddy_dir) / MODELS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = data.get("models") if isinstance(data, dict) else None
    hosts = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        host = url_host(entry.get("url"))
        if model_id and host:
            hosts[model_id] = host
    return hosts


def _vendor_for_host(host: str):
    """The friendly vendor name for a host, or ``None`` when it is not known."""
    for suffix, name in _VENDOR_BY_HOST.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


def resolve_provider(request_model_id, model_hosts) -> str | None:
    """The route origin for one response, from ``providerData.requestModelId``.

    Cascade, most specific first — never a guess from the model name:

    * absent               -> ``None``        (unknown)
    * no ``:``             -> ``'builtin'``   (a built-in route)
    * ``"<endpoint>:<model id>"`` -> a third-party label, in order:
        - the vendor of the configured url host, when the model id is in
          ``models.json`` and its host is recognised (``"Volcano Ark"``);
        - that url host verbatim when the host is not recognised
          (``"ark.cn-beijing.volces.com"``);
        - the endpoint tag (``"custom-local"``) when the model id is not in
          ``models.json`` at all.
    """
    if not isinstance(request_model_id, str) or not request_model_id:
        return None
    if PROVIDER_SEP not in request_model_id:
        return PROVIDER_BUILTIN
    endpoint, _, model_id = request_model_id.partition(PROVIDER_SEP)
    host = model_hosts.get(model_id)
    if not host:
        return endpoint or None
    return _vendor_for_host(host) or host


def _record_model_response(conn, rec, sid, project, ts, model_hosts=None) -> None:
    """Persist one model response's token usage, deduped by messageId.

    Source of truth is ``providerData.rawUsage`` only. The same response can be
    re-emitted several times (a usage-bearing record plus usage-less copies that
    share its ``messageId``), so:

    * a record *with* ``rawUsage`` upserts the token columns;
    * a record *without* ``rawUsage`` only inserts a placeholder if the response
      is not known yet (``INSERT OR IGNORE``), never fabricating tokens.

    Missing token fields are stored as NULL and named in ``missing``. ``credit``
    and ``turn-metrics.tokenDelta`` are never read here. The cache hit is the one
    field with two spellings: the nested copy is read only when the top level is
    absent, so a record carrying both is counted once (see ``NESTED_CACHE_HIT``).

    ``provider`` (the route origin) is resolved from ``requestModelId`` and is
    independent of ``rawUsage``, so both branches store it — a usage-less
    placeholder still knows which route served it.
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
    origin = resolve_provider(provider.get("requestModelId"), model_hosts or {})

    if isinstance(raw, dict):
        vals = {k: raw.get(k) for k in USAGE_FIELDS}
        cache = {k: raw.get(k) for k in CACHE_USAGE_FIELDS}
        if cache["prompt_cache_hit_tokens"] is None:
            cache["prompt_cache_hit_tokens"] = _nested_cache_hit(raw)
        missing = [k for k in USAGE_FIELDS if raw.get(k) is None]
        if model is None:
            missing.append("model")
        conn.execute(
            "INSERT INTO model_responses"
            "(message_id, session_id, conversation_request_id, model, provider,"
            " prompt_tokens, completion_tokens,"
            " cache_read_input_tokens, cache_creation_input_tokens,"
            " prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
            " prompt_cache_write_tokens, provider_total_tokens,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'transcript',1,?)"
            " ON CONFLICT(message_id) DO UPDATE SET"
            "   session_id = COALESCE(excluded.session_id, session_id),"
            "   conversation_request_id ="
            "     COALESCE(excluded.conversation_request_id, conversation_request_id),"
            "   model = COALESCE(excluded.model, model),"
            "   provider = COALESCE(excluded.provider, provider),"
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
            (message_id, sid, conv, model, origin, vals["prompt_tokens"],
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
            "(message_id, session_id, conversation_request_id, model, provider,"
            " ts, project, source, usage_available, missing)"
            " VALUES(?,?,?,?,?,?,?,'transcript',0,'rawUsage')",
            (message_id, sid, conv, model, origin, ts, project),
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
                   unparsed=None, model_hosts=None) -> None:
    rtype = rec.get("type")
    sid = _session_id(rec)
    project = rec.get("cwd")
    ts = rec.get("timestamp")

    _record_model_response(conn, rec, sid, project, ts, model_hosts)

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


def format_doc(tools=None, agents=None, source: Path | None = None) -> str:
    """Render the format registry as markdown (``cbut format``).

    Every value is read back out of the constants rather than retyped, so the
    checked-in document cannot describe a surface the code no longer reads.
    ``scripts/tests/test_maintenance_docs.py`` holds the file to this output.

    The builtin catalogue is the one part that is *not* a constant any more, so
    it is passed in — together with where it came from, because a count of 61
    and a count of 35 are the same number of tools on a machine with a
    resolvable CodeBuddy package and one without, and a document that stated
    the first while the code used the second would be lying. Left to itself it
    resolves the live catalogue; ``test_maintenance_docs`` pins the fallback so
    the checked-in file describes the checked-in registry.
    """
    if tools is None or agents is None:
        _tools, _agents, resolved = builtin_catalog()
        tools = _tools if tools is None else tools
        agents = _agents if agents is None else agents
        source = source or resolved
    origin = "product.json" if source else "fallback constants"
    where = f" (`{source}`)" if source else " (product.json unresolved)"

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
        row("`rawUsage` nested cache path (fallback for the hit above)",
            NESTED_USAGE_FIELDS),
        row("`_meta` fields", BAGGAGE_FIELDS),
        row("message content-block fields", CONTENT_BLOCK_FIELDS),
        row("tool argument names", TOOL_ARG_FIELDS),
        row("inventory JSON keys", INVENTORY_JSON_FIELDS),
        "- **Tool names that drive classification**: "
        f"`{SKILL_TOOL}`, `{AGENT_TOOL}`, prefix `{MCP_TOOL_PREFIX}` "
        f"with separator `{MCP_TOOL_SEP}`\n",
        row("Meta tools (wrap another tool)", META_TOOLS),
        f"- **Tolerant name tables** (an unknown name still indexes): "
        f"{len(tools)} builtin tools, {len(agents)} builtin agents, "
        f"default agent type `{DEFAULT_AGENT_TYPE}`\n",
        f"- **Builtin catalogue source**: {origin}{where} — the installed "
        f"`{PRODUCT_CLI_NAME}` package's `{PRODUCT_JSON}`, found by resolving "
        "the executable on `PATH` and walking up; `CBUT_PRODUCT_JSON` overrides "
        "the search. Membership follows the package, so a name it drops stops "
        "being inventoried; `kind` (active/internal) stays this tool's taxonomy\n",
        "- **Markers parsed out of text**: "
        f"`{CMD_RE.pattern}` · `{BAGGAGE_SID_RE.pattern}`\n",
        "- **On-disk layout, relative to `~/.codebuddy`**:\n"
        f"  - `{DIR_PROJECTS}` / `{TRANSCRIPT_GLOB}` — transcripts, the only "
        "ingested log tree\n"
        f"  - `{DIR_SKILLS}` — a direct child holding `{SKILL_MANIFEST}` is a "
        f"skill; one category level below is still read, as is `{SKILL_MANIFEST_GLOB}` "
        "for anything deeper. Symlinked entries are followed exactly one level: "
        "`glob(\"**\")` does not cross a symlink, which is how the whole symlinked "
        "tree went missing at once\n"
        f"  - `{DIR_AGENTS}` / `{AGENT_SPEC_GLOB}` — user agents\n"
        f"  - `{MCP_FILE}` — MCP servers\n"
        f"  - `{DIR_PLUGINS}` / `{PLUGIN_INDEX_FILE}`, keys split on "
        f"`{PLUGIN_MARKETPLACE_SEP}`\n"
        f"  - a name starting with `{INVENTORY_HIDDEN_PREFIX}` is never an "
        "installed item\n"
        + pairs("plugin subdirectories (dir → kind)", PLUGIN_SUBDIRS) +
        f"  - a plugin holding `{SKILL_MANIFEST}` at its **install root** is a "
        "skill under the plugin's own name: the directory there is the version, "
        "and the frontmatter `name:` is not read (a plugin here declares a "
        "different one)\n"
        f"- **Transcript framing**: one JSON record per line, separated by a "
        f"newline byte (`{TRANSCRIPT_LINE_SEP!r}`)\n",
        "- **`inventory` is a snapshot of what is installed now**: rebuilt on "
        "every sync, deleted and re-inserted in one transaction. A name that "
        "leaves it stays listed by the usage tables with `installed = 0`, which "
        "is the only thing that distinguishes \"removed\" from \"never installed\"\n",
        "- **Not inventoried from a plugin**: tools and MCP servers. No installed "
        "plugin ships either (`plugin.json` declares neither, and none of the nine "
        "on this machine has a tool or MCP file), so there is no rule to read and "
        "no rule to guess at. If a plugin ever does, a name it adds would come "
        "back from the usage tables alone and read as *removed* — the same failure "
        "a root `SKILL.md` had, so this line is the place to look first\n",
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
    # Resolved once per run: model id -> url host, for the route-origin column.
    model_hosts = load_model_hosts(db.CODEBUDDY_DIR)

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
                                    unparsed, model_hosts)
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

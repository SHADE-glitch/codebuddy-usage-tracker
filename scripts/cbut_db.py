"""cbut_db — storage layer for the CodeBuddy usage tracker.

Defines the SQLite schema (tables + views) and small helpers shared by the
indexer (cbut-sync.py), the headless CLI (cbut-stats.py) and the TUI
(cbut-tui.py).

The tracker is a *passive reader*: it never writes into CodeBuddy's own
directories. It only reads ``~/.codebuddy`` and writes this one database.

Standard library only.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 4

# --- locations -------------------------------------------------------------

CODEBUDDY_DIR = Path(
    os.environ.get("CBUT_CODEBUDDY_DIR", str(Path.home() / ".codebuddy"))
)

DB_PATH = Path(
    os.environ.get(
        "CBUT_DB",
        str(Path.home() / ".local" / "share" / "codebuddy-usage-tracker" / "usage.db"),
    )
)


# --- schema ----------------------------------------------------------------

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    project     TEXT,             -- decoded cwd the session ran in
    title       TEXT,
    started_at  INTEGER,          -- epoch ms
    ended_at    INTEGER,
    model       TEXT,
    tokens      INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);

-- One row per model response, keyed by providerData.messageId (globally unique
-- in the transcripts: a usage-bearing messageId always appears exactly once).
-- Token columns come *only* from providerData.rawUsage. They are never derived
-- from credit, turn-metrics.tokenDelta, context-window estimates or model
-- multipliers. A missing field is stored as NULL (never a fabricated 0) and is
-- named in `missing`; `usage_available` is 0 when the response carried no usage.
--
-- In real transcripts prompt_tokens is partitioned as
--   prompt_tokens = prompt_cache_hit_tokens
--                 + prompt_cache_miss_tokens
--                 + prompt_cache_write_tokens
-- (all three are *parts* of the input; they are never added on top of it).
-- cache_read/creation_input_tokens are a separate, usually-zero legacy pair and
-- are never summed with prompt_cache_*.
CREATE TABLE IF NOT EXISTS model_responses (
    message_id                 TEXT PRIMARY KEY,
    session_id                 TEXT,
    conversation_request_id    TEXT,
    model                      TEXT,
    prompt_tokens              INTEGER,
    completion_tokens          INTEGER,
    cache_read_input_tokens    INTEGER,
    cache_creation_input_tokens INTEGER,
    -- Newer rawUsage cache fields (schema v3). Real cache values live here;
    -- cache_read/creation above are usually 0. Absent field -> NULL (never 0).
    prompt_cache_hit_tokens    INTEGER,
    prompt_cache_miss_tokens   INTEGER,
    prompt_cache_write_tokens  INTEGER,
    -- The provider's own total (schema v4), copied verbatim from
    -- providerData.rawUsage.total_tokens. Absent -> NULL; a real 0 stays 0.
    -- Never derived here (the derived fallback is applied at query time).
    provider_total_tokens      INTEGER,
    ts                         INTEGER,
    project                    TEXT,
    source                     TEXT DEFAULT 'transcript',
    usage_available            INTEGER DEFAULT 0,
    missing                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_resp_session ON model_responses(session_id);
CREATE INDEX IF NOT EXISTS idx_model_resp_model   ON model_responses(model);
CREATE INDEX IF NOT EXISTS idx_model_resp_ts      ON model_responses(ts);
CREATE INDEX IF NOT EXISTS idx_model_resp_model_ts ON model_responses(model, ts);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id     TEXT PRIMARY KEY,
    session_id  TEXT,
    project     TEXT,
    tool_name   TEXT,
    category    TEXT,             -- builtin | skill | agent | mcp | meta
    ts          INTEGER,
    duration_ms INTEGER,
    status      TEXT,             -- completed | incomplete | NULL(pending)
    model       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_ts      ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_cat     ON tool_calls(category);

CREATE TABLE IF NOT EXISTS skill_usage (
    call_id     TEXT PRIMARY KEY,
    skill       TEXT,
    has_args    INTEGER DEFAULT 0,
    plugin      TEXT,
    session_id  TEXT,
    project     TEXT,
    ts          INTEGER,
    status      TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill_usage(skill);

CREATE TABLE IF NOT EXISTS agent_usage (
    call_id     TEXT PRIMARY KEY,
    agent_type  TEXT,
    description TEXT,
    kind        TEXT,             -- active | internal
    source      TEXT,             -- tool | span
    session_id  TEXT,
    project     TEXT,
    ts          INTEGER,
    status      TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_type ON agent_usage(agent_type);

CREATE TABLE IF NOT EXISTS mcp_usage (
    call_id     TEXT PRIMARY KEY,
    server      TEXT,
    tool        TEXT,
    session_id  TEXT,
    project     TEXT,
    ts          INTEGER,
    status      TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mcp_server ON mcp_usage(server);
CREATE INDEX IF NOT EXISTS idx_mcp_tool   ON mcp_usage(tool);

CREATE TABLE IF NOT EXISTS plugin_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin      TEXT,
    marketplace TEXT,
    kind        TEXT,             -- skill | command | agent | tool
    target      TEXT,
    session_id  TEXT,
    project     TEXT,
    ts          INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_plugin_usage
    ON plugin_usage(plugin, kind, target, session_id, ts);

CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT,
    session_id  TEXT,
    project     TEXT,
    ts          INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_commands ON commands(command, session_id, ts);

CREATE TABLE IF NOT EXISTS inventory (
    kind         TEXT,            -- tool | skill | agent | plugin | mcp
    name         TEXT,
    owner_plugin TEXT,
    version      TEXT,
    path         TEXT,
    source       TEXT,            -- builtin | user | plugin
    PRIMARY KEY (kind, name, owner_plugin)
);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path TEXT PRIMARY KEY,
    size      INTEGER,
    mtime     REAL,
    offset    INTEGER
);
"""

# --- name sets for the "used ∪ installed" union ---------------------------
#
# Every panel lists the union of what is *installed/available* (the static
# ``inventory``) and what has *ever been used* (the usage tables), keyed by
# NAME alone. Name is the primary key: a renamed, updated or removed entity
# keeps its history, a name that appears under several owners merges into one
# row, and an installed-but-unused entity still shows with a zero count. Under
# a time window only the counts shrink — the list itself stays full.
#
# These CTEs are shared by the v_* views (all-time, for ``cbut export``) and by
# the q_* functions (which add the optional window in the LEFT JOIN's ON clause
# so an out-of-window entity still yields a zero-count row).
_TOOL_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='tool'"
    " UNION SELECT DISTINCT tool_name FROM tool_calls WHERE tool_name IS NOT NULL)")
_SKILL_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='skill'"
    " UNION SELECT DISTINCT skill FROM skill_usage WHERE skill IS NOT NULL)")
_AGENT_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='agent'"
    " UNION SELECT DISTINCT agent_type FROM agent_usage WHERE agent_type IS NOT NULL)")
_PLUGIN_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='plugin'"
    " UNION SELECT DISTINCT plugin FROM plugin_usage WHERE plugin IS NOT NULL)")
# MCP is keyed by (server, tool). A configured server with no usage yet is
# listed once as (server, NULL) so the tab shows configured-but-unused servers
# without forking a server that already has recorded calls.
_MCP_NAMES_CTE = (
    "WITH pairs AS ("
    " SELECT server, tool FROM mcp_usage"
    " UNION SELECT name, NULL FROM inventory i WHERE kind='mcp'"
    "   AND NOT EXISTS (SELECT 1 FROM mcp_usage m WHERE m.server = i.name))")

VIEWS_SQL = f"""
DROP VIEW IF EXISTS v_tools;
CREATE VIEW v_tools AS
{_TOOL_NAMES_CTE}
SELECT n.name AS tool_name,
       COUNT(c.call_id)                                      AS calls,
       SUM(CASE WHEN c.status = 'completed' THEN 1 ELSE 0 END) AS completed,
       SUM(CASE WHEN c.status = 'incomplete' THEN 1 ELSE 0 END) AS failed,
       SUM(CASE WHEN c.call_id IS NOT NULL AND c.status IS NULL THEN 1 ELSE 0 END) AS pending,
       CAST(AVG(c.duration_ms) AS INTEGER)                   AS avg_ms,
       MAX(c.ts)                                             AS last_used,
       MIN(c.ts)                                             AS first_used,
       COUNT(DISTINCT c.project)                             AS projects,
       COUNT(DISTINCT c.session_id)                          AS sessions
FROM names n
LEFT JOIN tool_calls c ON c.tool_name = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_skills;
CREATE VIEW v_skills AS
{_SKILL_NAMES_CTE}
SELECT n.name AS skill,
       COUNT(s.call_id)                                      AS calls,
       SUM(CASE WHEN s.status = 'completed' THEN 1 ELSE 0 END) AS completed,
       MAX(s.ts)                                             AS last_used,
       MIN(s.ts)                                             AS first_used,
       COUNT(DISTINCT s.project)                             AS projects,
       COUNT(DISTINCT s.session_id)                          AS sessions
FROM names n
LEFT JOIN skill_usage s ON s.skill = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_agents;
CREATE VIEW v_agents AS
{_AGENT_NAMES_CTE}
SELECT n.name AS agent_type,
       COUNT(a.call_id)                                      AS calls,
       MAX(a.ts)                                             AS last_used,
       MIN(a.ts)                                             AS first_used,
       COUNT(DISTINCT a.project)                             AS projects,
       COUNT(DISTINCT a.session_id)                          AS sessions
FROM names n
LEFT JOIN agent_usage a ON a.agent_type = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_mcp;
CREATE VIEW v_mcp AS
{_MCP_NAMES_CTE}
SELECT p.server,
       p.tool,
       COUNT(m.call_id)                                      AS calls,
       SUM(CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END) AS completed,
       MAX(m.ts)                                             AS last_used,
       MIN(m.ts)                                             AS first_used,
       COUNT(DISTINCT m.project)                             AS projects,
       COUNT(DISTINCT m.session_id)                          AS sessions
FROM pairs p
LEFT JOIN mcp_usage m ON m.server = p.server AND m.tool = p.tool
GROUP BY p.server, p.tool;

DROP VIEW IF EXISTS v_plugins;
CREATE VIEW v_plugins AS
{_PLUGIN_NAMES_CTE}
SELECT n.name AS plugin,
       COUNT(u.id)                                           AS uses,
       MAX(u.ts)                                             AS last_used,
       (SELECT COUNT(*) FROM inventory x
          WHERE x.owner_plugin = n.name AND x.kind = 'skill')   AS skills,
       (SELECT COUNT(*) FROM inventory x
          WHERE x.owner_plugin = n.name AND x.kind = 'agent')   AS agents,
       (SELECT COUNT(*) FROM inventory x
          WHERE x.owner_plugin = n.name AND x.kind = 'command') AS commands
FROM names n
LEFT JOIN plugin_usage u ON u.plugin = n.name
GROUP BY n.name;
"""

SCHEMA_SQL = TABLES_SQL + VIEWS_SQL


# --- helpers ---------------------------------------------------------------


def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def open_db(path: Path | str = DB_PATH, readonly: bool = False) -> sqlite3.Connection:
    """Open the tracker DB. Creates the file/schema unless ``readonly``."""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        ensure_dir(path)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the original schema, kept here so the migration and the
# CREATE TABLE stay in sync. v3: the three prompt_cache_* cache parts.
# v4: provider_total_tokens (the provider's own rawUsage total).
_NEW_MODEL_COLUMNS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_write_tokens",
    "provider_total_tokens",
)


def _migrate_model_responses(conn: sqlite3.Connection) -> None:
    """Add v3/v4 columns to a pre-existing ``model_responses`` table.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so check
    ``PRAGMA table_info`` first. When any column is actually added (i.e. an old
    database is being upgraded), clear ``sync_state`` so the next incremental
    ``cbut sync`` re-reads the transcripts and backfills the new columns via the
    existing messageId UPSERT — row counts stay stable. ``sessions`` is cleared
    too: its ``tokens``/``duration_ms`` are *accumulated* from ``turn-metrics``,
    so a full re-read would otherwise add every turn's delta a second time.
    Fresh databases already have the columns, so this is a no-op for them.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
    added = [c for c in _NEW_MODEL_COLUMNS if c not in existing]
    if not added:
        return
    for col in added:
        conn.execute(f"ALTER TABLE model_responses ADD COLUMN {col} INTEGER")
    conn.execute("DELETE FROM sync_state")
    conn.execute("DELETE FROM sessions")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate_model_responses(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def migrate(db_path: Path | str = DB_PATH) -> bool:
    """Best-effort upgrade of the tracker DB to the current schema.

    Opens the DB read-write, runs :func:`ensure_schema` (which adds the v3/v4
    columns and, when it actually adds columns, clears ``sync_state`` so the next
    incremental sync backfills them), and closes. Returns ``True`` on success.

    Swallows ``sqlite3.Error`` so a read-only environment can still launch the
    TUI; callers that need the newer columns should tolerate ``False``.
    """
    try:
        conn = open_db(db_path)
        try:
            ensure_schema(conn)
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def reset(conn: sqlite3.Connection) -> None:
    """Drop all data (keeps the schema). Used by ``sync --full``."""
    for table in (
        "plugin_usage",
        "commands",
        "mcp_usage",
        "agent_usage",
        "skill_usage",
        "tool_calls",
        "model_responses",
        "sessions",
        "inventory",
        "sync_state",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


# --- shared queries (used by both the CLI and the TUI) ---------------------
#
# NOTE: the aggregate SQL below intentionally duplicates VIEWS_SQL. The views
# remain the export/back-compat path (`cbut export`, ViewGroupByTest); these
# ``q_*`` functions aggregate the base tables directly so they can take an
# optional time window (SQLite views cannot be parameterized).


def _on_time(conds) -> tuple[str, list]:
    """``(" AND <pred> AND ...", params)`` for the LEFT JOIN's ON clause.

    The window lives in ON (never WHERE) so an out-of-window entity still
    yields a row with a zero count instead of dropping out of the list.
    """
    if not conds:
        return "", []
    return (" AND " + " AND ".join(c for c, _ in conds),
            [p for _, p in conds])


def q_tools(conn, limit=None, start_ts=None, end_ts=None):
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="c.ts"))
    sql = (f"{_TOOL_NAMES_CTE}"
           " SELECT n.name AS tool_name, COUNT(c.call_id) AS calls,"
           " SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " SUM(CASE WHEN c.status='incomplete' THEN 1 ELSE 0 END) AS failed,"
           " SUM(CASE WHEN c.call_id IS NOT NULL AND c.status IS NULL"
           "          THEN 1 ELSE 0 END) AS pending,"
           " CAST(AVG(c.duration_ms) AS INTEGER) AS avg_ms,"
           " MAX(c.ts) AS last_used, MIN(c.ts) AS first_used,"
           " COUNT(DISTINCT c.project) AS projects,"
           " COUNT(DISTINCT c.session_id) AS sessions"
           " FROM names n LEFT JOIN tool_calls c ON c.tool_name = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_skills(conn, limit=None, start_ts=None, end_ts=None):
    """Skills keyed by NAME only — a skill name that ships under several
    plugins is merged into one row (no ``plugin`` column)."""
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="s.ts"))
    sql = (f"{_SKILL_NAMES_CTE}"
           " SELECT n.name AS skill, COUNT(s.call_id) AS calls,"
           " SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " MAX(s.ts) AS last_used, MIN(s.ts) AS first_used,"
           " COUNT(DISTINCT s.project) AS projects,"
           " COUNT(DISTINCT s.session_id) AS sessions"
           " FROM names n LEFT JOIN skill_usage s ON s.skill = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_agents(conn, limit=None, start_ts=None, end_ts=None):
    """Agents keyed by NAME only.

    ``kind`` (active/internal) is deliberately not exposed: internal agents live
    in the OTel traces, which are not ingested, so the column is always
    ``active`` (or absent for an unused installed agent) and carries no signal.
    """
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="a.ts"))
    sql = (f"{_AGENT_NAMES_CTE}"
           " SELECT n.name AS agent_type, COUNT(a.call_id) AS calls,"
           " MAX(a.ts) AS last_used, MIN(a.ts) AS first_used,"
           " COUNT(DISTINCT a.project) AS projects,"
           " COUNT(DISTINCT a.session_id) AS sessions"
           " FROM names n LEFT JOIN agent_usage a ON a.agent_type = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_mcp(conn, limit=None, start_ts=None, end_ts=None):
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="m.ts"))
    sql = (f"{_MCP_NAMES_CTE}"
           " SELECT p.server, p.tool, COUNT(m.call_id) AS calls,"
           " SUM(CASE WHEN m.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " MAX(m.ts) AS last_used, MIN(m.ts) AS first_used,"
           " COUNT(DISTINCT m.project) AS projects,"
           " COUNT(DISTINCT m.session_id) AS sessions"
           " FROM pairs p"
           " LEFT JOIN mcp_usage m ON m.server = p.server AND m.tool = p.tool"
           f"{on_time} GROUP BY p.server, p.tool"
           " ORDER BY calls DESC, p.server, p.tool")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_plugins(conn):
    """Every plugin, keyed by NAME: installed (static inventory) ∪ ever used.

    No version column and no time window: name is the primary key, so a version
    bump never forks a plugin and an uninstalled-but-used plugin keeps its row.
    ``uses`` counts every attributed skill/agent/command invocation.
    """
    return conn.execute(
        f"{_PLUGIN_NAMES_CTE}"
        " SELECT n.name AS plugin,"
        " COUNT(u.id) AS uses, MAX(u.ts) AS last_used,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name AND x.kind = 'skill')  AS skills,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name AND x.kind = 'agent')  AS agents,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name AND x.kind = 'command') AS commands"
        " FROM names n"
        " LEFT JOIN plugin_usage u ON u.plugin = n.name"
        " GROUP BY n.name ORDER BY uses DESC, n.name",
    ).fetchall()


def q_recent(conn, limit=30):
    return conn.execute(
        "SELECT ts, tool_name, category, status, duration_ms, project"
        " FROM tool_calls ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    ).fetchall()


def q_history(conn, kind, name, limit=50):
    """Recent calls for one entity. kind in tool|skill|agent|mcp."""
    where = {
        "tool": ("tool_calls", "tool_name"),
        "skill": ("skill_usage", "skill"),
        "agent": ("agent_usage", "agent_type"),
        "mcp": ("mcp_usage", "tool"),
    }[kind]
    table, col = where
    return conn.execute(
        f"SELECT ts, project, session_id, status, duration_ms"
        f" FROM {table} WHERE {col} = ? ORDER BY ts DESC LIMIT ?",
        (name, int(limit)),
    ).fetchall()


def q_model_responses(conn, limit=50, model=None):
    """Recent per-response model usage rows (newest first).

    Token columns are the raw providerData.rawUsage values; NULL means the field
    was absent. ``total_tokens`` prefers the provider's own total and falls back
    to ``prompt + completion``; ``total_tokens_source`` says which was used
    ('provider' | 'derived' | NULL).
    """
    sql = (
        "SELECT message_id, session_id, conversation_request_id, model,"
        " prompt_tokens, completion_tokens, cache_read_input_tokens,"
        " cache_creation_input_tokens,"
        " prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
        " prompt_cache_write_tokens, provider_total_tokens,"
        f" {_TOTAL_EXPR} AS total_tokens,"
        f" {_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        " ts, project, source, usage_available, missing"
        " FROM model_responses"
    )
    params: list = []
    if model:
        sql += " WHERE model = ?"
        params.append(model)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def q_model_tokens(conn, start_ts=None, end_ts=None):
    """Per-model token totals.

    ``SUM`` skips NULLs, so responses recorded with ``usage_available=0`` add
    nothing instead of a fabricated zero. ``tokenDelta`` and credit are never
    part of these sums.

    ``total_tokens`` is the **API Total**: the sum of the per-row totals, i.e.
    the provider's own ``provider_total_tokens`` where present, else
    ``prompt + completion``. It is NULL only when a model contributes no total
    at all. ``total_tokens_source`` reports 'provider' / 'derived' / 'mixed' /
    NULL. The cache columns (hit/miss/write) are reported separately and are
    deliberately NOT added into ``total_tokens``.

    ``usage_total_tokens`` is a **display-only** metric:
    ``prompt + completion + cache hit``. Because ``prompt_tokens`` already
    contains the cache hit (``prompt = hit + miss + write``), this re-adds the
    hit and is roughly twice the API Total. It never replaces ``total_tokens``.
    Rows without cache data contribute no hit (COALESCE to 0), so they are not
    inflated.
    """
    where, params = _time_filter(start_ts, end_ts)
    return conn.execute(
        "SELECT model,"
        " COUNT(*) AS responses,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        " SUM(cache_read_input_tokens) AS cache_read_input_tokens,"
        " SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,"
        " SUM(prompt_cache_hit_tokens) AS prompt_cache_hit_tokens,"
        " SUM(prompt_cache_miss_tokens) AS prompt_cache_miss_tokens,"
        " SUM(prompt_cache_write_tokens) AS prompt_cache_write_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_AGG_USAGE_TOTAL_EXPR} AS usage_total_tokens"
        f" FROM model_responses{where} GROUP BY model ORDER BY responses DESC",
        params,
    ).fetchall()


# --- usage statistics (calendar-day windows) -------------------------------

# Window sizes in CALENDAR days; None = All time (no lower bound). A window is
# whole local days ending *now*: "1d" starts at today's local 00:00, "2d" at
# yesterday's 00:00, and so on — never "now minus N hours".
USAGE_RANGES = {"1d": 1, "2d": 2, "3d": 3, "7d": 7, "30d": 30, "all": None}
USAGE_RANGE_LABELS = {
    "1d": "Today", "2d": "2 days", "3d": "3 days",
    "7d": "7 days", "30d": "30 days", "all": "All time",
}

# No provider/account/site/endpoint field exists in the transcripts, so the
# tracker does not model a provider at all (nothing to infer it from).

# Per-row total: prefer the provider's own rawUsage total, else fall back to
# prompt + completion. NULL only when neither is available (never a fabricated 0).
_TOTAL_EXPR = (
    "COALESCE(provider_total_tokens,"
    " CASE WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " THEN prompt_tokens + completion_tokens ELSE NULL END)"
)
# Provenance of the per-row value above: 'provider' | 'derived' | NULL.
_TOTAL_SOURCE_EXPR = (
    "CASE WHEN provider_total_tokens IS NOT NULL THEN 'provider'"
    " WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " THEN 'derived' ELSE NULL END"
)
# Aggregate total: SUM of the PER-ROW total. Never SUM(provider_total_tokens),
# which would silently drop rows that fall back to the derived value.
_AGG_TOTAL_EXPR = f"SUM({_TOTAL_EXPR})"
# Row counts that feed the aggregate provenance below.
_AGG_PROVIDER_N = (
    "SUM(CASE WHEN provider_total_tokens IS NOT NULL THEN 1 ELSE 0 END)"
)
_AGG_DERIVED_N = (
    "SUM(CASE WHEN provider_total_tokens IS NULL"
    " AND prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " THEN 1 ELSE 0 END)"
)
# Aggregate provenance over the contributing rows:
#   NULL     -> no row contributes a total
#   provider -> every contributing row carries a provider total
#   derived  -> none carry one (all fell back to prompt + completion)
#   mixed    -> both kinds are present
# COALESCE guards the empty-window case, where SUM(...) is NULL (not 0).
_AGG_TOTAL_SOURCE_EXPR = (
    f"CASE WHEN COALESCE({_AGG_PROVIDER_N},0) = 0"
    f" AND COALESCE({_AGG_DERIVED_N},0) = 0 THEN NULL"
    f" WHEN COALESCE({_AGG_DERIVED_N},0) = 0 THEN 'provider'"
    f" WHEN COALESCE({_AGG_PROVIDER_N},0) = 0 THEN 'derived'"
    " ELSE 'mixed' END"
)

# --- display-only "Usage Total" -------------------------------------------
# prompt + completion + cache hit. The prompt already contains the cache hit
# (prompt = hit + miss + write, verified on real data), so this deliberately
# re-adds it and lands at roughly twice the API total. It is shown *next to*
# the API total, never instead of it. NULL when the API parts are missing;
# a missing cache-hit column contributes 0 (COALESCE) so a row without cache
# data is not inflated and is not silently dropped from the sum.
_USAGE_TOTAL_EXPR = (
    "CASE WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " THEN prompt_tokens + completion_tokens"
    " + COALESCE(prompt_cache_hit_tokens, 0) ELSE NULL END"
)
_AGG_USAGE_TOTAL_EXPR = f"SUM({_USAGE_TOTAL_EXPR})"


def window_bounds(range_key: str, now_ms: int):
    """``(start_ms, end_ms)`` for a whole-calendar-day window ending at ``now_ms``.

    The window is a whole number of **local** calendar days: ``"1d"`` runs from
    today's local 00:00 to ``now_ms``, ``"2d"`` from yesterday's 00:00, and so
    on. It never uses "now minus N hours", so the bounds are stable through the
    day and match what a person means by "today" / "the last 2 days".

    ``start_ms`` is ``None`` for ``"all"`` (no lower bound).
    """
    days = USAGE_RANGES[range_key]
    end = int(now_ms)
    if days is None:
        return None, end
    midnight = datetime.fromtimestamp(end / 1000).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days - 1)
    return int(start.timestamp() * 1000), end


def cache_hit_rate(hit, miss, write=None):
    """``hit / (hit + miss + write)`` as a fraction, or ``None`` when undefined.

    The denominator is the *cacheable input* — exactly the three parts that
    partition ``prompt_tokens`` (``prompt = hit + miss + write``). This matches
    cc-switch's definition ("cache read tokens as a share of cacheable input")
    and, unlike ``hit / (hit + miss)``, does not overstate the rate on rows that
    also wrote to the cache. ``write=None`` counts as 0 (rows that carry no
    cache data leave the rate undefined via the NULL checks below).

    NULL on either ``hit`` or ``miss``, or a zero denominator, yields ``None`` —
    never a fabricated rate.
    """
    if hit is None or miss is None:
        return None
    base = hit + miss + (write or 0)
    if base == 0:
        return None
    return hit / base


def _ts_where(start_ts, end_ts):
    if start_ts is None:
        return "ts < ?", [end_ts]
    return "ts >= ? AND ts < ?", [start_ts, end_ts]


def _time_conds(start_ts, end_ts, col="ts"):
    """时间谓词列表 ``(sql, param)``；两者皆 None 时为空（= 全部时间）。"""
    conds = []
    if start_ts is not None:
        conds.append((f"{col} >= ?", start_ts))
    if end_ts is not None:
        conds.append((f"{col} < ?", end_ts))
    return conds


def _time_filter(start_ts, end_ts, col="ts"):
    """返回 ``(where_prefix, params)``；无过滤时为 ``("", [])``。"""
    conds = _time_conds(start_ts, end_ts, col)
    if not conds:
        return "", []
    return " WHERE " + " AND ".join(c for c, _ in conds), [p for _, p in conds]


def q_usage_summary(conn, start_ts, end_ts) -> dict:
    """Totals for one rolling window. ``SUM`` keeps NULL (no fabricated 0).

    ``total_tokens`` is the **API Total** (provider total, else
    ``prompt + completion``); ``usage_total_tokens`` is the **display-only**
    ``prompt + completion + cache hit`` (see :data:`_USAGE_TOTAL_EXPR`). The two
    are separate keys and are never conflated.
    """
    where, params = _ts_where(start_ts, end_ts)
    row = conn.execute(
        "SELECT COUNT(*) AS requests,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_AGG_USAGE_TOTAL_EXPR} AS usage_total_tokens,"
        " SUM(prompt_cache_hit_tokens) AS cache_hit,"
        " SUM(prompt_cache_miss_tokens) AS cache_miss,"
        " SUM(prompt_cache_write_tokens) AS cache_write,"
        " SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS missing_prompt,"
        " SUM(CASE WHEN completion_tokens IS NULL THEN 1 ELSE 0 END)"
        " AS missing_completion"
        f" FROM model_responses WHERE {where}",
        params,
    ).fetchone()
    return dict(row)


def q_usage_kpi(conn, start_ts=None, end_ts=None) -> dict:
    """Windowed call counts for the Dashboard (the status-bar numbers, windowed).

    ``COUNT(*)`` per usage table equals the sum of the ``calls`` column of
    q_tools/q_skills/q_agents/q_mcp in the same window (every call carries a
    name), so the Dashboard always agrees with the per-entity tabs. ``plugins``
    is ``COUNT(DISTINCT plugin)`` — the windowed form of
    ``overview()['plugins_used']``. ``start_ts=None`` means all-time up to
    ``end_ts``; both ``None`` is all-time.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")

    def one(sql):
        return conn.execute(sql, params).fetchone()[0]

    return {
        "tool_calls": one(f"SELECT COUNT(*) FROM tool_calls{where}"),
        "skills": one(f"SELECT COUNT(*) FROM skill_usage{where}"),
        "agents": one(f"SELECT COUNT(*) FROM agent_usage{where}"),
        "mcp": one(f"SELECT COUNT(*) FROM mcp_usage{where}"),
        "plugins": one(f"SELECT COUNT(DISTINCT plugin) FROM plugin_usage{where}"),
    }


def q_usage_request_logs(conn, start_ts, end_ts, limit=100, offset=0):
    """Per-response rows in the window, newest first (never re-parses logs).

    Carries both ``total_tokens`` (API total) and ``usage_total_tokens`` (the
    display-only re-add of cache hit), plus the raw cache parts.
    """
    where, params = _ts_where(start_ts, end_ts)
    sql = (
        "SELECT ts, model, prompt_tokens, completion_tokens,"
        f" {_TOTAL_EXPR} AS total_tokens,"
        f" {_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_USAGE_TOTAL_EXPR} AS usage_total_tokens,"
        " prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
        " prompt_cache_write_tokens, usage_available, source, message_id, project"
        f" FROM model_responses WHERE {where}"
        " ORDER BY ts DESC, message_id LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, params + [int(limit), int(offset)]).fetchall()


# Whitelisted orderings for q_usage_model_stats (never user-supplied SQL).
_MODEL_STATS_ORDER = {
    "total_tokens": "total_tokens DESC, requests DESC",
    "requests": "requests DESC, total_tokens DESC",
    "model": "model COLLATE NOCASE ASC",
}


def q_usage_model_stats(conn, start_ts, end_ts, order_by="total_tokens"):
    """Per-model rollup for one window. ``order_by`` is whitelisted."""
    where, params = _ts_where(start_ts, end_ts)
    order = _MODEL_STATS_ORDER.get(order_by, _MODEL_STATS_ORDER["total_tokens"])
    return conn.execute(
        "SELECT model,"
        " COUNT(*) AS requests,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        " SUM(prompt_cache_hit_tokens) AS cache_hit,"
        " SUM(prompt_cache_miss_tokens) AS cache_miss,"
        " SUM(prompt_cache_write_tokens) AS cache_write"
        f" FROM model_responses WHERE {where}"
        f" GROUP BY model ORDER BY {order}",
        params,
    ).fetchall()


def q_usage_activity(conn, start_ts=None, end_ts=None) -> dict:
    """Windowed tool-runtime rollup for the Dashboard's "more metrics" panel.

    How the calls in the window behaved: ``completed``/``incomplete`` split the
    ``calls`` total, ``avg_ms`` is the mean tool duration (NULL when nothing
    ran, never a fabricated 0), and ``sessions``/``projects`` are the DISTINCT
    ids seen in the window — the windowed form of the all-time ``overview()``
    counts. Both bounds ``None`` means all-time.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")
    row = conn.execute(
        "SELECT COUNT(*) AS calls,"
        " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
        " SUM(CASE WHEN status='incomplete' THEN 1 ELSE 0 END) AS incomplete,"
        " CAST(AVG(duration_ms) AS INTEGER) AS avg_ms,"
        " COUNT(DISTINCT session_id) AS sessions,"
        " COUNT(DISTINCT project) AS projects"
        f" FROM tool_calls{where}",
        params,
    ).fetchone()
    return dict(row)


def q_usage_daily(conn, start_ts=None, end_ts=None):
    """Tool-call counts per LOCAL calendar day in the window, oldest first.

    Rows are ``{"day": "YYYY-MM-DD", "calls": n}`` for days that have calls.
    The day is bucketed in local time (``localtime``) so it lines up with the
    local-day windows; the Dashboard draws the series as a sparkline. Days with
    no calls are simply absent — the caller fills the gaps.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")
    return conn.execute(
        "SELECT strftime('%Y-%m-%d', ts / 1000, 'unixepoch', 'localtime') AS day,"
        " COUNT(*) AS calls"
        f" FROM tool_calls{where}"
        " GROUP BY day ORDER BY day",
        params,
    ).fetchall()


def q_inventory(conn, kind=None):
    sql = "SELECT * FROM inventory"
    params = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY owner_plugin, name"
    return conn.execute(sql, params).fetchall()


def overview(conn) -> dict:
    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    return {
        "tool_calls": one("SELECT COUNT(*) FROM tool_calls"),
        "distinct_tools": one("SELECT COUNT(DISTINCT tool_name) FROM tool_calls"),
        "skills": one("SELECT COUNT(*) FROM skill_usage"),
        "agents": one("SELECT COUNT(*) FROM agent_usage"),
        "mcp": one("SELECT COUNT(*) FROM mcp_usage"),
        "plugins_used": one("SELECT COUNT(DISTINCT plugin) FROM plugin_usage"),
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "commands": one("SELECT COUNT(*) FROM commands"),
        "projects": one("SELECT COUNT(DISTINCT project) FROM sessions WHERE project IS NOT NULL"),
        "first_ts": one("SELECT MIN(ts) FROM tool_calls"),
        "last_ts": one("SELECT MAX(ts) FROM tool_calls"),
        # NB: sessions.tokens is the turn-metrics.tokenDelta context metric, NOT
        # model prompt/completion tokens. The two are deliberately kept apart.
        "total_tokens": one("SELECT COALESCE(SUM(tokens),0) FROM sessions"),
        "model_responses": one("SELECT COUNT(*) FROM model_responses"),
        "model_responses_with_usage":
            one("SELECT COUNT(*) FROM model_responses WHERE usage_available=1"),
        "model_prompt_tokens":
            one("SELECT COALESCE(SUM(prompt_tokens),0) FROM model_responses"),
        "model_completion_tokens":
            one("SELECT COALESCE(SUM(completion_tokens),0) FROM model_responses"),
        # Sum of the per-row totals: provider total when present, else
        # prompt + completion. Cache columns are reported separately.
        "model_total_tokens":
            one(f"SELECT COALESCE(SUM({_TOTAL_EXPR}),0) FROM model_responses"),
        "model_prompt_cache_hit_tokens":
            one("SELECT COALESCE(SUM(prompt_cache_hit_tokens),0) FROM model_responses"),
        "model_prompt_cache_miss_tokens":
            one("SELECT COALESCE(SUM(prompt_cache_miss_tokens),0) FROM model_responses"),
        "model_prompt_cache_write_tokens":
            one("SELECT COALESCE(SUM(prompt_cache_write_tokens),0) FROM model_responses"),
        "inventory": one("SELECT COUNT(*) FROM inventory"),
    }


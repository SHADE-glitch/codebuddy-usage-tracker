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
from pathlib import Path

SCHEMA_VERSION = 1

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

VIEWS_SQL = """
DROP VIEW IF EXISTS v_tools;
CREATE VIEW v_tools AS
SELECT tool_name,
       COUNT(*)                                            AS calls,
       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
       SUM(CASE WHEN status = 'incomplete' THEN 1 ELSE 0 END) AS failed,
       SUM(CASE WHEN status IS NULL THEN 1 ELSE 0 END)     AS pending,
       CAST(AVG(duration_ms) AS INTEGER)                   AS avg_ms,
       MAX(ts)                                             AS last_used,
       MIN(ts)                                             AS first_used,
       COUNT(DISTINCT project)                             AS projects,
       COUNT(DISTINCT session_id)                          AS sessions
FROM tool_calls
GROUP BY tool_name;

DROP VIEW IF EXISTS v_skills;
CREATE VIEW v_skills AS
SELECT skill,
       plugin,
       COUNT(*)                                            AS calls,
       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
       MAX(ts)                                             AS last_used,
       MIN(ts)                                             AS first_used,
       COUNT(DISTINCT project)                             AS projects,
       COUNT(DISTINCT session_id)                          AS sessions
FROM skill_usage
GROUP BY skill;

DROP VIEW IF EXISTS v_agents;
CREATE VIEW v_agents AS
SELECT agent_type,
       kind,
       COUNT(*)                                            AS calls,
       MAX(ts)                                             AS last_used,
       MIN(ts)                                             AS first_used,
       COUNT(DISTINCT project)                             AS projects,
       COUNT(DISTINCT session_id)                          AS sessions
FROM agent_usage
GROUP BY agent_type;

DROP VIEW IF EXISTS v_mcp;
CREATE VIEW v_mcp AS
SELECT server,
       tool,
       COUNT(*)                                            AS calls,
       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
       MAX(ts)                                             AS last_used,
       MIN(ts)                                             AS first_used,
       COUNT(DISTINCT project)                             AS projects,
       COUNT(DISTINCT session_id)                          AS sessions
FROM mcp_usage
GROUP BY server, tool;

DROP VIEW IF EXISTS v_plugins;
CREATE VIEW v_plugins AS
SELECT plugin,
       marketplace,
       COUNT(*)                                            AS uses,
       MAX(ts)                                             AS last_used,
       COUNT(DISTINCT session_id)                          AS sessions
FROM plugin_usage
GROUP BY plugin;
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """Drop all data (keeps the schema). Used by ``sync --full``."""
    for table in (
        "plugin_usage",
        "commands",
        "mcp_usage",
        "agent_usage",
        "skill_usage",
        "tool_calls",
        "sessions",
        "inventory",
        "sync_state",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


# --- shared queries (used by both the CLI and the TUI) ---------------------


def q_tools(conn, limit=None):
    sql = "SELECT * FROM v_tools ORDER BY calls DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def q_skills(conn, limit=None):
    sql = "SELECT * FROM v_skills ORDER BY calls DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def q_agents(conn, limit=None):
    sql = "SELECT * FROM v_agents ORDER BY calls DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def q_mcp(conn, limit=None):
    sql = "SELECT * FROM v_mcp ORDER BY calls DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def q_plugins(conn):
    """Installed plugins with their attributed usage counts."""
    return conn.execute(
        "SELECT i.name AS plugin, i.version, "
        "  COALESCE(u.uses, 0) AS uses, u.last_used, "
        "  (SELECT COUNT(*) FROM inventory x"
        "    WHERE x.owner_plugin = i.name AND x.kind = 'skill')  AS skills,"
        "  (SELECT COUNT(*) FROM inventory x"
        "    WHERE x.owner_plugin = i.name AND x.kind = 'agent')  AS agents,"
        "  (SELECT COUNT(*) FROM inventory x"
        "    WHERE x.owner_plugin = i.name AND x.kind = 'command') AS commands"
        " FROM inventory i"
        " LEFT JOIN v_plugins u ON u.plugin = i.name"
        " WHERE i.kind = 'plugin'"
        " ORDER BY uses DESC, i.name"
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
        "total_tokens": one("SELECT COALESCE(SUM(tokens),0) FROM sessions"),
        "inventory": one("SELECT COUNT(*) FROM inventory"),
    }


"""cbut_db — storage layer for the CodeBuddy usage tracker.

Defines the SQLite schema (tables + views) and small helpers shared by the
indexer (cbut-sync.py), the headless CLI (cbut-stats.py) and the TUI
(cbut-tui.py).

The tracker is a *passive reader*: it never writes into CodeBuddy's own
directories. It only reads ``~/.codebuddy`` and writes this one database.

Standard library only.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 6

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


def load_sync():
    """Import ``cbut-sync.py`` — a hyphenated name cannot be imported normally.

    The TUI's sync worker, ``cbut format`` and the tests all need the parser as
    a module, so the loader lives here instead of being copied a third time.
    """
    name = "cbut_sync_embedded"
    mod = sys.modules.get(name)
    if mod is None:
        path = Path(__file__).with_name("cbut-sync.py")
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


# --- schema ----------------------------------------------------------------

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- A session is identified by its id and located by its cwd. There is
-- deliberately no `title` column: the AI-generated session title is prose
-- derived from the conversation, so storing it would break the metadata-only
-- promise. Schema v5 dropped the column that used to hold it.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    project     TEXT,             -- decoded cwd the session ran in
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

-- `agent_type` is the Agent tool's own identifier (and the documented default
-- when the caller omits it). There is no `description` column: that argument is
-- free text written by the model, and schema v5 dropped the column that used to
-- store it. Nothing ever read it — it only sat in the database and in
-- `cbut export`.
CREATE TABLE IF NOT EXISTS agent_usage (
    call_id     TEXT PRIMARY KEY,
    agent_type  TEXT,
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

-- Records the indexer saw but could not use: an unknown ``type`` discriminator
-- (the usual sign CodeBuddy's log format changed) or a complete line that is not
-- JSON. Accumulated, because a run that silently claims nothing still prints
-- "done" — the count is what turns a format change into something visible.
CREATE TABLE IF NOT EXISTS unparsed (
    reason TEXT PRIMARY KEY,       -- "type:<name>" | "type:<missing>" | "unparseable_line"
    count  INTEGER NOT NULL DEFAULT 0
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


# --- snapshots -------------------------------------------------------------
#
# A migration or a ``--full`` rebuild deletes indexed state, and the only way
# back is re-reading CodeBuddy's transcripts — which the host may rotate away at
# any time. So every destructive path takes a snapshot first.
#
# Managed snapshots live in their own ``backups/`` directory. Hand-made
# ``usage.db.bak-*`` files beside the database are listed as restore sources but
# never pruned: this code did not create them and does not get to delete them.

BACKUP_KEEP = 5
BACKUP_GLOB = "usage.db.bak-*"          # the shape a person makes by hand
BACKUP_PATTERN = "usage-*.db"           # the shape this module makes


def db_file(conn) -> Path | None:
    """The file behind an open connection (``None`` for an in-memory one)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def backup_dir(db_path=None) -> Path:
    return Path(db_path or DB_PATH).parent / "backups"


def snapshot(conn, reason: str = "manual", db_path=None) -> Path:
    """Copy the live database aside using SQLite's online backup API.

    ``conn.backup`` rather than a file copy: with WAL on, the newest committed
    frames can still be sitting in ``-wal``, so a copied main file is a stale
    database — exactly the thing you need the snapshot to not be.
    """
    src = Path(db_path) if db_path else db_file(conn)
    dest_dir = backup_dir(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    base = f"usage-{now:%Y%m%d-%H%M%S}"
    dest = dest_dir / f"{base}-{reason}.db"
    n = 1
    while dest.exists():            # never overwrite an earlier snapshot
        dest = dest_dir / f"{base}-{reason}-{n}.db"
        n += 1
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    prune_backups(src)
    return dest


def prune_backups(db_path=None, keep: int = BACKUP_KEEP) -> list[Path]:
    """Delete managed snapshots beyond the newest ``keep``.

    Only files matching :data:`BACKUP_PATTERN` inside the managed directory are
    eligible. Ordered by mtime, not by name: several snapshots can land in the
    same second, and the ``-1``/``-2`` suffixes that disambiguate them do not
    sort in creation order — trusting them would delete the newer file.
    """
    d = backup_dir(db_path)
    if not d.is_dir():
        return []
    newest_first = sorted(d.glob(BACKUP_PATTERN),
                          key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    stale = newest_first[keep:]
    for path in stale:
        path.unlink()
    return stale


def list_backups(db_path=None) -> list[Path]:
    """Everything worth restoring: managed snapshots plus hand-made copies."""
    src = Path(db_path or DB_PATH)
    out: list[Path] = []
    d = backup_dir(src)
    if d.is_dir():
        out += list(d.glob(BACKUP_PATTERN))
    if src.parent.is_dir():
        out += list(src.parent.glob(BACKUP_GLOB))
    return sorted(out, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def restore(backup_path, db_path=None, reason: str = "pre-restore") -> Path:
    """Replace the database with a snapshot, after snapshotting what is live.

    A wrong restore should be undoable, so the current database is copied first.
    The TUI must not be running: it holds the file open and would keep writing
    into the copy you are replacing.
    """
    src = Path(db_path or DB_PATH)
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f"no such backup: {backup_path}")
    if src.exists():
        conn = sqlite3.connect(src)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            snapshot(conn, reason=reason, db_path=src)
        finally:
            conn.close()
    ensure_dir(src)
    source = sqlite3.connect(backup_path)
    target = sqlite3.connect(src)
    try:
        target.execute("PRAGMA busy_timeout = 5000")
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


# Columns added after the original schema, kept here so the migration and the
# CREATE TABLE stay in sync. A test (test_schema_matches_migrations) compares
# what a fresh database gets against what an old one ends up with, so adding a
# column to CREATE TABLE without listing it here fails loudly instead of leaving
# upgraded databases permanently short of it.
# v3: the three prompt_cache_* cache parts. v4: provider_total_tokens.
_NEW_MODEL_COLUMNS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_write_tokens",
    "provider_total_tokens",
)

# Columns the current schema no longer has. v5 removed both of these because
# they held prose: an AI-generated session title, and the Agent tool's own
# `description` argument.
_DROPPED_COLUMNS = (
    ("sessions", "title"),
    ("agent_usage", "description"),
)


def stored_version(conn: sqlite3.Connection) -> int:
    """The schema version this database was last written at.

    ``0`` when there is no usable value — which is what a pre-v4 database looks
    like, and the right answer: it is as old as it can get, so every migration
    below runs against it.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _migrate_model_responses(conn: sqlite3.Connection) -> bool:
    """Add v3/v4 columns to a pre-existing ``model_responses`` table.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so check
    ``PRAGMA table_info`` first. When any column is actually added (i.e. an old
    database is being upgraded), clear ``sync_state`` so the next incremental
    ``cbut sync`` re-reads the transcripts and backfills the new columns via the
    existing messageId UPSERT — row counts stay stable. ``sessions`` is cleared
    too: its ``tokens``/``duration_ms`` are *accumulated* from ``turn-metrics``,
    so a full re-read would otherwise add every turn's delta a second time —
    and ``unparsed`` for the same reason. Fresh databases already have the
    columns, so this is a no-op for them.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
    added = [c for c in _NEW_MODEL_COLUMNS if c not in existing]
    if not added:
        return False
    for col in added:
        conn.execute(f"ALTER TABLE model_responses ADD COLUMN {col} INTEGER")
    conn.execute("DELETE FROM sync_state")
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM unparsed")
    return True


def _drop_prose_columns(conn: sqlite3.Connection) -> bool:
    """v5: remove the two columns that stored prose.

    Only drops what is actually present, so a database created by the current
    schema is untouched. Nothing is re-indexed: no query ever read these columns
    (only ``cbut export`` passed them through), and every remaining row is still
    correct.
    """
    present = [(table, col) for table, col in _DROPPED_COLUMNS
               if col in {r[1] for r in conn.execute(
                   f"PRAGMA table_info({table})")}]
    if not present:
        return False
    for table, col in present:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    return True


DEAD_SESSION_INDEXES = ("idx_tool_session", "idx_model_resp_session")


def _drop_dead_session_indexes(conn: sqlite3.Connection) -> bool:
    """v6: remove the two indexes that serve no query.

    Both exist to answer ``WHERE session_id = ?``, and no query in this file has one
    (``test_no_query_filters_by_session_id`` keeps that true). They are paid for on every
    insert — ≈ 2.2 MiB of a ≈ 15 MiB database — and are the only indexes that earn nothing.
    Dropping them costs no information: an index is derived state, and rebuilding one is
    a single ``CREATE INDEX`` if a session-keyed screen ever arrives.
    """
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    dead = [name for name in DEAD_SESSION_INDEXES if name in present]
    if not dead:
        return False
    for name in dead:
        conn.execute(f"DROP INDEX {name}")
    return True


MIGRATIONS = (
    ("model_responses cache and total columns", _migrate_model_responses),
    ("prose columns removed", _drop_prose_columns),
    ("dead session indexes dropped", _drop_dead_session_indexes),
)


def _has_indexed_state(conn: sqlite3.Connection) -> bool:
    """Whether a snapshot would preserve anything at all.

    ``sync_state``, ``tool_calls`` and ``model_responses`` are exactly what the
    upgrade clears and rebuilds. A freshly created database has none of them —
    and its ``meta`` table has no version row either, so it otherwise looks like a
    database from version 0. Without this test every first launch (and every test
    run) would write a snapshot of an empty database into ``backups/``.
    """
    for table in ("sync_state", "tool_calls", "model_responses"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return True
    return False


def ensure_schema(conn: sqlite3.Connection) -> str:
    """Create or upgrade the schema. Returns a status, never raises on drift.

    ``"newer"`` means the database was written by a future build of this tool and
    was deliberately left alone: ``CREATE TABLE IF NOT EXISTS`` is additive, but
    the migrations above clear ``sync_state`` and ``sessions`` and would destroy
    state this version of the code cannot interpret.
    """
    conn.executescript(SCHEMA_SQL)
    stored = stored_version(conn)
    if stored > SCHEMA_VERSION:
        conn.commit()
        return "newer"
    if stored < SCHEMA_VERSION and _has_indexed_state(conn):
        # One snapshot for the whole upgrade, taken before anything is modified
        # and after a commit. SQLite's backup API cannot read a source that is
        # holding its own uncommitted writes, so a per-step snapshot would
        # deadlock the moment a migration spans two steps.
        conn.commit()
        snapshot(conn, reason="pre-migration")
    changed = [name for name, step in MIGRATIONS if step(conn)]
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return "migrated" if changed else "current"


def migrate(db_path: Path | str = DB_PATH) -> str:
    """Best-effort upgrade of the tracker DB to the current schema.

    Returns a status rather than a boolean: ``"current"``, ``"migrated"``,
    ``"newer"`` (a database from a future build — not touched), or
    ``"failed: <sqlite message>"``. Callers used to see ``False`` for all three
    of the last cases, which let a locked or damaged file wear the same face as a
    successful no-op.
    """
    try:
        conn = open_db(db_path)
        try:
            return ensure_schema(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"failed: {exc}"


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
        "unparsed",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def note_unparsed(conn: sqlite3.Connection, reason: str, n: int) -> None:
    """Add ``n`` to the count of records the indexer could not use.

    Called once per reason per file, not per record: a format change can make
    tens of thousands of records unknown in one run, and one statement each
    would cost more than the parsing it is reporting on.
    """
    conn.execute(
        "INSERT INTO unparsed(reason, count) VALUES(?,?)"
        " ON CONFLICT(reason) DO UPDATE SET count = count + excluded.count",
        (reason, n),
    )


def q_unparsed(conn: sqlite3.Connection):
    """Every reason records were skipped for, most frequent first."""
    return conn.execute(
        "SELECT reason, count FROM unparsed ORDER BY count DESC, reason").fetchall()


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
        # Records no handler claimed. Non-zero after a CodeBuddy upgrade means
        # the format moved under us, not that the user stopped using a tool.
        "unparsed_records": one("SELECT COALESCE(SUM(count),0) FROM unparsed"),
        "unparsed_kinds": one("SELECT COUNT(*) FROM unparsed"),
    }


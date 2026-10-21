"""test_indexes.py — no index this schema declares may be one nothing reaches.

An index is not free: it is written on every insert, it is copy-on-write for every
`UPDATE`, and it is half its own size in the file, for the rest of the database's
life. The only thing that buys any of that back is a statement that reads through it.
This schema has already been cut twice for exactly this reason — the two session
indexes (v6) and `idx_tool_cat` (v8) — and both times the cut was found by a
measurement nobody could repeat: the statements the suite really executes, captured
with `set_trace_callback` and re-planned with `EXPLAIN QUERY PLAN`. The v8 docstring
records the numbers and then adds "established against the 8,339 statements the suite
really executes … not against a regex over source, because a grep for `WHERE category`
cannot see through an f-string". That is the right method with the wrong lifetime: a
reading taken by hand guards nothing after the commit that took it.

This file is that method as a permanent check. The rule is general — **every index
`TABLES_SQL` declares must be named by the plan of a statement this build actually
runs** — and the statements are not read out of the source, they are *run*: every
report command in `cbut-stats.py` and every public `q_*` in `cbut_db.py`, called
through its own signature (which is what the TUI calls). `set_trace_callback` records
what ran; `EXPLAIN QUERY PLAN` then says which index each statement can reach.

Reachability is necessary but not sufficient, and the v9 cut is the case that says so:
`idx_model_resp_model` **was** named by 3 of the 87 plans — one equality lookup and two
full scans, which the wider key serves identically — so the reachability rule passes
it. The second rule is structural and needs no rows: an index whose key is a **strict
prefix** of another declared index's key on the same table can never win a plan,
because the wider index answers every lookup the narrower one can. `PRAGMA index_info`
reads the keys from the schema `ensure_schema` builds, so neither rule parses DDL.

Two properties make it exact rather than approximate:

* **Plans do not depend on rows, so nothing has to be sized.** Nothing in this build
  runs `ANALYZE`, so there is no `sqlite_stat1` for the planner to read and a plan is
  a function of the schema alone (measured: `SELECT category, COUNT(*) FROM tool_calls
  GROUP BY category` picks the covering index on a table with no rows in it, and does
  the same on the real 37,345-row table). A guard that needed realistic row counts is
  a guard nobody runs on a laptop.
* **A statement is skipped by keyword, never by a failure it caused.** `PRAGMA`, the
  transaction statements and the DDL of `SCHEMA_SQL` have no plan to read; anything
  else that fails to plan fails the test with the statement in the message. A guard
  that swallowed its own errors would report "nothing unreachable" on the day its own
  parser broke.

**Scope, stated honestly.** The corpus is the *read* surface a person runs: the report
CLI and the query functions the TUI calls. The indexer's own statements are not in it,
because building a fixture CodeBuddy tree is `test_sync.py`'s job and not this check's.
The direction of that error is the safe one: a statement only the indexer runs can make
this check *report an index that is in fact reached* (a false alarm a maintainer resolves
by reading this docstring), never make it pass silently on a dead one. It was checked
both ways on 2026-10-17 for the index that got cut here — `idx_model_resp_model` is
named in 0 of the 48 distinct statements the report surface runs and in 0 of the 1,961
statements one index run over the real host produced.

Standard library only, like every other module here: `unittest` discovers this file and
so does `pytest`.
"""

import contextlib
import importlib.util
import inspect
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_stats_indexes",
                                               SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

# `_DECLARED_INDEXES` does not exist in cbut_db the way it does in the sibling ocut
# repository — this schema's indexes are whatever the DDL says, so they are read out
# of `TABLES_SQL` here rather than from a second list that could drift from it. The
# `ux_` prefix is included: a unique index is an index, and it costs the same insert.
DECLARED_INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)")
DECLARED_INDEXES = tuple(DECLARED_INDEX.findall(db.TABLES_SQL))
# The names a plan can name. `sqlite_autoindex_*` is a primary key's own structure and
# is not declared above, so it is deliberately not matched.
INDEX_IN_PLAN = re.compile(r"\b(?:idx|ux)_\w+")

# The statements a plan cannot be read from: SQLite's knobs, the transaction
# boundaries, and the DDL `ensure_schema` runs.
NO_PLAN = ("PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
           "CREATE", "DROP", "ALTER", "VACUUM", "ANALYZE", "EXPLAIN")
PLANNABLE = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE")

# Values for the `q_*` parameters, so every query function can be called the way the
# TUI calls it without this file keeping a list of the functions themselves. A query
# function that grows a parameter this map does not have fails the check below instead
# of being quietly skipped.
QUERY_ARGUMENTS = {
    "limit": 5, "start_ts": None, "end_ts": None, "kind": "tool", "name": "bash",
    "model": "model-x", "provider": "builtin",
    "plugin": "some-plugin", "server": "some-server",
    "offset": 0, "key": "call-1",
    # `None` for the whitelisted sort key: `q_usage_model_stats` falls back to its own
    # default for an unknown value, so the fallback's ORDER BY is what gets planned.
    "order_by": None,
}
# A query that leaves ts unconstrained can reach a different index than the same query
# with a window, and the tabs only ever ask the windowed form.
WINDOW = {"start_ts": 0, "end_ts": 2**62}


def placeholders(sql: str) -> int:
    """How many bindings the statement needs, ignoring `?` inside a literal."""
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    return stripped.count("?")


def run_statements(path: Path):
    """Every statement the packaged code runs on its own database, as it runs it.

    The patch is `db.open_db` and not a list of call sites, so this covers the report
    CLI and the query functions through the same door production uses — and it covers a
    *new* caller without this file being edited.

    `CODEBUDDY_DIR` and `CONFIG_PATH` are pinned into the temp directory for the same
    reason `test_stats.py` pins them: `cbut health` reads the transcript tree and the
    settings file, and reading the real ones would make this a measurement of this
    machine (`_hermetic.py` refuses it outright).
    """
    seen: list[str] = []
    real_open = db.open_db
    empty_host = path.parent / "codebuddy"
    (empty_host / "projects").mkdir(parents=True, exist_ok=True)

    def recording(*args, **kwargs):
        opened = real_open(*args, **kwargs)
        opened.set_trace_callback(seen.append)
        return opened

    with mock.patch.object(db, "open_db", recording), \
            mock.patch.object(db, "CODEBUDDY_DIR", empty_host), \
            mock.patch.object(db, "CONFIG_PATH", path.parent / "no-such-config.toml"):
        for name in sorted(stats.COMMANDS):
            argv = ["show", "tool", "bash"] if name == "show" else [name]
            with contextlib.redirect_stdout(io.StringIO()):
                stats.main(["--db", str(path), *argv])
        call_every_query(path)
    return seen


def call_every_query(path: Path) -> None:
    """Call every public `q_*` in `cbut_db`, all-time and windowed.

    The parameter names come from `inspect.signature`, so a query function added to the
    module is planned without this file being touched; a *parameter* with no value in
    `QUERY_ARGUMENTS` is a hard failure, because a query this check cannot call is a
    query whose index reachability is not being checked.
    """
    conn = db.open_db(path, readonly=True)
    try:
        for name, fn in sorted(vars(db).items()):
            if not name.startswith("q_") or not inspect.isfunction(fn):
                continue
            parameters = list(inspect.signature(fn).parameters)[1:]
            unknown = sorted(set(parameters) - set(QUERY_ARGUMENTS))
            assert not unknown, (
                f"{name} takes {unknown}, which this check has no value for: add "
                "them to QUERY_ARGUMENTS so its statement is still planned")
            for overrides in ({}, WINDOW):
                kwargs = {p: QUERY_ARGUMENTS[p] for p in parameters}
                kwargs.update({k: v for k, v in overrides.items() if k in kwargs})
                fn(conn, **kwargs)
    finally:
        conn.close()


def plan_index(statements, conn):
    """``(indexes named, statements planned)`` for one corpus."""
    named: set[str] = set()
    planned = 0
    for sql in sorted(set(statements)):
        text = " ".join(sql.split())
        if not text:
            continue
        keyword = text.split()[0].upper()
        if keyword in NO_PLAN or keyword not in PLANNABLE:
            continue
        try:
            steps = conn.execute("EXPLAIN QUERY PLAN " + sql,
                                 [None] * placeholders(sql)).fetchall()
        except sqlite3.Error as exc:
            raise AssertionError(
                f"a statement this build runs could not be planned ({exc}): {text}"
            ) from exc
        planned += 1
        for step in steps:
            named.update(INDEX_IN_PLAN.findall(step["detail"]))
    return named, planned


def unreachable(named, declared):
    """The declared indexes no plan named."""
    return sorted(set(declared) - set(named))


def shape(conn, names):
    """Each declared index's table and key columns, read from the schema it built.

    Not parsed out of `TABLES_SQL`: a regex over DDL is a second reader of it, and the
    one that drifts. `PRAGMA index_info` answers what SQLite actually created.
    """
    out = {}
    for name in names:
        cols = tuple(r[2] or "" for r in conn.execute(f"PRAGMA index_info({name})"))
        table = conn.execute("SELECT tbl_name FROM sqlite_master WHERE name = ?",
                             (name,)).fetchone()
        out[name] = (table[0] if table else "", cols)
    return out


def prefix_duplicates(shape_map):
    """Indexes whose key is a strict prefix of another index's key on the same table.

    Reachability cannot see these, which is why they need a rule of their own: put
    `idx_model_resp_model` back and 3 of the 87 plans name it — one equality lookup and
    two full scans, each served just as well by `idx_model_resp_model_ts`, which leads
    with the same column (timed: the cost does not move). A wider index answers every
    lookup the narrower one can, so the narrower is a write cost with no plan it wins.
    """
    return sorted(
        name for name, (table, cols) in shape_map.items()
        if any(other != name and other_table == table and len(cols) < len(other_cols)
               and other_cols[:len(cols)] == cols
               for other, (other_table, other_cols) in shape_map.items()))


class IndexReachabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One throwaway database and one corpus, shared by both cases below."""
        cls._tmp = tempfile.TemporaryDirectory()  # noqa: SIM115 -- closed in tearDownClass
        cls.path = Path(cls._tmp.name) / "usage.db"
        conn = db.open_db(cls.path)
        db.ensure_schema(conn)
        conn.close()
        cls.statements = run_statements(cls.path)
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        db.ensure_schema(cls.conn)
        cls.named, cls.planned = plan_index(cls.statements, cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def test_no_declared_index_is_unreachable_from_a_statement_the_code_runs(self):
        """An index nothing reads is a write cost with a comment attached to it."""
        # A corpus that came out empty would report every index as unreachable, so the
        # floor is not decoration: it is what separates "nothing is dead" from "nothing
        # was captured". `idx_tool_name` is asserted *reached* for the same reason.
        self.assertGreaterEqual(
            self.planned, 40,
            f"only {self.planned} statements were planned: the corpus is too small for "
            "this check to mean anything")
        self.assertIn(
            "idx_tool_name", self.named,
            "`idx_tool_name` is reached by the tools report; if the plans no longer name "
            "it, this check is reading something other than the planner's answer")
        missing = unreachable(self.named, DECLARED_INDEXES)
        self.assertEqual(
            missing, [],
            f"{missing} is declared by TABLES_SQL and named in none of the "
            f"{self.planned} plans this build's own statements produce. An index no "
            "statement reads is written on every insert and buys nothing — either drop "
            "it from TABLES_SQL with a migration that removes it from existing files, "
            "or write the query that reads it.")

    def test_no_declared_index_is_a_prefix_of_a_wider_one(self):
        """The rule the reachability check above cannot be, and the one that cut last.

        The v9 cut (`idx_model_resp_model`) was named by 3 of the 87 plans, so the case
        above would have passed it. What finds it is this: `(model)` is `(model, ts)`
        minus a column, and without `ANALYZE` the planner cannot prefer the narrower key
        for a plan the wider one also serves.
        """
        shape_map = shape(self.conn, DECLARED_INDEXES)
        self.assertEqual(
            sorted(n for n, (_, cols) in shape_map.items() if not cols), [],
            "a declared index that `ensure_schema` does not build has an unknown key, "
            "so this check cannot see the index that duplicates it")
        self.assertEqual(
            prefix_duplicates(shape_map), [],
            f"{prefix_duplicates(shape_map)} has a key that is a strict prefix of "
            "another declared index's key on the same table. A wider index serves every "
            "lookup the narrower one can, so drop it from `TABLES_SQL` with a migration "
            "that removes it from existing files.")

    def test_a_prefix_duplicate_is_reported_and_its_wider_key_is_not(self):
        """The guard, provoked with a name instead of with the repository."""
        planted = {
            "idx_a": ("t", ("model",)),
            "idx_b": ("t", ("model", "ts")),
            "idx_c": ("t", ("tool",)),
            "idx_d": ("other", ("model",)),
            "idx_e": ("t", ("ts", "model")),
        }
        self.assertEqual(
            prefix_duplicates(planted), ["idx_a"],
            "a narrower key must be reported, and the wider key it duplicates, the same "
            "key on a different table and a different column order must not be")

    def test_an_index_that_never_appears_in_a_plan_is_reported(self):
        """The guard, provoked with a name instead of with the repository.

        Editing `TABLES_SQL` to add a dead index would prove the same thing and leave a
        broken schema behind for the length of the run (AGENTS.md: a guard is provoked
        with a string, not with the repository). Handing `unreachable` a declared name
        the statements cannot reach is the whole check: it must come back, and it must
        come back alone.
        """
        planted = "idx_nothing_reads"
        self.assertNotIn(planted, self.named, "the planted name reached a plan")
        self.assertEqual(
            unreachable(self.named, (*DECLARED_INDEXES, planted)), [planted],
            "a declared index no plan names must be reported, and only it")
        self.assertEqual(
            unreachable(self.named, DECLARED_INDEXES), [],
            "the same call with the real list must be empty, or the provocation above "
            "proves nothing about the real list")


if __name__ == "__main__":
    unittest.main()

"""test_stats.py — the headless report CLI (`cbut stats …`).

`cbut-stats.py` is what CI and a shell reach for when there is no terminal to
render the TUI into, so its output *is* its contract. Until now only two of its
paths were checked anywhere: `test_usage.py` drives `main(["stats"])` through the
stale-schema refusal and `main(["health"])` once. Every other subcommand — the
per-category tables, the model/token report, `show`/`recent`/`inventory`/`export`,
the backup/restore pair and `format` — was exercised by hand and by memory.

These tests drive the real entry point: `stats.main(["--db", <temp>, <cmd>])`,
with stdout and stderr captured. The argument parser, the read-only connection,
the dispatch table and each command's formatting all run as shipped, so a rename
or a dropped column fails here instead of in someone's terminal.

Every database is built in a temp directory and every command is pointed at it
with `--db`; nothing touches the real `usage.db` or `~/.codebuddy`. `health` reads
the settings file and the transcript tree, so those two paths are pinned into the
temp directory as well.
"""

import contextlib
import importlib.util
import io
import json
import os
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

_spec = importlib.util.spec_from_file_location("cbut_stats", SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

# One fixed instant so `ts()` renders a known string. 2026-10-01 12:00 local.
TS = 1_790_000_000_000


def run(*argv):
    """Invoke the CLI exactly as a shell would, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = stats.main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def seed_db(path):
    """A database with one row in every table the reports read.

    Deliberately not minimal: each report is asserted on a value that only exists
    if its own table was queried, so a command that silently read the wrong table
    fails rather than printing an empty-but-plausible table.
    """
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO sessions(session_id, project, started_at, ended_at,"
                 " model, tokens, duration_ms) VALUES('s1','/proj',?,?, 'claude',"
                 " 500, 9000)", (TS, TS + 1000))
    conn.execute("INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
                 " category, ts, duration_ms, status, model)"
                 " VALUES('c1','s1','/proj','Read','builtin',?,1200,'completed','claude')",
                 (TS,))
    conn.execute("INSERT INTO skill_usage(call_id, skill, has_args, plugin, session_id,"
                 " project, ts, status, duration_ms)"
                 " VALUES('k1','commit',1,'p1','s1','/proj',?,'completed',500)", (TS,))
    conn.execute("INSERT INTO agent_usage(call_id, agent_type, kind, source, session_id,"
                 " project, ts, status, duration_ms)"
                 " VALUES('a1','Explore','internal','tool','s1','/proj',?,'completed',300)",
                 (TS,))
    conn.execute("INSERT INTO mcp_usage(call_id, server, tool, session_id, project, ts,"
                 " status, duration_ms)"
                 " VALUES('m1','context7','query-docs','s1','/proj',?,'completed',40)",
                 (TS,))
    conn.execute("INSERT INTO plugin_usage(plugin, marketplace, kind, target, session_id,"
                 " project, ts) VALUES('p1','mp','skill','commit','s1','/proj',?)", (TS,))
    conn.execute("INSERT INTO commands(command, session_id, project, ts)"
                 " VALUES('compact','s1','/proj',?)", (TS,))
    conn.execute("INSERT INTO inventory(kind, name, owner_plugin, version, path, source)"
                 " VALUES('tool','Read',NULL,'1.0','/bin/read','builtin')")
    conn.execute("INSERT INTO inventory(kind, name, owner_plugin, version, path, source)"
                 " VALUES('skill','commit','p1','2.0','/p/skills/commit','plugin')")
    conn.execute("INSERT INTO inventory(kind, name, owner_plugin, version, path, source)"
                 " VALUES('plugin','p1',NULL,'2.0','/p','plugin')")
    # model_responses: one fully-measured built-in response and one with no usage.
    conn.execute(
        "INSERT INTO model_responses(message_id, model, provider, prompt_tokens,"
        " completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,"
        " prompt_cache_write_tokens, provider_total_tokens, ts, usage_available)"
        " VALUES('r1','claude-sonnet-4','builtin',100,20,30,60,10,120,?,1)", (TS,))
    conn.execute(
        "INSERT INTO model_responses(message_id, model, provider, prompt_tokens,"
        " completion_tokens, ts, usage_available)"
        " VALUES('r2','gpt-x','api.vendor.example',50,10,?,0)", (TS + 1,))
    conn.commit()
    conn.close()


class StatsCliTest(unittest.TestCase):
    """The overview and the per-category tables."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        seed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats_prints_every_section(self):
        rc, out, err = run("--db", self.db_path, "stats")
        self.assertEqual(rc, 0, err)
        self.assertIn("CodeBuddy usage — overview", out)
        for section in ("Top tools", "Skills", "Agents", "Plugins", "MCP"):
            self.assertIn(section, out, f"the overview lost its {section} section")
        # The one tool call took 1200 ms, which the report must render as seconds.
        self.assertIn("1.2s", out, "the avg column no longer scales ms to s")

    def test_tools_table_lists_tool_counts(self):
        rc, out, err = run("--db", self.db_path, "tools")
        self.assertEqual(rc, 0, err)
        self.assertIn("Read", out)
        self.assertIn("1.2s", out)

    def test_skills_table_lists_skills(self):
        rc, out, err = run("--db", self.db_path, "skills")
        self.assertEqual(rc, 0, err)
        self.assertIn("commit", out)

    def test_agents_table_lists_agents(self):
        rc, out, err = run("--db", self.db_path, "agents")
        self.assertEqual(rc, 0, err)
        self.assertIn("Explore", out)

    def test_mcp_table_lists_server_and_tool(self):
        rc, out, err = run("--db", self.db_path, "mcp")
        self.assertEqual(rc, 0, err)
        self.assertIn("context7", out)
        self.assertIn("query-docs", out)

    def test_empty_reports_say_none_not_a_bare_header(self):
        # `table(...) or "(none)"` never reached the fallback: table() always
        # returns a header and a separator line, so an empty report printed a
        # bare header. The fallback now branches on the rows.
        empty = Path(self.tmp.name) / "empty.db"
        conn = db.open_db(empty)
        db.ensure_schema(conn)
        conn.close()
        for cmd in ("mcp", "skills", "agents"):
            rc, out, err = run("--db", empty, cmd)
            self.assertEqual(rc, 0, err)
            self.assertIn("(none)", out, f"cbut {cmd} printed a bare header")
            self.assertNotIn("---", out, f"cbut {cmd} still drew an empty table")

    def test_plugins_table_lists_attributed_usage(self):
        rc, out, err = run("--db", self.db_path, "plugins")
        self.assertEqual(rc, 0, err)
        self.assertIn("p1", out)

    def test_show_prints_recent_calls_for_one_entity(self):
        rc, out, err = run("--db", self.db_path, "show", "tool", "Read")
        self.assertEqual(rc, 0, err)
        self.assertIn("tool Read", out)
        self.assertIn("/proj", out)

    def test_show_says_when_nothing_recorded(self):
        rc, out, err = run("--db", self.db_path, "show", "tool", "NoSuchTool")
        self.assertEqual(rc, 0, err)
        self.assertIn("no recorded calls for", out)

    def test_recent_lists_newest_calls(self):
        rc, out, err = run("--db", self.db_path, "recent")
        self.assertEqual(rc, 0, err)
        self.assertIn("Read", out)

    def test_inventory_lists_items_and_filters_by_kind(self):
        rc, out, err = run("--db", self.db_path, "inventory")
        self.assertEqual(rc, 0, err)
        self.assertIn("Read", out)
        self.assertIn("p1", out)

        rc, out, err = run("--db", self.db_path, "inventory", "--kind", "plugin")
        self.assertEqual(rc, 0, err)
        self.assertIn("p1", out)
        self.assertNotIn("Read", out, "--kind plugin must filter the tool out")

    def test_export_dumps_every_table_as_json(self):
        rc, out, err = run("--db", self.db_path, "export")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)          # a traceback would not parse
        for key in ("v_tools", "v_skills", "v_agents", "v_mcp", "v_plugins",
                    "inventory", "model_tokens", "model_responses", "sessions",
                    "overview"):
            self.assertIn(key, data, f"export dropped the {key} table")
        self.assertEqual(data["overview"]["tool_calls"], 1)

    def test_missing_database_is_named_not_traced(self):
        rc, out, err = run("--db", Path(self.tmp.name) / "absent.db", "stats")
        self.assertEqual(rc, 1)
        self.assertIn("no database at", err)
        self.assertNotIn("Traceback", err)

    def test_an_unknown_subcommand_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit) as caught:
            run("--db", self.db_path, "definitely-not-a-command")
        self.assertEqual(caught.exception.code, 2)


class ModelReportTest(unittest.TestCase):
    """`cbut models` — the token table and its narrow-terminal column plan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        seed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_models(self, width):
        size = os.terminal_size((width, 24))
        with mock.patch.object(stats.shutil, "get_terminal_size", return_value=size):
            return run("--db", self.db_path, "models")

    def test_models_table_shows_totals_and_usage_flag(self):
        rc, out, err = self._run_models(100)
        self.assertEqual(rc, 0, err)
        self.assertIn("Model token usage", out)
        self.assertIn("claude-sonnet-4", out)
        self.assertIn("Built-in", out, "the built-in route label did not render")
        self.assertIn("120", out, "the provider total is missing from the table")
        self.assertIn("yes", out)          # r1 carried usage
        self.assertIn("no", out)           # r2 did not

    def test_models_keeps_cache_columns_on_a_wide_terminal(self):
        rc, out, err = self._run_models(100)
        self.assertEqual(rc, 0, err)
        self.assertIn("cache hit", out)
        self.assertIn("cache write", out)

    def test_models_drops_cache_columns_on_a_narrow_terminal(self):
        rc, out, err = self._run_models(50)
        self.assertEqual(rc, 0, err)
        # Only the per-model table is fitted; the "Recent model responses" table
        # below it is a fixed column set and keeps its cache columns.
        head = out.split("Recent model responses")[0]
        self.assertNotIn("cache hit", head)
        self.assertNotIn("cache miss", head)
        self.assertNotIn("cache write", head)
        # The identity and size columns are never dropped.
        for kept in ("model", "provider", "resp", "in", "out"):
            self.assertIn(kept, head)

    def test_models_says_so_when_no_responses_indexed(self):
        empty = Path(self.tmp.name) / "empty.db"
        conn = db.open_db(empty)
        db.ensure_schema(conn)
        conn.close()
        with mock.patch.object(stats.shutil, "get_terminal_size",
                               return_value=os.terminal_size((100, 24))):
            rc, out, err = run("--db", empty, "models")
        self.assertEqual(rc, 0, err)
        self.assertIn("no model responses indexed yet", out)
        self.assertIn("(none)", out, "the empty recent-responses table lost its notice")


class HealthReportTest(unittest.TestCase):
    """`cbut health` — the DB and data-source check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "usage.db"
        self.empty_codebuddy = self.dir / "codebuddy"
        (self.empty_codebuddy / "projects").mkdir(parents=True)
        self.absent_config = self.dir / "no-such-config.toml"
        seed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_health(self, db_path):
        # health reads the settings file and the transcript tree, both of which
        # default to the maintainer's home. Pin them into the temp directory so
        # the assertions describe this test's state, not the machine's.
        with mock.patch.object(db, "CONFIG_PATH", self.absent_config), \
                mock.patch.object(db, "CODEBUDDY_DIR", self.empty_codebuddy):
            return run("--db", db_path, "health")

    def test_health_reports_schema_counts_and_settings(self):
        rc, out, err = self._run_health(self.db_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("cbut health", out)
        self.assertIn("schema version", out)
        self.assertIn(str(db.SCHEMA_VERSION), out)
        self.assertIn("1 tool calls", out)
        self.assertIn("(absent — defaults)", out,
                      "the settings line no longer names a missing file")
        self.assertIn("unparsed       0 — every record type was claimed", out)

    def test_health_flags_unparsed_records(self):
        conn = db.open_db(self.db_path)
        conn.execute("INSERT INTO unparsed(reason, count) VALUES('type:NewThing', 3)")
        conn.commit()
        conn.close()
        rc, out, err = self._run_health(self.db_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNPARSED", out)
        self.assertIn("type:NewThing", out)

    def test_health_splits_the_tally_into_known_and_new(self):
        """The total alone could not be read, which is why the split exists.

        The three prose types this build declines run to tens of thousands of
        records on any real database, so the line was permanent and alarming — and
        a record type nothing here has been taught about looked exactly like it.
        The check here is that the two cases read differently, not that either
        number is pretty.
        """
        conn = db.open_db(self.db_path)
        for reason, n in (("type:reasoning", 22146), ("type:summary", 528)):
            conn.execute("INSERT INTO unparsed(reason, count) VALUES(?,?)",
                         (reason, n))
        conn.commit()
        conn.close()
        rc, out, err = self._run_health(self.db_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("(22674 known, 0 not)", out)
        self.assertNotIn("NEW", out, "a known reason was marked as drift")
        self.assertIn("nothing has moved", out)

        conn = db.open_db(self.db_path)
        conn.execute("INSERT INTO unparsed(reason, count) VALUES('type:watermark', 3)")
        conn.commit()
        conn.close()
        rc, out, err = self._run_health(self.db_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("(22674 known, 3 not)", out)
        self.assertIn("NEW", out)
        self.assertIn("type:watermark", out)
        self.assertIn("the format moving", out)

    def test_health_reports_a_missing_schema_version_table(self):
        # A database so old it has no meta table: the schema-version line must
        # report that in place, not abort the whole report.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE meta")
        conn.commit()
        conn.close()
        rc, out, err = self._run_health(self.db_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("schema version (missing)", out)


class SnapshotCliTest(unittest.TestCase):
    """`cbut backup` / `cbut restore` — the two commands that work on the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        seed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_then_restore_round_trip(self):
        rc, out, err = run("--db", self.db_path, "backup")
        self.assertEqual(rc, 0, err)
        self.assertIn("snapshot", out)

        rc, out, err = run("--db", self.db_path, "restore")
        self.assertEqual(rc, 0, err)
        self.assertIn("backups for", out)
        name = out.splitlines()[1].split()[0]      # the listed snapshot's name

        rc, out, err = run("--db", self.db_path, "restore", name)
        self.assertEqual(rc, 0, err)
        self.assertIn("restored", out)

        # The restored file is a working database with the row still in it.
        conn = db.open_db(self.db_path, readonly=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_restore_without_any_backup_says_so(self):
        rc, out, err = run("--db", self.db_path, "restore")
        self.assertEqual(rc, 1)
        self.assertIn("no backups under", err)

    def test_restore_unknown_name_is_refused(self):
        run("--db", self.db_path, "backup")
        rc, out, err = run("--db", self.db_path, "restore", "usage-19990101-000000-manual.db")
        self.assertEqual(rc, 1)
        self.assertIn("no backup named", err)

    def test_backup_refuses_when_the_database_is_missing(self):
        rc, out, err = run("--db", Path(self.tmp.name) / "absent.db", "backup")
        self.assertEqual(rc, 1)
        self.assertIn("no database at", err)


class FormatCliTest(unittest.TestCase):
    """`cbut format` — the generated CodeBuddy format-dependency surface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "doc.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_format_prints_the_registry_surface(self):
        # No --db and no database: format reads the code, not the store.
        rc, out, err = run("format")
        self.assertEqual(rc, 0, err)
        self.assertIn("Record types a handler claims", out)
        self.assertIn("On-disk layout", out)

    def test_replace_generated_block_rewrites_only_the_marked_region(self):
        self.doc.write_text(
            "before\n"
            f"{stats.FORMAT_BEGIN}\nold body\n{stats.FORMAT_END}\n"
            "after\n", encoding="utf-8")
        self.assertTrue(stats._replace_generated_block(self.doc, "new body\n"))
        text = self.doc.read_text(encoding="utf-8")
        self.assertIn("new body", text)
        self.assertNotIn("old body", text)
        self.assertIn("before", text)
        self.assertIn("after", text)

    def test_replace_generated_block_refuses_without_markers(self):
        # Refusing beats appending: a generated section dropped into hand-written
        # prose is how a document ends up stating two things at once.
        self.doc.write_text("hand-written prose, no markers\n", encoding="utf-8")
        self.assertFalse(stats._replace_generated_block(self.doc, "new body\n"))
        self.assertEqual(self.doc.read_text(encoding="utf-8"),
                         "hand-written prose, no markers\n")


if __name__ == "__main__":
    unittest.main()

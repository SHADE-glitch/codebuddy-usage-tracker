"""test_privacy.py — the privacy invariants, as executable checks.

The repository promises three things out loud (README.md "Privacy", AGENTS.md
"Hard rules"): nothing leaves the machine, CodeBuddy's own files are never
written, and the database holds metadata only — never prompt/response text and
never a tool argument *value*. Until now those were prose, not tests, so a
regression could only be found by reading the parser.

Two shapes of check are used deliberately:

* the import scan is a **whole-file** assertion, so a future `import urllib`
  fails even if the call site is added somewhere else;
* the storage assertions scan **every text column of every table** rather than
  naming the column that once leaked. A guard that only checks `description`
  would miss the next free-text slot; "no column anywhere may contain this
  string" cannot be satisfied by moving the leak to a different column.

Each scan carries a positive control (an identifier that *must* be in the DB):
a scan over an empty result would pass vacuously, which is the failure mode
these checks exist to prevent.

Standard library only, and no real state is touched: the transcript is a
fixture inside a temp directory.
"""

import ast
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_sync", SCRIPTS / "cbut-sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

PRODUCTION_SCRIPTS = ["cbut_db.py", "cbut-sync.py", "cbut-stats.py", "cbut-tui.py"]

# Anything a usage index has no business importing. Deliberately broader than
# "modules we currently use": HTTP client libraries, mail, FTP, raw sockets and
# process spawning are all named so that adding one is a test failure rather
# than a quiet convention break.
FORBIDDEN_ROOTS = {
    "socket", "ssl", "http", "urllib", "urllib2", "smtplib", "poplib", "imaplib",
    "ftplib", "telnetlib", "nntplib", "socketserver", "asyncio",
    "requests", "httpx", "aiohttp", "urllib3", "websocket", "grpc",
    "subprocess", "multiprocessing", "threading",
}

# Names the production scripts are allowed to import. `textual` is listed for
# cbut-tui.py only (see ALLOWED_BY_FILE): measured over the installed venv, nothing in it
# imports `socket`/`ssl` and its only network-capable source is `textual/demo/`, which
# needs `httpx` — not installed. Either way that is a dependency fact, not a network path
# in this project; this check is about *our* code.
#
# `tomllib` (the settings parser) was added only after its import closure was listed:
# collections, datetime, functools, re, string, sys, types, typing — no network, process
# or thread module anywhere in it.
ALLOWED_ROOTS = {
    "argparse", "datetime", "importlib", "json", "os", "pathlib", "re",
    "shutil", "sqlite3", "sys", "time", "tomllib", "cbut_db", "__future__",
}
ALLOWED_BY_FILE = {"cbut-tui.py": ALLOWED_ROOTS | {"textual"}}

# Sentinel argument *values*. Each sits in a slot the parser genuinely has to
# look at: to name the agent, to name the skill, to find the slash command.
# None of them may reach the database.
SENTINEL_DESC = "SENTINEL-agent-description-must-never-be-stored"
SENTINEL_PROMPT = "SENTINEL-prompt-body-must-never-be-stored"
SENTINEL_TITLE = "SENTINEL-ai-title-must-never-be-stored"
SENTINEL_TITLE2 = "SENTINEL-session-meta-title-must-never-be-stored"
SENTINEL_ARGS = "SENTINEL-skill-args-value-must-never-be-stored"
SENTINEL_URL = "SENTINEL-tool-argument-value-must-never-be-stored"
SENTINEL_BODY = "SENTINEL-message-content-body-must-never-be-stored"
SENTINELS = (SENTINEL_DESC, SENTINEL_PROMPT, SENTINEL_TITLE, SENTINEL_TITLE2,
             SENTINEL_ARGS, SENTINEL_URL, SENTINEL_BODY)

# Identifiers that MUST be stored. Without these the "nothing contains a
# sentinel" assertion could pass on a database that indexed nothing at all.
MUST_STORE = ("Explore", "my-skill", "playwright-cli", "WebFetch")


def content_fixture():
    """One transcript exercising every slot where free text could leak."""
    return [
        # An Agent call: the agent TYPE is metadata, the DESCRIPTION is an
        # argument value, the PROMPT is content.
        {"type": "function_call", "name": "Agent", "callId": "a1",
         "sessionId": "s1", "cwd": "/proj", "timestamp": 1000,
         "providerData": {"model": "m1", "messageId": "msg-a1"},
         "arguments": json.dumps({"subagent_type": "Explore",
                                  "description": SENTINEL_DESC,
                                  "prompt": SENTINEL_PROMPT})},
        # A Skill call: the skill NAME is metadata, its ARGS are a value.
        {"type": "function_call", "name": "Skill", "callId": "a2",
         "sessionId": "s1", "cwd": "/proj", "timestamp": 1100,
         "arguments": json.dumps({"skill": "my-skill", "args": SENTINEL_ARGS})},
        # A builtin tool whose only interesting part is an argument value.
        {"type": "function_call", "name": "WebFetch", "callId": "a3",
         "sessionId": "s1", "cwd": "/proj", "timestamp": 1200,
         "arguments": json.dumps({"url": SENTINEL_URL})},
        {"type": "function_call_result", "callId": "a1",
         "status": "completed", "timestamp": 1500},
        # A slash command buried in message content: only the name inside the
        # marker is metadata. The prose alongside it is content. The block type
        # is input_text because that is what local transcripts use — a fixture
        # built on an assumed shape proves nothing about the real path.
        {"type": "message", "sessionId": "s1", "cwd": "/proj", "timestamp": 1300,
         "providerData": {"model": "m1", "messageId": "msg-m1"},
         "content": [
             {"type": "input_text",
              "text": "please run <command-name>/playwright-cli</command-name>"},
             {"type": "text", "text": SENTINEL_BODY}]},
        # Two title carriers, both prose-derived.
        {"type": "ai-title", "sessionId": "s1", "cwd": "/proj",
         "timestamp": 1400, "aiTitle": SENTINEL_TITLE},
        {"type": "session-meta", "sessionId": "s1", "cwd": "/proj",
         "timestamp": 1410,
         "meta": {"codebuddy.ai/sessionTitle": [{"aiTitle": SENTINEL_TITLE2}]}},
        # turn-metrics: numbers only. tokenDelta is a context metric, never a
        # place text can hide, but keep it in the fixture so the scan sees a
        # populated sessions row.
        {"type": "turn-metrics", "_meta": {"baggage": "codebuddy.session_id=s1"},
         "timestamp": 1450, "tokenDelta": 1234, "durationMs": 500},
    ]


class IndexPrivacyTest(unittest.TestCase):
    """No free-text slot may reach the database, in any column, any table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.tr = Path(self.tmp.name) / "s.jsonl"
        with open(self.tr, "w", encoding="utf-8") as fh:
            for rec in content_fixture():
                fh.write(json.dumps(rec) + "\n")
        sync.index_file(self.conn, self.tr, 0, {"my-skill": "p"}, {}, {})
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def all_text_values(self):
        """Every TEXT value in every user table. Wide on purpose."""
        tables = [r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        self.assertGreaterEqual(
            len(tables), 8,
            f"the scan lost reach: only {len(tables)} tables in the schema")
        values = []
        for table in tables:
            for row in self.conn.execute(f"SELECT * FROM {table}"):
                values.extend(v for v in tuple(row) if isinstance(v, str))
        return values

    def test_fixture_was_actually_indexed(self):
        """Positive control: the scan below has something to look at."""
        values = self.all_text_values()
        joined = "\n".join(values)
        for name in MUST_STORE:
            self.assertIn(name, joined,
                          f"the fixture did not store {name!r}; a leak check on an "
                          "empty database proves nothing")

    def test_no_sentinel_appears_in_any_column_of_any_table(self):
        values = self.all_text_values()
        leaked = []
        for value in values:
            for sentinel in SENTINELS:
                if sentinel in value:
                    leaked.append((sentinel, value[:60]))
        self.assertEqual(leaked, [],
                         "an argument value or message body reached the database")

    def test_tool_argument_values_are_not_serialised_into_a_column(self):
        # The old code json.dumps()ed the whole content blob to regex a command
        # name out of it. A dump would leave JSON punctuation and the sentinel
        # behind, so the command name alone must be what survives.
        cmds = [r["command"] for r in
                self.conn.execute("SELECT command FROM commands")]
        self.assertIn("playwright-cli", cmds)
        for cmd in cmds:
            self.assertNotIn("{", cmd)
            self.assertNotIn(SENTINEL_BODY, cmd)

    def test_no_prose_column_exists_in_a_usage_table(self):
        # Structural, not per-value: a session is identified by id and located
        # by project, an agent call by its type. A column that could hold an
        # AI-generated sentence should not exist at all — otherwise the next
        # free-text slot is only caught if someone thinks to write a sentinel
        # for it.
        prose_shaped = {
            "sessions": {"title", "subject", "summary", "name"},
            "agent_usage": {"description", "prompt", "detail", "summary"},
            "skill_usage": {"args", "arguments", "prompt", "detail"},
            "tool_calls": {"arguments", "args", "input", "output", "result"},
            "commands": {"args", "arguments", "detail"},
        }
        for table, banned in prose_shaped.items():
            cols = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertFalse(
                cols & banned,
                f"{table} has a column that can hold content: {sorted(cols & banned)}")

    def test_has_args_is_a_flag_not_a_value(self):
        row = self.conn.execute(
            "SELECT has_args FROM skill_usage WHERE skill='my-skill'").fetchone()
        self.assertIsNotNone(row, "the skill row is missing")
        self.assertEqual(row["has_args"], 1)


class HostDirNotWrittenTest(unittest.TestCase):
    """CodeBuddy's own tree is read-only to us — proved by hashing it."""

    def test_indexing_leaves_the_input_tree_byte_identical(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name) / "codebuddy"
            projects = root / "projects" / "proj"
            projects.mkdir(parents=True)
            src = projects / "s.jsonl"
            src.write_text("".join(json.dumps(r) + "\n" for r in content_fixture()),
                           encoding="utf-8")
            before = tree_fingerprint(root)

            conn = db.open_db(Path(tmp.name) / "t.db")
            db.ensure_schema(conn)
            sync.index_file(conn, src, 0, {}, {}, {})
            conn.commit()
            conn.close()

            self.assertEqual(before, tree_fingerprint(root),
                             "indexing changed something under the CodeBuddy root")
            # Nothing may be created next to the database either.
            extra = sorted(p.name for p in Path(tmp.name).iterdir()
                           if p.name not in ("codebuddy", "t.db", "t.db-wal",
                                             "t.db-shm"))
            self.assertEqual(extra, [])
        finally:
            tmp.cleanup()


class ImportSurfaceTest(unittest.TestCase):
    """No network path and no process spawning in our own code."""

    def roots_of(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
        return roots

    def test_production_scripts_import_nothing_forbidden(self):
        for name in PRODUCTION_SCRIPTS:
            roots = self.roots_of(SCRIPTS / name)
            hit = roots & FORBIDDEN_ROOTS
            self.assertFalse(hit, f"{name} imports {sorted(hit)} — a network or "
                                  "subprocess path is a privacy invariant break")

    def test_production_scripts_import_only_known_modules(self):
        for name in PRODUCTION_SCRIPTS:
            allowed = ALLOWED_BY_FILE.get(name, ALLOWED_ROOTS)
            unknown = self.roots_of(SCRIPTS / name) - allowed
            self.assertFalse(unknown, f"{name} gained an unlisted import: "
                            f"{sorted(unknown)} — add it to ALLOWED_ROOTS only if "
                            "it is stdlib and cannot reach the network")

    def test_our_scripts_do_not_call_the_network_functions_directly(self):
        # Covers `socket.socket(...)` style use reached through a module we do
        # allow, and any dynamic import built from a string.
        needles = ("urlopen", "create_connection", "socket.socket", "Popen",
                   "system(", "popen(", "getaddrinfo")
        for name in PRODUCTION_SCRIPTS:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, text, f"{name} calls {needle}")


def tree_fingerprint(root: Path):
    """(relpath, size, sha256) for every file under root."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest())
    return out


if __name__ == "__main__":
    unittest.main()

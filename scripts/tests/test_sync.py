"""Tests for the cbut indexer. Standard library only (unittest).

Run:  python3 -m unittest discover -s scripts/tests
"""

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


def write_records(path: Path, recs) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.tr = Path(self.tmp.name) / "s.jsonl"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def index(self, offset=0):
        new = sync.index_file(self.conn, self.tr, offset, {}, {}, {})
        self.conn.commit()
        return new

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # -- MCP: unwrap the deferred-tool wrapper (no real data exists yet) -----

    def test_mcp_unwrapped_from_defer(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "DeferExecuteTool", "callId": "c1",
             "sessionId": "s1", "cwd": "/p", "timestamp": 1000,
             "arguments": json.dumps(
                 {"toolName": "mcp__context7__query-docs", "params": {}})},
            {"type": "function_call_result", "callId": "c1",
             "status": "completed", "timestamp": 1500},
        ])
        self.index()
        row = self.conn.execute("SELECT * FROM mcp_usage").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["server"], "context7")
        self.assertEqual(row["tool"], "query-docs")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["duration_ms"], 500)

    def test_direct_mcp_name(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "mcp__memory__create_entities",
             "callId": "c2", "sessionId": "s1", "cwd": "/p", "timestamp": 10},
        ])
        self.index()
        row = self.conn.execute("SELECT server, tool FROM mcp_usage").fetchone()
        self.assertEqual((row["server"], row["tool"]), ("memory", "create_entities"))

    # -- skills / agents ----------------------------------------------------

    def test_skill_extraction(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "Skill", "callId": "c3",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "arguments": json.dumps({"skill": "commit", "args": "-m x"})},
        ])
        self.index()
        row = self.conn.execute("SELECT * FROM skill_usage").fetchone()
        self.assertEqual(row["skill"], "commit")
        self.assertEqual(row["has_args"], 1)

    def test_agent_subagent_type(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "Agent", "callId": "c4",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "arguments": json.dumps({"subagent_type": "Explore"})},
        ])
        self.index()
        row = self.conn.execute("SELECT * FROM agent_usage").fetchone()
        self.assertEqual(row["agent_type"], "Explore")
        self.assertEqual(row["kind"], "active")

    # -- incremental / idempotent ------------------------------------------

    def test_incremental_offset_and_idempotency(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "Bash", "callId": "b1",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "arguments": json.dumps({"command": "ls"})},
        ])
        off = self.index(0)
        self.assertEqual(self.count("tool_calls"), 1)
        # re-reading from the recorded offset yields nothing new
        off2 = self.index(off)
        self.assertEqual(off2, off)
        self.assertEqual(self.count("tool_calls"), 1)
        # a new appended record is picked up
        with open(self.tr, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "function_call", "name": "Read", "callId": "b2",
                 "sessionId": "s1", "cwd": "/p", "timestamp": 20,
                 "arguments": json.dumps({"file_path": "/x"})}) + "\n")
        self.index(off)
        self.assertEqual(self.count("tool_calls"), 2)

    def test_partial_trailing_line_not_consumed(self):
        with open(self.tr, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "function_call", "name": "Bash", "callId": "b1",
                 "sessionId": "s1", "cwd": "/p", "timestamp": 10}) + "\n")
            fh.write('{"type": "function_call", "name": "Re')  # no newline
        off = self.index(0)
        self.assertEqual(self.count("tool_calls"), 1)
        # offset stops at the last newline, leaving the partial line behind
        self.assertEqual(off, len(json.dumps(
            {"type": "function_call", "name": "Bash", "callId": "b1",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10}) + "\n"))

    # -- inventory dedup ----------------------------------------------------

    def test_inventory_owner_empty_string_not_null(self):
        # inventory PK includes owner_plugin; NULL there would defeat dedup
        sync.scan_inventory(self.conn)
        sync.scan_inventory(self.conn)
        n1 = self.count("inventory")
        sync.scan_inventory(self.conn)
        self.assertEqual(self.count("inventory"), n1)


if __name__ == "__main__":
    unittest.main()

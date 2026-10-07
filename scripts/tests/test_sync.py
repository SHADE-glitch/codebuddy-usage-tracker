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


def model_rec(message_id, raw=None, rtype="function_call",
              model="test-model", session_id="s1", cwd="/proj", ts=1000,
              conv="conv-1"):
    """A synthetic transcript model-response record.

    These are FIXTURES — the token numbers are made up for testing, not real
    CodeBuddy data. ``raw=None`` means the response carried no ``rawUsage``.
    """
    rec = {
        "type": rtype,
        "id": f"{message_id}-rec",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": ts,
        "providerData": {
            "conversationRequestId": conv,
            "messageId": message_id,
            "model": model,
        },
    }
    if raw is not None:
        rec["providerData"]["rawUsage"] = raw
    return rec


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

    # -- model responses: real token usage from rawUsage --------------------
    # All token numbers below are FIXTURES, not real CodeBuddy data.

    def model_row(self, mid):
        return self.conn.execute(
            "SELECT * FROM model_responses WHERE message_id=?", (mid,)
        ).fetchone()

    def test_model_response_full_usage(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 20,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
               "total_tokens": 120, "credit": 3.5}
        write_records(self.tr, [model_rec("m1", raw=raw, ts=111)])
        self.index()
        row = self.model_row("m1")
        self.assertEqual(row["model"], "test-model")
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["completion_tokens"], 20)
        self.assertEqual(row["cache_read_input_tokens"], 0)
        self.assertEqual(row["cache_creation_input_tokens"], 0)
        self.assertEqual(row["usage_available"], 1)
        self.assertIsNone(row["missing"])
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["conversation_request_id"], "conv-1")
        self.assertEqual(row["project"], "/proj")
        self.assertEqual(row["source"], "transcript")

    def test_model_response_without_usage(self):
        # model present, no rawUsage -> record the fact, never fabricate tokens
        write_records(self.tr, [model_rec("m2", raw=None)])
        self.index()
        row = self.model_row("m2")
        self.assertEqual(row["model"], "test-model")
        self.assertEqual(row["usage_available"], 0)
        self.assertIsNone(row["prompt_tokens"])
        self.assertIsNone(row["completion_tokens"])
        self.assertEqual(row["missing"], "rawUsage")

    def test_model_response_partial_usage(self):
        # only prompt/completion present: cache fields absent -> NULL + named
        write_records(self.tr, [model_rec(
            "m3", raw={"prompt_tokens": 50, "completion_tokens": 5})])
        self.index()
        row = self.model_row("m3")
        self.assertEqual(row["prompt_tokens"], 50)
        self.assertIsNone(row["cache_read_input_tokens"])
        self.assertIsNone(row["cache_creation_input_tokens"])
        self.assertEqual(row["usage_available"], 1)
        self.assertIn("cache_read_input_tokens", row["missing"])
        self.assertIn("cache_creation_input_tokens", row["missing"])

    def test_cache_zero_is_a_value_not_missing(self):
        # an explicit 0 is real data -> stored as 0, not treated as missing
        raw = {"prompt_tokens": 1, "completion_tokens": 1,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        write_records(self.tr, [model_rec("m4", raw=raw)])
        self.index()
        row = self.model_row("m4")
        self.assertEqual(row["cache_read_input_tokens"], 0)
        self.assertEqual(row["cache_creation_input_tokens"], 0)
        self.assertIsNone(row["missing"])

    def test_same_message_id_deduped(self):
        # the same response re-emitted: one usage-less copy + one usage copy
        raw = {"prompt_tokens": 7, "completion_tokens": 3,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        write_records(self.tr, [
            model_rec("m5", raw=None, ts=1000, rtype="function_call"),
            model_rec("m5", raw=raw, ts=1000, rtype="message"),
        ])
        self.index()
        self.assertEqual(self.count("model_responses"), 1)
        row = self.model_row("m5")
        self.assertEqual(row["usage_available"], 1)
        self.assertEqual(row["prompt_tokens"], 7)

    def test_rescan_does_not_duplicate(self):
        raw = {"prompt_tokens": 9, "completion_tokens": 1,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        write_records(self.tr, [model_rec("m6", raw=raw)])
        self.index(0)
        self.index(0)                      # rescan from the start
        self.assertEqual(self.count("model_responses"), 1)

    def test_model_usage_record_type_is_captured(self):
        raw = {"prompt_tokens": 11, "completion_tokens": 2,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        write_records(self.tr, [model_rec("m7", raw=raw, rtype="model-usage")])
        self.index()
        self.assertEqual(self.model_row("m7")["prompt_tokens"], 11)

    def test_credit_is_never_stored(self):
        raw = {"prompt_tokens": 1, "completion_tokens": 1,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
               "credit": 42.0}
        write_records(self.tr, [model_rec("m8", raw=raw)])
        self.index()
        cols = [r[1] for r in self.conn.execute(
            "PRAGMA table_info(model_responses)")]
        self.assertNotIn("credit", cols)
        dump = json.dumps(dict(self.model_row("m8")))
        self.assertNotIn("credit", dump)
        self.assertNotIn("42.0", dump)

    def test_token_delta_is_not_model_tokens(self):
        # turn-metrics.tokenDelta is a context metric, not prompt/completion
        write_records(self.tr, [{
            "type": "turn-metrics", "timestamp": 5000, "durationMs": 100,
            "tokenDelta": 489522, "cwd": "/proj",
            "_meta": {"baggage": "codebuddy.session_id=s9"},
        }])
        self.index()
        self.assertEqual(self.count("model_responses"), 0)
        o = db.overview(self.conn)
        self.assertEqual(o["model_prompt_tokens"], 0)
        self.assertEqual(o["model_completion_tokens"], 0)
        # ...and it landed in the separate context-metric column
        sess = self.conn.execute(
            "SELECT tokens FROM sessions WHERE session_id='s9'").fetchone()
        self.assertEqual(sess["tokens"], 489522)

    # -- incremental / robustness for model responses -----------------------

    def test_model_response_partial_trailing_line(self):
        full = json.dumps(model_rec("m9", raw={"prompt_tokens": 3,
                                               "completion_tokens": 1}))
        with open(self.tr, "w", encoding="utf-8") as fh:
            fh.write(full + "\n")
            fh.write('{"type":"function_call","providerData":{"messageId":"m10"')  # partial
        off = self.index(0)
        self.assertEqual(self.count("model_responses"), 1)
        self.assertEqual(off, len(full) + 1)

    def test_model_response_incremental_append(self):
        write_records(self.tr, [model_rec("mA", raw={"prompt_tokens": 1,
                                                     "completion_tokens": 1})])
        off = self.index(0)
        self.assertEqual(self.count("model_responses"), 1)
        with open(self.tr, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(model_rec(
                "mB", raw={"prompt_tokens": 2, "completion_tokens": 2})) + "\n")
        self.index(off)
        self.assertEqual(self.count("model_responses"), 2)

    def test_truncation_recovery_via_run(self):
        root = Path(self.tmp.name) / "cb"
        (root / "projects" / "p").mkdir(parents=True)
        f = root / "projects" / "p" / "s.jsonl"
        old_dir = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        raw = {"prompt_tokens": 4, "completion_tokens": 2,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        try:
            write_records(f, [model_rec("mT", raw=raw)])
            sync.run(quiet=True, db_path=self.db_path)
            self.assertEqual(self.count("model_responses"), 1)
            # truncate the transcript, then rewrite the same response
            write_records(f, [])
            sync.run(quiet=True, db_path=self.db_path)
            write_records(f, [model_rec("mT", raw=raw)])
            sync.run(quiet=True, db_path=self.db_path)
            self.assertEqual(self.count("model_responses"), 1)
        finally:
            sync.db.CODEBUDDY_DIR = old_dir


if __name__ == "__main__":
    unittest.main()

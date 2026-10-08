"""Tests for the cbut indexer. Standard library only (unittest).

Run:  python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
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

    def test_agent_without_subagent_type_defaults_to_general_purpose(self):
        # The Agent tool runs as general-purpose when subagent_type is omitted,
        # so a call carrying only description/prompt must not fragment into a
        # "?" row.
        write_records(self.tr, [
            {"type": "function_call", "name": "Agent", "callId": "c5",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "arguments": json.dumps({"description": "x", "prompt": "y"})},
        ])
        self.index()
        row = self.conn.execute("SELECT * FROM agent_usage").fetchone()
        self.assertEqual(row["agent_type"], "general-purpose")
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
        # rawUsage.total_tokens is persisted verbatim as provider_total_tokens
        self.assertEqual(row["provider_total_tokens"], 120)
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

    # -- schema v3: prompt_cache_* fields + derived total -------------------
    # All token numbers below are FIXTURES, not real CodeBuddy data.

    def test_prompt_cache_fields_recorded(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 20,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
               "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
               "prompt_cache_write_tokens": 5}
        write_records(self.tr, [model_rec("c1", raw=raw)])
        self.index()
        row = self.model_row("c1")
        self.assertEqual(row["prompt_cache_hit_tokens"], 80)
        self.assertEqual(row["prompt_cache_miss_tokens"], 20)
        self.assertEqual(row["prompt_cache_write_tokens"], 5)

    def test_prompt_cache_zero_is_zero_not_missing(self):
        raw = {"prompt_tokens": 1, "completion_tokens": 1,
               "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
               "prompt_cache_write_tokens": 0}
        write_records(self.tr, [model_rec("c2", raw=raw)])
        self.index()
        row = self.model_row("c2")
        self.assertEqual(row["prompt_cache_hit_tokens"], 0)
        self.assertEqual(row["prompt_cache_miss_tokens"], 0)
        self.assertEqual(row["prompt_cache_write_tokens"], 0)

    def test_prompt_cache_absent_is_null(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 2}
        write_records(self.tr, [model_rec("c3", raw=raw)])
        self.index()
        row = self.model_row("c3")
        self.assertIsNone(row["prompt_cache_hit_tokens"])
        self.assertIsNone(row["prompt_cache_miss_tokens"])
        self.assertIsNone(row["prompt_cache_write_tokens"])

    def test_total_tokens_is_prompt_plus_completion(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 3,
               "prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 3}
        write_records(self.tr, [model_rec("c4", raw=raw)])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 13)

    def test_total_excludes_cache_hit_and_miss(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 3,
               "prompt_cache_hit_tokens": 999999,
               "prompt_cache_miss_tokens": 888888}
        write_records(self.tr, [model_rec("c5", raw=raw)])
        self.index()
        self.assertEqual(db.q_model_tokens(self.conn)[0]["total_tokens"], 13)
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 13)   # not 10+3+999999+888888

    def test_total_null_when_a_part_is_missing(self):
        write_records(self.tr, [model_rec("c6", raw={"prompt_tokens": 4})])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertIsNone(r["total_tokens"])

    def test_credit_not_in_any_token_field_or_total(self):
        raw = {"prompt_tokens": 5, "completion_tokens": 5, "credit": 12.5,
               "prompt_cache_hit_tokens": 1}
        write_records(self.tr, [model_rec("c7", raw=raw)])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 10)
        dump = json.dumps(dict(r))
        self.assertNotIn("credit", dump)
        self.assertNotIn("12.5", dump)

    def test_token_delta_not_in_model_total(self):
        write_records(self.tr, [
            model_rec("c8", raw={"prompt_tokens": 2, "completion_tokens": 1}),
            {"type": "turn-metrics", "timestamp": 9000, "tokenDelta": 777777,
             "cwd": "/proj", "_meta": {"baggage": "codebuddy.session_id=s1"}},
        ])
        self.index()
        o = db.overview(self.conn)
        self.assertEqual(o["model_total_tokens"], 3)     # not 3 + 777777
        self.assertEqual(o["model_prompt_cache_hit_tokens"], 0)

    def test_overview_and_queries_expose_new_fields(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 2,
               "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4,
               "prompt_cache_write_tokens": 1}
        write_records(self.tr, [model_rec("c9", raw=raw)])
        self.index()
        o = db.overview(self.conn)
        self.assertEqual(o["model_total_tokens"], 12)
        self.assertEqual(o["model_prompt_cache_hit_tokens"], 6)
        self.assertEqual(o["model_prompt_cache_miss_tokens"], 4)
        self.assertEqual(o["model_prompt_cache_write_tokens"], 1)
        tok = db.q_model_tokens(self.conn)[0]
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                    "prompt_cache_write_tokens", "total_tokens"):
            self.assertIn(key, tok.keys())

    def test_old_v2_db_upgrade_adds_columns_and_resets_sync_state(self):
        path = Path(self.tmp.name) / "old.db"
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE model_responses("
            " message_id TEXT PRIMARY KEY, session_id TEXT,"
            " conversation_request_id TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
            " ts INTEGER, project TEXT, source TEXT,"
            " usage_available INTEGER, missing TEXT);"
            "CREATE TABLE sync_state(file_path TEXT PRIMARY KEY, size INTEGER,"
            " mtime REAL, offset INTEGER);"
        )
        c.execute("INSERT INTO model_responses"
                  "(message_id, model, prompt_tokens, completion_tokens,"
                  " usage_available) VALUES('old1','m',5,6,1)")
        c.execute("INSERT INTO sync_state(file_path, size, mtime, offset)"
                  " VALUES('f',1,1.0,1)")
        c.commit()
        c.close()

        conn = db.open_db(path)
        db.ensure_schema(conn)                      # v2 -> v3 upgrade
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
        self.assertIn("prompt_cache_hit_tokens", cols)
        row = conn.execute(
            "SELECT prompt_cache_hit_tokens, prompt_tokens FROM model_responses"
            " WHERE message_id='old1'").fetchone()
        self.assertIsNone(row["prompt_cache_hit_tokens"])   # old row -> NULL
        self.assertEqual(row["prompt_tokens"], 5)
        # sync_state was cleared so the next sync re-reads and backfills
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 0)
        self.assertEqual(db.q_model_tokens(conn)[0]["total_tokens"], 11)
        conn.close()

    def test_schema_version_is_four(self):
        ver = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, "4")

    def test_no_message_id_is_not_recorded(self):
        # a usage-bearing record with no providerData.messageId must be skipped,
        # never given a guessed dedup key.
        rec = {"type": "function_call", "id": "x", "sessionId": "s1",
               "cwd": "/p", "timestamp": 1,
               "providerData": {"model": "m",
                                "rawUsage": {"prompt_tokens": 1,
                                             "completion_tokens": 1}}}
        write_records(self.tr, [rec])
        self.index()
        self.assertEqual(self.count("model_responses"), 0)

    # -- provider_total_tokens (schema v4) --------------------------------

    def test_provider_total_tokens_stored(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        write_records(self.tr, [model_rec("p1", raw=raw)])
        self.index()
        self.assertEqual(self.model_row("p1")["provider_total_tokens"], 12)

    def test_provider_total_tokens_zero_is_zero_not_null(self):
        raw = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        write_records(self.tr, [model_rec("p2", raw=raw)])
        self.index()
        self.assertEqual(self.model_row("p2")["provider_total_tokens"], 0)

    def test_provider_total_tokens_absent_is_null(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 2}   # no total_tokens
        write_records(self.tr, [model_rec("p3", raw=raw)])
        self.index()
        self.assertIsNone(self.model_row("p3")["provider_total_tokens"])

    def test_provider_total_never_derived(self):
        # prompt + completion present, but no raw total -> column stays NULL
        write_records(self.tr, [model_rec(
            "p4", raw={"prompt_tokens": 1, "completion_tokens": 2})])
        self.index()
        self.assertIsNone(self.model_row("p4")["provider_total_tokens"])

    def test_provider_total_preferred_over_derived(self):
        raw = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 999}
        write_records(self.tr, [model_rec("p5", raw=raw)])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 999)
        self.assertEqual(r["total_tokens_source"], "provider")
        self.assertEqual(db.q_model_tokens(self.conn)[0]["total_tokens"], 999)

    def test_derived_total_used_when_provider_absent(self):
        write_records(self.tr, [model_rec(
            "p6", raw={"prompt_tokens": 4, "completion_tokens": 6})])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 10)
        self.assertEqual(r["total_tokens_source"], "derived")

    def test_provider_total_updated_on_reemit(self):
        # same messageId re-emitted with a newer total -> UPSERT updates it
        write_records(self.tr, [model_rec(
            "p7", raw={"prompt_tokens": 1, "completion_tokens": 1,
                       "total_tokens": 5})])
        self.index()
        write_records(self.tr, [model_rec(
            "p7", raw={"prompt_tokens": 1, "completion_tokens": 1,
                       "total_tokens": 7})])
        self.index(offset=0)   # fresh index of the rewritten file
        self.assertEqual(self.model_row("p7")["provider_total_tokens"], 7)

    def test_placeholder_row_has_null_provider_total(self):
        write_records(self.tr, [model_rec("p8", raw=None)])
        self.index()
        row = self.model_row("p8")
        self.assertIsNone(row["provider_total_tokens"])
        self.assertEqual(row["usage_available"], 0)

    def test_total_source_is_null_when_no_total_available(self):
        # only prompt present -> no provider total and no complete derived pair
        write_records(self.tr, [model_rec("p9", raw={"prompt_tokens": 4})])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertIsNone(r["total_tokens"])
        self.assertIsNone(r["total_tokens_source"])

    def test_v3_db_upgrade_adds_provider_column_and_resets_sync_state(self):
        path = Path(self.tmp.name) / "v3.db"
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE model_responses("
            " message_id TEXT PRIMARY KEY, session_id TEXT,"
            " conversation_request_id TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
            " prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER,"
            " prompt_cache_write_tokens INTEGER,"
            " ts INTEGER, project TEXT, source TEXT,"
            " usage_available INTEGER, missing TEXT);"
            "CREATE TABLE sync_state(file_path TEXT PRIMARY KEY, size INTEGER,"
            " mtime REAL, offset INTEGER);"
        )
        c.execute("INSERT INTO model_responses"
                  "(message_id, model, prompt_tokens, completion_tokens,"
                  " usage_available) VALUES('v3row','m',5,6,1)")
        c.execute("INSERT INTO sync_state(file_path, size, mtime, offset)"
                  " VALUES('f',1,1.0,1)")
        c.commit()
        c.close()

        conn = db.open_db(path)
        db.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
        self.assertIn("provider_total_tokens", cols)
        row = conn.execute("SELECT provider_total_tokens FROM model_responses"
                           " WHERE message_id='v3row'").fetchone()
        self.assertIsNone(row["provider_total_tokens"])       # old row -> NULL
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 0)
        conn.close()

    # -- F1: sessions totals are idempotent (never double-counted) ----------
    # turn-metrics.tokenDelta accumulates into sessions.tokens; a full re-read
    # (migration / --full / rewrite) must rebuild, not add on top.

    def _cb_root(self):
        root = Path(self.tmp.name) / "cb"
        (root / "projects" / "p").mkdir(parents=True, exist_ok=True)
        return root

    def _write_tm(self, f, sid, tok, dur, ts):
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "turn-metrics", "timestamp": ts,
                "tokenDelta": tok, "durationMs": dur, "cwd": "/proj",
                "_meta": {"baggage": f"codebuddy.session_id={sid}"},
            }) + "\n")

    def _sum(self, table, col):
        return self.conn.execute(
            f"SELECT COALESCE(SUM({col}),0) FROM {table}").fetchone()[0]

    def _session(self, sid):
        row = self.conn.execute(
            "SELECT tokens, duration_ms FROM sessions WHERE session_id=?",
            (sid,)).fetchone()
        return None if row is None else (row["tokens"], row["duration_ms"])

    def _rewrite_tm_compact(self, f, sid, tok, dur, ts):
        """Overwrite ``f`` with one compact turn-metrics line.

        Compact separators make the rewritten file strictly shorter than the
        spaced form written by :meth:`_write_tm`, so the pre-pass detects the
        stale offset by size alone (no reliance on mtime granularity).
        """
        rec = {"type": "turn-metrics", "timestamp": ts, "tokenDelta": tok,
               "durationMs": dur, "cwd": "/proj",
               "_meta": {"baggage": f"codebuddy.session_id={sid}"}}
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def _sub(self, root, name="agent-1"):
        """Create (and return) a subagent transcript for project ``p``."""
        d = root / "projects" / "p" / "sess" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.jsonl"

    def test_session_totals_match_transcript_ratio_one(self):
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        self._write_tm(f, "sA", 100, 10, 1000)
        self._write_tm(f, "sA", 50, 5, 2000)
        self._write_tm(f, "sB", 7, 3, 1500)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        # transcript tokenDelta sum == DB sessions.tokens sum (ratio 1.0)
        self.assertEqual(self._sum("sessions", "tokens"), 157)
        self.assertEqual(self._sum("sessions", "duration_ms"), 18)

    def test_full_resync_does_not_change_session_totals(self):
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        self._write_tm(f, "sA", 100, 10, 1000)
        self._write_tm(f, "sA", 50, 5, 2000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            first = self._sum("sessions", "tokens")
            sync.run(full=True, quiet=True, db_path=self.db_path)
            second = self._sum("sessions", "tokens")
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(first, 150)
        self.assertEqual(second, 150)

    # -- F1b: one session spans main + subagent transcripts -----------------
    # A new subagent file read at offset 0 must ADD to the session's totals,
    # never reset them (the per-file DELETE regression).

    def test_session_accumulates_across_main_and_subagent_files(self):
        root = self._cb_root()
        main = root / "projects" / "p" / "sess.jsonl"
        self._write_tm(main, "S", 100, 1000, 1000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            self.assertEqual(self._session("S"), (100, 1000))
            # a brand-new subagent transcript is discovered and read at offset 0
            self._write_tm(self._sub(root), "S", 5, 50, 2000)
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(self._session("S"), (105, 1050))

    def test_multifile_totals_idempotent_incremental_then_full(self):
        root = self._cb_root()
        main = root / "projects" / "p" / "sess.jsonl"
        self._write_tm(main, "S", 100, 1000, 1000)
        self._write_tm(self._sub(root), "S", 5, 50, 2000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            a = self._session("S")
            sync.run(quiet=True, db_path=self.db_path)        # incremental
            b = self._session("S")
            sync.run(full=True, quiet=True, db_path=self.db_path)   # full
            c = self._session("S")
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(a, (105, 1050))
        self.assertEqual(b, a)          # untouched re-run adds nothing
        self.assertEqual(c, a)          # full rebuild reproduces the same sum

    def test_rewritten_subagent_file_rebuilds_without_inflation(self):
        root = self._cb_root()
        main = root / "projects" / "p" / "sess.jsonl"
        sub = self._sub(root)
        self._write_tm(main, "S", 100, 1000, 1000)
        self._write_tm(sub, "S", 5, 50, 2000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            self.assertEqual(self._session("S"), (105, 1050))
            # rewrite the subagent with a different contribution; the stale
            # offset must trigger a whole-DB rebuild (not a partial add).
            self._rewrite_tm_compact(sub, "S", 7, 70, 3000)
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        # 100 (main) + 7 (rewritten sub) -- not 112 (105+7) and not 7
        self.assertEqual(self._session("S"), (107, 1070))

    def test_stale_file_escalates_to_full_rebuild(self):
        # A stale offset clears sync_state + sessions for the WHOLE DB, so all
        # files are re-read once and the totals are rebuilt exactly.
        root = self._cb_root()
        main = root / "projects" / "p" / "sess.jsonl"
        self._write_tm(main, "S", 100, 1000, 1000)
        self._write_tm(self._sub(root), "S", 5, 50, 2000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            self._rewrite_tm_compact(main, "S", 40, 400, 5000)
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(self._session("S"), (45, 450))   # 40 + 5, rebuilt once

    def test_sync_state_and_sessions_cleared_together_is_idempotent(self):
        # The migration/--full contract clears sync_state AND sessions together;
        # the re-read then rebuilds the accumulated totals exactly once.
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        self._write_tm(f, "sA", 100, 10, 1000)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=self.db_path)
            first = self._sum("sessions", "tokens")
            self.conn.execute("DELETE FROM sync_state")
            self.conn.execute("DELETE FROM sessions")
            self.conn.commit()
            sync.run(quiet=True, db_path=self.db_path)
            second = self._sum("sessions", "tokens")
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(first, 100)
        self.assertEqual(second, 100)

    def test_migration_then_sync_rebuilds_multifile_sessions(self):
        # A v2 -> v4 upgrade clears sync_state + sessions; the following sync
        # rebuilds the totals across main + subagent files exactly once.
        root = self._cb_root()
        main = root / "projects" / "p" / "sess.jsonl"
        self._write_tm(main, "S", 100, 1000, 1000)
        self._write_tm(self._sub(root), "S", 5, 50, 2000)
        path = Path(self.tmp.name) / "old3.db"
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE model_responses("
            " message_id TEXT PRIMARY KEY, session_id TEXT,"
            " conversation_request_id TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
            " ts INTEGER, project TEXT, source TEXT,"
            " usage_available INTEGER, missing TEXT);"
            "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, project TEXT,"
            " title TEXT, started_at INTEGER, ended_at INTEGER, model TEXT,"
            " tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0);"
            "CREATE TABLE sync_state(file_path TEXT PRIMARY KEY, size INTEGER,"
            " mtime REAL, offset INTEGER);"
        )
        c.execute("INSERT INTO sessions(session_id, tokens, duration_ms)"
                  " VALUES('S',999,111)")
        c.execute("INSERT INTO sync_state(file_path, size, mtime, offset)"
                  " VALUES('gone',1,1.0,1)")
        c.commit()
        c.close()
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.run(quiet=True, db_path=path)          # migrates, then re-reads
        finally:
            sync.db.CODEBUDDY_DIR = old
        conn = db.open_db(path, readonly=True)
        row = conn.execute("SELECT tokens, duration_ms FROM sessions"
                           " WHERE session_id='S'").fetchone()
        conn.close()
        self.assertEqual((row["tokens"], row["duration_ms"]), (105, 1050))

    def test_upgrade_clears_sessions_so_totals_are_not_doubled(self):
        path = Path(self.tmp.name) / "old2.db"
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE model_responses("
            " message_id TEXT PRIMARY KEY, session_id TEXT,"
            " conversation_request_id TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
            " ts INTEGER, project TEXT, source TEXT,"
            " usage_available INTEGER, missing TEXT);"
            "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, project TEXT,"
            " title TEXT, started_at INTEGER, ended_at INTEGER, model TEXT,"
            " tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0);"
        )
        c.execute("INSERT INTO sessions(session_id, tokens, duration_ms)"
                  " VALUES('s1',999,111)")
        c.commit()
        c.close()

        conn = db.open_db(path)
        db.ensure_schema(conn)                      # v2 -> v3/v4 upgrade
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        conn.close()

    # -- F2: plugin-owned skills/agents reached via slash commands ----------

    def test_slash_invoked_plugin_skill_is_attributed(self):
        write_records(self.tr, [
            {"type": "message", "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "content": "<command-name>/playwright-cli</command-name>"},
        ])
        sync.index_file(self.conn, self.tr, 0,
                        {"playwright-cli": "playwright-cli"}, {}, {})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM plugin_usage").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["plugin"], "playwright-cli")
        self.assertEqual(row["kind"], "skill")
        self.assertEqual(row["target"], "playwright-cli")

    def test_slash_invoked_plugin_command_is_attributed(self):
        write_records(self.tr, [
            {"type": "message", "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "content": "<command-name>/langchain-agent</command-name>"},
        ])
        sync.index_file(self.conn, self.tr, 0, {}, {},
                        {"langchain-agent": "llm-application-dev"})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM plugin_usage").fetchone()
        self.assertEqual(
            (row["plugin"], row["kind"]), ("llm-application-dev", "command"))

    def test_non_plugin_command_is_not_attributed(self):
        write_records(self.tr, [
            {"type": "message", "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "content": "<command-name>/model</command-name>"},
        ])
        sync.index_file(self.conn, self.tr, 0, {}, {}, {})
        self.conn.commit()
        self.assertEqual(self.count("commands"), 1)
        self.assertEqual(self.count("plugin_usage"), 0)

    # -- F3: rewritten / replaced files are re-read from zero ---------------

    def test_rewritten_larger_file_is_reread_from_zero(self):
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            write_records(f, [{"type": "function_call", "name": "Bash",
                               "callId": "a1", "sessionId": "s1", "cwd": "/p",
                               "timestamp": 10}])
            sync.run(quiet=True, db_path=self.db_path)
            # rewrite with a longer, different transcript (new size >= offset)
            write_records(f, [{"type": "function_call", "name": "Read",
                               "callId": "b1", "sessionId": "s1", "cwd": "/p",
                               "timestamp": 20,
                               "arguments": json.dumps(
                                   {"file_path": "/a/much/longer/path/here"})}])
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tool_calls"
                              " WHERE call_id='b1'").fetchone()[0], 1)

    def test_rewritten_same_size_file_is_reread_from_zero(self):
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            write_records(f, [{"type": "function_call", "name": "Bash",
                               "callId": "a1", "sessionId": "s1", "cwd": "/p",
                               "timestamp": 10}])
            sync.run(quiet=True, db_path=self.db_path)
            # same byte length, different call_id -> a pure rewrite
            write_records(f, [{"type": "function_call", "name": "Bash",
                               "callId": "b1", "sessionId": "s1", "cwd": "/p",
                               "timestamp": 10}])
            st = f.stat()
            os.utime(f, (st.st_atime, st.st_mtime + 5))   # force a new mtime
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tool_calls"
                              " WHERE call_id='b1'").fetchone()[0], 1)

    # -- F4: a final line without a trailing newline is consumed ------------

    def test_final_line_without_newline_is_consumed(self):
        line = json.dumps({"type": "function_call", "name": "Bash",
                           "callId": "b1", "sessionId": "s1", "cwd": "/p",
                           "timestamp": 10})
        with open(self.tr, "w", encoding="utf-8") as fh:
            fh.write(line)                       # no trailing newline
        off = self.index(0)
        self.assertEqual(self.count("tool_calls"), 1)
        self.assertEqual(off, len(line))

    def test_run_consumes_final_line_without_newline(self):
        root = self._cb_root()
        f = root / "projects" / "p" / "s.jsonl"
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "function_call", "name": "Bash",
                                     "callId": "b1", "sessionId": "s1",
                                     "cwd": "/p", "timestamp": 10}))
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tool_calls"
                              " WHERE call_id='b1'").fetchone()[0], 1)

    # -- F5: robust MCP name parsing ----------------------------------------

    def test_mcp_from_name_parses_wellformed(self):
        self.assertEqual(sync._mcp_from_name("mcp__context7__query-docs"),
                         ("context7", "query-docs"))
        # a tool segment may itself contain "__"
        self.assertEqual(sync._mcp_from_name("mcp__srv__a__b"), ("srv", "a__b"))

    def test_mcp_from_name_rejects_malformed(self):
        for bad in ("mcp__server", "mcp__", "mcp__server__",
                    "mcp__server__tool__", "Bash", "mcp_server__tool"):
            self.assertIsNone(sync._mcp_from_name(bad), bad)

    def test_malformed_mcp_name_is_not_recorded_as_mcp(self):
        write_records(self.tr, [
            {"type": "function_call", "name": "mcp__broken", "callId": "x1",
             "sessionId": "s1", "cwd": "/p", "timestamp": 10},
        ])
        self.index()
        self.assertEqual(self.count("mcp_usage"), 0)


if __name__ == "__main__":
    unittest.main()

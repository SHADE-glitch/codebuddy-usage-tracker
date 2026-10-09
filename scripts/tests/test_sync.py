"""Tests for the cbut indexer. Standard library only (unittest).

Run:  python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
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

    def test_user_skills_come_from_the_manifest_not_the_category(self):
        # The layout is skills/<category>/<skill>/SKILL.md. Listing the
        # directories one level down registered "ai" and "backend" as skills and
        # left every real skill out, so assert both halves: the manifests' parents
        # are in, the categories and manifest-less directories are not.
        root = self._cb_root()
        for category, skill in (("ai", "add-tests"), ("backend", "api-contract")):
            d = root / "skills" / category / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
        (root / "skills" / "README.md").write_text("# not a skill\n",
                                                   encoding="utf-8")
        no_manifest = root / "skills" / "_meta"
        no_manifest.mkdir()
        (no_manifest / "notes.md").write_text("# no manifest here\n",
                                              encoding="utf-8")

        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.scan_inventory(self.conn)
        finally:
            sync.db.CODEBUDDY_DIR = old
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM inventory WHERE kind='skill' AND source='user'")}
        self.assertEqual(names, {"add-tests", "api-contract"})

    def test_user_agents_are_inventoried(self):
        # ~/.codebuddy/agents/<name>.md was never scanned, so an installed agent
        # could not appear in the Agents panel before its first call.
        root = self._cb_root()
        agents = root / "agents"
        agents.mkdir()
        (agents / "code-reviewer.md").write_text("# fixture\n", encoding="utf-8")
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.scan_inventory(self.conn)
        finally:
            sync.db.CODEBUDDY_DIR = old
        row = self.conn.execute(
            "SELECT * FROM inventory WHERE kind='agent' AND name='code-reviewer'"
        ).fetchone()
        self.assertIsNotNone(row, "an installed agent must appear even unused")
        self.assertEqual(row["source"], "user")

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

    def test_current_schema_version_is_recorded(self):
        # ensure_schema must leave the version it applied readable in `meta`: the
        # migration gate reads it to tell a database from a future build apart
        # from an old one. Asserted against the constant, so bumping the schema is
        # one edit and not a stale test that pins a number nobody changed.
        ver = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, str(db.SCHEMA_VERSION))

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

    def test_command_in_an_input_text_block_is_indexed(self):
        """The block shape local transcripts actually use.

        Every command fixture above carries a bare string ``content``, so none
        of them entered the block path. When a rewrite filtered blocks on
        ``type == "text"`` — a type that carries no command marker in any of the
        499 local transcripts — the Commands panel dropped to zero rows with the
        whole suite green. This test is the branch that check needed.
        """
        write_records(self.tr, [
            {"type": "message", "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "content": [
                 {"type": "input_text",
                  "text": "run <command-name>/model</command-name> now"},
                 {"type": "output_text",
                  "text": "<command-name>/clear</command-name>"}]},
        ])
        sync.index_file(self.conn, self.tr, 0, {}, {}, {})
        self.conn.commit()
        got = {r["command"] for r in
               self.conn.execute("SELECT command FROM commands")}
        self.assertEqual(got, {"model", "clear"})

    def test_marker_outside_a_block_text_field_is_not_invented(self):
        """Only ``text`` is scanned: a marker parked in another field is not a
        command we observed, and blocks without text are not errors."""
        write_records(self.tr, [
            {"type": "message", "sessionId": "s1", "cwd": "/p", "timestamp": 10,
             "content": [
                 {"type": "input_text",
                  "content": "<command-name>/ghost</command-name>"},
                 "a bare string block", 12]},
        ])
        sync.index_file(self.conn, self.tr, 0, {}, {}, {})
        self.conn.commit()
        self.assertEqual(self.count("commands"), 0)
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


class UnparsedAccountingTest(unittest.TestCase):
    """Records no handler claims must become a number, never vanish quietly.

    A CodeBuddy format change zeroes every panel while `cbut sync` still prints
    "done" — these tests pin the one signal that distinguishes "you stopped
    using tools" from "the log changed under us".

    Standalone rather than a subclass of ``IndexerTest``: inheriting it would
    re-run all 61 of its tests under this class name.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.root = Path(self.tmp.name) / "cb"
        (self.root / "projects" / "p").mkdir(parents=True)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _run_on(self, records, raw_lines=()):
        """Index records through run() against a throwaway CodeBuddy root."""
        f = self.root / "projects" / "p" / "s.jsonl"
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            with open(f, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec) + "\n")
                for line in raw_lines:
                    fh.write(line + "\n")
            return sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old

    def _reasons(self):
        return {r["reason"]: r["count"] for r in
                self.conn.execute("SELECT reason, count FROM unparsed")}

    def call(self, call_id, ts=10):
        return {"type": "function_call", "name": "Bash", "callId": call_id,
                "sessionId": "s1", "cwd": "/p", "timestamp": ts}

    def test_unclaimed_type_is_counted_per_reason(self):
        stats = self._run_on(
            [{"type": "reasoning", "sessionId": "s1", "timestamp": t}
             for t in (10, 20, 30)] + [self.call("b1")])
        self.assertEqual(self._reasons(), {"type:reasoning": 3})
        self.assertEqual(self.count("tool_calls"), 1,
                         "the known record must still be indexed")
        self.assertEqual(stats["unparsed_this_run"], 3)

    def test_record_without_a_type_is_counted_as_missing(self):
        self._run_on([{"sessionId": "s1", "timestamp": 10}])
        self.assertEqual(self._reasons(), {"type:<missing>": 1})

    def test_complete_line_that_is_not_json_is_counted(self):
        self._run_on([self.call("b1")], raw_lines=["{not json at all"])
        self.assertEqual(self._reasons(), {"unparseable_line": 1})

    def test_partial_tail_line_is_not_counted(self):
        # An in-progress session ends mid-line. That tail is retried next run,
        # so counting it would make every live session look like a format break.
        f = self.root / "projects" / "p" / "s.jsonl"
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.call("b1")) + "\n")
                fh.write('{"type": "function_cal')     # cut off, no newline
            sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(self._reasons(), {})
        self.assertEqual(self.count("tool_calls"), 1)

    def test_second_run_over_an_untouched_transcript_does_not_recount(self):
        self._run_on([{"type": "reasoning", "sessionId": "s1", "timestamp": 10}])
        self.assertEqual(self._reasons(), {"type:reasoning": 1})
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            sync.run(quiet=True, db_path=self.db_path)   # no new bytes
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(self._reasons(), {"type:reasoning": 1},
                         "an untouched file is skipped, so the count must not "
                         "inflate on every poll")

    def test_rewritten_transcript_recounts_instead_of_double_counting(self):
        # A rewritten prefix forces a whole-DB re-read (the offsets cannot be
        # subtracted in isolation). The unparsed total must follow that logic,
        # exactly as sessions.tokens had to.
        records = [{"type": "reasoning", "sessionId": "s1", "timestamp": 10}]
        self._run_on(records)
        self._run_on(records)                     # same bytes, new mtime
        self.assertEqual(self._reasons(), {"type:reasoning": 1},
                         "a forced rebuild must reset the accumulated count")

    def test_full_rebuild_recounts_instead_of_double_counting(self):
        records = [{"type": "reasoning", "sessionId": "s1", "timestamp": 10}]
        self._run_on(records)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            sync.run(full=True, quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old
        self.assertEqual(self._reasons(), {"type:reasoning": 1},
                         "--full re-reads everything, so the count must be the "
                         "recount, not the sum of two passes")

    def test_overview_reports_unparsed_totals(self):
        self._run_on([{"type": "reasoning", "sessionId": "s1", "timestamp": 10},
                      {"type": "watermark", "sessionId": "s1", "timestamp": 11}])
        o = db.overview(self.conn)
        self.assertEqual(o["unparsed_records"], 2)
        self.assertEqual(o["unparsed_kinds"], 2)

    def test_a_type_handled_elsewhere_is_not_counted_as_unparsed(self):
        # model-usage carries no transcript fields of its own: its tokens land in
        # model_responses via _record_model_response. It appeared in the real
        # unparsed table anyway, so pin that a known type is claimed even when no
        # branch acts on it — otherwise the count reports a format change that
        # never happened.
        stats = self._run_on([
            {"type": "model-usage", "sessionId": "s1", "cwd": "/p",
             "timestamp": 10,
             "providerData": {"messageId": "mu1", "model": "m",
                              # FIXTURE numbers, not real CodeBuddy usage.
                              "rawUsage": {"prompt_tokens": 5,
                                           "completion_tokens": 6}}}])
        self.assertEqual(self._reasons(), {},
                         "a type this parser understands must not be counted")
        self.assertEqual(self.count("model_responses"), 1,
                         "…and its tokens are genuinely recorded")
        self.assertEqual(stats["unparsed_this_run"], 0)


class CrashSafetyTest(unittest.TestCase):
    """The offsets we store must survive a crash and a rewrite honestly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.root = Path(self.tmp.name) / "cb"
        (self.root / "projects" / "p").mkdir(parents=True)
        self.f = self.root / "projects" / "p" / "s.jsonl"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _run(self):
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            return sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_killed_run_rolls_back_rows_and_offset_together(self):
        # sessions.tokens *accumulates*, so a crash that kept the rows but lost
        # the offset would double-count the context total on the retry. Rows and
        # offset are written in the same transaction as the commit, so a crash
        # loses both — this pins that boundary rather than trusting it.
        rec = {"type": "turn-metrics", "timestamp": 1000, "tokenDelta": 100,
               "durationMs": 1000, "cwd": "/p",
               "_meta": {"baggage": "codebuddy.session_id=S"}}
        self.f.write_text(json.dumps(rec) + "\n", encoding="utf-8")

        crash = db.open_db(self.db_path)
        db.ensure_schema(crash)
        sync.index_file(crash, self.f, 0, {}, {}, {})
        crash.close()                       # an *imitated* crash; see the next test

        self.assertEqual(self.count("sessions"), 0,
                         "an uncommitted run must leave no session row")
        self.assertEqual(self.count("sync_state"), 0,
                         "…and no offset claiming the bytes were consumed")

        self._run()
        row = self.conn.execute("SELECT tokens, duration_ms FROM sessions"
                                " WHERE session_id='S'").fetchone()
        self.assertEqual((row["tokens"], row["duration_ms"]), (100, 1000),
                         "the retry must count the turn exactly once")

    # A `close()` only imitates a crash: Python still runs SQLite's rollback on
    # the way out. This test kills the process for real, so recovery has to come
    # from the WAL alone — the honest version of "incremental *and* idempotent".
    CHILD_CRASH = (
        "import json, os, sys, importlib.util\n"
        "s = os.environ['CBUT_CRASH_SCRIPTS']\n"
        "sys.path.insert(0, s)\n"
        "import cbut_db as db\n"
        "spec = importlib.util.spec_from_file_location('sync', "
        "os.path.join(s, 'cbut-sync.py'))\n"
        "sync = importlib.util.module_from_spec(spec); spec.loader.exec_module(sync)\n"
        "conn = db.open_db(os.environ['CBUT_CRASH_DB'])\n"
        "db.ensure_schema(conn)\n"
        "with open(os.environ['CBUT_CRASH_FILE'], 'rb') as fh:\n"
        "    lines = fh.read().splitlines()\n"
        "sync._handle_record(conn, json.loads(lines[0]), {}, {}, {}, {})\n"
        "sync._handle_record(conn, json.loads(lines[1]), {}, {}, {}, {})\n"
        "os._exit(7)\n"                     # no commit, no close, no checkpoint
    )

    def test_a_real_process_kill_mid_transaction_leaves_no_partial_state(self):
        rec = {"type": "turn-metrics", "timestamp": 1000, "tokenDelta": 100,
               "durationMs": 1000, "cwd": "/p",
               "_meta": {"baggage": "codebuddy.session_id=S"}}
        call = {"type": "function_call", "name": "Bash", "callId": "k1",
                "sessionId": "S", "cwd": "/p", "timestamp": 1001,
                "arguments": "{}"}
        write_records(self.f, [rec, call])

        # The child must be the only connection, or it would be our own open
        # read transaction that decides when the WAL is recovered.
        self.conn.close()
        try:
            done = subprocess.run(
                [sys.executable, "-c", self.CHILD_CRASH],
                env=dict(os.environ, CBUT_CRASH_SCRIPTS=str(SCRIPTS),
                         CBUT_CRASH_DB=str(self.db_path),
                         CBUT_CRASH_FILE=str(self.f)),
                capture_output=True, text=True)
        finally:
            self.conn = db.open_db(self.db_path)

        self.assertEqual(done.returncode, 7,
                         "the child was supposed to die mid-transaction, not "
                         "raise: " + done.stderr[-400:])
        for table in ("sessions", "sync_state", "tool_calls"):
            self.assertEqual(self.count(table), 0,
                             f"an uncommitted {table} write survived a real kill")

        self._run()
        self.assertEqual(self.count("sync_state"), 1,
                         "the retry must re-read the file from the same offset")
        row = self.conn.execute("SELECT tokens, duration_ms FROM sessions"
                                " WHERE session_id='S'").fetchone()
        self.assertEqual((row["tokens"], row["duration_ms"]), (100, 1000),
                         "recovered WAL + retry must count the turn once, not twice")
        self.assertEqual(self.count("tool_calls"), 1,
                         "the killed run's tool call must not appear twice")

    def test_sync_state_keeps_the_real_size_when_a_tail_is_deferred(self):
        line = json.dumps({"type": "function_call", "name": "Bash",
                           "callId": "b1", "sessionId": "s1", "cwd": "/p",
                           "timestamp": 10})
        self.f.write_text(line + "\n" + '{"type": "function_cal',
                          encoding="utf-8")   # half a record, no newline
        self._run()
        row = self.conn.execute(
            "SELECT size, offset FROM sync_state WHERE file_path=?",
            (str(self.f),)).fetchone()
        self.assertEqual(row["offset"], len(line) + 1,
                         "the deferred tail must not be claimed as consumed")
        self.assertEqual(row["size"], self.f.stat().st_size,
                         "`size` must be the file's real size, not the offset")

    def test_needs_reset_detects_a_shrink_that_stays_above_the_offset(self):
        # Five 20-byte lines; three were consumed (offset 60). The file is then
        # rewritten to 70 bytes — smaller than it was, still larger than the
        # offset. Recording the real size catches it; recording the offset (60)
        # as the size made 70 look like growth, so the stale offset survived and
        # the file was re-read from the middle of a rewritten prefix.
        self.f.write_text("".join(f"{i:<19}\n" for i in range(5)),
                          encoding="utf-8")
        st = self.f.stat()
        self.assertEqual(st.st_size, 100)
        real_size_row = {"size": 100, "mtime": st.st_mtime - 5, "offset": 60}
        offset_as_size = {"size": 60, "mtime": st.st_mtime - 5, "offset": 60}
        self.assertTrue(sync._needs_reset(self.f, st, real_size_row),
                        "a rewrite that shrank the file must force a rebuild")
        self.assertFalse(sync._needs_reset(self.f, st, offset_as_size),
                         "what the old size column would have concluded")

    def test_unreadable_file_is_counted_and_the_run_survives(self):
        good = self.root / "projects" / "p" / "a.jsonl"
        good.write_text(json.dumps({"type": "function_call", "name": "Bash",
                                    "callId": "b1", "sessionId": "s1",
                                    "cwd": "/p", "timestamp": 10}) + "\n",
                        encoding="utf-8")
        bad = self.root / "projects" / "p" / "z.jsonl"
        bad.write_text(json.dumps({"type": "function_call", "name": "Read",
                                   "callId": "r1", "sessionId": "s2",
                                   "cwd": "/p", "timestamp": 20}) + "\n",
                       encoding="utf-8")
        os.chmod(bad, 0)
        try:
            stats = self._run()
        finally:
            os.chmod(bad, 0o600)
        self.assertEqual(self.count("tool_calls"), 1,
                         "a transcript that could be read must still be indexed")
        self.assertEqual(stats["unparsed_this_run"], 1,
                         "the unreadable file has to show up as a number")
        reason = self.conn.execute(
            "SELECT reason FROM unparsed").fetchone()["reason"]
        self.assertTrue(reason.startswith("unreadable_file:"), reason)
        self.assertEqual(self.count("sync_state"), 1,
                         "only the readable file may claim an offset")

    def test_a_retry_picks_the_unreadable_file_back_up(self):
        # No offset is stored for a skipped file, so the next run retries it —
        # otherwise one transient lock would silently drop a whole session.
        bad = self.root / "projects" / "p" / "z.jsonl"
        bad.write_text(json.dumps({"type": "function_call", "name": "Read",
                                   "callId": "r1", "sessionId": "s2",
                                   "cwd": "/p", "timestamp": 20}) + "\n",
                       encoding="utf-8")
        os.chmod(bad, 0)
        try:
            self._run()
        finally:
            os.chmod(bad, 0o600)
        self.assertEqual(self.count("tool_calls"), 0)
        self._run()
        self.assertEqual(self.count("tool_calls"), 1,
                         "the file must be indexed once it is readable again")


    def test_record_without_a_project_creates_no_session_row(self):
        # A metadata-only record carries a sessionId and a timestamp but no cwd.
        # Each one used to create a sessions row with a NULL project, no tool
        # calls and no model responses behind it — invisible in every panel, but
        # counted as a session (308 of them in the real index).
        self.f.write_text(json.dumps({"type": "session-meta", "sessionId": "ghost",
                                      "timestamp": 1000}) + "\n",
                          encoding="utf-8")
        self._run()
        self.assertEqual(self.count("sessions"), 0)
        self.assertEqual(self.count("sync_state"), 1,
                         "the file is still consumed; only the row is declined")

    def test_a_session_with_a_project_keeps_its_row_and_is_not_wiped(self):
        rec1 = {"type": "function_call", "name": "Bash", "callId": "b1",
                "sessionId": "s1", "cwd": "/p", "timestamp": 1000}
        rec2 = {"type": "session-meta", "sessionId": "s1", "timestamp": 2000}
        self.f.write_text(json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n",
                          encoding="utf-8")
        self._run()
        row = self.conn.execute(
            "SELECT project, started_at, ended_at FROM sessions"
            " WHERE session_id='s1'").fetchone()
        self.assertIsNotNone(row, "a real session must have its row")
        self.assertEqual(row["project"], "/p")
        # Honest cost: the span now ends at the last record that said where it
        # ran, so a cwd-less trailing record no longer stretches ended_at.
        # Duration is accumulated from turn-metrics (which carries cwd), so only
        # the start/end window is affected.
        self.assertEqual((row["started_at"], row["ended_at"]), (1000, 1000))


if __name__ == "__main__":
    unittest.main()

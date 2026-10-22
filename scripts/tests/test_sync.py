"""Tests for the cbut indexer. Standard library only (unittest).

Run:  python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database
from _catalog import catalog_pinned  # noqa: E402 -- every scan below reads the snapshot fixture

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_sync", SCRIPTS / "cbut-sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

# scan_inventory resolves the installed CodeBuddy package. Pin it for the whole
# module: a per-test pin is a convention the next test forgets, and forgetting it
# does not fail -- it just measures whatever CLI the developer has installed.
_CATALOG_PIN = catalog_pinned()


def setUpModule():
    _CATALOG_PIN.__enter__()


def tearDownModule():
    _CATALOG_PIN.__exit__(None, None, None)


def write_records(path: Path, recs) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def model_rec(message_id, raw=None, rtype="function_call",
              model="test-model", session_id="s1", cwd="/proj", ts=1000,
              conv="conv-1", request_model_id=None):
    """A synthetic transcript model-response record.

    These are FIXTURES — the token numbers are made up for testing, not real
    CodeBuddy data. ``raw=None`` means the response carried no ``rawUsage``.
    ``request_model_id`` drives the route-origin column; ``None`` leaves the
    field absent (an unknown route).
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
    if request_model_id is not None:
        rec["providerData"]["requestModelId"] = request_model_id
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

    def test_a_malformed_tool_name_is_counted_and_not_stored(self):
        """A name with a newline is not a tool call, and it is not a name any
        table can render: a real one on this machine was
        ``REPO<...\n<... parameter name=``, produced by a broken model call.
        It is counted under its own reason instead of stored."""
        write_records(self.tr, [
            {"type": "function_call", "name": "REPO\n<parameter name=",
             "callId": "bad", "sessionId": "s1", "cwd": "/p", "timestamp": 10},
            {"type": "function_call", "name": "Bash", "callId": "good",
             "sessionId": "s1", "cwd": "/p", "timestamp": 11},
        ])
        unparsed = {}
        sync.index_file(self.conn, self.tr, 0, {}, {}, {}, unparsed)
        for reason, n in unparsed.items():
            db.note_unparsed(self.conn, reason, n)
        self.conn.commit()
        self.assertEqual(self.count("tool_calls"), 1)
        row = self.conn.execute(
            "SELECT count FROM unparsed WHERE reason=?",
            (sync.INVALID_TOOL_NAME,)).fetchone()
        self.assertIsNotNone(row, "the rejected name was not counted")
        self.assertEqual(row[0], 1)

    def test_a_result_stamped_before_its_call_has_no_duration(self):
        """The two records disagree about their order, so there is no duration.

        CodeBuddy wrote the result record first for 2,540 of this machine's
        45,262 calls (2026-10-21), and `result.ts - call.ts` turned that into a
        negative duration that every AVG averaged in: Read read 719ms against a
        true 826ms. Unknown is NULL, and AVG skips NULL; a 0 would drag the mean
        down the other way and a positive number would be invented."""
        write_records(self.tr, [
            {"type": "function_call", "name": "Read", "callId": "back",
             "sessionId": "s1", "cwd": "/p", "timestamp": 2000},
            {"type": "function_call_result", "callId": "back",
             "status": "completed", "timestamp": 1900},
            {"type": "function_call", "name": "Read", "callId": "fwd",
             "sessionId": "s1", "cwd": "/p", "timestamp": 3000},
            {"type": "function_call_result", "callId": "fwd",
             "status": "completed", "timestamp": 3400},
        ])
        self.index()
        got = {r["call_id"]: r["duration_ms"] for r in self.conn.execute(
            "SELECT call_id, duration_ms FROM tool_calls")}
        self.assertEqual(got, {"back": None, "fwd": 400})
        self.assertEqual(self.conn.execute(
            "SELECT CAST(AVG(duration_ms) AS INTEGER) FROM tool_calls"
        ).fetchone()[0], 400,
            "the negative row was averaged in instead of skipped")

    def test_a_refused_call_whose_name_no_catalogue_holds_is_withdrawn(self):
        """Code Mode refuses every name it does not expose directly. For a real
        tool (Bash, REPL) that is the model calling from the wrong layer and the
        row stays. For a name no catalogue ever held (REPO_PLACEHOLDER, a
        placeholder the model emitted instead of a name) nothing ran, yet the row
        read "removed" in every panel — a state that means "was installed, is
        gone" — for a tool that never existed. Only the second is withdrawn."""
        refusal = {"type": "text", "text": 'In ptc Code Mode, "X" is not a '
                   'direct tool - it is a REPL sandbox global.'}
        self.conn.execute("INSERT INTO inventory(kind,name,owner_plugin)"
                          " VALUES('tool','Bash','')")
        write_records(self.tr, [
            {"type": "function_call", "name": "REPO_PLACEHOLDER",
             "callId": "bad", "sessionId": "s1", "cwd": "/p", "timestamp": 10},
            {"type": "function_call_result", "name": "REPO_PLACEHOLDER",
             "callId": "bad", "status": "incomplete", "timestamp": 20,
             "output": refusal},
            {"type": "function_call", "name": "Bash", "callId": "kept",
             "sessionId": "s1", "cwd": "/p", "timestamp": 30},
            {"type": "function_call_result", "name": "Bash", "callId": "kept",
             "status": "incomplete", "timestamp": 40, "output": refusal},
        ])
        unparsed = {}
        sync.index_file(self.conn, self.tr, 0, {}, {}, {}, unparsed)
        for reason, n in unparsed.items():
            db.note_unparsed(self.conn, reason, n)
        self.conn.commit()
        kept = [r["tool_name"] for r in self.conn.execute(
            "SELECT tool_name FROM tool_calls")]
        self.assertEqual(kept, ["Bash"],
                         "a refused call to a catalogued tool is still a call")
        row = self.conn.execute("SELECT count FROM unparsed WHERE reason=?",
                                (sync.INVALID_TOOL_NAME,)).fetchone()
        self.assertIsNotNone(row, "the withdrawn call was not counted")
        self.assertEqual(row[0], 1)

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
        # inventory PK includes owner_plugin; NULL there would defeat dedup. Scan a
        # throwaway host (never the real ~/.codebuddy): re-scanning must not grow the
        # table, and a NULL owner_plugin would insert a twin instead of replacing.
        root = self._cb_root()
        d = root / "skills" / "ai" / "add-tests"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.scan_inventory(self.conn)
            n1 = self.count("inventory")
            self.assertGreater(n1, 0, "the fixture host produced nothing to dedup")
            sync.scan_inventory(self.conn)
            sync.scan_inventory(self.conn)
        finally:
            sync.db.CODEBUDDY_DIR = old
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

    def test_catalogue_models_and_commands_are_inventoried(self):
        """The package's own arrays decide the inventory list.

        Tokens is usage-only, but ``cbut inventory`` still answers "what does the
        installed package offer", so those rows have to be there.
        """
        snapshot = json.loads((
            Path(__file__).resolve().parent / "fixtures"
            / "product.json.snapshot").read_text(encoding="utf-8"))
        root = self._cb_root()
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.scan_inventory(self.conn)
        finally:
            sync.db.CODEBUDDY_DIR = old
        models = {r[0] for r in self.conn.execute(
            "SELECT name FROM inventory WHERE kind='model'")}
        commands = {r[0] for r in self.conn.execute(
            "SELECT name FROM inventory WHERE kind='command'")}
        self.assertEqual(models, set(snapshot["models"]))
        self.assertEqual(commands, set(snapshot["commands"]))
        provider = self.conn.execute(
            "SELECT owner_plugin FROM inventory WHERE kind='model'"
            " AND name=?", (sorted(models)[0],)).fetchone()[0]
        self.assertEqual(provider, "builtin")

    def test_custom_model_route_ids_are_not_inventoried(self):
        """``models.json`` holds route ids, not models: ``ark-code-latest``
        answers as ``deepseek-v4-1-flash``. Inventoring it put a zero-request
        Tokens row under a name nobody had called. The file is still read for
        the url host that labels a real response (``Volcano Ark``).
        """
        root = self._cb_root()
        (root / "models.json").write_text(json.dumps({"models": [
            {"id": "custom-ark",
             "url": "https://ark.cn-beijing.volces.com/api/coding/v3"},
            {"id": "custom-nourl"},
        ]}), encoding="utf-8")
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = root
        try:
            sync.scan_inventory(self.conn)
        finally:
            sync.db.CODEBUDDY_DIR = old
        user_models = {r[0] for r in self.conn.execute(
            "SELECT name FROM inventory WHERE kind='model' AND source='user'")}
        self.assertEqual(user_models, set())
        self.assertEqual(sync.load_model_hosts(root),
                         {"custom-ark": "ark.cn-beijing.volces.com"})

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
        # ...and credit verbatim in its own billing column
        self.assertEqual(row["credit"], 3.5)
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

    def test_credit_is_stored_beside_tokens_never_as_one(self):
        raw = {"prompt_tokens": 1, "completion_tokens": 1,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
               "credit": 42.0}
        write_records(self.tr, [model_rec("m8", raw=raw)])
        self.index()
        row = self.model_row("m8")
        self.assertEqual(row["credit"], 42.0)
        # The billing figure lives in its own column and changes no token field.
        self.assertEqual(row["prompt_tokens"], 1)
        self.assertEqual(row["completion_tokens"], 1)
        self.assertIsNone(row["provider_total_tokens"])

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

    # -- the nested cache copy a custom endpoint is the only one sending -----
    # Real transcripts write the cache number two ways: built-in routes put it in
    # ``prompt_cache_hit_tokens`` *and* repeat the identical value in
    # ``prompt_tokens_details.cached_tokens``; a ``custom-local`` route sends only
    # the nested one. Measured 2026-10-11: built-in 19,358 records, both spellings
    # summing to exactly 2,614,796,037; custom 712 records with no top-level field
    # at all and 67,664,640 in the nested one — which is why every custom row in
    # the database read as NULL while the number existed the whole time.

    def test_a_nested_cache_hit_is_recorded_when_the_top_level_is_absent(self):
        raw = {"prompt_tokens": 10, "completion_tokens": 2,
               "prompt_tokens_details": {"cached_tokens": 6, "audio_tokens": 0}}
        write_records(self.tr, [model_rec("c3n", raw=raw)])
        self.index()
        row = self.model_row("c3n")
        self.assertEqual(row["prompt_cache_hit_tokens"], 6)
        # The boundary, stated in the data rather than in prose: miss and write
        # really are not sent by this route, so they must stay NULL.
        self.assertIsNone(row["prompt_cache_miss_tokens"])
        self.assertIsNone(row["prompt_cache_write_tokens"])

    def test_the_two_spellings_of_one_cache_number_are_not_added(self):
        """Built-in transcripts carry the same value twice, so the nested copy is a
        fallback and never a second source: summing them would report twice the
        cache every built-in model actually used.
        """
        raw = {"prompt_tokens": 100, "completion_tokens": 20,
               "prompt_cache_hit_tokens": 80,
               "prompt_tokens_details": {"cached_tokens": 80}}
        write_records(self.tr, [model_rec("c3d", raw=raw)])
        self.index()
        self.assertEqual(self.model_row("c3d")["prompt_cache_hit_tokens"], 80)

    def test_an_explicit_zero_cache_hit_survives_a_nested_value(self):
        """``0`` is a measurement, not a gap — the top level still outranks it."""
        raw = {"prompt_tokens": 10, "completion_tokens": 2,
               "prompt_cache_hit_tokens": 0,
               "prompt_tokens_details": {"cached_tokens": 5}}
        write_records(self.tr, [model_rec("c3z", raw=raw)])
        self.index()
        self.assertEqual(self.model_row("c3z")["prompt_cache_hit_tokens"], 0)

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

    def test_credit_is_never_added_into_a_token_total(self):
        raw = {"prompt_tokens": 5, "completion_tokens": 5, "credit": 12.5,
               "prompt_cache_hit_tokens": 1}
        write_records(self.tr, [model_rec("c7", raw=raw)])
        self.index()
        r = db.q_model_responses(self.conn, model="test-model")[0]
        self.assertEqual(r["total_tokens"], 10)
        self.assertEqual(r["credit"], 12.5)

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

    def test_a_known_unclaimed_type_is_counted_but_is_not_drift(self):
        """The three prose types are named, so "unparsed" can mean drift again.

        They stay inside the tally — removing them would hide the day one of them
        starts carrying something we index — and what they gain is a name, so every
        view can tell a familiar reason from a new one. This pins the vocabulary the
        views share; the views themselves are pinned where they print.
        """
        for reason in ("type:reasoning", "type:file-history-snapshot",
                       "type:summary"):
            self.assertTrue(sync.unparsed_is_expected(reason), reason)
        # A model emitting a name no tool has is understood and recurring, not
        # the format moving: counted as drift it put a permanent "!" on the
        # status line, which is the bar-nobody-reads failure the split prevents.
        self.assertTrue(sync.unparsed_is_expected(sync.INVALID_TOOL_NAME))
        # Losing data is never "expected", whatever else the build knows: these are
        # the reader dropping something, not declining it.
        for reason in ("unparseable_line", "type:<missing>",
                       "unreadable_file:OSError", "type:watermark"):
            self.assertFalse(sync.unparsed_is_expected(reason), reason)

    def test_split_unparsed_separates_known_records_from_new_ones(self):
        rows = [{"reason": "type:reasoning", "count": 22146},
                {"reason": "type:summary", "count": 528},
                {"reason": "type:watermark", "count": 3},
                {"reason": "unparseable_line", "count": 2}]
        expected, unexpected, names = sync.split_unparsed(rows)
        self.assertEqual(expected, 22146 + 528)
        self.assertEqual(unexpected, 5)
        self.assertEqual(names, ["type:watermark", "unparseable_line"])

    def test_no_type_is_both_claimed_and_declined(self):
        """Two sets describing one question have to disagree about nothing.

        A record type in both `HANDLED_RECORD_TYPES` and `UNCLAIMED_RECORD_TYPES`
        would be indexed by a branch and described as deliberately not indexed,
        which is two answers to "why is this not in the tables".
        """
        overlap = sync.HANDLED_RECORD_TYPES & sync.UNCLAIMED_RECORD_TYPES
        self.assertEqual(overlap, set(),
                         f"a type is both claimed and declined: {overlap}")


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


class ProviderOriginTest(unittest.TestCase):
    """The route-origin column: read from requestModelId, never from the model name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.tr = self.dir / "s.jsonl"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def index(self, model_hosts=None):
        sync.index_file(self.conn, self.tr, 0, {}, {}, {},
                        model_hosts=model_hosts)
        self.conn.commit()

    def provider_of(self, message_id):
        return self.conn.execute(
            "SELECT provider FROM model_responses WHERE message_id=?",
            (message_id,)).fetchone()["provider"]

    # -- pure derivation ---------------------------------------------------
    def test_resolve_provider_cascade(self):
        hosts = {"ark-code-latest": "ark.cn-beijing.volces.com"}
        self.assertIsNone(sync.resolve_provider(None, hosts))
        self.assertIsNone(sync.resolve_provider("", hosts))
        self.assertEqual(sync.resolve_provider("deepseek-v4.1-flash", hosts),
                         "builtin")
        self.assertEqual(
            sync.resolve_provider("custom-local:ark-code-latest", hosts),
            "Volcano Ark")
        self.assertEqual(sync.resolve_provider("custom-local:not-listed", hosts),
                         "custom-local")
        self.assertEqual(
            sync.resolve_provider("custom-local:x", {"x": "llm.internal.example"}),
            "llm.internal.example")
        self.assertEqual(
            sync.resolve_provider("openai:gpt-x", {"gpt-x": "api.openai.com"}),
            "OpenAI")

    def test_url_host(self):
        self.assertEqual(
            sync.url_host("https://ark.cn-beijing.volces.com/api/coding/v3"),
            "ark.cn-beijing.volces.com")
        self.assertEqual(sync.url_host("http://Host.EXAMPLE:8080/x"),
                         "host.example")
        self.assertEqual(sync.url_host("https://user:pw@api.example.com/v1"),
                         "api.example.com")
        self.assertEqual(sync.url_host(""), "")
        self.assertEqual(sync.url_host(None), "")

    # -- models.json reader ------------------------------------------------
    def test_load_model_hosts_reads_only_id_and_url(self):
        (self.dir / "models.json").write_text(json.dumps({
            "models": [{
                "id": "ark-code-latest", "name": "ark-code-latest",
                "url": "https://ark.cn-beijing.volces.com/api/coding/v3",
                "apiKey": "ark-SECRET-DO-NOT-READ",
            }],
        }), encoding="utf-8")
        hosts = sync.load_model_hosts(self.dir)
        self.assertEqual(hosts, {"ark-code-latest": "ark.cn-beijing.volces.com"})
        self.assertNotIn("ark-SECRET-DO-NOT-READ", json.dumps(hosts))

    def test_sync_never_names_the_api_key_field(self):
        src = (SCRIPTS / "cbut-sync.py").read_text(encoding="utf-8")
        for token in ('"apiKey"', "'apiKey'", ".apiKey"):
            self.assertNotIn(
                token, src,
                "models.json carries an api key; the parser must never read it")

    def test_load_model_hosts_degrades(self):
        self.assertEqual(sync.load_model_hosts(self.dir), {})   # no file
        (self.dir / "models.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(sync.load_model_hosts(self.dir), {})
        (self.dir / "models.json").write_text(
            json.dumps({"models": [{"id": "x"}]}), encoding="utf-8")
        self.assertEqual(sync.load_model_hosts(self.dir), {})   # no url

    # -- recorded into model_responses -------------------------------------
    def test_builtin_route_is_recorded(self):
        write_records(self.tr, [model_rec(
            "m1", raw={"prompt_tokens": 1, "completion_tokens": 1},
            request_model_id="deepseek-v4.1-flash")])
        self.index()
        self.assertEqual(self.provider_of("m1"), "builtin")

    def test_custom_route_uses_the_vendor_of_a_known_host(self):
        write_records(self.tr, [model_rec(
            "m2", raw={"prompt_tokens": 1, "completion_tokens": 1},
            model="deepseek-v4-1-flash",
            request_model_id="custom-local:ark-code-latest")])
        self.index(model_hosts={"ark-code-latest": "ark.cn-beijing.volces.com"})
        self.assertEqual(self.provider_of("m2"), "Volcano Ark")

    def test_custom_route_without_a_known_model_uses_the_endpoint_tag(self):
        write_records(self.tr, [model_rec(
            "m3", raw={"prompt_tokens": 1, "completion_tokens": 1},
            request_model_id="custom-local:mystery")])
        self.index(model_hosts={})
        self.assertEqual(self.provider_of("m3"), "custom-local")

    def test_custom_route_without_a_known_vendor_uses_the_host(self):
        write_records(self.tr, [model_rec(
            "m4", raw={"prompt_tokens": 1, "completion_tokens": 1},
            request_model_id="custom-local:mystery")])
        self.index(model_hosts={"mystery": "llm.internal.example"})
        self.assertEqual(self.provider_of("m4"), "llm.internal.example")

    def test_provider_is_recorded_even_without_rawusage(self):
        write_records(self.tr, [model_rec(
            "m5", raw=None, request_model_id="custom-local:ark-code-latest")])
        self.index(model_hosts={"ark-code-latest": "ark.cn-beijing.volces.com"})
        self.assertEqual(self.provider_of("m5"), "Volcano Ark")

    def test_provider_is_not_clobbered_by_a_later_null_copy(self):
        # A usage-bearing record sets the origin; a second copy of the same
        # messageId that carries no requestModelId must not erase it.
        write_records(self.tr, [
            model_rec("m6", raw={"prompt_tokens": 1, "completion_tokens": 1},
                      request_model_id="deepseek-v4.1-flash"),
            model_rec("m6", raw={"prompt_tokens": 2, "completion_tokens": 2},
                      request_model_id=None),
        ])
        self.index()
        self.assertEqual(self.provider_of("m6"), "builtin")


class BuiltinCatalogTest(unittest.TestCase):
    """The builtin tool/agent catalogue is read from the installed package.

    Before this, ``BUILTIN_TOOLS`` was 35 hand-typed names and the installed
    package listed 61: the tool a panel said did not exist, the tool CodeBuddy
    would happily have run. The check below is that the package decides, and that
    the checked-in constants are a *fallback* rather than the answer.
    """

    SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "product.json.snapshot"

    def catalog(self, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            return sync.builtin_catalog()

    def test_the_installed_package_decides_membership(self):
        """``cli`` is in product.json and was never in the hand-typed table;
        ``fork`` is in the table and not in the package. Neither fact may be lost
        by the hand-off in ``builtin_catalog``."""
        raw = json.loads(self.SNAPSHOT.read_text(encoding="utf-8"))
        tools, agents, path = self.catalog(
            {"CBUT_PRODUCT_JSON": str(self.SNAPSHOT)})
        self.assertEqual(path, self.SNAPSHOT)
        self.assertIn("cli", agents, "a name the package ships must be inventoried")
        self.assertNotIn("fork", agents,
                         "a name the package dropped must stop being inventoried "
                         "-- otherwise it lingers forever as a 0-call row")
        self.assertEqual(set(tools), set(raw[sync.PRODUCT_TOOLS_FIELD]))
        self.assertEqual(set(agents), set(raw[sync.PRODUCT_AGENTS_FIELD]))

    def test_an_unresolvable_package_falls_back_to_the_checked_in_constants(self):
        """No package, no empty Tools panel: the shipped constants answer."""
        tools, agents, path = self.catalog(
            {"CBUT_PRODUCT_JSON": "/nonexistent/cbut/product.json"})
        self.assertIsNone(path)
        self.assertEqual(tools, frozenset(sync.BUILTIN_TOOLS))
        self.assertEqual(agents, sync.BUILTIN_AGENTS)
        self.assertIn("fork", agents)

    def test_an_override_naming_nothing_locates_nothing(self):
        """A pinned path that is not a file locates nothing.

        Handing the path back anyway would make `cbut health` print a file that
        does not exist as the catalogue in force — naming the one thing health
        exists to disambiguate. Path-scoped, so the PATH scan is not needed:
        the override short-circuits before it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            for pinned in (Path(tmp) / "absent.json", Path(tmp)):
                with self.subTest(pinned=pinned):
                    with mock.patch.dict(os.environ,
                                         {"CBUT_PRODUCT_JSON": str(pinned)},
                                         clear=False):
                        self.assertIsNone(sync.resolve_product_json())
        with mock.patch.dict(os.environ,
                             {"CBUT_PRODUCT_JSON": str(self.SNAPSHOT)},
                             clear=False):
            self.assertEqual(sync.resolve_product_json(), self.SNAPSHOT)

    def test_a_product_json_without_a_catalogue_falls_back_rather_than_half_reads(self):
        """The four variant configs (internal, cloudhosted, ioa, selfhosted) carry
        no ``tools``/``agents`` key at all. Reading one must not quietly empty the
        Tools panel -- falling back to the constants is the honest answer."""
        with tempfile.TemporaryDirectory() as tmp:
            variant = Path(tmp) / sync.PRODUCT_JSON
            variant.write_text(json.dumps({"productName": "cloudhosted"}),
                               encoding="utf-8")
            tools, agents, path = self.catalog({"CBUT_PRODUCT_JSON": str(variant)})
        self.assertIsNone(path)
        self.assertEqual(tools, frozenset(sync.BUILTIN_TOOLS))

    def test_an_unreadable_or_corrupt_file_falls_back(self):
        for name, body in (("garbage.json", "{not json"),
                           ("empty.json", "")):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    bad = Path(tmp) / name
                    bad.write_text(body, encoding="utf-8")
                    tools, _agents, path = self.catalog(
                        {"CBUT_PRODUCT_JSON": str(bad)})
                self.assertIsNone(path)
                self.assertEqual(tools, frozenset(sync.BUILTIN_TOOLS))

    def test_kind_stays_our_taxonomy_not_the_packages(self):
        """product.json has no active/internal field -- it is cbut's own
        distinction, and an internal agent must not be reclassified because the
        package stopped mentioning it."""
        _tools, agents, _path = self.catalog(
            {"CBUT_PRODUCT_JSON": str(self.SNAPSHOT)})
        self.assertEqual(agents["compact"], "internal")
        self.assertEqual(agents["contextSummary"], "internal")
        self.assertEqual(agents["Explore"], "active")

    def test_the_resolver_finds_the_package_by_walking_up_from_the_executable(self):
        """The real search, not the override: a fake prefix whose ``codebuddy`` is
        a symlink into a nested directory, with the package root above it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pkg" / "node_modules" / "codebuddy-code"
            (root / "bin").mkdir(parents=True)
            target = root / sync.PRODUCT_JSON
            target.write_text(json.dumps({sync.PRODUCT_TOOLS_FIELD: [{"name": "Bash"}],
                                          sync.PRODUCT_AGENTS_FIELD: [{"name": "cli"}]}),
                              encoding="utf-8")
            launcher = root / "bin" / "codebuddy"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)          # PATH entries are only tried if executable
            prefix = Path(tmp) / "bin"
            prefix.mkdir()
            (prefix / sync.PRODUCT_CLI_NAME).symlink_to(root / "bin" / "codebuddy")
            tools, agents, found = self.catalog(
                {"CBUT_PRODUCT_JSON": "", "PATH": str(prefix)})
        self.assertEqual(found, target)
        self.assertEqual(set(tools), {"Bash"})
        self.assertEqual(set(agents), {"cli"})

    def test_an_empty_path_resolves_to_nothing_rather_than_raising(self):
        tools, _agents, path = self.catalog(
            {"CBUT_PRODUCT_JSON": "", "PATH": ""})
        self.assertIsNone(path)
        self.assertEqual(tools, frozenset(sync.BUILTIN_TOOLS))

    def test_no_package_means_no_subprocess(self):
        """The refusal to shell out is load-bearing, not stylistic: the obvious
        implementation of "find the CLI" is ``readlink $(which codebuddy)`` and
        that breaks the invariant ``test_privacy`` enforces."""
        source = Path(sync.__file__).read_text(encoding="utf-8")
        body = source[source.index("def resolve_product_json"):]
        body = body[:body.index("\ndef ")] if "\ndef " in body else body
        for forbidden in ("subprocess", "Popen", "os.system", "shutil.which"):
            self.assertNotIn(forbidden, body,
                             f"resolve_product_json must not use {forbidden}")

    def test_the_snapshot_has_not_drifted_from_the_installed_package(self):
        """The snapshot is what a machine with no CLI compares against, so it has
        to be current. Skipped where there is nothing to compare against -- but
        the snapshot is still parsed, so an emptied or hand-edited fixture fails
        everywhere instead of passing quietly."""
        snapshot = json.loads(self.SNAPSHOT.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(snapshot[sync.PRODUCT_TOOLS_FIELD]), 30,
                                "the snapshot lost its tools -- regenerate it")
        self.assertGreaterEqual(len(snapshot[sync.PRODUCT_AGENTS_FIELD]), 10,
                                "the snapshot lost its agents -- regenerate it")
        with mock.patch.dict(os.environ, {"CBUT_PRODUCT_JSON": ""}, clear=False):
            live_path = sync.resolve_product_json()
        if live_path is None:
            self.skipTest("no CodeBuddy package installed to compare the snapshot against")
        live = sync._load_json(live_path)
        for key in (sync.PRODUCT_TOOLS_FIELD, sync.PRODUCT_AGENTS_FIELD):
            self.assertEqual(
                sorted(set(sync._catalog_names(live.get(key)))),
                snapshot[key],
                f"{key} drifted from {live_path}. Re-run "
                "scripts/tests/product_snapshot.py and re-read the surface.")


class InventorySnapshotTest(unittest.TestCase):
    """``inventory`` answers "what is installed now", and nothing else.

    It used to be written with INSERT OR REPLACE and never deleted from, so a
    plugin that was uninstalled and a skill directory that was removed kept their
    rows forever, and every panel kept listing them as if they were installed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        db.ensure_schema(self.conn)
        self.root = Path(self.tmp.name) / "cb"
        (self.root / "projects").mkdir(parents=True)
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        self.addCleanup(setattr, sync.db, "CODEBUDDY_DIR", old)
        self.addCleanup(self.tmp.cleanup)

    def scan(self):
        return sync.scan_inventory(self.conn)

    def names(self, kind=None):
        sql = "SELECT name FROM inventory"
        if kind:
            sql += " WHERE kind=?"
        return {r[0] for r in self.conn.execute(sql, (kind,) if kind else ())}

    def make_skill(self, rel):
        d = self.root / "skills" / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
        return d

    def make_plugin(self, name, version="1", subdirs=(), root_manifest=False):
        """Lay out one installed plugin the way the cache does: a directory per
        plugin, a version directory inside it, and ``installPath`` pointing at
        that version directory (so a plugin *is* its version directory as far as
        the parser can see)."""
        ipath = self.root / "plugins" / "cache" / name / version
        ipath.mkdir(parents=True)
        for sub in subdirs:
            (ipath / sub).mkdir()
        if root_manifest:
            (ipath / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        self.installed(name, ipath, version)
        return ipath

    def installed(self, name, ipath, version="1", marketplace="official"):
        """Add one install to installed_plugins.json, keeping what is already there."""
        plugins = self.root / "plugins"
        plugins.mkdir(exist_ok=True)
        index = plugins / sync.PLUGIN_INDEX_FILE
        data = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        data.setdefault("plugins", {}).setdefault(
            f"{name}@{marketplace}", []).append(
            {"installPath": str(ipath), "version": version})
        index.write_text(json.dumps(data), encoding="utf-8")

    def state_of(self, kind, name):
        """The state the panels would render for this row, read through the real
        query rather than through the flags the test just wrote."""
        query, key = {"skill": (db.q_skills, "skill"),
                      "agent": (db.q_agents, "agent")}[kind]
        for row in query(self.conn):
            if row[key] == name:
                return db.entity_state(row["installed"], row["ever_used"])
        return None

    def use(self, kind, name, ts=None):
        """Record a call against a name, the way the transcript reader would."""
        now = ts if ts is not None else 1_700_000_000
        if kind == "skill":
            self.conn.execute(
                "INSERT OR IGNORE INTO skill_usage(call_id, skill, ts, status)"
                " VALUES(?,?,?,'completed')", (f"c-{name}", name, now))
        else:
            self.conn.execute(
                "INSERT OR IGNORE INTO agent_usage(call_id, agent_type, ts, status)"
                " VALUES(?,?,?,'completed')", (f"c-{name}", name, now))
        self.conn.commit()

    def plugin_of(self, skill):
        """The owner `skill_usage` carries for this skill, as stored."""
        row = self.conn.execute(
            "SELECT plugin FROM skill_usage WHERE skill=?", (skill,)).fetchone()
        return row[0] if row else None

    # -- Q1: symlinked skills ------------------------------------------------

    def test_a_symlinked_skill_is_found(self):
        """The bug that emptied the Skills panel: ``~/.codebuddy/skills`` is 27
        symlinks into ``~/.agents/skills``, and ``glob("**/SKILL.md")`` does not
        descend into a symlinked directory -- it matched exactly one entry, a
        dot-directory. Nothing installed was visible."""
        real = Path(self.tmp.name) / "elsewhere" / "grilling"
        real.mkdir(parents=True)
        (real / sync.SKILL_MANIFEST).write_text("# fixture\n", encoding="utf-8")
        (self.root / "skills").mkdir()
        (self.root / "skills" / "grilling").symlink_to(real)
        self.scan()
        self.assertIn("grilling", self.names("skill"),
                      "a symlinked skill is not visible to a ** glob")

    def test_a_real_skill_beside_a_symlinked_one_is_still_found(self):
        real = Path(self.tmp.name) / "elsewhere" / "linked-skill"
        real.mkdir(parents=True)
        (real / sync.SKILL_MANIFEST).write_text("# fixture\n", encoding="utf-8")
        (self.root / "skills").mkdir()
        (self.root / "skills" / "linked-skill").symlink_to(real)
        self.make_skill("plain-skill")
        self.scan()
        self.assertEqual(self.names("skill"), {"linked-skill", "plain-skill"})

    def test_the_retired_category_layout_is_still_read(self):
        """The old layout was skills/<category>/<skill>/SKILL.md. Following
        symlinks must not have come at the cost of it."""
        self.make_skill("ai/add-tests")
        self.make_skill("backend/api-contract")
        self.scan()
        self.assertEqual(self.names("skill"), {"add-tests", "api-contract"})

    def test_a_symlink_without_a_manifest_is_not_a_skill(self):
        dangling = Path(self.tmp.name) / "gone"
        (self.root / "skills").mkdir()
        (self.root / "skills" / "broken").symlink_to(dangling)
        self.make_skill("real-one")
        self.scan()
        self.assertEqual(self.names("skill"), {"real-one"})

    def test_a_symlink_is_never_descended_into(self):
        """One level, exactly. A link pointing at a tree of skills must contribute
        its own directory or nothing -- following it would turn a second skill
        store into a second source of truth for the same panel."""
        outer = Path(self.tmp.name) / "elsewhere" / "container"
        inner = outer / "hidden-skill"
        inner.mkdir(parents=True)
        (inner / sync.SKILL_MANIFEST).write_text("# fixture\n", encoding="utf-8")
        (outer / sync.SKILL_MANIFEST).write_text("# fixture\n", encoding="utf-8")
        (self.root / "skills").mkdir()
        (self.root / "skills" / "container").symlink_to(outer)
        self.scan()
        self.assertEqual(self.names("skill"), {"container"})

    def test_a_dot_directory_is_never_a_skill(self):
        """``.skill`` was inventoried as a skill because the hidden-prefix rule
        only ran inside a plugin."""
        self.make_skill(".skill")
        self.make_skill("visible")
        self.scan()
        self.assertEqual(self.names("skill"), {"visible"})

    def test_a_dot_directory_is_not_searched_for_nested_skills(self):
        self.make_skill(".cache/nested-skill")
        self.scan()
        self.assertEqual(self.names("skill"), set())

    # -- plugin-root skills --------------------------------------------------

    def test_a_plugin_that_is_a_skill_is_inventoried_as_one(self):
        """The report: a plugin that ships ``<installPath>/SKILL.md`` and nothing
        else CodeBuddy can count. docx, pptx and xlsx are installed in exactly
        that shape. Scanned as subdirectories only, they never reached the table,
        so the union had nothing to call installed and printed "removed" for a
        skill the user is looking straight at."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.scan()
        self.assertIn("docx", self.names("skill"))

    def test_a_used_plugin_skill_is_not_reported_as_removed(self):
        """The symptom, end to end: the same row read back through q_skills."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.scan()
        self.use("skill", "docx")
        self.assertEqual(self.state_of("skill", "docx"), db.ENTITY_STATE_USED)

    def test_the_plugins_own_name_is_used_not_the_versions_directory(self):
        """installPath ends in the version, so the directory would file the skill
        under "unknown" and "1.3.0" -- names nothing can ever call it by."""
        self.make_plugin("pptx", version="unknown", root_manifest=True)
        self.make_plugin("agent-browser", version="1.3.0", root_manifest=True)
        self.scan()
        self.assertEqual(self.names("skill"), {"pptx", "agent-browser"})

    def test_a_plugin_without_a_root_manifest_is_not_a_skill(self):
        """jdtls-lsp ships an LSP server and two READMEs. That is not a skill,
        and inventing one would put a row in the panel with no file behind it."""
        self.make_plugin("jdtls-lsp", subdirs=("references",))
        self.scan()
        self.assertEqual(self.names("skill"), set())

    def test_a_root_manifest_does_not_hide_the_plugin_skill_directory(self):
        """Both surfaces are read; one is not a substitute for the other."""
        ipath = self.make_plugin("mixed", version="2.0", root_manifest=True,
                                 subdirs=("skills",))
        (ipath / "skills" / "extra").mkdir()
        (ipath / "skills" / "extra" / "SKILL.md").write_text("# x\n", encoding="utf-8")
        self.scan()
        self.assertEqual(self.names("skill"), {"mixed", "extra"})

    def test_a_root_manifest_skill_is_attributed_to_its_plugin(self):
        """Without the owner the call could not be counted anywhere: skill_usage
        stores the plugin name, and plugin_usage is built from that map. This is
        why the Plugins panel showed docx at zero uses."""
        self.make_plugin("xlsx", version="unknown", root_manifest=True)
        skill_owner, _agent_owner, _command_owner = self.scan()
        self.assertEqual(skill_owner["xlsx"], "xlsx")

    # -- the owner fill ------------------------------------------------------

    def test_a_historical_call_is_attributed_once_the_inventory_names_it(self):
        """The 8 docx calls written before the inventory could see a plugin-root
        skill: `plugin` stayed NULL because the map had no entry, and nothing
        ever revisited the row. The fill is what makes them countable."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.scan()
        self.use("skill", "docx")
        self.assertIsNone(self.plugin_of("docx"), "fixture wrote an owner itself")

        sync.backfill_plugin_usage(self.conn)

        self.assertEqual(self.plugin_of("docx"), "docx")
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT plugin, kind, target FROM plugin_usage")]
        self.assertEqual(rows, [("docx", "skill", "docx")])

    def test_a_skill_no_plugin_owns_is_left_null(self):
        """The user's own library is not a plugin's: no inventory row names it,
        so the fill must not invent an owner."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.make_skill("grilling")
        self.scan()
        self.use("skill", "grilling")

        sync.backfill_plugin_usage(self.conn)

        self.assertIsNone(self.plugin_of("grilling"))

    def test_the_fill_never_overwrites_an_owner(self):
        """A row that already names a plugin keeps it: the write-time answer is
        evidence, the fill only supplies what was missing."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.scan()
        self.use("skill", "docx")
        self.conn.execute(
            "UPDATE skill_usage SET plugin='elsewhere' WHERE skill='docx'")
        self.conn.commit()

        sync.backfill_plugin_usage(self.conn)

        self.assertEqual(self.plugin_of("docx"), "elsewhere")

    def test_a_removed_plugin_takes_its_root_skill_with_it(self):
        """The snapshot has to answer both ways: a plugin that ships a root skill
        cannot leave one behind when it is uninstalled."""
        self.make_plugin("docx", version="unknown", root_manifest=True)
        self.scan()
        self.assertIn("docx", self.names("skill"))
        index = self.root / "plugins" / sync.PLUGIN_INDEX_FILE
        index.write_text(json.dumps({"plugins": {}}), encoding="utf-8")
        self.scan()
        self.assertNotIn("docx", self.names("skill"))

    # -- Q2: the snapshot replaces rather than accumulates -------------------

    def test_a_removed_skill_leaves_the_inventory(self):
        skill = self.make_skill("temporary")
        self.scan()
        self.assertIn("temporary", self.names("skill"))
        shutil.rmtree(skill)
        self.scan()
        self.assertNotIn("temporary", self.names("skill"),
                         "a skill deleted from disk is still listed as installed")

    def test_a_removed_plugin_leaves_the_inventory(self):
        plugins = self.root / "plugins"
        plugins.mkdir()
        index = plugins / sync.PLUGIN_INDEX_FILE
        index.write_text(json.dumps({"plugins": {"keep@official": [
            {"installPath": str(plugins / "keep"), "version": "1"}],
            "drop@official": [{"installPath": str(plugins / "drop"), "version": "1"}]}}),
            encoding="utf-8")
        self.scan()
        self.assertIn("drop", self.names("plugin"))
        index.write_text(json.dumps({"plugins": {"keep@official": [
            {"installPath": str(plugins / "keep"), "version": "1"}]}}), encoding="utf-8")
        self.scan()
        self.assertEqual(self.names("plugin"), {"keep"})

    def test_a_removed_agent_leaves_the_inventory(self):
        agents = self.root / "agents"
        agents.mkdir()
        spec = agents / "code-reviewer.md"
        spec.write_text("# fixture\n", encoding="utf-8")
        self.scan()
        self.assertIn("code-reviewer", self.names("agent"))
        spec.unlink()
        self.scan()
        self.assertNotIn("code-reviewer", self.names("agent"))

    def test_a_failed_scan_leaves_the_previous_snapshot_alone(self):
        """All-or-nothing. A half-completed scan that emptied the table would be
        worse than a stale one: every panel would read as empty while the history
        underneath it was intact."""
        self.make_skill("before-the-failure")
        self.scan()
        before = self.names("skill")

        original = sync.collect_inventory
        def exploding():
            rows, owners, path = original()
            raise OSError("the filesystem went away mid-scan")
        with mock.patch.object(sync, "collect_inventory", exploding):
            with self.assertRaises(OSError):
                self.scan()
        self.assertEqual(self.names("skill"), before,
                         "a failed scan must not clear what was already known")

    def test_the_replace_is_one_transaction(self):
        """A reader must never see the empty gap between the delete and the
        insert, and a failure mid-insert must leave the old rows.

        The failure is induced with a real trigger rather than a mocked
        ``executemany`` — ``sqlite3.Connection`` does not accept attribute
        patching, and a proxy would test the proxy. ``RAISE(ABORT)`` inside a
        statement rolls that statement back, which is exactly the shape of the
        interruption being guarded against.
        """
        self.make_skill("keep-me")
        self.scan()
        self.conn.execute(
            "CREATE TRIGGER refuse_new_rows BEFORE INSERT ON inventory"
            " BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            sync.replace_inventory(
                self.conn, [("skill", "ghost", "", None, None, "user")])
        self.conn.execute("DROP TRIGGER refuse_new_rows")
        self.assertEqual(self.names("skill"), {"keep-me"},
                         "the delete and the insert are not in one transaction")

    def test_rescanning_is_idempotent(self):
        self.make_skill("stable")
        for _ in range(3):
            self.scan()
        self.assertEqual(self.names("skill"), {"stable"})


class SyncLockTest(unittest.TestCase):
    """Two syncs at once must not double the accumulator tables.
    'sessions' adds its tokenDelta again and 'unparsed' bumps its counts
    again on every pass -- nothing in their rows identifies a repeat. The
    whole-run flock serializes writers; these tests pin that the sidecar
    exists beside the database, that a second writer genuinely waits, and
    that the serialized path counts one turn once.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.root = Path(self.tmp.name) / "cb"
        (self.root / "projects" / "p").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()
    def _run(self):
        old = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root
        try:
            return sync.run(quiet=True, db_path=self.db_path)
        finally:
            sync.db.CODEBUDDY_DIR = old

    def _write_turn(self):
        rec = {"type": "turn-metrics", "timestamp": 1000, "tokenDelta": 100,
               "durationMs": 1000, "cwd": "/p",
               "_meta": {"baggage": "codebuddy.session_id=S"}}
        (self.root / "projects" / "p" / "s.jsonl").write_text(
            json.dumps(rec) + "\n", encoding="utf-8")
    def test_the_sidecar_lives_beside_the_database(self):
        fd = sync._acquire_sync_lock(self.db_path)
        try:
            self.assertIsNotNone(fd)
            self.assertTrue(Path(str(self.db_path) + ".lock").exists(),
                            "the lock must be a sidecar beside the db")
        finally:
            sync._release_sync_lock(fd)
        again = sync._acquire_sync_lock(self.db_path)
        self.assertIsNotNone(again, "a released lock must be acquirable")
        sync._release_sync_lock(again)
    def test_a_second_writer_blocks_until_the_first_releases(self):
        fd = sync._acquire_sync_lock(self.db_path)
        got = []
        worker = threading.Thread(
            target=lambda: got.append(sync._acquire_sync_lock(self.db_path)))
        worker.start()
        try:
            time.sleep(0.3)
            self.assertEqual(got, [],
                             "the second writer must wait while the lock is held")
        finally:
            sync._release_sync_lock(fd)
        worker.join(timeout=5)
        self.assertEqual(len(got), 1, "the waiter must acquire after release")
        sync._release_sync_lock(got[0])
    def test_serialized_runs_count_one_turn_once(self):
        self._write_turn()
        self._run()
        self.assertTrue(Path(str(self.db_path) + ".lock").exists(),
                        "run() must hold the lock for the whole pass")
        self._run()
        conn = db.open_db(self.db_path)
        try:
            row = conn.execute("SELECT tokens FROM sessions"
                               " WHERE session_id='S'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["tokens"], 100,
                         "two serialized syncs must count the turn once")


if __name__ == "__main__":
    unittest.main()

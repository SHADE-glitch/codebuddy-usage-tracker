"""test_traces.py — the OTel trace ingestion (the internal agent spans).

The transcripts never carry the internal agents (`autoModeClassifier`,
`summaryGenerator`, ...): they live only in the trace tree,
`~/.codebuddy/traces/<pid>/trace_<id>.jsonl` (the live span stream) and
`trace_<id>.json` (the whole trace, written once at completion — and the
only place a sessionId exists). These tests feed fixture streams and final
files through the real readers and pin: only agent spans are stored, the
runtime roots (`cli`, `ptc`) are declined, a running span is a
placeholder its own `span_end` upgrades, status is never downgraded,
and the stream and the final file dedup onto one row per spanId.

Every database and every fixture tree is built in a temp directory;
`db.CODEBUDDY_DIR` is patched per test, so the real logs are never read.
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database
from _catalog import catalog_pinned  # noqa: E402 -- scans below read the snapshot fixture

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_sync", SCRIPTS / "cbut-sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

_CATALOG_PIN = catalog_pinned()


def setUpModule():
    _CATALOG_PIN.__enter__()


def tearDownModule():
    _CATALOG_PIN.__exit__(None, None, None)


def iso(ms: int) -> str:
    """An epoch-ms value as the trace tree stamps spans (UTC, millisecond Z)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def agent_start(span_id, agent, ts, parent="span_root"):
    return {"type": "span_start", "span": {
        "traceId": "trace_t", "spanId": span_id, "parentId": parent,
        "name": agent, "type": "agent", "startedAt": iso(ts),
        "endedAt": None, "duration": None, "status": "running",
        "error": None, "agentName": agent}}


def agent_end(span_id, agent, ts, duration, status="ok"):
    return {"type": "span_end", "span": {
        "traceId": "trace_t", "spanId": span_id, "parentId": "span_root",
        "name": agent, "type": "agent", "startedAt": iso(ts),
        "endedAt": iso(ts + duration), "duration": duration,
        "status": status, "error": None, "agentName": agent}}


def other_span(span_id, kind, ts):
    return {"type": "span_start", "span": {
        "traceId": "trace_t", "spanId": span_id, "parentId": "span_root",
        "name": kind, "type": kind, "startedAt": iso(ts),
        "endedAt": None, "duration": None, "status": "running",
        "error": None}}


def write_stream(path: Path, recs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def write_final(path: Path, records, session_id=None):
    """Write a whole-trace .json from stream-shaped records.

    The spans array holds the span OBJECTS directly — no `type`/`span`
    wrapper, unlike the stream — so the records are unwrapped here, which is
    also the shape difference the readers must agree on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    trace = {"traceId": "trace_t", "name": "Agent workflow", "status": "ok",
             "metadata": {}}
    if session_id is not None:
        trace["sessionId"] = session_id
    spans = [r["span"] if isinstance(r, dict) and "span" in r else r
             for r in records]
    path.write_text(json.dumps({"trace": trace, "spans": spans}),
                    encoding="utf-8")


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.conn = db.open_db(self.db_path)
        db.ensure_schema(self.conn)
        self.host = Path(self.tmp.name) / "codebuddy"
        self.patcher = mock.patch.object(db, "CODEBUDDY_DIR", self.host)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.unparsed = {}

    def rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM agent_usage WHERE source='span'")]

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # -- the stream -------------------------------------------------------

    def test_an_agent_span_is_recorded_with_its_outcome(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_start("sp1", "summaryGenerator", 1000),
                         agent_end("sp1", "summaryGenerator", 1000, 250)])
        new = sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        row = self.rows()[0]
        self.assertEqual(row["agent_type"], "summaryGenerator")
        self.assertEqual(row["kind"], "internal")     # BUILTIN_AGENTS taxonomy
        self.assertEqual(row["source"], "span")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["duration_ms"], 250)
        self.assertEqual(row["ts"], 1000)
        self.assertIsNone(row["session_id"])          # the stream has no ids
        self.assertIsNone(row["project"])
        self.assertEqual(new, p.stat().st_size)

    def test_an_error_span_reads_incomplete(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_end("sp1", "Explore", 1000, 5, status="error")])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        self.assertEqual(self.rows()[0]["status"], "incomplete")

    def test_the_runtime_roots_are_declined(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_start("sp1", "cli", 1000),
                         agent_start("sp2", "ptc", 1000)])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.count("unparsed"), 0)   # declined, not drift

    def test_generation_and_tool_spans_are_ignored(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [other_span("sp1", "generation", 1000),
                         other_span("sp2", "tool", 1001)])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        self.assertEqual(self.rows(), [])

    def test_a_running_span_is_a_placeholder_its_end_upgrades(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_start("sp1", "compact", 1000)])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        self.assertIsNone(self.rows()[0]["status"])
        # the end arrives as appended bytes; the incremental read upgrades
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                agent_end("sp1", "compact", 1000, 900)) + "\n")
        sync.index_trace_stream(self.conn, p,
                                p.stat().st_size - (len(json.dumps(
                                    agent_end("sp1", "compact", 1000, 900)))
                                    + 1), self.unparsed)
        self.conn.commit()
        row = self.rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["duration_ms"], 900)
        self.assertEqual(len(self.rows()), 1)

    def test_status_is_never_downgraded_by_a_replayed_start(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_end("sp1", "compact", 1000, 900)])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        # a stale-offset replay writes the start again (a reset re-reads from 0)
        replay = self.host / "traces" / "42" / "trace_b.jsonl"
        write_stream(replay, [agent_start("sp1", "compact", 1000)])
        sync.index_trace_stream(self.conn, replay, 0, self.unparsed)
        self.conn.commit()
        self.assertEqual(self.rows()[0]["status"], "completed")

    def test_an_invalid_agent_name_is_counted_not_stored(self):
        p = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(p, [agent_end("sp1", "bad\nname", 1000, 5)])
        sync.index_trace_stream(self.conn, p, 0, self.unparsed)
        self.conn.commit()
        self.assertEqual(self.rows(), [])
        # the reader reports into the run's tally dict; _run is what persists
        # it into the unparsed table (see the full-run test)
        self.assertEqual(self.unparsed.get(sync.INVALID_TOOL_NAME), 1)

    # -- the final file ---------------------------------------------------

    def test_the_final_file_links_the_session(self):
        p = self.host / "traces" / "42" / "trace_a.json"
        write_final(p, [agent_end("sp1", "autoModeClassifier", 1000, 50)],
                    session_id="s-abc")
        n = sync.index_final_trace(self.conn, p, self.unparsed)
        self.conn.commit()
        self.assertEqual(n, 1)
        self.assertEqual(self.rows()[0]["session_id"], "s-abc")

    def test_stream_placeholder_then_final_json_merge_into_one_row(self):
        stream = self.host / "traces" / "42" / "trace_a.jsonl"
        write_stream(stream, [agent_start("sp1", "autoModeClassifier", 1000)])
        sync.index_trace_stream(self.conn, stream, 0, self.unparsed)
        final = self.host / "traces" / "42" / "trace_a.json"
        write_final(final, [agent_end("sp1", "autoModeClassifier", 1000, 50)],
                    session_id="s-abc")
        sync.index_final_trace(self.conn, final, self.unparsed)
        self.conn.commit()
        rows = self.rows()
        self.assertEqual(len(rows), 1)                # one spanId, one row
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["session_id"], "s-abc")

    def test_the_final_file_re_read_is_idempotent(self):
        p = self.host / "traces" / "42" / "trace_a.json"
        write_final(p, [agent_end("sp1", "Explore", 1000, 50),
                        agent_end("sp2", "Plan", 1001, 60)],
                    session_id="s-abc")
        for _ in range(3):
            sync.index_final_trace(self.conn, p, self.unparsed)
        self.conn.commit()
        self.assertEqual(len(self.rows()), 2)

    # -- the whole run ------------------------------------------------------

    def test_a_full_run_indexes_traces_and_stays_idempotent(self):
        write_stream(self.host / "traces" / "42" / "trace_a.jsonl",
                     [agent_start("sp1", "promptSuggestion", 1000),
                      agent_end("sp1", "promptSuggestion", 1000, 30)])
        write_final(self.host / "traces" / "42" / "trace_a.json",
                    [agent_end("sp1", "promptSuggestion", 1000, 30)],
                    session_id="s-1")
        for _ in range(2):                            # two syncs, no doubles
            stats = sync._run(self.conn, quiet=True)
            self.conn.commit()
        self.assertEqual(stats["agent_spans"], 1)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row["session_id"], "s-1")
        self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()

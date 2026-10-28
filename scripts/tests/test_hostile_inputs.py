"""Hostile input: every value a reader can meet, in every field it reads.

A line that parses as JSON is not necessarily a record — ``null``, ``[]``, ``"text"`` and ``42`` all
parse — and a record is not necessarily well-typed. One such line used to end the
whole run: a non-object line raised AttributeError out of the dispatch, and a
container where a scalar belongs raised sqlite3.ProgrammingError at the bind.
Either way every project's index stopped at that record, and the reason never
reached ``cbut health``. A tracker that dies on one bad line is worse than one
that skips it and says so.

The cases are generated: each field of each known shape is replaced by every value
in ``BAD_VALUES``, deleted, or the record is replaced whole. The list is ordered and
nothing is random, so a failure names the exact shape that broke — and the same
case list runs in CI forever, which is the point: this is a guard, not a one-off
cleanup.
"""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_sync", SCRIPTS / "cbut-sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


# The shapes the indexer actually reads, copied from transcripts and traces rather
# than invented: a mutation of a shape that never occurs proves nothing.
RECORDS = {
    "call": {"type": "function_call", "name": "Bash", "callId": "c1",
             "sessionId": "s1", "cwd": "/p", "timestamp": 1000},
    "result": {"type": "function_call_result", "callId": "c1", "sessionId": "s1",
               "status": "completed", "timestamp": 1001, "durationMs": 5,
               # The refusal path reads both: a dict output's text, and the
               # provider block's toolResult. They are structures, not scalars.
               "output": {"text": "done"},
               "providerData": {"toolResult": {"content": "done"}}},
    "turn": {"type": "turn-metrics", "timestamp": 1000, "tokenDelta": 100,
             "durationMs": 1000,
             "_meta": {"baggage": "codebuddy.session_id=S"}},
    "usage": {"type": "model-usage", "sessionId": "s1", "timestamp": 1000,
              "providerData": {"messageId": "m1", "model": "x",
                               "rawUsage": {"input_tokens": 1}}},
    "message": {"type": "message", "role": "assistant", "content": "hi",
                "sessionId": "s1", "timestamp": 1000},
}

SPANS = {
    "span": {"type": "span_start",
             "span": {"traceId": "t", "spanId": "sp1", "name": "explore",
                      "type": "agent", "startedAt": "2026-10-16T08:21:14.179Z",
                      "status": "running", "agentName": "explore"}},
}

# Each value is something a writer can emit and a reader can meet. The containers
# are the ones that reach a bind: sqlite3 takes str, int, float, bytes and None.
BAD_VALUES = [None, 0, -1, "", "x" * 50, [], {}, True, 1.5, "not-a-date"]


def variants(shapes):
    """``(label, record)`` for every mutation of every shape, in a fixed order.

    Two depths: the record's own fields, and one level inside each structure it
    holds — ``providerData.rawUsage`` is where a response's numbers live, and a
    string there reaches arithmetic, not just a bind. Deeper than that nothing is
    read, so mutating it would only prove the reader ignores it.
    """
    for name, rec in shapes.items():
        yield f"{name}:honest", rec
        for key in list(rec):
            for value in BAD_VALUES:
                changed = copy.deepcopy(rec)
                changed[key] = value
                yield f"{name}:{key}={value!r}", changed
            dropped = copy.deepcopy(rec)
            del dropped[key]
            yield f"{name}:no-{key}", dropped
        for key, value in rec.items():
            if not isinstance(value, dict):
                continue
            for inner in list(value):
                for bad in BAD_VALUES:
                    changed = copy.deepcopy(rec)
                    changed[key][inner] = bad
                    yield f"{name}:{key}.{inner}={bad!r}", changed
        for whole in (None, [], "string", 42, 1.5, True):
            yield f"{name}:whole={whole!r}", whole


class HostileInputTest(unittest.TestCase):
    """A record the indexer cannot use costs that record, never the run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = db.open_db(self.root / "t.db")
        db.ensure_schema(self.conn)
        self.path = self.root / "s.jsonl"

    def index_one(self, rec, unparsed=None):
        """Write one record as a one-line transcript and index it from zero."""
        self.path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        return sync.index_file(self.conn, self.path, 0, {}, {}, {},
                               {} if unparsed is None else unparsed)

    def test_no_transcript_shape_ends_the_run(self):
        for label, rec in variants(RECORDS):
            with self.subTest(label):
                try:
                    self.index_one(rec)
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"{label} ended the run with {exc!r}")

    def test_no_trace_shape_ends_the_run(self):
        stream = self.root / "traces" / "42" / "trace_a.jsonl"
        stream.parent.mkdir(parents=True, exist_ok=True)
        final = self.root / "traces" / "42" / "trace_a.json"
        for label, rec in variants(SPANS):
            with self.subTest(label):
                stream.write_text(json.dumps(rec) + "\n", encoding="utf-8")
                try:
                    sync.index_trace_stream(self.conn, stream, 0, {})
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"stream {label} ended the run with {exc!r}")
                span = rec.get("span") if isinstance(rec, dict) else None
                final.write_text(json.dumps(
                    {"trace": {"sessionId": "s1"},
                     "spans": [span if span is not None else rec]}),
                    encoding="utf-8")
                try:
                    sync.index_final_trace(self.conn, final, {})
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"final {label} ended the run with {exc!r}")

    def test_an_unusable_record_is_counted_under_one_reason(self):
        """A record that is not an object, or holds a container where a scalar

        belongs, is skipped and counted — one reason for both, so a format change
        shows up in ``cbut health`` instead of stopping the sync.
        """
        cases = [("null line", None), ("list line", []),
                 ("string line", "text"), ("number line", 42),
                 ("container project", {"type": "function_call", "name": "Bash",
                                        "callId": "c1", "cwd": ["/p"],
                                        "timestamp": 1000}),
                 ("container provider", {"type": "model-usage",
                                         "providerData": "not an object",
                                         "timestamp": 1000})]
        for label, rec in cases:
            with self.subTest(label):
                unparsed = {}
                self.index_one(rec, unparsed)
                self.assertEqual(unparsed, {sync.UNUSABLE_RECORD: 1})

    def test_a_well_typed_record_still_indexes(self):
        """The guard must reject shapes, not records: the honest one still lands."""
        unparsed = {}
        self.index_one(RECORDS["call"], unparsed)
        self.assertEqual(unparsed, {})
        row = self.conn.execute(
            "SELECT tool_name, project FROM tool_calls WHERE call_id='c1'").fetchone()
        self.assertEqual((row["tool_name"], row["project"]), ("Bash", "/p"))


if __name__ == "__main__":
    unittest.main()

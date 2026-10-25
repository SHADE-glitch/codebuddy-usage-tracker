"""test_cross_repo_golden.py — one fixed input set, pinned against this repo's own code.

The Usage Total expression and the typed date/time entry were copied verbatim between
the three trackers in this family (cbut, ocut, qcut) and have been maintained separately
ever since; the NULL semantics already drifted once between all-usage-trackers and the
siblings (audit P1-12 / P2-17). The fix the audit recommends is not a rewrite into a
shared library — it is a golden fixture: one input set, run through each repo's own
code, with the expected results written down where a drift fails a test.

This file is the cbut half. It reads ``fixtures/cross_repo_golden.json`` — a byte-for-byte
copy of the canonical file in all-usage-trackers — builds the fixture rows through
``cbut_db``'s own schema, and asserts:

* every row's Usage Total, through this repo's own ``_USAGE_TOTAL_EXPR``;
* every window preset's request count and windowed Usage Total, through
  ``window_bounds`` + ``q_usage_summary``;
* the per-model rollup, through ``q_usage_model_stats`` — spellings stay separate and a
  NULL model is its own group;
* the typed date/time normalisation and the calendar-day window bounds, through
  ``parse_date`` / ``parse_time`` / ``parse_datetime`` / ``window_bounds``;
* the same, plus end-to-end row aggregation, on both US DST transition days of 2026,
  with the process timezone switched to America/New_York.

The expected values in the golden follow this repo's documented definition (README: "a
display-only Input + Output + cache hit ... Fields absent from a response are shown as
-, never as a fabricated 0"), not the implementation. Where the three repos' documented
definitions differ — a row with a missing prompt but a present output is NULL here and
contributes on ocut — the golden records each repo's own answer, so the difference is
pinned rather than smoothed over.

Standard library only. Run:  python3 -m unittest discover -s scripts/tests
"""

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "fixtures" / "cross_repo_golden.json"

#: Digest of the canonical copy in all-usage-trackers. A repo whose golden file has been
#: edited on its own fails here, with the digest it actually has in the message; the
#: update procedure (edit the canonical file, re-copy, update this constant in all three
#: repos) is written into the golden file itself.
CANONICAL_SHA256 = "56a6de9eb4cef58426b0835fe7178e9f2a5d5ba7d68d07d965df58330a58db2f"

REPO = "cbut"
DST_ZONE = "America/New_York"


def load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def ms(iso):
    """A local wall-clock ISO string as epoch milliseconds, the unit every bound uses."""
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


@contextlib.contextmanager
def in_timezone(name):
    """Run a block with the process timezone switched, then put it back exactly."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def golden_provider():
    """The one provider every fixture row carries, so rollup keys are stable."""
    return load_golden()["usage_total"]["provider"]


def build_db(path, rows):
    """Insert golden rows into this repo's own table through its own column map.

    cbut stores the whole prompt in ``prompt_tokens`` (prompt = hit + miss + write), so
    the abstract ``input_full`` maps straight onto it and ``cache_hit`` onto
    ``prompt_cache_hit_tokens``. The cache miss/write columns stay NULL: they are not
    part of Usage Total and the fixture does not claim to measure them.
    """
    conn = db.open_db(path)
    db.ensure_schema(conn)
    for row in rows:
        conn.execute(
            "INSERT INTO model_responses(message_id, model, provider, prompt_tokens,"
            " completion_tokens, prompt_cache_hit_tokens, ts, usage_available)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (row["id"], row["model"], golden_provider(), row["input_full"],
             row["output"], row["cache_hit"], ms(row["ts"]),
             1 if row["reports_usage"] else 0),
        )
    conn.commit()
    return conn


def row_totals(conn, golden):
    """{row id: Usage Total} through this repo's own per-row expression."""
    table = golden["repos"][REPO]["table"]
    key = golden["repos"][REPO]["key_column"]
    got = {}
    for r in conn.execute(
            f"SELECT {key} AS id, {db._USAGE_TOTAL_EXPR} AS usage_total"
            f" FROM {table}").fetchall():
        got[r["id"]] = r["usage_total"]
    return got


def windowed_summary(conn, key, now_ms):
    """(requests, usage_total) for one preset, through this repo's own queries."""
    start, end = db.window_bounds(key, now_ms)
    summary = db.q_usage_summary(conn, start, end)
    return summary["requests"], summary["usage_total_tokens"]


def model_rollup(conn, now_ms):
    """{(model, provider): usage_total} through this repo's own rollup query."""
    start, end = db.window_bounds("all", now_ms)
    return {(r["model"], r["provider"]): r["usage_total_tokens"]
            for r in db.q_usage_model_stats(conn, start, end)}


class GoldenCopyTest(unittest.TestCase):
    def test_the_copy_is_the_canonical_one(self):
        """A golden file edited in only one repo is the drift this fixture exists to catch.

        Each repo's CI checks out only itself, so the copies cannot read one shared file
        through a sibling-relative path — that would be red on every runner. The digest
        is what keeps the copies one document instead of three.
        """
        actual = hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            actual, CANONICAL_SHA256,
            "this repo's copy of cross_repo_golden.json is not the canonical one. "
            "Edit the canonical copy in all-usage-trackers, re-copy it here, and update "
            "CANONICAL_SHA256 in this file (and in the other two repos' copies of this "
            "test) in the same change.")


class UsageTotalTest(unittest.TestCase):
    """The per-row expression, the windowed aggregate, and the rollup."""

    @classmethod
    def setUpClass(cls):
        cls.golden = load_golden()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.conn = build_db(Path(cls._tmp.name) / "usage.db",
                            cls.golden["usage_total"]["rows"])
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.conn.close)

    def test_every_row_matches_the_golden_usage_total(self):
        """One case per NULL/partial/cache/spelling shape, through _USAGE_TOTAL_EXPR.

        The interesting one is u4 — a missing prompt with a present output: this repo's
        documented definition returns NULL (a part is absent, so nothing is claimed),
        while ocut's keeps the partly-reported row. Both are pinned; neither is smoothed.
        """
        got = row_totals(self.conn, self.golden)
        for row in self.golden["usage_total"]["rows"]:
            with self.subTest(row=row["id"]):
                self.assertEqual(got[row["id"]], row["expect"][REPO],
                                 f"{row['id']}: {row['meaning']}")

    def test_the_window_aggregates_match(self):
        """Every preset's requests and windowed Usage Total, through q_usage_summary.

        w1 sits at exactly ``now``: the window end is exclusive, so it is outside every
        preset — all-time included. w3 and w7 sit exactly on a local midnight: the start
        is inclusive. That pair is the whole boundary contract in two rows.
        """
        now = ms(self.golden["usage_total"]["now"])
        for case in self.golden["usage_total"]["windows"]:
            with self.subTest(window=case["key"]):
                requests, total = windowed_summary(self.conn, case["key"], now)
                self.assertEqual(requests, case["requests"])
                self.assertEqual(total, case["usage_total"][REPO])

    def test_the_model_rollup_keeps_spellings_and_null_separate(self):
        """The rollup groups by (model, provider) exactly: no case folding, no merge.

        ``step-3`` and ``Step-3`` are two rows, and the NULL model is its own group with
        its tokens intact — a group that quietly disappears from the Tokens tab is the
        same class of bug as a fabricated 0.
        """
        now = ms(self.golden["usage_total"]["now"])
        got = model_rollup(self.conn, now)
        want = {(g["model"], g["provider"]): g["usage_total"][REPO]
                for g in self.golden["usage_total"]["model_groups"]}
        self.assertEqual(got, want)


class WindowBoundsTest(unittest.TestCase):
    """window_bounds: whole local calendar days, ending exactly at now."""

    @classmethod
    def setUpClass(cls):
        cls.golden = load_golden()

    def test_the_preset_bounds(self):
        section = self.golden["window_bounds"]
        now = ms(section["now"])
        for case in section["cases"]:
            with self.subTest(window=case["key"]):
                start, end = db.window_bounds(case["key"], now)
                self.assertEqual(end, now)
                if case["start"] is None:
                    self.assertIsNone(start, "only 'all' has no lower bound")
                else:
                    self.assertEqual(start, ms(case["start"]))


class DateEntryTest(unittest.TestCase):
    """The typed date/time normalisation the date picker feeds on."""

    @classmethod
    def setUpClass(cls):
        cls.golden = load_golden()

    def test_parse_date_cases(self):
        section = self.golden["date_parsing"]
        for case in section["parse_date"]:
            if case["expect"] == "ValueError":
                continue
            with self.subTest(date=case["input"]):
                year = case.get("default_year", section["default_year"])
                got = db.parse_date(case["input"], year).date().isoformat()
                self.assertEqual(got, case["expect"])

    def test_parse_date_rejects_what_it_cannot_read(self):
        section = self.golden["date_parsing"]
        for case in section["parse_date"]:
            if case["expect"] != "ValueError":
                continue
            with self.subTest(date=case["input"]):
                year = case.get("default_year", section["default_year"])
                with self.assertRaises(ValueError):
                    db.parse_date(case["input"], year)

    def test_parse_time_cases(self):
        section = self.golden["date_parsing"]
        for case in section["parse_time"]:
            if case["expect"] == "ValueError":
                continue
            with self.subTest(time=case["input"]):
                self.assertEqual(list(db.parse_time(case["input"])), case["expect"])

    def test_parse_time_rejects_what_it_cannot_read(self):
        section = self.golden["date_parsing"]
        for case in section["parse_time"]:
            if case["expect"] != "ValueError":
                continue
            with self.subTest(time=case["input"]):
                with self.assertRaises(ValueError):
                    db.parse_time(case["input"])

    def test_parse_datetime_cases(self):
        section = self.golden["date_parsing"]
        for case in section["parse_datetime"]:
            if case["expect"] in (None, "ValueError"):
                continue
            with self.subTest(pair=(case["date"], case["time"])):
                year = case.get("default_year", section["default_year"])
                got = db.parse_datetime(case["date"], case["time"], year)
                self.assertEqual(got, ms(case["expect"]))

    def test_parse_datetime_blank_date_is_none_and_errors_raise(self):
        section = self.golden["date_parsing"]
        for case in section["parse_datetime"]:
            if case["expect"] not in (None, "ValueError"):
                continue
            with self.subTest(pair=(case["date"], case["time"])):
                year = case.get("default_year", section["default_year"])
                if case["expect"] is None:
                    self.assertIsNone(db.parse_datetime(case["date"], case["time"], year))
                else:
                    with self.assertRaises(ValueError):
                        db.parse_datetime(case["date"], case["time"], year)


@unittest.skipUnless(hasattr(time, "tzset"), "this platform cannot switch timezones")
class DstTest(unittest.TestCase):
    """The calendar-day rule on the two 2026 US DST transition days.

    "2 days back" must land on local midnight two calendar days ago, not on now minus 48
    hours: on the spring-forward day those differ by eleven hours, and a window that
    silently became hour-based would include or drop a whole day of calls. The row
    aggregation is re-run inside the switched zone, because that is where a local-day
    boundary is actually decided.
    """

    @classmethod
    def setUpClass(cls):
        try:
            ZoneInfo(DST_ZONE)
        except Exception as exc:  # no tzdata on this box: nothing to assert against
            raise unittest.SkipTest(f"no {DST_ZONE} database: {exc}")
        cls.golden = load_golden()

    def test_spring_forward_window_bounds(self):
        section = self.golden["dst"]["spring_forward"]
        with in_timezone(DST_ZONE):
            now = ms(section["now"])
            for case in section["window_bounds"]:
                with self.subTest(window=case["key"]):
                    start, end = db.window_bounds(case["key"], now)
                    self.assertEqual(end, now)
                    self.assertEqual(start, ms(case["start"]))

    def test_spring_forward_rows_stay_in_the_local_day(self):
        """d2 (01:30 EST) and d3 (03:30 EDT) are the same local day; d4 is at now."""
        section = self.golden["dst"]["spring_forward"]
        with tempfile.TemporaryDirectory() as tmp, in_timezone(DST_ZONE):
            conn = build_db(Path(tmp) / "usage.db", section["rows"])
            try:
                now = ms(section["now"])
                for case in section["windows"]:
                    with self.subTest(window=case["key"]):
                        requests, total = windowed_summary(conn, case["key"], now)
                        self.assertEqual(requests, case["requests"])
                        self.assertEqual(total, case["usage_total"][REPO])
            finally:
                conn.close()

    def test_fall_back_window_bounds(self):
        section = self.golden["dst"]["fall_back"]
        with in_timezone(DST_ZONE):
            now = ms(section["now"])
            for case in section["window_bounds"]:
                with self.subTest(window=case["key"]):
                    start, end = db.window_bounds(case["key"], now)
                    self.assertEqual(end, now)
                    self.assertEqual(start, ms(case["start"]))

    def test_fall_back_rows_stay_in_the_local_day(self):
        """The repeated 01:30 hour is the same local day; d8 at now stays outside."""
        section = self.golden["dst"]["fall_back"]
        with tempfile.TemporaryDirectory() as tmp, in_timezone(DST_ZONE):
            conn = build_db(Path(tmp) / "usage.db", section["rows"])
            try:
                now = ms(section["now"])
                for case in section["windows"]:
                    with self.subTest(window=case["key"]):
                        requests, total = windowed_summary(conn, case["key"], now)
                        self.assertEqual(requests, case["requests"])
                        self.assertEqual(total, case["usage_total"][REPO])
            finally:
                conn.close()

    def test_the_ambiguous_hour_resolves_like_the_stdlib(self):
        """The gap hour and the repeated hour resolve exactly like datetime.timestamp().

        parse_datetime is built on a naive ``datetime.timestamp()``, so the pre-jump
        01:30 is EST, the post-jump 03:30 is EDT, and November's repeated 01:30 is the
        first pass (fold=0, EDT). A hand-rolled offset table is where this drifts.
        """
        section = self.golden["date_parsing"]
        with in_timezone(DST_ZONE):
            for case in self.golden["dst"]["ambiguous_hour"]:
                with self.subTest(pair=(case["date"], case["time"])):
                    got = db.parse_datetime(case["date"], case["time"],
                                            section["default_year"])
                    self.assertEqual(got, ms(case["expect"]))


if __name__ == "__main__":
    unittest.main()

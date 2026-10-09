"""test_snapshots.py — a destructive step must always have a way back.

The owner's rule is that a migration is only safe if it can be rolled back, and
``--full`` deletes every indexed row before re-reading the transcripts. These
tests assert the two halves of that promise:

* the snapshot is taken **before** the change, so it still shows the old shape
  (a snapshot taken afterwards would be worthless);
* restoring it actually brings the rows back.

Retention is tested too, including the one thing that must never happen: pruning
a ``usage.db.bak-*`` file somebody made by hand.

Standard library only; every database here lives in a temp directory.
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

V2_COLUMNS = (
    " message_id TEXT PRIMARY KEY, session_id TEXT,"
    " conversation_request_id TEXT, model TEXT,"
    " prompt_tokens INTEGER, completion_tokens INTEGER,"
    " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
    " ts INTEGER, project TEXT, source TEXT, usage_available INTEGER,"
    " missing TEXT"
)


def build_v2_database(path: Path) -> None:
    """A database as an older release of this tool would have left it."""
    conn = sqlite3.connect(path)
    conn.executescript(f"CREATE TABLE model_responses({V2_COLUMNS});")
    for i in range(7):
        conn.execute("INSERT INTO model_responses(message_id, model,"
                     " prompt_tokens, completion_tokens, ts, usage_available)"
                     " VALUES(?,?,?,?,?,1)", (f"m{i}", "old-model", 10, 20, 1000 + i))
    conn.commit()
    conn.close()


class MigrationSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrate_snapshots_the_old_shape(self):
        build_v2_database(self.db_path)
        self.assertTrue(db.migrate(self.db_path), "migrate reported failure")

        snaps = db.list_backups(self.db_path)
        pre = [p for p in snaps if "pre-migration" in p.name]
        self.assertEqual(len(pre), 1, f"expected one pre-migration snapshot: {snaps}")

        # The snapshot must be the BEFORE state: no new columns, all rows intact.
        conn = sqlite3.connect(pre[0])
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(model_responses)")}
            n = conn.execute("SELECT COUNT(*) FROM model_responses").fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("prompt_cache_hit_tokens", cols,
                         "the snapshot was taken after the ALTER, so it cannot "
                         "roll the migration back")
        self.assertEqual(n, 7)

    def test_migrate_on_a_current_database_snapshots_nothing(self):
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.close()
        self.assertTrue(db.migrate(self.db_path))
        self.assertTrue(db.migrate(self.db_path))
        self.assertEqual(db.list_backups(self.db_path), [],
                         "a no-op migration that still snapshots would fill the "
                         "backups directory on every TUI launch")


class FullRebuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        self.root = Path(self.tmp.name) / "cb"
        (self.root / "projects" / "p").mkdir(parents=True)
        self.old_dir = sync.db.CODEBUDDY_DIR
        sync.db.CODEBUDDY_DIR = self.root

    def tearDown(self):
        sync.db.CODEBUDDY_DIR = self.old_dir
        self.tmp.cleanup()

    def _write(self, n=4):
        f = self.root / "projects" / "p" / "s.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(json.dumps({"type": "function_call", "name": "Bash",
                                     "callId": f"c{i}", "sessionId": "s1",
                                     "cwd": "/p", "timestamp": 1000 + i}) + "\n")

    def test_full_rebuild_snapshots_then_restore_returns_the_rows(self):
        self._write(4)
        sync.run(quiet=True, db_path=self.db_path)
        sync.run(full=True, quiet=True, db_path=self.db_path)

        pre = [p for p in db.list_backups(self.db_path)
               if "pre-full-rebuild" in p.name]
        self.assertEqual(len(pre), 1, "a rebuild that clears tables must snapshot")

        # Now lose the index the way a half-finished rebuild would, and come back.
        conn = db.open_db(self.db_path)
        conn.execute("DELETE FROM tool_calls")
        conn.commit()
        conn.close()

        db.restore(pre[0], db_path=self.db_path)
        conn = db.open_db(self.db_path, readonly=True)
        try:
            after = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(after, 4, "restore did not bring the rows back")

    def test_restore_also_saves_what_it_is_overwriting(self):
        self._write(2)
        sync.run(quiet=True, db_path=self.db_path)
        conn = db.open_db(self.db_path)
        try:
            snap = db.snapshot(conn, reason="manual")
        finally:
            conn.close()

        conn = db.open_db(self.db_path)
        conn.execute("DELETE FROM tool_calls")
        conn.commit()
        conn.close()

        db.restore(snap, db_path=self.db_path)
        reasons = [p.name for p in db.list_backups(self.db_path)]
        self.assertTrue(any("pre-restore" in r for r in reasons),
                        f"the overwritten database was not kept: {reasons}")

    def test_empty_database_is_not_snapshotted_by_full(self):
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.close()
        sync.run(full=True, quiet=True, db_path=self.db_path)
        self.assertEqual(db.list_backups(self.db_path), [],
                         "snapshotting an empty index is clutter, not safety")


class RetentionTest(unittest.TestCase):
    """Pruning may only ever delete snapshots this module made."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        conn = db.open_db(self.db_path)
        db.ensure_schema(conn)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, name, age_seconds):
        path = db.backup_dir(self.db_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        stamp = 1_700_000_000 - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_only_the_newest_keep_survive_and_in_mtime_order(self):
        made = [self._make(f"usage-2023010{i}-120000-manual.db", i * 10)
                for i in range(1, 8)]          # 7 snapshots, oldest = i=7
        gone = db.prune_backups(self.db_path, keep=db.BACKUP_KEEP)
        left = sorted(p.name for p in db.backup_dir(self.db_path).glob(
            db.BACKUP_PATTERN))
        self.assertEqual(len(left), db.BACKUP_KEEP, f"kept {left}")
        self.assertEqual({p.name for p in gone},
                         {made[5].name, made[6].name},
                         "pruning must remove exactly the two oldest")

    def test_same_second_snapshots_are_ordered_by_mtime_not_name(self):
        # Names carry the same timestamp, so only mtime orders them correctly;
        # the -1/-2 suffixes a name sort would compare lexicographically.
        first = self._make("usage-20230101-120000-manual.db", 60)
        second = self._make("usage-20230101-120000-manual-1.db", 10)
        gone = db.prune_backups(self.db_path, keep=1)
        self.assertEqual([p.name for p in gone], [first.name],
                         "the older of two same-second snapshots must be the "
                         "one pruned")
        self.assertTrue(second.exists())

    def test_hand_made_backups_are_never_pruned_but_are_restorable(self):
        hand = self.db_path.parent / "usage.db.bak-20261007-093557"
        conn = sqlite3.connect(hand)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        for i in range(1, 9):
            self._make(f"usage-2023010{i}-120000-manual.db", i * 10)

        gone = db.prune_backups(self.db_path, keep=3)
        self.assertNotIn(hand, gone)
        self.assertTrue(hand.exists(), "pruning deleted a hand-made backup")
        self.assertIn(hand, db.list_backups(self.db_path),
                      "a hand-made snapshot should still be offered as a "
                      "restore source")


class RestoreErrorTest(unittest.TestCase):
    def test_unknown_backup_is_refused_before_anything_is_touched(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            live = Path(tmp.name) / "usage.db"
            conn = db.open_db(live)
            db.ensure_schema(conn)
            conn.close()
            with self.assertRaises(FileNotFoundError):
                db.restore(Path(tmp.name) / "nope.db", db_path=live)
            # The live database must be untouched, including no pre-restore copy.
            self.assertEqual(db.list_backups(live), [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

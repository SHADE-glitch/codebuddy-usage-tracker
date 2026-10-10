"""test_hermetic.py — the tripwire that proves the suite stays off the real database.

AGENTS.md promises "Tests never touch real state": every suite builds its own throwaway
database and the live ``usage.db`` is never opened. Until now the promise was held by
attention alone, and attention has twice failed — a dispatcher test once ran a real
``cbut sync --full`` against the live database, and on 2026-10-10 a moved mtime on
``usage.db`` could not be explained by anything in the repository (it was the maintainer's
own TUI session, but nothing could have said so, and an hour went into assuming a leak).

Five cases, in the order they matter:

1. The predicate names the real file and only the real file, in both the plain path and the
   ``file:…?mode=ro`` URI form.
2. A connect aimed at the real path is refused — proven by handing the event to the hook,
   because a case that tested the real call would have to open the file it is protecting.
3. The hook is *live* in this process, proven end-to-end on a child spawn.
4. Control probes: the same spawn with a pinned ``CBUT_DB`` is allowed, and a spawn of
   something that is not ours is allowed. Without these, a hook that refused every
   subprocess would look like a pass.
5. No test module may skip the tripwire.
6. The predicate fires on *running* our code, not on merely naming it: a ``git`` pathspec, a
   ``bash -n`` syntax check and an inline ``python -c`` are all left alone, while a direct
   ``bash bin/cbut`` and ``python cbut-sync.py`` are still caught.
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import _hermetic as hermetic  # noqa: E402


class PredicateTest(unittest.TestCase):
    def test_the_real_path_is_recognised_and_nothing_else(self):
        self.assertEqual(hermetic._real_target(str(hermetic.PROTECTED)),
                         hermetic.PROTECTED.resolve())
        # The URI form is the read-only open, and "never opened" covers it too.
        self.assertEqual(
            hermetic._real_target(f"file:{hermetic.PROTECTED}?mode=ro"),
            hermetic.PROTECTED.resolve())

    def test_an_ordinary_test_database_is_left_alone(self):
        """The control for the case above: the predicate must not fire on everything."""
        with tempfile.TemporaryDirectory() as tmp:
            for candidate in (str(Path(tmp) / "usage.db"),
                              f"file:{Path(tmp) / 'usage.db'}?mode=ro",
                              ":memory:", str(hermetic.PROTECTED) + ".bak"):
                self.assertIsNone(hermetic._real_target(candidate),
                                  f"refused a throwaway path: {candidate}")

    def test_the_protected_path_is_derived_from_the_home_directory(self):
        """Nothing in this repository may hardcode one machine's paths — it is public.

        The tripwire is allowed to name a file only by building it from ``Path.home()``,
        the same way the data layer does, so it protects whatever *this* install's default
        is rather than a literal copied from the maintainer's box.
        """
        from pathlib import Path as P
        expected = P.home() / ".local" / "share" / "codebuddy-usage-tracker" / "usage.db"
        self.assertEqual(hermetic.PROTECTED, expected)

    def test_a_child_with_nothing_to_point_it_away_would_use_the_real_database(self):
        stats = str(SCRIPTS / "cbut-stats.py")
        self.assertTrue(hermetic._child_would_open_the_real_db(
            [sys.executable, stats, "health"], {"PATH": "/usr/bin"}))
        pinned = {"CBUT_DB": "/tmp/somewhere/usage.db"}
        self.assertFalse(hermetic._child_would_open_the_real_db(
            [sys.executable, stats, "health"], pinned))
        self.assertFalse(hermetic._child_would_open_the_real_db(
            [sys.executable, stats, "--db", "/tmp/somewhere/usage.db", "health"],
            {"PATH": "/usr/bin"}))
        # Not ours: a plain interpreter run is nobody's business.
        self.assertFalse(hermetic._child_would_open_the_real_db(
            [sys.executable, "-c", "print(1)"], None))

    def test_naming_an_entry_point_without_running_one_is_not_a_launch(self):
        """The boundary the argv rule draws.

        These three shapes each name an entry point and none of them *executes* one, so the
        predicate must leave them alone — a guard that refused them would be a false positive,
        and the suite would grow meaningless ``CBUT_DB`` pins to appease it. The last two cases
        are the control: the same entry points *run* are still caught.
        """
        bin_cbut = str(ROOT / "bin" / "cbut")
        self.assertFalse(hermetic._child_would_open_the_real_db(
            ["git", "-C", str(ROOT), "log", "--name-only", "--", "bin/cbut"], None))
        self.assertFalse(hermetic._child_would_open_the_real_db(
            ["bash", "-n", bin_cbut], None))
        self.assertFalse(hermetic._child_would_open_the_real_db(
            [sys.executable, "-c", "import cbut_db"], None))
        # Control: actually running it is caught, or the exemptions above blind the guard.
        self.assertTrue(hermetic._child_would_open_the_real_db(
            ["bash", bin_cbut, "sync"], None))
        self.assertTrue(hermetic._child_would_open_the_real_db(
            [sys.executable, str(SCRIPTS / "cbut-sync.py"), "--full"], None))


class EnforcementTest(unittest.TestCase):
    def test_connecting_to_the_real_database_is_refused(self):
        """Hand the event to the hook rather than making the call.

        The obvious version of this case is ``with self.assertRaises(...):
        sqlite3.connect(PROTECTED)``, and it is a trap: it only fails safely *if the hook
        works*. With the hook broken it opens the maintainer's live database to prove a
        point, which is the exact damage this file exists to prevent.
        """
        with self.assertRaises(hermetic.Refusal) as ctx:
            hermetic._hook("sqlite3.connect", (str(hermetic.PROTECTED),))
        self.assertIn(str(hermetic.PROTECTED.parent), str(ctx.exception))
        with self.assertRaises(hermetic.Refusal):
            hermetic._hook("sqlite3.connect",
                           (f"file:{hermetic.PROTECTED}?mode=ro",))

    def test_the_hook_is_live_in_this_process(self):
        """An audit hook that was never installed passes every case above vacuously."""
        with self.assertRaises(hermetic.Refusal):
            subprocess.run([sys.executable, str(SCRIPTS / "cbut-stats.py"), "--help"],
                           capture_output=True, env=_env_without_db())

    def test_the_same_child_is_allowed_once_its_database_is_pinned(self):
        """The matched pair for the case above.

        Identical argv, one differing environment entry. Without this the previous case
        would be satisfied by a hook that refuses every subprocess, and the rule would have
        been replaced by a denial of service on the suite's own CLI tests.
        """
        env = _env_without_db()
        with tempfile.TemporaryDirectory() as tmp:
            env["CBUT_DB"] = str(Path(tmp) / "usage.db")
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "cbut-stats.py"), "--help"],
                capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("usage", proc.stdout.lower())

    def test_an_unrelated_subprocess_is_not_refused(self):
        proc = subprocess.run([sys.executable, "-c", "print('ran')"],
                              capture_output=True, text=True, env=_env_without_db())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ran", proc.stdout)


def _env_without_db():
    """The ambient environment minus anything that pins a database.

    A copy with ``CBUT_DB`` removed is what a test that forgot to pin would hand the child,
    and it is also what this case needs: the parent's own environment must not rescue the
    child, which is the whole reason a child is a hole in the in-process rule.
    """
    env = dict(os.environ)
    env.pop("CBUT_DB", None)
    return env


class CoverageTest(unittest.TestCase):
    def test_every_test_module_installs_the_tripwire(self):
        """One import anywhere covers the run, so *which* module imports it is load-bearing.

        Relying on discovery order — ``_hermetic`` being reached early by alphabetical luck
        — is the kind of invariant that breaks when someone adds a file named before the
        one that carried it. Every module imports it, and this case enumerates the
        directory so a new one cannot be born unguarded.
        """
        missing = []
        for path in sorted(TESTS.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
            if "_hermetic" not in names:
                missing.append(path.name)
        self.assertEqual(missing, [],
                         f"these suites never install the hermeticity tripwire: {missing}")


if __name__ == "__main__":
    unittest.main()

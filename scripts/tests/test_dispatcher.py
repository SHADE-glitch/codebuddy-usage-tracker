"""test_dispatcher.py — bin/cbut, the bash entry point, under test.

`bin/cbut` cannot be a Python module: it has to run and explain itself when the
venv is broken (see AGENTS.md). That also makes it the one production file the
test suite could not reach, so its resolution rules were only ever checked by
hand. These tests drive it as a subprocess against stub "scripts" in a temp
directory, so no interpreter choice, no path fallback and no help line is
asserted from memory.

Nothing here touches the real database or ~/.codebuddy: every path is a temp
directory, and the scripts being exec'd are stubs that print their own argv.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CBUT = ROOT / "bin" / "cbut"

STUB = (
    "import sys, os\n"
    "print(os.path.basename(sys.argv[0]) + ' ' + ' '.join(sys.argv[1:]))\n"
)


def make_stub_scripts(dirpath: Path) -> Path:
    """A scripts/ dir whose three entry points just report how they were called."""
    scripts = dirpath / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("cbut-tui.py", "cbut-sync.py", "cbut-stats.py"):
        (scripts / name).write_text(STUB, encoding="utf-8")
    return scripts


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.scripts = make_stub_scripts(self.dir)
        self.python = sys.executable

    def tearDown(self):
        self.tmp.cleanup()

    def run_cbut(self, *args, scripts=None, venv=None, clear_scripts=False,
                 cbut=None):
        env = dict(os.environ)
        for key in ("CBUT_SCRIPTS", "CBUT_VENV", "CBUT_DB", "CBUT_CODEBUDDY_DIR"):
            env.pop(key, None)
        # Pin the data paths into this test's temp dir even though the stubs
        # never read them: if script resolution ever fell through to the real
        # repo, an inherited CBUT_DB would point a test at the live database.
        env["CBUT_DB"] = str(self.dir / "usage.db")
        env["CBUT_CODEBUDDY_DIR"] = str(self.dir / "empty-codebuddy")
        (self.dir / "empty-codebuddy").mkdir(exist_ok=True)
        if not clear_scripts:
            env["CBUT_SCRIPTS"] = str(scripts or self.scripts)
        if venv is not None:
            env["CBUT_VENV"] = str(venv)
        else:
            env["CBUT_VENV"] = self.python        # a binary, not a venv dir
        proc = subprocess.run(["bash", str(cbut or CBUT), *args],
                              capture_output=True, text=True, env=env)
        return proc

    # --- dispatch ----------------------------------------------------------

    def test_no_arguments_launches_the_tui(self):
        self.assertEqual(self.run_cbut().stdout.strip(), "cbut-tui.py")

    def test_the_tui_subcommand_forwards_its_flags(self):
        out = self.run_cbut("tui", "--db", "/tmp/x.db").stdout.strip()
        self.assertEqual(out, "cbut-tui.py --db /tmp/x.db")

    def test_sync_forwards_its_flags(self):
        self.assertEqual(self.run_cbut("sync", "--full").stdout.strip(),
                         "cbut-sync.py --full")

    def test_unknown_commands_fall_through_to_the_report_cli(self):
        # `cbut tools` etc. are not listed in the case statement; the catch-all
        # must keep the subcommand in argv, which is why that branch does not
        # shift. Asserting it pins the one place a "tidy up" shift would break.
        self.assertEqual(self.run_cbut("tools", "--limit", "5").stdout.strip(),
                         "cbut-stats.py tools --limit 5")

    # --- interpreter resolution -------------------------------------------

    def test_cbut_venv_may_name_a_binary(self):
        """CBUT_VENV documented as "a venv dir or a python binary"."""
        fake = self.dir / "myinterpreter"
        fake.write_text('#!/bin/sh\necho "CHOSEN $*"\n', encoding="utf-8")
        fake.chmod(0o755)
        out = self.run_cbut("stats", venv=fake).stdout.strip()
        self.assertTrue(out.startswith("CHOSEN"),
                        f"an explicit interpreter was ignored: {out!r}")
        self.assertIn("cbut-stats.py", out)

    def test_cbut_venv_may_name_a_venv_directory(self):
        venv = self.dir / "venv"
        (venv / "bin").mkdir(parents=True)
        link = venv / "bin" / "python"
        link.write_text('#!/bin/sh\necho "VENVDIR $*"\n', encoding="utf-8")
        link.chmod(0o755)
        out = self.run_cbut("models", venv=venv).stdout.strip()
        self.assertTrue(out.startswith("VENVDIR"),
                        f"a venv dir was not honoured: {out!r}")

    def test_an_unusable_cbut_venv_fails_loudly(self):
        # Silently falling back to system python3 is what made this hard to see.
        proc = self.run_cbut("stats", venv=self.dir / "does-not-exist")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CBUT_VENV", proc.stderr)

    # --- scripts resolution -------------------------------------------------

    def _lonely_entry(self):
        """A copy of bin/cbut with no scripts/ beside it.

        The resolver's only other candidate is CBUT_SCRIPTS; with neither
        present it has nowhere left to look. install.sh only ever symlinks
        bin/cbut into ~/.local/bin — it never copies the scripts anywhere, which
        is why the old fallback path pointed at a directory that did not exist.
        """
        lonely = self.dir / "lonely" / "bin"
        lonely.mkdir(parents=True, exist_ok=True)
        entry = lonely / "cbut"
        shutil.copy(CBUT, entry)
        return entry

    def test_missing_scripts_directory_is_reported_not_execd(self):
        proc = self.run_cbut("stats", cbut=self._lonely_entry(),
                             clear_scripts=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no scripts directory", proc.stderr)

    def test_help_works_even_when_scripts_cannot_be_resolved(self):
        # The entry point must always be able to explain itself: that is the
        # reason it is bash and not Python.
        proc = self.run_cbut("--help", cbut=self._lonely_entry(),
                             clear_scripts=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("CodeBuddy usage tracker", proc.stdout)

    # --- help text drift (D-001) --------------------------------------------

    def test_help_lists_the_subcommands_that_actually_exist(self):
        out = self.run_cbut("help").stdout
        for name in ("sync", "stats", "tools", "health", "backup", "restore"):
            self.assertIn(name, out, f"cbut {name} works but help never mentions it")
        self.assertIn("eight tabs", out,
                      "the tab count in help drifted from the TUI again")

    def test_help_does_not_leak_bash_internals(self):
        # The header is printed by stripping a leading '#'; the shebang and the
        # code below the block must not appear.
        out = self.run_cbut("help").stdout
        self.assertNotIn("#!", out)
        self.assertNotIn("set -euo pipefail", out)
        self.assertFalse(out.startswith("\n"))

    def test_syntax_check(self):
        proc = subprocess.run(["bash", "-n", str(CBUT)], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()

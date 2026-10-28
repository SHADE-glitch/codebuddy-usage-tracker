"""test_readme_counts.py — the test counts printed in the READMEs are the real counts.

`README.md` used to carry "177 tests" and a three-row suite table, both typed by hand.
The number drifted (the suite is now ~2.5× that) and four suites had no row at all, so
the page a visitor reads understated what is covered. The aggregate and every per-suite
row are now checked against `unittest`'s own loader.

The assertion is *set equality*: a suite added without a row fails, and a row left
pointing at a deleted file fails. Counting from the loader rather than from a
`grep -c "def test_"` also counts parametrised cases the way the runner reports them.

Standard library only. Nothing is executed — the modules are imported and counted.

The last case is not about a count: it checks that both pages still document the whole
settings surface (every key with its shipped default, every variable that overrides it),
because that list in the README is hand-copied and the loader is the only complete copy.
"""

import contextlib
import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
TESTS_DIR = SCRIPTS / "tests"
sys.path.insert(0, str(SCRIPTS))

import cbut_db as db  # noqa: E402  — the settings surface the READMEs must document

_stats_spec = importlib.util.spec_from_file_location(
    "cbut_stats_for_readme", SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_stats_spec)
_stats_spec.loader.exec_module(stats)

ROW = re.compile(r"^\| `(test_\w+)\.py` \| (\d+) \|", re.M)
AGGREGATE = re.compile(r"Ran (\d+) tests")
# A count in prose: "the 618 tests above", "上面那 618 个用例". The aggregate check reads
# only the `Ran NNN tests` line, so the repository-layout row was free to say 591 while the
# line fifty lines above it said 618 — the same row that had already been caught once for
# listing a subcommand the parser did not have.
PROSE_COUNT = re.compile(r"\b(\d+)\s+tests\b|(\d+)\s*个用例")


def module_count(stem):
    """How many cases the runner will report for one test module."""
    spec = importlib.util.spec_from_file_location(stem, TESTS_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return unittest.TestLoader().loadTestsFromModule(mod).countTestCases()


def on_disk():
    return {p.stem for p in TESTS_DIR.glob("test_*.py")}


class ReadmeCountsTest(unittest.TestCase):
    def rows(self, name):
        text = (REPO / name).read_text(encoding="utf-8")
        return {m: int(n) for m, n in ROW.findall(text)}

    def test_every_suite_has_a_row_in_english_readme(self):
        rows = self.rows("README.md")
        self.assertEqual(set(rows), on_disk(),
                         "the README table and scripts/tests/ are not the same set "
                         f"— missing: {sorted(on_disk() - set(rows))}, "
                         f"stale: {sorted(set(rows) - on_disk())}")

    def test_each_row_matches_the_real_count(self):
        wrong = {}
        for stem, claimed in self.rows("README.md").items():
            real = module_count(stem)
            if real != claimed:
                wrong[stem] = (claimed, real)
        self.assertEqual(wrong, {},
                         f"README counts disagree with the loader: {wrong}")

    def test_the_aggregate_line_matches(self):
        total = sum(module_count(s) for s in sorted(on_disk()))
        for name in ("README.md", "README.zh-CN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            found = AGGREGATE.findall(text)
            self.assertTrue(found,
                            f"{name} no longer prints a total anywhere the check "
                            "can see it")
            for claimed in found:
                self.assertEqual(int(claimed), total,
                                 f"{name} says 'Ran {claimed} tests', the loader "
                                 f"reports {total}")

    def test_every_test_count_in_prose_matches(self):
        """A total repeated in prose is a total nobody re-reads.

        The repository-layout row read "the 591 tests above" while the command block
        fifty lines above it read 618, and `AGGREGATE` could not see it: that check
        reads the `Ran NNN tests` line, and this was a sentence. The row is the same one
        that once listed a `vacuum` subcommand the parser did not have — one row, two
        independent ways of going stale — so the number is checked wherever it appears
        rather than only in the line that happened to have a pattern for it.
        """
        total = sum(module_count(s) for s in sorted(on_disk()))
        for name in ("README.md", "README.zh-CN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            wrong = [(number, match.group(0))
                     for number, line in enumerate(text.splitlines(), 1)
                     for match in PROSE_COUNT.finditer(line)
                     if int(match.group(1) or match.group(2)) != total]
            self.assertEqual(wrong, [],
                             f"{name} states a test count the loader does not report "
                             f"({total}): {wrong}")
            # Positive control: a pattern that stopped matching would pass over a page
            # with no counts in prose at all.
            self.assertGreaterEqual(
                len(PROSE_COUNT.findall(text)), 2,
                f"{name} no longer states a test count in prose where this check can "
                "see it, so the layout row is unguarded again")

    def test_the_zh_readme_agrees_with_the_en_one(self):
        """The two pages are one document: the same suites, the same numbers."""
        self.assertEqual(self.rows("README.md"), self.rows("README.zh-CN.md"))

    def test_both_readmes_document_every_setting(self):
        """A setting nobody can find is a setting nobody uses.

        The Configuration section is a hand-copied list — the same shape that just bit
        `cbut health` (D-029) and this suite's own environment isolation (D-030). The
        loader already knows the complete set, so the page is checked against it rather
        than against someone's memory of it: every key shown with the default it ships
        with, and every variable that can override it named.
        """
        for name in ("README.md", "README.zh-CN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            missing_keys = [f"{k} = {v}" for k, v in sorted(db.DEFAULTS.items())
                            if f"{k} = {v}" not in text]
            self.assertFalse(missing_keys,
                             f"{name} does not show these settings with their shipped "
                             f"default: {missing_keys}")
            missing_env = sorted(v for v in db.ENV_NAMES.values() if v not in text)
            self.assertFalse(missing_env,
                             f"{name} never names the variable that overrides these: "
                             f"{missing_env}")


class ReadmeSurfaceTest(unittest.TestCase):
    """The READMEs list the CLI surface, and the CLI is the authority on it.

    `cbut stats` grew a `vacuum` subcommand and the repository-layout row was
    never updated — while a later section of the same page listed it, so one
    page contradicted itself. The list is derived from the parser here, not from
    someone remembering to add a name.
    """

    def _subcommands(self):
        """Every subcommand `cbut-stats.py` accepts, read from its own parser."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                stats.main(["--help"])
        match = re.search(r"\{([a-z,\-]+)\}", out.getvalue())
        self.assertIsNotNone(match, f"cannot read the subcommand list: {out.getvalue()!r}")
        return set(match.group(1).split(","))

    def test_both_readmes_list_every_stats_subcommand(self):
        real = self._subcommands()
        self.assertGreater(len(real), 10, "the parser reported no subcommands")
        for name in ("README.md", "README.zh-CN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            row = next(line for line in text.splitlines()
                       if "`scripts/cbut-stats.py`" in line)
            listed = set(re.findall(r"`([a-z]+)`", row))
            self.assertFalse(
                real - listed,
                f"{name} does not list these subcommands: {sorted(real - listed)}",
            )


if __name__ == "__main__":
    unittest.main()

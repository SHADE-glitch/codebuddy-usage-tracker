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

import importlib.util
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

ROW = re.compile(r"^\| `(test_\w+)\.py` \| (\d+) \|", re.M)
AGGREGATE = re.compile(r"Ran (\d+) tests")


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


if __name__ == "__main__":
    unittest.main()

"""The two READMEs are one document in two languages, so their shape must not drift.

README.md (English, landing page) and README.zh-CN.md (Chinese) are edited together
by convention — AGENTS.md says so — but until this guard nothing enforced it: a
section added on one side alone was drift only a human reviewer would catch.

The guard is deliberately structural, not textual. It pins what the two files must
*share* — existence, cross-links, the language switcher, and the number of `##`
sections — and never the prose, because the section titles legitimately differ per
language. Comparing the titles would fail on every honest translation; comparing
their count is what catches a section added on one side alone.

stdlib unittest only, like every other suite here, so it runs under both
`python3 -m unittest discover -s scripts/tests` and pytest.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EN = ROOT / "README.md"
ZH = ROOT / "README.zh-CN.md"

H2_RE = re.compile(r"^## ", re.M)


def read(path):
    return path.read_text(encoding="utf-8")


def h2_count(text):
    return len(H2_RE.findall(text))


def before_first_section(text):
    """Everything above the first `##` heading — the title, badges and switcher."""
    match = H2_RE.search(text)
    return text[: match.start()] if match else text


class TestReadmeBilingual(unittest.TestCase):
    def test_both_readmes_exist_and_cross_link(self):
        self.assertTrue(EN.is_file(), "missing README.md")
        self.assertTrue(ZH.is_file(), "missing README.zh-CN.md")
        self.assertIn("README.zh-CN.md", read(EN),
                      "the English entry must link the Chinese one")
        self.assertIn("README.md", read(ZH),
                      "the Chinese entry must point back at the English one")

    def test_section_counts_match(self):
        en, zh = h2_count(read(EN)), h2_count(read(ZH))
        self.assertEqual(
            en, zh,
            "the two READMEs have drifted: %d '##' sections in README.md, "
            "%d in README.zh-CN.md — add the missing section to the other side"
            % (en, zh))

    def test_both_open_with_the_language_switcher(self):
        """The switcher is the one thing a reader needs before the first section."""
        for path, other in ((EN, "README.zh-CN.md"), (ZH, "README.md")):
            head = before_first_section(read(path))
            self.assertIn(
                other, head,
                "%s must offer the language switcher (%s) before its first '##' section"
                % (path.name, other))


if __name__ == "__main__":
    unittest.main()

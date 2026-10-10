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
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

ROOT = Path(__file__).resolve().parents[2]
EN = ROOT / "README.md"
ZH = ROOT / "README.zh-CN.md"

H2_RE = re.compile(r"^## ", re.M)

# --- HTTP status-code parity across a bilingual pair -------------------------
# A status code named in one language alone is a fact that silently contradicts the
# other language. A code counts only in an HTTP context (or quoted in backticks), so
# `500 ms` or `200 rows` is never mistaken for one.
_STATUS = (
    "200|201|202|204|206|301|302|303|304|307|308|400|401|402|403|404|405|406|"
    "407|408|409|410|411|412|413|414|415|416|417|418|421|422|423|424|425|426|"
    "428|429|431|451|499|500|501|502|503|504|505|506|507|508|510|511"
)
_STATUS_RE = re.compile(
    r"(?:HTTP|status|状态码|返回|returns?|responds?|replies?)[^\n]{0,30}?\b(" + _STATUS + r")\b"
    r"|`(" + _STATUS + r")`"
    r"|`(" + _STATUS + r")\s*\+", re.I)


def status_codes(path):
    return {m.group(1) or m.group(2) or m.group(3)
            for m in _STATUS_RE.finditer(read(path))}

# GitHub's anchor: lowercase, drop everything that is not a letter, digit, space or
# hyphen, then spaces to hyphens. Emoji disappear with it — but the variation selector
# and zero-width joiner *survive*, hiding inside the anchor. Stripping them (the obvious
# regex) reports every emoji heading as a broken link: a false positive on 28/28 entries.
_ANCHOR_KEEP = re.compile(r"[^\w \-️‍]+", re.U)
TOC_LINK = re.compile(r"^\s*-\s*\[[^\]]*\]\(#([^)]+)\)", re.M)
HEADING = re.compile(r"^#{2,3}\s+(.*)$", re.M)


def read(path):
    return path.read_text(encoding="utf-8")


def anchor_of(heading):
    return _ANCHOR_KEEP.sub("", heading.strip().lower()).replace(" ", "-")


def dead_toc_links(text):
    """In-page TOC links with no matching heading — the class this guard owns."""
    anchors = {anchor_of(m.group(1)) for m in HEADING.finditer(text)}
    return [link for link in TOC_LINK.findall(text) if link not in anchors]


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


    def test_every_toc_link_resolves_to_a_heading(self):
        """A TOC entry whose heading was deleted renders as a dead link on GitHub.

        Deleting a section is the mistake this catches: the section-count test above
        only compares the two files against each other, so removing one section from
        *both* READMEs passes it while leaving both tables of contents pointing at
        nothing. Includes its own control — an instrument that cannot report a known
        dead link is not guarding anything.
        """
        for path in (EN, ZH):
            dead = dead_toc_links(read(path))
            self.assertEqual(
                dead, [],
                "%s links these anchors with no heading: %s" % (path.name, dead))

        control = "## 🧹 Uninstall\n\n- [🤝 Contributing](#-contributing)\n"
        self.assertEqual(dead_toc_links(control), ["-contributing"],
                         "the checker cannot see a dead link, so it proves nothing")
        real = "## 🧹 Uninstall\n\n- [🧹 Uninstall](#-uninstall)\n"
        self.assertEqual(dead_toc_links(real), [],
                         "the checker reports a dead link that is not dead")


    def test_status_codes_match(self):
        """A status code named in one language alone contradicts the other language."""
        for zh in sorted(ROOT.glob("*.zh-CN.md")):
            en = ROOT / zh.name.replace(".zh-CN.md", ".md")
            if not en.is_file():
                continue
            ce, cz = status_codes(en), status_codes(zh)
            self.assertEqual(
                ce, cz,
                "%s and %s disagree on an HTTP status code: only %s -> %s; only %s -> %s"
                % (en.name, zh.name, en.name, sorted(ce - cz) or None,
                   zh.name, sorted(cz - ce) or None))


if __name__ == "__main__":
    unittest.main()

"""test_doc_anchors.py — a link in a document promises that something is there.

A dozen tracked Markdown files carry a few dozen relative links between them, and until
this guard not one was checked end to end. `test_readme_bilingual` resolved the *table
of contents* of the two READMEs and nothing else, so renaming a heading in
`docs/maintenance/compatibility.md` — the target of a cross-file anchor in both READMEs
— left 44 anchor-adjacent cases green against two links pointing at a section that no
longer existed.

So the sweep here is the whole corpus, not the pair: every relative link in every
tracked `.md`, resolved the way GitHub resolves it — the file has to exist, and a
`#fragment` has to land on a heading. External links are out of scope: checking them
needs the network, and a suite that needs the network is only checked on one machine.

Resolution is not the whole promise, though. A table of contents that *drops* a line
leaves its section unreachable while every remaining link still lands, and the pair
check compares the two READMEs against each other, so dropping the same line from both
passes it. The TOC is the map of its own document, so it is checked against the section
list it claims to describe: every `##` but itself, in document order.

Two traps are pinned by the control case below, because neither is visible in a diff:

* GitHub keeps the variation selector and zero-width joiner inside an anchor, so
  `## 🏗️ How it works` anchors at `#️-how-it-works` — an invisible U+FE0F sits between
  the hyphen and the `h`. Stripping it (the obvious regex) reports every emoji heading
  as broken; keeping it means the link has to match the heading character for character.
* A repeated heading gets `-1`, `-2` appended, so a second `## Same` anchors at
  `#same-1`. Without that, a document with two identically named sections would make
  one of its own anchors look dead.

Nothing here is written, and nothing is fetched. Standard library only.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

# The repository root, found by the one marker every clone has.
REPO = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())

# A fenced block is sample text, not a link. Without this, a document that *shows* a
# broken link — the obvious way to explain this guard — would fail it.
FENCE = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)", re.M | re.S)
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
H2 = re.compile(r"^## (.+)$", re.M)
TOC_ITEM = re.compile(r"^\s*-\s*\[[^\]]*\]\(#([^)]+)\)", re.M)
EXTERNAL = ("http://", "https://", "mailto:", "ftp://")

# GitHub's anchor: lowercase, drop everything that is not a letter, digit, space or
# hyphen, then spaces to hyphens. Emoji disappear with it — but the variation selector
# and zero-width joiner survive, hiding inside the anchor.
_ANCHOR_KEEP = re.compile(r"[^\w \-️‍]+", re.U)
# A heading is not the only way to mint an anchor: `<a name="x"></a>` above a heading
# pins a fragment that survives a retranslation, which is how ocut keeps one
# `#known-limitations` working across both of its languages. A guard that read only
# headings would call such a link dead.
EXPLICIT_ANCHOR = re.compile(r"<[a-z][^>]*?\b(?:name|id)\s*=\s*[\"']([^\"']+)[\"']", re.I)


def anchor_of(heading):
    return _ANCHOR_KEEP.sub("", heading.strip().lower()).replace(" ", "-")


def anchors_of(text):
    """Every anchor GitHub would mint for this document, `-1`/`-2` repeats included."""
    body = FENCE.sub("", text)
    seen, anchors = {}, set()
    for match in HEADING.finditer(body):
        base = anchor_of(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else "%s-%d" % (base, count))
    return anchors | set(EXPLICIT_ANCHOR.findall(body))


def sections(text):
    """`[(anchor, body)]` for every `##` heading, in document order."""
    body = FENCE.sub("", text)
    found = list(H2.finditer(body))
    out = []
    for index, match in enumerate(found):
        stop = found[index + 1].start() if index + 1 < len(found) else len(body)
        out.append((anchor_of(match.group(1)), body[match.end():stop]))
    return out


def toc_drift(text):
    """(anchors a table of contents should list, anchors it lists) — None if no TOC.

    The TOC is found mechanically, as the one `##` section whose body carries the list
    items, so nothing here depends on the words "Table of Contents" or "目录".
    """
    secs = sections(text)
    counts = [len(TOC_ITEM.findall(body)) for _anchor, body in secs]
    if not any(counts):
        return None
    index = counts.index(max(counts))
    listed = TOC_ITEM.findall(secs[index][1])
    return [anchor for i, (anchor, _body) in enumerate(secs) if i != index], listed


def documents():
    """Every tracked Markdown file, as absolute paths."""
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [REPO / name for name in out.stdout.split() if (REPO / name).is_file()]


def links_of(text):
    """The target of every inline link in `text`, fenced blocks removed."""
    return LINK.findall(FENCE.sub("", text))


def broken_links(docs, root=REPO):
    """Every relative link that points at nothing, and every fragment that misses.

    `root` is the boundary a link may not leave: a target outside it belongs to another
    repository, and checking it would make this suite depend on what else is on disk.
    The control case passes its own temporary directory instead.
    """
    anchors = {}
    problems = []
    for src in docs:
        for target in links_of(src.read_text(encoding="utf-8")):
            if target.startswith(EXTERNAL) or target.startswith("<"):
                continue
            path, _, fragment = target.partition("#")
            dest = (src.parent / path).resolve() if path else src.resolve()
            try:
                dest.relative_to(root)
            except ValueError:
                continue
            if not dest.exists():
                problems.append("%s: %s -> no such file"
                                % (src.relative_to(root), target))
                continue
            if not fragment or dest.is_dir() or dest.suffix != ".md":
                continue  # a directory or a source file: no headings to land on
            if dest not in anchors:
                anchors[dest] = anchors_of(dest.read_text(encoding="utf-8"))
            if fragment not in anchors[dest]:
                problems.append("%s: %s -> %s has no heading `%s`"
                                % (src.relative_to(root), target,
                                   dest.relative_to(root), fragment))
    return problems


class DocAnchorsTest(unittest.TestCase):
    def test_the_corpus_is_the_whole_repository(self):
        """A sweep over an empty list passes while checking nothing."""
        docs = documents()
        self.assertGreaterEqual(len(docs), 10,
                                "git ls-files returned almost no Markdown")
        names = {p.name for p in docs}
        for expected in ("README.md", "README.zh-CN.md", "AGENTS.md", "CHANGELOG.md"):
            self.assertIn(expected, names)

    def test_the_corpus_has_both_kinds_of_link(self):
        """Both branches below are live: in-page fragments and links across files."""
        texts = {p: p.read_text(encoding="utf-8") for p in documents()}
        targets = [t for text in texts.values() for t in links_of(text)]
        in_page = [t for t in targets if t.startswith("#")]
        cross = [t for t in targets if "#" in t and not t.startswith(("#",) + EXTERNAL)]
        self.assertGreater(len(in_page), 0, "no document links within itself")
        self.assertGreater(len(cross), 0, "no document links into another one")

    def test_every_relative_link_resolves(self):
        problems = broken_links(documents())
        self.assertEqual(
            [], problems,
            "a document points at something that is not there:\n  "
            + "\n  ".join(problems))

    def test_every_table_of_contents_lists_every_section(self):
        """A TOC is the map of its own document, so the map has to be complete.

        Resolution cannot see a *dropped* line — the remaining links all still land —
        and the pair check compares the two READMEs against each other, so deleting the
        same line from both passes it. Order counts too: a TOC that lists the right
        sections in the wrong order sends a reader to the wrong place on the page.
        """
        checked = 0
        for path in documents():
            drift = toc_drift(path.read_text(encoding="utf-8"))
            if drift is None:
                continue
            checked += 1
            expected, listed = drift
            self.assertEqual(
                listed, expected,
                "%s: its table of contents is not its section list — missing %s, "
                "not a section %s"
                % (path.relative_to(REPO),
                   [a for a in expected if a not in listed] or "none",
                   [a for a in listed if a not in expected] or "none"))
        self.assertGreaterEqual(checked, 2,
                                "no table of contents was found anywhere in the corpus")

    def test_the_toc_checker_can_report_drift(self):
        """Control: a complete map, a dropped line, and an invented line."""
        def drift(entries, headings):
            body = "## 📖 Contents\n\n" + "".join("- [x](#%s)\n" % e for e in entries)
            return toc_drift(body + "\n" + "".join("## %s\n\n" % h for h in headings))

        self.assertEqual(drift(["-a", "-b"], ["✨ A", "🤔 B"]),
                         (["-a", "-b"], ["-a", "-b"]))
        self.assertEqual(drift(["-a"], ["✨ A", "🤔 B"]),
                         (["-a", "-b"], ["-a"]), "a dropped line is drift")
        self.assertEqual(drift(["-a", "-b", "-zz"], ["✨ A", "🤔 B"]),
                         (["-a", "-b"], ["-a", "-b", "-zz"]),
                         "a line pointing at no section is drift too")

    def test_the_checker_can_report_every_kind_of_break(self):
        """An instrument that cannot report a known break is not guarding anything."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.md").write_text("## Real\n", encoding="utf-8")
            (root / "dup.md").write_text("## Same\n\n## Same\n", encoding="utf-8")
            page = root / "page.md"
            page.write_text(
                "## Real\n\n## 🏗️ How it works\n\n"
                "- [ok](#real)\n"
                "- [emoji](#️-how-it-works)\n"
                "- [repeat](dup.md#same-1)\n"
                "- [across](target.md#real)\n"
                "- [stripped](#-how-it-works)\n"
                "- [dead](#nowhere)\n"
                "- [nofile](missing.md)\n"
                "- [nofrag](target.md#nowhere)\n"
                "\n```\n[fenced](#nowhere)\n```\n",
                encoding="utf-8")
            problems = broken_links([page], root=root)

        self.assertEqual(
            ["page.md: #-how-it-works -> page.md has no heading `-how-it-works`",
             "page.md: #nowhere -> page.md has no heading `nowhere`",
             "page.md: missing.md -> no such file",
             "page.md: target.md#nowhere -> target.md has no heading `nowhere`"],
            problems,
            "the checker missed a break, invented one, or read a fenced example as a link")


if __name__ == "__main__":
    unittest.main()

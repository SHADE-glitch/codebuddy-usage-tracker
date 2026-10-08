"""
test_record_coverage.py — the recording-coverage check.

stdlib unittest only, like every other suite here, so it runs under both
`python3 -m unittest discover -s scripts/tests` and pytest.

Scope rule, stated in CHANGELOG.md's header and here: commits whose subject starts with `feat`
are **not** covered. This is an original project with no upstream, so a feature is the product, not
a droppable deviation. The exclusion is categorical (by commit class) and declared in source, not a
per-commit skip flag.

Checks:
  0. an empty record is legal only when the header declares an empty covered window out loud
  1. the coverage anchor resolves
  2. D-### ids unique, strictly increasing, gapless
  3. every covered commit is cited; every cited hash resolves
  4. every D-### in any tracked .md resolves to an entry
  5. every entry has all five fields and an allowed kind
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CODE_PATHS = ["bin/cbut", "scripts/cbut_db.py", "scripts/cbut-sync.py",
              "scripts/cbut-tui.py", "scripts/cbut-stats.py"]
EXCLUDED_SUBJECT = re.compile(r"^feat(\([^)]*\))?!?:")
KINDS = {"fix", "perf", "taste", "guard", "revert", "chore"}
FIELDS = ["Symptom", "Change", "Evidence", "Cost", "Commit"]
EMPTY_DECLARATION = "No covered commits:"


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=True).stdout


def changelog_text():
    path = ROOT / "CHANGELOG.md"
    assert path.exists(), f"CHANGELOG.md missing from {ROOT}"
    return path.read_text(encoding="utf-8")


def anchor_of(text):
    match = re.search(r"^Coverage:\s*([0-9a-zA-Z^~_/.-]+)\.\.HEAD\s*$", text, re.M)
    assert match, 'CHANGELOG.md needs a "Coverage: <anchor>..HEAD" line'
    return match.group(1)


def resolve(ref):
    try:
        return git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except subprocess.CalledProcessError:
        return None


def covered_commits(anchor):
    """Commits in the window that touched production code and are not excluded by class."""
    out = git("log", "--format=%H%x1f%s", f"{anchor}..HEAD", "--name-only", "--", *CODE_PATHS)
    result, sha, subject, touched = [], None, "", False

    def flush():
        if sha and touched and not EXCLUDED_SUBJECT.match(subject):
            result.append((sha, subject))

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\x1f" in line and re.fullmatch(r"[0-9a-f]{40}", line.split("\x1f", 1)[0]):
            flush()
            sha, subject = line.split("\x1f", 1)
            touched = False
        elif line in CODE_PATHS:
            touched = True
    flush()
    return result


def entry_ids(text):
    return [int(m) for m in re.findall(r"^### D-(\d+) · ", text, re.M)]


class TestRecordCoverage(unittest.TestCase):
    def setUp(self):
        self.text = changelog_text()
        self.anchor = anchor_of(self.text)

    def test_coverage_anchor_resolves(self):
        self.assertIsNotNone(resolve(self.anchor), "coverage anchor does not resolve")

    def test_empty_record_must_be_declared(self):
        covered = covered_commits(resolve(self.anchor))
        entries = entry_ids(self.text)
        if covered and not entries:
            self.fail(
                "the window has %d covered commit(s) but the record has none — a check over an "
                "empty set proves nothing. Add entries." % len(covered))
        if not covered and not entries:
            self.assertIn(
                EMPTY_DECLARATION, self.text,
                "an empty window is legal only when CHANGELOG.md declares it with a line "
                "starting %r" % EMPTY_DECLARATION)

    def test_ids_are_monotonic_and_gapless(self):
        numbers = entry_ids(self.text)
        self.assertEqual(numbers, sorted(numbers), "ids are not increasing")
        self.assertEqual(len(numbers), len(set(numbers)), "an id number is reused")
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)),
                         "there is a gap: a deleted entry is a failure, not a cleanup")

    def test_every_covered_commit_is_cited(self):
        cited = set()
        for refs in re.findall(r"^Commit\s+(.*)$", self.text, re.M):
            for ref in refs.split():
                sha = resolve(ref)
                self.assertIsNotNone(sha, "CHANGELOG cites a commit that does not exist: %s" % ref)
                cited.add(sha)
        missing = [(s[:7], subj) for s, subj in covered_commits(resolve(self.anchor)) if s not in cited]
        self.assertFalse(missing, "uncovered production commits:\n" +
                         "\n".join("  %s %s" % (s, subj) for s, subj in missing))

    def test_kind_and_fields(self):
        for block in re.split(r"^### ", self.text, flags=re.M)[1:]:
            head = block.split("\n", 1)[0]
            match = re.match(r"D-\d+ · \d{4}-\d{2}-\d{2} · ([a-z]+)(?: · .*)?$", head)
            self.assertIsNotNone(match, "malformed entry header: %r" % head)
            self.assertIn(match.group(1), sorted(KINDS), "unknown kind in %r" % head)
            entry_id = head.split(" · ")[0]
            for field in FIELDS:
                self.assertIsNotNone(re.search(rf"^{field}\s", block, re.M),
                                     "%s: missing field %s" % (entry_id, field))

    def test_references_from_other_documents_resolve(self):
        known = set(entry_ids(self.text))
        for rel in [f for f in git("ls-files", "-z", "*.md").split("\0") if f and f != "CHANGELOG.md"]:
            body = (ROOT / rel).read_text(encoding="utf-8")
            for number in {int(m) for m in re.findall(r"\bD-(\d{3,})\b", body)}:
                self.assertIn(number, known, "%s references D-%03d, which has no entry" % (rel, number))


if __name__ == "__main__":
    unittest.main()

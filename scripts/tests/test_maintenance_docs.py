"""test_maintenance_docs.py — the maintenance documents cannot drift from the code.

`docs/maintenance/codebuddy-format.md` carries a block **generated** from the
format registry in `cbut-sync.py`. A hand-copied list is how `schema_version`
once became a value nothing read: written, plausible, and disconnected from the
code. These checks are the difference between a document and a claim.

Two further rules are held here: every file under `docs/maintenance/` is
reachable from its index, and the writer refuses to rewrite a document whose
markers are gone (appending generated text into hand-written prose is how a
section ends up stating two things at once).

Standard library only. Nothing in the repository is written by these tests.
"""

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_stats", SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

DOC = REPO / "docs" / "maintenance" / "codebuddy-format.md"
DOCDIR = REPO / "docs" / "maintenance"

# The builtin catalogue is resolved from the *installed* CodeBuddy package, so a
# checked-in document compared against a live resolution would go red on every
# machine whose CLI version differs from the one that wrote the file — and green
# on exactly one. Pin the search to a path that cannot exist, which pins the
# document to the registry the repository actually ships. The pinned fallback is
# still asserted below, so this cannot quietly become "an empty document
# compared against an empty renderer".
_NO_PRODUCT_JSON = "/nonexistent/cbut-tests/product.json"


def fallback_format_doc() -> str:
    """``format_doc`` as it must read on a machine with no resolvable package."""
    with mock.patch.dict(os.environ, {"CBUT_PRODUCT_JSON": _NO_PRODUCT_JSON}):
        return db.load_sync().format_doc()


def block_of(text: str) -> str:
    head, _, rest = text.partition(stats.FORMAT_BEGIN + "\n")
    if head == text:
        raise AssertionError("the document has no generated block marker")
    body, _, _tail = rest.partition(stats.FORMAT_END)
    return body


class GeneratedFormatBlockTest(unittest.TestCase):
    def test_the_block_matches_the_registry(self):
        """The point of the file: what it states is decided by the parser."""
        self.assertEqual(
            block_of(DOC.read_text(encoding="utf-8")),
            fallback_format_doc(),
            "docs/maintenance/codebuddy-format.md is stale — run "
            "`cbut format --write` in the same change as the registry")

    def test_the_document_names_the_catalogue_it_used(self):
        """A bare "61 builtin tools" is two different statements depending on
        whether the package resolved, so the source travels with the count."""
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("Builtin catalogue source", text)
        self.assertIn("fallback constants", text,
                      "the checked-in document must say it describes the "
                      "shipped registry, not whatever CLI wrote it")
        self.assertIn("product.json unresolved", text)

    def test_a_product_json_catalogue_changes_the_document(self):
        """The renderer really does read the catalogue it is handed: give it a
        different one and the rendered block must move with it."""
        sync = db.load_sync()
        shrunk = frozenset({"Bash", "Read"})
        live = sync.format_doc(tools=shrunk, agents={"general-purpose": "active"},
                               source=Path("/somewhere/product.json"))
        self.assertIn("2 builtin tools", live)
        self.assertIn("1 builtin agents", live)
        self.assertIn("/somewhere/product.json", live)
        self.assertNotEqual(block_of(DOC.read_text(encoding="utf-8")), live)

    def test_every_read_field_appears_in_the_document(self):
        """Positive control. The comparison above would also pass on a renderer
        that returned an empty string, which would leave a tidy document saying
        nothing about the 30+ fields the parser actually reads."""
        text = DOC.read_text(encoding="utf-8")
        fields = db.load_sync().CODEBUDDY_FIELDS
        self.assertGreater(len(fields), 30)
        missing = [f for f in fields if f"`{f}`" not in text]
        self.assertEqual(missing, [], f"read by the parser, undocumented: {missing}")

    def test_a_registry_change_does_leave_the_document_stale(self):
        """The guard must be capable of failing: add a field to the registry and
        the checked-in block no longer matches what the code reads."""
        sync = db.load_sync()
        original = sync.RECORD_FIELDS
        try:
            sync.RECORD_FIELDS = original | {"aNewUpstreamField"}
            with mock.patch.dict(os.environ, {"CBUT_PRODUCT_JSON": _NO_PRODUCT_JSON}):
                drifted = sync.format_doc()
        finally:
            sync.RECORD_FIELDS = original
        self.assertNotEqual(block_of(DOC.read_text(encoding="utf-8")), drifted)
        self.assertIn("`aNewUpstreamField`", drifted)

    def test_the_writer_refuses_a_document_without_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "prose.md"
            doc.write_text("# hand-written notes\n", encoding="utf-8")
            before = doc.read_text(encoding="utf-8")
            self.assertFalse(stats._replace_generated_block(doc, "- generated\n"))
            self.assertEqual(doc.read_text(encoding="utf-8"), before,
                             "a file it refused was modified anyway")

    def test_the_writer_replaces_only_the_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text(
                f"intro\n{stats.FORMAT_BEGIN}\nold\n{stats.FORMAT_END}\ntrailer\n",
                encoding="utf-8")
            self.assertTrue(
                stats._replace_generated_block(doc, "fresh\n"),
                "a correctly marked document was refused")
            self.assertEqual(
                doc.read_text(encoding="utf-8"),
                f"intro\n{stats.FORMAT_BEGIN}\nfresh\n{stats.FORMAT_END}\ntrailer\n")

    def test_the_writer_is_idempotent(self):
        body = fallback_format_doc()
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text(f"{stats.FORMAT_BEGIN}\n{stats.FORMAT_END}\n",
                           encoding="utf-8")
            stats._replace_generated_block(doc, body)
            once = doc.read_text(encoding="utf-8")
            stats._replace_generated_block(doc, body)
            self.assertEqual(doc.read_text(encoding="utf-8"), once)


class DocTreeTest(unittest.TestCase):
    def test_the_maintenance_docs_exist(self):
        for name in ("README.md", "codebuddy-format.md", "compatibility.md",
                     "measurements.md"):
            path = DOCDIR / name
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertGreater(len(path.read_text(encoding="utf-8")), 200,
                               f"{name} is a stub")

    def test_every_document_is_reachable_from_the_index(self):
        index = (DOCDIR / "README.md").read_text(encoding="utf-8")
        for path in sorted(DOCDIR.glob("*.md")):
            if path.name == "README.md":
                continue
            self.assertIn(f"({path.name})", index,
                          f"{path.name} is not linked from the index")

    def test_both_readmes_point_into_docs(self):
        """The landing pages carry the repository layout; a directory nobody can
        walk to from them is a directory that does not get read."""
        for name in ("README.md", "README.zh-CN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("docs/", text, f"{name} never mentions docs/")


class CiMatrixIsDocumentedAsItRuns(unittest.TestCase):
    """What CI runs is stated in three files, and one of them had silently stopped
    being true.

    `docs/maintenance/compatibility.md` exists to be the version matrix; it even carries
    a row declaring that the AGENTS.md CI section "must keep matching the workflow above".
    On 2026-10-11 the workflow became a `3.11` + `3.14` matrix and AGENTS.md was updated in
    the same change, while the maintenance document went on announcing **3.12** and "the
    only interpreter CI runs" — in four places, with all 330 cases green and CI reporting
    success. A description of a check that no longer describes the check is the same failure
    as `schema_version`: it was true once and nobody re-read it. This class makes re-reading
    mandatory.
    """

    WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
    DOCS = ("AGENTS.md", "docs/maintenance/compatibility.md")

    MATRIX_RE = re.compile(r'^\s*python-version:\s*\[([^\]]*)\]', re.M)
    VERSION_RE = re.compile(r'\b3\.\d+\b')
    # A line only makes a claim about CI when it names the workflow file or says what CI
    # runs / is / holds / documents. Prose about the local venv (3.13 here, 3.14 there) is
    # somebody else's fact and must not be swept into this comparison.
    CLAIM_RE = re.compile(r'ci\.yml|CI (runs|is|matrix|section)')

    def setUp(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        lists = self.MATRIX_RE.findall(text)
        self.assertTrue(lists, f"{self.WORKFLOW} has no python-version list to compare against")
        self.matrix = {v.strip().strip("\"'") for chunk in lists for v in chunk.split(",")}

    def offending(self, text):
        """Lines that state a CI version, with the versions they state."""
        out = []
        for number, line in enumerate(text.splitlines(), 1):
            if not self.CLAIM_RE.search(line):
                continue
            versions = set(self.VERSION_RE.findall(line))
            if versions and versions != self.matrix:
                out.append((number, sorted(versions), line.strip()[:72]))
        return out

    def test_the_workflow_states_the_versions_it_runs(self):
        self.assertTrue(self.matrix, "empty matrix: the guard would pass every document")
        for version in self.matrix:
            self.assertRegex(version, r"^3\.\d+$",
                             f"unexpected token in the CI matrix: {version!r}")

    def test_every_ci_claim_in_the_docs_states_the_real_matrix(self):
        wrong = {}
        for name in self.DOCS:
            found = self.offending((REPO / name).read_text(encoding="utf-8"))
            if found:
                wrong[name] = found
        self.assertEqual(
            wrong, {},
            f"CI runs {sorted(self.matrix)}, but these lines say otherwise: {wrong}")

    def test_the_docs_really_do_make_ci_claims(self):
        """Positive control. Both checks above would pass on documents that never
        mention CI at all — the same emptiness that makes a scan over no fields a
        free pass. So the scan's coverage is asserted, not assumed."""
        for name in self.DOCS:
            lines = [(number, line) for number, line
                     in enumerate((REPO / name).read_text(encoding="utf-8").splitlines(), 1)
                     if self.CLAIM_RE.search(line) and self.VERSION_RE.search(line)]
            self.assertGreaterEqual(len(lines), 1,
                                    f"{name} no longer states a CI version anywhere the "
                                    "check can see it, so it is unguarded")

    def test_a_stale_version_next_to_a_ci_claim_is_detected(self):
        """The failure mode being guarded against, reproduced on a synthetic document:
        the workflow moved on and the prose did not."""
        fake = ("| `.github/workflows/ci.yml` | 3.12 | the only interpreter CI runs |\n"
                "| `pyproject.toml` `requires-python` | `>=3.11` | the floor |\n")
        found = self.offending(fake)
        self.assertEqual(len(found), 1,
                         f"a document that still says 3.12 must be caught, got {found}")
        self.assertEqual(found[0][1], ["3.12"])
        # The second line states 3.11 but makes no CI claim: it is the packaging floor.
        self.assertNotIn(2, [hit[0] for hit in found])


PIPELINE = REPO / "docs" / "maintenance" / "pipeline.md"

# A citation is a backticked ``:123`` (the file comes from the enclosing section's
# heading) or ``name.py:123``; a range is allowed. Requiring both the backticks and
# the colon is what keeps prose such as "local 00:00" out of the scan.
CITATION_RE = re.compile(r"`(?:(?P<file>[\w./-]+\.py):)?:(?P<start>\d+)(?:-(?P<end>\d+))?`")
HEADING_FILE_RE = re.compile(r"`([\w./-]+\.py)`")
# The symbol the sentence claims the cited line carries: the last backticked
# identifier before the citation, with nothing but a comma or spaces in between.
# A citation preceded by prose ("reasons (`x.py:388`)") makes no such claim.
SYMBOL_RE = re.compile(r"`([A-Za-z_][\w.]*)(?:\([^`]*\))?`[,\s(]*$")


class PipelineCitationsTest(unittest.TestCase):
    """`pipeline.md` is a map of file:line pointers, so it rots when code moves.

    A prose page about code is checked by nothing — that is how a document goes on
    describing a function renamed three releases ago, and how `schema_version`
    once became a number no code read. The checks here are mechanical: the cited
    line must exist, must not be blank, and where the sentence names the symbol it
    must carry that name. Nothing parses prose for meaning; every claim the page
    makes in the form this test understands is checked.
    """

    def setUp(self):
        self.text = PIPELINE.read_text(encoding="utf-8")

    def sections(self):
        """``(heading, body lines)`` per ``## `` section, in document order.

        The preamble before the first heading is a section with an empty heading:
        it can cite a file by name but cannot use the short form, and skipping it
        would leave those citations unchecked.
        """
        out, head, body = [], "", []
        for line in self.text.splitlines():
            if line.startswith("## "):
                out.append((head, body))
                head, body = line, []
            else:
                body.append(line)
        out.append((head, body))
        return out

    def citations(self):
        """``(file, start, end, symbol, sentence)`` with the file resolved."""
        out = []
        for head, body in self.sections():
            named = HEADING_FILE_RE.search(head)
            default = named.group(1) if named else None
            for line in body:
                for match in CITATION_RE.finditer(line):
                    symbol = SYMBOL_RE.search(line[:match.start()])
                    out.append((
                        match.group("file") or default,
                        int(match.group("start")),
                        int(match.group("end") or match.group("start")),
                        symbol.group(1) if symbol else None,
                        line.strip(),
                    ))
        return out

    def path_of(self, name):
        path = REPO / name
        return path if path.is_file() else SCRIPTS / name

    def test_every_citation_names_a_file(self):
        """The short form is readable only because the heading names the file."""
        bare = [cite for cite in self.citations() if cite[0] is None]
        self.assertEqual(bare, [],
                         f"citations in a section that names no file: {bare}")

    def test_every_citation_lands_on_a_line(self):
        for name, start, end, _symbol, sentence in self.citations():
            if name is None:
                continue          # reported by test_every_citation_names_a_file
            path = self.path_of(name)
            self.assertTrue(path.is_file(), f"{sentence}: {name} does not exist")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(end, len(lines),
                                 f"{sentence}: {name} has only {len(lines)} lines")
            self.assertTrue(lines[start - 1].strip(),
                            f"{sentence}: {name}:{start} is blank — the code moved "
                            "and the page did not")

    def test_a_citation_that_names_a_symbol_lands_on_it(self):
        wrong = []
        for name, start, end, symbol, sentence in self.citations():
            if not symbol or name is None:
                continue
            lines = self.path_of(name).read_text(encoding="utf-8").splitlines()
            leaf = symbol.split(".")[-1]
            window = lines[start - 1:min(end, len(lines))]
            if not any(leaf in line for line in window):
                wrong.append(f"{name}:{start} should carry {leaf!r} — {sentence[:60]}")
        self.assertEqual(wrong, [],
                         "the page points at lines that no longer hold the symbol:\n"
                         + "\n".join(wrong))

    def test_the_scan_is_not_empty(self):
        """Positive control: every check above passes over a page with no citations."""
        cites = self.citations()
        self.assertGreaterEqual(len(cites), 40,
                                f"only {len(cites)} citations found — the scan broke")
        named = [cite for cite in cites if cite[3]]
        self.assertGreaterEqual(len(named), 20,
                                f"only {len(named)} citations name a symbol — the "
                                "symbol check is nearly blind")


if __name__ == "__main__":
    unittest.main()

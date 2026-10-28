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
import sqlite3
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


class TimersAreDocumentedAsTheyRun(unittest.TestCase):
    """A live document may not name a periodic job the TUI does not run.

    `measurements.md` is a page of dated readings, and two of its sentences explained a
    number with a cadence: "the TUI's 30-second worker" and "the 5-second timer". Neither
    existed any more — `on_mount` syncs once and then stays still, and `r` is the only thing
    that syncs again — yet a reading wrapped in a mechanism reads as a fact about the
    program, not as a note about the day it was taken. D-073 corrected this same sentence in
    AGENTS.md and the maintenance index and left these two behind, which is the argument for
    a scan instead of a reading: the claim is cheap to make and expensive to remember.

    The check is about the number *and* the noun. "a 30-second timeout" is somebody else's
    fact and is not swept in; "30-second worker" names a job that either runs or does not.
    `CHANGELOG.md` is deliberately not scanned: its entries record what was true when they
    were written, and D-003 really did add a guard to a timer that existed then.
    """

    TUI = SCRIPTS / "cbut-tui.py"
    DOCS = ("AGENTS.md", "README.md", "README.zh-CN.md",
            "docs/maintenance/README.md", "docs/maintenance/pipeline.md",
            "docs/maintenance/measurements.md", "docs/maintenance/compatibility.md")
    CLAIM_RE = re.compile(r"\b(\d+)-second (?:timer|worker|auto-?sync)\b", re.I)
    # The cadences the TUI really sets. A `set_interval()` that passes an expression
    # instead of a literal fails `setUp` rather than being skipped: a guard that cannot
    # read the cadence must not report that there is none.
    SET_INTERVAL_RE = re.compile(r"set_interval\(\s*(\d+)\s*,")

    def setUp(self):
        text = self.TUI.read_text(encoding="utf-8")
        literals = self.SET_INTERVAL_RE.findall(text)
        calls = text.count("set_interval(")
        self.assertEqual(calls, len(literals),
                         "a set_interval() call passes something other than a literal "
                         "number, so this scan cannot say which cadences exist")
        self.timers = set(literals)

    def offending(self, text):
        """Lines naming a periodic job, with the cadences they name."""
        out = []
        for number, line in enumerate(text.splitlines(), 1):
            for match in self.CLAIM_RE.finditer(line):
                if match.group(1) not in self.timers:
                    out.append((number, match.group(0), line.strip()[:72]))
        return out

    def test_every_cadence_a_live_document_names_is_one_the_tui_sets(self):
        wrong = {}
        for name in self.DOCS:
            found = self.offending((REPO / name).read_text(encoding="utf-8"))
            if found:
                wrong[name] = found
        self.assertEqual(
            wrong, {},
            f"the TUI sets {sorted(self.timers) or 'no timer'}, but these lines name "
            f"another cadence: {wrong}")

    def test_the_scan_reaches_every_document(self):
        """Positive control on coverage: a missing file would otherwise read as a clean
        scan, which is how a guard over nothing passes forever."""
        for name in self.DOCS:
            path = REPO / name
            self.assertTrue(path.is_file(), f"{name} is gone; the scan is no longer whole")
            self.assertGreater(len(path.read_text(encoding="utf-8")), 200, name)

    def test_a_claimed_timer_the_tui_does_not_run_is_detected(self):
        """The failure mode, reproduced: a cadence that no `set_interval` sets, next to a
        number that is not a cadence at all."""
        self.timers = {"30"}
        fake = ("A full refresh is ~40 statements; the 5-second timer re-queries the tab.\n"
                "The client gives up after a 30-second timeout.\n")
        self.assertEqual([hit[1] for hit in self.offending(fake)], ["5-second timer"])


class SchemaVersionIsDocumentedAsItRuns(unittest.TestCase):
    """Four pages state the schema version this build writes, and one had stopped.

    `compatibility.md` said `schema_version` was **6** — thirteen versions after that
    stopped being true — while `pipeline.md` and both READMEs said 13, because each of
    them is written by hand and only the code moves. This is the failure `schema_version`
    itself was introduced to end (a number written, plausible, and read by nothing), one
    level up: the number is now read by the code and the documents copy it.

    Each page states it its own way, so each gets its own pattern rather than one pattern
    loose enough to match all four. A page that stops stating it at all fails the coverage
    control, because a scan over nothing is how a guard passes forever.
    """

    CLAIMS = {
        "docs/maintenance/pipeline.md": re.compile(r"SCHEMA_VERSION = (\d+)"),
        "docs/maintenance/compatibility.md":
            re.compile(r"schema_version[^\n]*currently \*\*(\d+)\*\*"),
        "README.md": re.compile(r"schema v(\d+)"),
        "README.zh-CN.md": re.compile(r"schema v(\d+)"),
    }

    def claims(self, text, pattern):
        return [(number, int(value))
                for number, line in enumerate(text.splitlines(), 1)
                for value in pattern.findall(line)]

    def test_every_stated_version_is_the_one_the_code_writes(self):
        wrong = {}
        for name, pattern in self.CLAIMS.items():
            found = self.claims((REPO / name).read_text(encoding="utf-8"), pattern)
            stale = [hit for hit in found if hit[1] != db.SCHEMA_VERSION]
            if stale:
                wrong[name] = stale
        self.assertEqual(
            wrong, {},
            f"the code writes schema {db.SCHEMA_VERSION}, but these lines say otherwise: "
            f"{wrong}")

    def test_every_page_still_states_a_version(self):
        """Positive control: a pattern that stopped matching would pass silently, and so
        would a page that dropped the sentence instead of correcting the number."""
        for name, pattern in self.CLAIMS.items():
            found = self.claims((REPO / name).read_text(encoding="utf-8"), pattern)
            self.assertGreaterEqual(len(found), 1,
                                    f"{name} no longer states a schema version where "
                                    "this check can see it, so it is unguarded")

    def test_a_version_behind_the_code_is_detected(self):
        """The failure mode, reproduced on a synthetic page."""
        pattern = self.CLAIMS["docs/maintenance/compatibility.md"]
        fake = ("The schema version lives in `meta` as `schema_version` (currently **6** — "
                "v5 removed two prose columns).\n"
                "v6 dropped two indexes that no query used.\n")
        self.assertEqual([hit[1] for hit in self.claims(fake, pattern)], [6])
        self.assertNotEqual(6, db.SCHEMA_VERSION, "the synthetic value caught up with the code")


class SchemaShapeIsDocumentedAsItRuns(unittest.TestCase):
    """`pipeline.md` states the store's shape as a pair of numbers beside the constants
    that build it: "12 tables and 5 views". The version in the same sentence is checked;
    the counts were not, and they move the moment a migration adds a table — which is
    exactly the change that would also move the version, so the two are worth checking
    together rather than trusting the reader to notice one of them.

    The counts are read from a built schema, not from the source: a `CREATE TABLE` inside
    a string is a table only if `executescript` accepts it.
    """

    DOC = REPO / "docs" / "maintenance" / "pipeline.md"
    CLAIM = re.compile(r"(\d+) tables and (\d+) views")

    def test_the_stated_shape_is_the_shape_the_code_builds(self):
        conn = sqlite3.connect(":memory:")
        try:
            db.ensure_schema(conn)
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            views = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0]
        finally:
            conn.close()
        found = self.CLAIM.findall(self.DOC.read_text(encoding="utf-8"))
        self.assertTrue(found, "pipeline.md no longer states the store's shape, so this "
                               "check covers nothing")
        for claimed in found:
            self.assertEqual((int(claimed[0]), int(claimed[1])), (tables, views),
                             f"pipeline.md says {claimed[0]} tables and {claimed[1]} views, "
                             f"the schema builds {tables} and {views}")


class UnparsedReasonsAreDocumented(unittest.TestCase):
    """Every reason `cbut health` can print is named on the page a reader opens when a
    number is wrong.

    D-080 added `unusable_record` to the reason strings and to the generated block, and
    the hand-written list two sections below — the one the "When a panel goes empty"
    checklist points at — still ended at `unreadable_file`. The constants live in the sync
    module, so the page is checked against them rather than against someone's memory of
    what was added last.
    """

    DOC = REPO / "docs" / "maintenance" / "codebuddy-format.md"
    CONSTANTS = ("UNPARSEABLE_LINE", "INVALID_TOOL_NAME", "UNUSABLE_RECORD")
    # Not constants: one carries an OS error, the other a record type.
    PREFIXES = ("unreadable_file:", "type:")

    def test_every_reason_the_sync_can_print_is_named(self):
        sync = db.load_sync()
        text = self.DOC.read_text(encoding="utf-8")
        missing = [f"`{getattr(sync, name)}`" for name in self.CONSTANTS
                   if f"`{getattr(sync, name)}`" not in text]
        missing += [f"`{prefix}" for prefix in self.PREFIXES
                    if f"`{prefix}" not in text]
        self.assertEqual(missing, [],
                         f"{self.DOC.name} never names these reasons, so a reader "
                         f"following it cannot identify them: {missing}")


class TracesAreDocumentedAsRead(unittest.TestCase):
    """No page may call the trace tree unread while the registry reads it.

    D-073 fixed this sentence in AGENTS.md ("not ingested" in one paragraph, "indexes
    the logs (transcripts and OTel traces)" in the next) and left the same sentence in
    two maintenance pages: `codebuddy-format.md`'s "Deliberately not read" section and
    `compatibility.md`'s log-volume row, both contradicted by the generated block above
    them. A page that describes a coupling the code no longer has is the exact failure
    the generated block exists to prevent — it just does not cover prose.

    The rule is narrow on purpose: a line that names the trace directory may not deny
    reading it. "The internal agents' *token* counts are 0 by construction" is true and
    passes; "not ingested" is the claim this repository has now made twice by mistake.
    """

    DOCS = ("AGENTS.md", "README.md", "README.zh-CN.md",
            "docs/maintenance/codebuddy-format.md",
            "docs/maintenance/compatibility.md",
            "docs/maintenance/pipeline.md")
    NAMES_TRACES = re.compile(r"~/.codebuddy/traces|\btraces/|\btrace_\*")
    DENIES = re.compile(r"\bnot\b[^\w]{0,4}\b(?:ingested|read)\b|\bnever\b[^\w]{0,4}"
                        r"\b(?:ingested|read)\b", re.I)

    def offending(self, text):
        return [(number, line.strip()[:80])
                for number, line in enumerate(text.splitlines(), 1)
                if self.NAMES_TRACES.search(line) and self.DENIES.search(line)]

    def test_no_page_denies_reading_the_trace_tree(self):
        wrong = {}
        for name in self.DOCS:
            found = self.offending((REPO / name).read_text(encoding="utf-8"))
            if found:
                wrong[name] = found
        self.assertEqual(wrong, {},
                         f"the sync indexes ~/.codebuddy/traces, but these lines deny it: "
                         f"{wrong}")

    def test_the_pages_do_name_the_trace_tree(self):
        """Positive control: the scan must be able to see the directory it is about."""
        seen = [name for name in self.DOCS
                if self.NAMES_TRACES.search((REPO / name).read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(seen), 2,
                                f"only {seen} name the trace directory; the check is "
                                "looking at pages that never mention it")

    def test_a_denial_next_to_the_trace_tree_is_detected(self):
        fake = ("- **`~/.codebuddy/traces`** (OTel spans). Not ingested: that is where\n"
                "  internal agents live.\n"
                "| `traces/` is **not** ingested |\n"
                "- Traces are read for agent spans only.\n")
        self.assertEqual([hit[0] for hit in self.offending(fake)], [1, 3])


PIPELINE = REPO / "docs" / "maintenance" / "pipeline.md"

# A citation is a backticked ``:123`` (the file comes from the enclosing section's
# heading) or ``name.py:123``; a range is allowed. Requiring both the backticks and
# the colon is what keeps prose such as "local 00:00" out of the scan.
# Both forms occur: `:123` inside a section that names the file, and
# `file.py:123` where it does not. The file-prefixed form used to need a double
# colon, so every `file.py:123` in the page went unchecked — and four of them had
# drifted to lines that hold something else.
CITATION_RE = re.compile(r"`(?:(?P<file>[\w./-]+\.py))?::?(?P<start>\d+)(?:-(?P<end>\d+))?`")
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

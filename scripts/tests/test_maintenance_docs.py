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
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cbut_stats", SCRIPTS / "cbut-stats.py")
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

DOC = REPO / "docs" / "maintenance" / "codebuddy-format.md"
DOCDIR = REPO / "docs" / "maintenance"


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
            db.load_sync().format_doc(),
            "docs/maintenance/codebuddy-format.md is stale — run "
            "`cbut format --write` in the same change as the registry")

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
        body = db.load_sync().format_doc()
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


if __name__ == "__main__":
    unittest.main()

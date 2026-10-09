"""test_format_registry.py — the CodeBuddy format coupling is registered, not scattered.

``cbut`` reads JSON that upstream owns. A renamed field or record type raises
nothing: the panels quietly empty out, `cbut sync` prints "done", CI stays green.
The registry at the top of ``cbut-sync.py`` is the one place that states what we
depend on, and these tests read that file's AST to prove the statement is still
true — in both directions, so the block cannot decay into a description of the
parser that stopped matching it.

Two enforcement styles, chosen to match how each entry is used:

* layout, separators and tool names are **source** — the code reads them through
  the constant, and a bare literal at a call site fails;
* record field names stay **inline** in the handlers (a parser is easier to trust
  when the field it grabs is visible) and are *mirrored* by the ``*_FIELDS`` sets:
  an unregistered read fails and a registered read nobody performs fails.

Every failure names the offending line, because the point of the check is to
point at the call sites a rename still owes.

Standard library only; nothing is executed and no database is touched beyond one
in-memory schema.
"""

import ast
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cbut_db as db  # noqa: E402

SYNC_PATH = SCRIPTS / "cbut-sync.py"
_spec = importlib.util.spec_from_file_location("cbut_sync", SYNC_PATH)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

SRC = SYNC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)

# Methods that *parse* upstream text: their string argument is a claim about
# CodeBuddy's naming or layout. Output formatting (", ".join of a display line)
# is deliberately out of scope — it states nothing about the input format.
FORMAT_METHODS = {"startswith", "endswith", "split", "partition", "rfind",
                  "glob"}

# Constants that must be *read by the code*, not just declared. The mirror sets
# (``*_FIELDS``, HANDLED_RECORD_TYPES) are deliberately absent: their consumer is
# this file, and asserting otherwise would forbid the mirror design.
SOURCE_CONSTANTS = (
    "DIR_PROJECTS", "DIR_SKILLS", "DIR_AGENTS", "DIR_PLUGINS", "TRANSCRIPT_GLOB",
    "SKILL_MANIFEST_GLOB", "AGENT_SPEC_GLOB", "MCP_FILE", "PLUGIN_INDEX_FILE",
    "PLUGIN_MARKETPLACE_SEP", "INVENTORY_HIDDEN_PREFIX", "PLUGIN_SUBDIRS",
    "TRANSCRIPT_LINE_SEP", "SKILL_TOOL", "AGENT_TOOL", "MCP_TOOL_PREFIX",
    "MCP_TOOL_SEP", "CMD_RE", "BAGGAGE_SID_RE", "MODEL_RESPONSE_TYPES",
    "USAGE_FIELDS", "CACHE_USAGE_FIELDS", "UNPARSEABLE_LINE",
)


def get_literal_keys(tree):
    """``{key: [lineno, ...]}`` for every ``x.get("<literal>", ...)`` call."""
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.setdefault(node.args[0].value, []).append(node.lineno)
    return out


def format_method_literals(tree):
    """Literal string arguments to naming/layout methods, with locations."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in FORMAT_METHODS and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.append((node.lineno, node.func.attr, node.args[0].value))
    return out


def path_division_literals(tree):
    """Literal right-hand operands of ``/`` — i.e. inlined path segments."""
    return [(n.lineno, n.right.value) for n in ast.walk(tree)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
            and isinstance(n.right, ast.Constant)
            and isinstance(n.right.value, str)]


def bytes_literals(tree):
    """Every bytes literal, so the JSONL separator cannot be re-typed inline."""
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, bytes)]


def dispatch_types(tree):
    """The ``type`` values ``_handle_record`` branches on.

    The local holding the record type is found by its own assignment rather than
    by name, so renaming it does not blind the check.
    """
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_handle_record"),
              None)
    assert fn is not None, "_handle_record is gone from cbut-sync.py"
    holders = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == "get" and node.value.args
                        and getattr(node.value.args[0], "value", None) == "type"):
                    holders.add(t.id)
    assert holders, "_handle_record no longer reads the record type"
    found = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id in holders):
            right = node.comparators[0]
            if isinstance(right, ast.Constant) and isinstance(right.value, str):
                found.add(right.value)
            elif isinstance(right, (ast.Tuple, ast.Set, ast.List)):
                found.update(e.value for e in right.elts
                             if isinstance(e, ast.Constant))
    return found, holders


def referenced_names(tree):
    """Names *read* somewhere. Load context only, so a constant's own
    definition cannot count as its use."""
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def where(pairs):
    return ", ".join(f"L{p[0]}:{p[1]}" if len(p) > 1 else f"L{p}" for p in pairs)


class FieldsAreRegisteredTest(unittest.TestCase):
    """Record field reads cannot go off the books."""

    def test_every_field_read_is_registered(self):
        reads = get_literal_keys(TREE)
        unregistered = {k: v for k, v in reads.items()
                        if k not in sync.CODEBUDDY_FIELDS}
        self.assertEqual(
            unregistered, {},
            "field(s) read from CodeBuddy data without being registered in the "
            f"format registry: {unregistered} — add them to the *_FIELDS sets in "
            "cbut-sync.py (and check they are not argument *values*)")

    def test_no_registered_field_is_dead(self):
        """A registered field nobody reads is the schema_version failure mode:
        a declaration that stopped being true. The two token tuples are read
        dynamically (``raw.get(k)``) so they are exempted *by construction* —
        RAW_USAGE_FIELDS is derived from them, not typed out again."""
        reads = set(get_literal_keys(TREE))
        dynamic = frozenset(sync.USAGE_FIELDS) | frozenset(sync.CACHE_USAGE_FIELDS)
        dead = sync.CODEBUDDY_FIELDS - reads - dynamic
        self.assertEqual(dead, frozenset(),
                         f"registered but never read: {sorted(dead)}")

    def test_the_registry_covers_every_read(self):
        # Positive control: a scan over an empty result would pass vacuously, so
        # pin that the parser really is reading a large registered surface.
        self.assertGreaterEqual(len(get_literal_keys(TREE)), 30)

    def test_each_raw_usage_field_lands_in_a_column(self):
        """The dynamic tuples are the one place a field can be registered and
        read yet still dropped: the INSERT lists its columns by hand. A field
        with no column would be indexed into nothing, silently."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.ensure_schema(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(model_responses)")}
        conn.close()
        # The provider's own total is stored under an explicit name because the
        # column must say whose total it is.
        stored = {f: f for f in sync.RAW_USAGE_FIELDS}
        stored["total_tokens"] = "provider_total_tokens"
        missing = {f: c for f, c in stored.items() if c not in cols}
        self.assertEqual(missing, {},
                         f"rawUsage fields with no column to persist them: {missing}")


class DispatchTypesAreRegisteredTest(unittest.TestCase):
    def test_dispatch_chain_matches_the_registry(self):
        found, holders = dispatch_types(TREE)
        self.assertEqual(
            found, set(sync.HANDLED_RECORD_TYPES),
            "the registry and the if/elif chain disagree about which record "
            f"types are claimed (branching on {sorted(holders)}): "
            f"only in code={sorted(found - sync.HANDLED_RECORD_TYPES)}, "
            f"only in registry="
            f"{sorted(sync.HANDLED_RECORD_TYPES - found)}")

    def test_model_response_types_are_claimed_types(self):
        found, _ = dispatch_types(TREE)
        unclaimed = set(sync.MODEL_RESPONSE_TYPES) - found
        self.assertEqual(
            unclaimed, set(),
            f"a type treated as a model response is never dispatched: {unclaimed}")


class CouplingIsCentralizedTest(unittest.TestCase):
    """Layout, separators and tool names must be reached through the registry."""

    def test_no_bare_literal_passed_to_a_format_method(self):
        offenders = format_method_literals(TREE)
        self.assertEqual(
            offenders, [],
            "inlined format coupling — use the registry constant instead: "
            + ", ".join(f"L{ln}.{meth}({val!r})" for ln, meth, val in offenders))

    def test_no_bare_literal_path_segment(self):
        offenders = path_division_literals(TREE)
        self.assertEqual(
            offenders, [],
            "path segments inlined at the division site; register them: "
            + where(offenders))

    def test_transcript_line_separator_is_not_retyped(self):
        # b"" is excluded on purpose: an empty literal is how `complete` is
        # initialised, and carries no format claim to centralize.
        offenders = [p for p in bytes_literals(TREE)
                     if p[1] and p[1] != sync.TRANSCRIPT_LINE_SEP]
        self.assertEqual(offenders, [],
                         "a bytes literal other than the registered JSONL "
                         "separator: " + where(offenders))

    def test_source_constants_are_actually_used(self):
        """Guards against the registry becoming a second schema_version: a
        constant declared, plausible-looking, and read by nothing."""
        missing = [n for n in SOURCE_CONSTANTS if not hasattr(sync, n)]
        self.assertEqual(missing, [], f"registry constant removed: {missing}")
        used = referenced_names(TREE)
        unused = [n for n in SOURCE_CONSTANTS if n not in used]
        self.assertEqual(unused, [], f"declared and never used: {unused}")


if __name__ == "__main__":
    unittest.main()

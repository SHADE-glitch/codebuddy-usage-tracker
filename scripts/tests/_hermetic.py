"""_hermetic.py — a runtime tripwire for "tests never touch real state".

AGENTS.md promises that the suite builds its own throwaway databases and that the real
``usage.db`` is never opened. That promise had no check: it has already been broken once (a
dispatcher test ran a real ``cbut sync --full`` against the live database), and on 2026-10-10
the maintainer spent an hour believing it was broken again, because ``usage.db``'s mtime had
moved — the cause turned out to be his own TUI session, but nothing in the repository could
have said so. A rule with no check is a preference, and a wrong guess about it costs an hour.

So this module installs a process-wide audit hook that *refuses* three things:

* opening the real database file — with :meth:`sqlite3.Connection` in any mode, read-only
  included, since the rule is "never opened", not "never written";
* **reading the real host footprint** — any ``open``, ``os.scandir``/``os.listdir`` (a glob
  reaches the host's files without an ``open``) or ``sqlite3.connect`` that names a file
  under the host dir the reader treats as input. The host is *input*, not state, so it was
  never covered; but a test that reads it stops testing the code and starts testing this
  machine. The sibling `ocut` shipped exactly that bug — a fixture read the developer's
  real plugin-events sidecar and three tests were red only where the sidecar existed. A
  test must build a throwaway host in a temp dir instead;
* launching one of this repository's entry points in a child process that would resolve the
  real database **or read the real host** — no pinned ``CBUT_DB``/``--db`` and no pinned
  ``CBUT_CODEBUDDY_DIR``. "Launching" is read from argv, not from the raw text: a ``git``
  pathspec or a ``bash -n`` syntax check names an entry point without running one, and
  refusing those would be a false positive (see :func:`_script_being_run`).

The hook runs at import, and ``unittest`` imports every test module before running any case,
so one import anywhere covers the whole run. Every ``test_*.py`` imports it anyway, and
``test_hermetic.py`` fails on any that stops doing so, so coverage does not depend on file
names sorting early.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Frozen from the real home directory at import, deliberately *not* from ``cbut_db.DB_PATH``:
# the data layer's default moves when CBUT_DB or HOME is set, and a test that fakes one of
# those must not be able to redefine what counts as "the real database".
PROTECTED = Path.home() / ".local" / "share" / "codebuddy-usage-tracker" / "usage.db"

# The host's own footprint: everything the reader treats as *input*. Frozen from the real
# home at import, for the same reason as ``PROTECTED`` — a test that fakes HOME or
# CBUT_CODEBUDDY_DIR must not be able to redefine what counts as "the real host".
PROTECTED_DIRS = (Path.home() / ".codebuddy",)

# The environment variables that move each protected host dir. A child given none of these
# falls back to the real dir; the Popen guard below refuses that.
HOST_ENVS = ("CBUT_CODEBUDDY_DIR",)

# Anything naming one of these is a child that will reach for the default database unless it
# is told otherwise.
ENTRY_POINTS = ("cbut_db", "cbut-sync", "cbut-stats", "cbut-tui", "bin/cbut")


class Refusal(RuntimeError):
    """Raised instead of performing an action the invariant forbids."""


def _real_target(database: str) -> Path | None:
    """The file a ``sqlite3.connect`` argument names, or None if it is not the real one.

    Handles the URI form (``file:…?mode=ro``) as well as a plain path: a read-only open of
    the live database is still an open of the live database.
    """
    text = str(database)
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    if not text or text == ":memory:":
        return None
    try:
        resolved = Path(text).expanduser().resolve()
    except OSError:
        return None
    return resolved if resolved == PROTECTED.resolve() else None


def _under(path, root: Path) -> bool:
    """Whether ``path`` names a file at or under ``root``.

    Accepts the int fd some ``open`` calls pass, and a path that cannot be resolved: both
    are simply not under a protected dir. SQLite's ``file:…`` URI is reduced to its path.
    """
    text = str(path)
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    try:
        Path(text).expanduser().resolve().relative_to(root.resolve())
    except (OSError, ValueError, TypeError):
        return False
    return True


def _protected_read(path) -> Path | None:
    """The protected host dir a read of ``path`` targets, or None."""
    for root in PROTECTED_DIRS:
        if _under(path, root):
            return root
    return None


def _effective_db(env: dict | None) -> str | None:
    """The database a child would use, from its own environment.

    ``env=None`` means the child inherits ours, which is the same thing for this decision.
    """
    source = os.environ if env is None else env
    return source.get("CBUT_DB")


# Interpreters that can be handed one of our scripts to *run*. A shell invoked with ``-n``
# (syntax check) or ``-c`` (inline string) runs no script file, and neither does ``python -c``.
_SHELLS = ("bash", "sh", "dash", "zsh", "ksh")


def _names_an_entry_point(token: str) -> bool:
    """Whether one argv token is a path to one of our executable entry points."""
    text = str(token).replace("\\", "/")
    base = text.rsplit("/", 1)[-1]
    if any(base == f"{stem}.py" for stem in ENTRY_POINTS if "/" not in stem):
        return True
    return text == "bin/cbut" or text.endswith("/bin/cbut")


def _script_being_run(command) -> str | None:
    """The entry-point script a child is about to *execute*, or None.

    The invariant is about *running* our code, so this reads argv positions rather than
    whether the text merely mentions a path. ``git log -- bin/cbut`` and ``bash -n
    bin/cbut`` both name an entry point without executing one; a child that does not run our
    code cannot open the real database, and refusing it would be a false positive that trains
    the suite to add meaningless pins. The script is the first non-option argument handed to
    an interpreter (``bash FILE``, ``python FILE``); ``bash -n``/``-c`` and ``python -c`` hand
    over no script *file*. When the program is itself one of our scripts, that is the launch.
    (An inline ``bash -c "bin/cbut sync"`` is therefore out of scope: it is not a script file,
    and scanning inline strings is exactly what made the crash-child probe a false positive.)
    """
    parts = [str(part) for part in command]
    if not parts:
        return None
    program = os.path.basename(parts[0])
    args = parts[1:]
    if program in _SHELLS:
        if any(arg in ("-n", "--noexec", "-c") for arg in args):
            return None
    elif program.startswith("python"):
        if args and args[0] == "-c":
            return None
    else:
        # Not an interpreter: our code runs only if the program *is* our script.
        return parts[0] if _names_an_entry_point(parts[0]) else None
    for arg in args:
        if arg.startswith("-"):
            continue
        return arg if _names_an_entry_point(arg) else None
    return None


def _child_would_open_the_real_db(command, env: dict | None) -> bool:
    """True when a spawn of our code has nothing steering it away from ``PROTECTED``."""
    if _script_being_run(command) is None:
        return False
    text = " ".join(str(part) for part in command)
    # An explicit --db is the documented way to point a report at another file.
    match = re.search(r"--db[=\s]+([^\s]+)", text)
    if match:
        try:
            return Path(match.group(1)).expanduser().resolve() == PROTECTED.resolve()
        except OSError:
            return True
    target = _effective_db(env)
    if target is None:
        return True
    try:
        return Path(target).expanduser().resolve() == PROTECTED.resolve()
    except OSError:
        return True


def _child_would_read_the_real_host(env: dict | None) -> bool:
    """True when a child of ours would read a real protected host dir.

    A child inherits our environment, so an unpinned host variable means it reads the real
    footprint. Any one unpinned variable is enough: ``tcut`` has two host dirs and either
    left at its default leaks.
    """
    source = os.environ if env is None else env
    for var in HOST_ENVS:
        value = source.get(var)
        if value is None:
            return True
        try:
            resolved = Path(value).expanduser().resolve()
        except OSError:
            return True
        if any(resolved == root.resolve() for root in PROTECTED_DIRS):
            return True
    return False


def _refuse_host_read(path, how: str) -> None:
    """Refuse a read or listing under a real protected host dir, if ``path`` is one."""
    root = _protected_read(path)
    if root is not None:
        raise Refusal(
            f"a test tried to {how} under the real host footprint {root}. The suite builds "
            "a throwaway host in a temp directory and points CBUT_CODEBUDDY_DIR at it — see "
            "the 'Tests never touch real state' rule in AGENTS.md. Reading the real host "
            "makes the test a measurement of this machine, not of the code."
        )


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        _refuse_host_read(args[0], "read a file")
    elif event in ("os.scandir", "os.listdir"):
        # A glob or a directory walk reaches the host's files without an ``open`` — the
        # health report globs ``projects/**/*.jsonl`` this way. Cover the listing itself.
        _refuse_host_read(args[0], "list a directory")
    elif event == "sqlite3.connect":
        if _real_target(args[0]) is not None:
            raise Refusal(
                f"a test tried to open the real database {PROTECTED}. The suite builds "
                "throwaway databases in a temp directory — see the 'Tests never touch real "
                "state' rule in AGENTS.md. Read-only is refused too: the rule is 'never "
                "opened'."
            )
        _refuse_host_read(args[0], "open a database")
    elif event == "subprocess.Popen":
        executable, command, _cwd, env = args[0], args[1], args[2], args[3]
        if isinstance(command, (list, tuple)):
            argv = [str(part) for part in command]
        else:
            argv = [str(command)] if command else []
        if not argv:
            argv = [str(executable)]
        if _script_being_run(argv) is None:
            return
        if _child_would_open_the_real_db(argv, env):
            raise Refusal(
                f"a test launched {' '.join(argv)[:160]} without CBUT_DB or --db pointing "
                f"away from the real database ({PROTECTED}). A child inherits the ambient "
                "path, so it indexes into the maintainer's live store — pin CBUT_DB in the "
                "environment it is given."
            )
        if _child_would_read_the_real_host(env):
            raise Refusal(
                f"a test launched {' '.join(argv)[:160]} without CBUT_CODEBUDDY_DIR pointing "
                f"away from the real host ({PROTECTED_DIRS[0]}). A child inherits the ambient "
                "path, so it reads the maintainer's real CodeBuddy footprint — pin "
                "CBUT_CODEBUDDY_DIR in the environment it is given."
            )


# One hook per process, however many test modules import this file.
INSTALLED = False
if not getattr(sys, "_cbut_hermetic_installed", False):
    sys.addaudithook(_hook)
    sys._cbut_hermetic_installed = True
    INSTALLED = True

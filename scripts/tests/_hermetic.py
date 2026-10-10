"""_hermetic.py — a runtime tripwire for "tests never touch real state".

AGENTS.md promises that the suite builds its own throwaway databases and that the real
``usage.db`` is never opened. That promise had no check: it has already been broken once (a
dispatcher test ran a real ``cbut sync --full`` against the live database), and on 2026-10-10
the maintainer spent an hour believing it was broken again, because ``usage.db``'s mtime had
moved — the cause turned out to be his own TUI session, but nothing in the repository could
have said so. A rule with no check is a preference, and a wrong guess about it costs an hour.

So this module installs a process-wide audit hook that *refuses* two things:

* opening the real database file — with :meth:`sqlite3.Connection` in any mode, read-only
  included, since the rule is "never opened", not "never written";
* launching one of this repository's entry points in a child process that would resolve the
  real database — no pinned ``CBUT_DB`` and no explicit ``--db``. "Launching" is read from
  argv, not from the raw text: a ``git`` pathspec or a ``bash -n`` syntax check names an
  entry point without running one, and refusing those would be a false positive (see
  :func:`_script_being_run`).

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


def _hook(event: str, args: tuple) -> None:
    if event == "sqlite3.connect":
        if _real_target(args[0]) is not None:
            raise Refusal(
                f"a test tried to open the real database {PROTECTED}. The suite builds "
                "throwaway databases in a temp directory — see the 'Tests never touch real "
                "state' rule in AGENTS.md. Read-only is refused too: the rule is 'never "
                "opened'."
            )
    elif event == "subprocess.Popen":
        executable, command, _cwd, env = args[0], args[1], args[2], args[3]
        if isinstance(command, (list, tuple)):
            argv = [str(part) for part in command]
        else:
            argv = [str(command)] if command else []
        if not argv:
            argv = [str(executable)]
        if _child_would_open_the_real_db(argv, env):
            raise Refusal(
                f"a test launched {' '.join(argv)[:160]} without CBUT_DB or --db pointing "
                f"away from the real database ({PROTECTED}). A child inherits the ambient "
                "path, so it indexes into the maintainer's live store — pin CBUT_DB in the "
                "environment it is given."
            )


# One hook per process, however many test modules import this file.
INSTALLED = False
if not getattr(sys, "_cbut_hermetic_installed", False):
    sys.addaudithook(_hook)
    sys._cbut_hermetic_installed = True
    INSTALLED = True

# Compatibility

What this tool claims to work against, and how much of that claim is actually checked. Every line
was read off this machine or out of a file on 2026-10-09 / 2026-10-10 / 2026-10-11; nothing here is
remembered. The CI rows are not prose anyone has to keep correct: `CiMatrixIsDocumentedAsItRuns` in
`scripts/tests/test_maintenance_docs.py` reads the matrix out of the workflow file and fails any
document that states a different set.

## Python

| Where | Version | What it means |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.11` | the floor the project promises |
| `.github/workflows/ci.yml` | **3.11 and 3.14** | the matrix CI runs: the floor and the declared upper bound, `fail-fast: false` |
| `AGENTS.md` (CI section) | 3.11 and 3.14 | must keep matching the workflow above |
| `install.sh` | `uv venv --python 3.13` (falls back to `uv venv` / `python3 -m venv`) | what a fresh local install gets when `uv` is present |
| `README.md` | "3.11+ — the full suite run green on 3.11, 3.12, 3.13 and 3.14" | the claim a visitor reads |
| this machine, `.venv` | 3.13.14 | what the TUI actually runs on |
| this machine, `python3` | 3.14.4 | what the headless commands run on |

**Green CI does not certify the interpreter you run.** CI runs 3.11 and 3.14 — the two ends of the
promise, deliberately, so an incompatibility at either bound is caught. It does **not** run 3.12 or
3.13, and 3.13 is exactly what the TUI on this machine uses, so a claim about "CI passed" and a claim
about "it works on my machine" are still different claims, each needing its own run. `__pycache__` on
this machine holds `cpython-313` and `cpython-314` artefacts, which is how the gap showed up in the
first place — 3.11 and 3.12 have since been exercised in throwaway venvs (table below) rather than
guessed at.

What has actually been executed, most recently on 2026-10-11 — re-run after the settings layer, after
the column plan was extended to every table, after the plan got its source-level guard, after the cell
cap and the dynamic-import check landed, after the cap became a setting (`name_cap`), after the READMEs
were put under a check, after the hermeticity tripwire was installed, and again after the indexer began
reading the nested cache number and after the CI-matrix guard was added — because the first imports
`tomllib`, which is standard library only from exactly the floor this table's bottom row sits on, because
layout code is drawn by every interpreter, because `sys.addaudithook` is behaviour an interpreter can
legitimately differ on, and because the newest change descends into a nested dict on a path that a
different Python could parse differently:

| Interpreter | Suite on this machine | Notes |
|---|---|---|
| `.venv` 3.13.14 | **Ran 334, OK** (163.6 s) | what the TUI runs on, sqlite 3.53.1 — the interpreter **no CI job covers** |
| 3.14.4 (system build, sqlite 3.46.1) | **Ran 334, OK** (147.6 s) | throwaway venv on the system interpreter, and one of CI's two jobs — a different SQLite build, same green |
| 3.12.14 | **Ran 334, OK** (169.0 s) | throwaway `uv venv` on 3.12 + `textual>=8.2,<9`. CI does not run this any more since the matrix moved to the two bounds; it is still the version `install.sh`-era users land on, so it is measured rather than assumed |
| 3.11.15 (the promised floor) | **Ran 334, OK** (180.7 s) | throwaway `uv venv` on 3.11 in `/tmp`, `textual>=8.2,<9`, sqlite 3.53.1 — CI's lower job, and the row that would fail first if `tomllib` were not available at the floor. Not a project venv, delete it and nothing is lost |

One serial pass on the tree D-033 lands in (code `f3e5b3f` + the record `1175ab6`) printed **Ran 330**,
and the table above is the same pass re-run once the CI-matrix guard had shipped — four interpreters,
four `OK`s, on `fd4136f`. Each interpreter into its
**own** log, and each log's mtime read back before its number was believed: the previous day's pass used
the same `/tmp/mx-<interpreter>.log` names, so a stale `Ran 313, OK` was sitting in two of the files
looking exactly like evidence from today — a re-run that overwrites names one interpreter at a time
cannot tell the two apart unless the file's own timestamp is checked. The pass certifies a **worktree**,
not a commit: `test_record_coverage` reads `CHANGELOG.md`
from disk, so the same four interpreters run at `dd2ef52` alone came back `3.13 OK / 3.12, 3.11, 3.14
one failure each` — the guard citing a record that did not exist yet, which is the guard working, not
a portability difference. Earlier runs of the same day, kept because the counts moved and a reader
will wonder: 298 when the settings layer landed, 303 when the
column plan reached every table, 304 with the source-level table census guard, 308 with the cell cap,
310 with the dynamic-import check, 313 when the cap became a setting and `cbut health` started
reporting every key it read, 315 when the settings suite stopped depending on the shell it runs in,
316 with the check that both READMEs document every setting the loader knows, 326 with the hermeticity
tripwire, 327 when the bilingual README pair got its status-code guard, and 330 with the nested cache
read.

The 315 pass was run **twice on this machine on purpose**: once with a clean environment, once with
all six settings exported (`CBUT_TOP_N=9 CBUT_NAME_CAP=40 CBUT_REFRESH_SECS=2 CBUT_LOG_LIMIT=7
CBUT_DETAIL_LIMIT=3 CBUT_SYNC_SECS=99`). Both printed `Ran 315` and failed only on the two README
count guards, identically — the claim being that nothing in the suite depends on the ambient shell
anymore, not that the number is 315.

All four interpreters this project can plausibly meet are therefore measured, and both interpreters CI
runs are covered locally as well. What is still *not* certified: the exact runner image, `pip` resolution
and Python patch level GitHub Actions uses (`3.11.x` and `3.14.x` on `ubuntu-latest`), and any interpreter nobody installed
here at all. The three throwaway venvs from this pass were deleted afterwards; the project `.venv` was
never touched.
Reproducing either run — the interpreter is resolved from an installed path, not from `python3` on the
PATH, because the version on the PATH is the one thing this table is about:

```bash
uv venv /tmp/cbut312 --python /path/to/cpython-3.12/bin/python3.12
uv pip install --python /tmp/cbut312/bin/python -r requirements.txt
/tmp/cbut312/bin/python -m unittest discover -s scripts/tests     # ~2 min, one log per interpreter

uv venv /tmp/cbut311 --python /path/to/cpython-3.11/bin/python3.11
uv pip install --python /tmp/cbut311/bin/python "textual>=8.2,<9"
/tmp/cbut311/bin/python -m unittest discover -s scripts/tests
```

Running it *continuously* rather than once per batch means putting those interpreters into the workflow
matrix — ~2 minutes per added job, and the suite needs `textual` installed because the TUI suites use
its `run_test` harness. The matrix moved to the two bounds on 2026-10-11; widening it further is
`python-version` in `.github/workflows/ci.yml`, and changing it there is enough to make every document
above fail until it is updated with the change, which is what `AGENTS.md` asks for in the same commit.

## SQLite

The two local interpreters **link different SQLite builds**, so "the SQLite here" is not one number:

| Interpreter | `sqlite3.sqlite_version` |
|---|---|
| `.venv` (3.13.14) — what the TUI and `install.sh` create | **3.53.1** |
| system `python3` (3.14.4) — what headless commands get without the venv | **3.46.1** |

No separate `sqlite3` binary is required or assumed; the library comes from whichever build Python's
`sqlite3` module links against. The journal is WAL and `PRAGMA user_version` is unused: the schema
version lives in the `meta` table as `schema_version` (currently **6** — v5 removed two prose columns,
v6 dropped two indexes that no query used) and `ensure_schema()` refuses
to open a newer database with older code rather than silently no-op'ing the `CREATE TABLE IF NOT
EXISTS` set.

Two features below need SQLite ≥ 3.35 and are the reason the floor is not "any version":
`ALTER TABLE … DROP COLUMN` (the v5 migration that removed the two prose columns) and
upsert (`ON CONFLICT … DO UPDATE`), used throughout the indexer. Both builds clear that bar, but a
machine on an older distro Python may not — if `cbut sync` dies on `no such table: <new table>` or a
`DROP COLUMN` syntax error, print `sqlite3.sqlite_version` before anything else.

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"   # needs >= 3.35
```

## CodeBuddy

| Fact | Status |
|---|---|
| Record shapes, field names and on-disk layout | **verified against the logs on this machine** — see [`codebuddy-format.md`](codebuddy-format.md); `test_format_registry.py` holds the code and that list together |
| `dpkg` reports `codebuddy-cn 4.12.0` | the **IDE** package, not the CLI whose logs are read. Do not cite it as the log format's version |
| A CodeBuddy **version number** (e.g. "2.161.4") | **not machine-confirmable here, so not claimed.** The CLI is launched through a shell alias, no transcript line carries a `version`/`clientVersion`/`appVersion` field (measured: 0 hits), and `dpkg` only proves the IDE package. The READMEs used to print "schema verified against 2.161.4"; that claim was removed because nothing here can re-derive it — the verified statement is "the schema matches the logs on this machine". If the owner supplies the source, put the number back with its provenance attached |
| Log volume actually parsed | ≈ 500 `projects/**/*.jsonl` transcripts. `traces/` (≈ 1.4k `.jsonl` + ≈ 0.7k `.json`) is **not** ingested. Exact counts are personal usage data — see [`measurements.md`](measurements.md) for the rounding rule and the commands that reprint them |

If CodeBuddy's log layout changes, the symptom is a panel going empty while `cbut sync` reports
success — follow the checklist in [`codebuddy-format.md`](codebuddy-format.md#when-a-panel-goes-empty).

## Textual

Pinned `>=8.2,<9` in `requirements.txt`; **8.2.8** installed. The TUI is the only consumer — every
headless command is stdlib-only, which is why `bin/cbut` can still print an error when the venv is
broken.

What the installed set actually contains, measured 2026-10-09 on this machine (9 distributions:
textual **8.2.8** + 8 runtime deps):

- **No package imports `socket` or `ssl` at all** — not textual, not rich, not pygments.
- `urllib` appears in five modules and every one of them is `urllib.parse`: string work, no I/O
  (`textual/_slug.py`, `textual/validation.py`, `textual/widgets/_markdown.py`,
  `markdown_it/common/normalize_url.py`, `mdurl/_encode.py`).
- Network-capable *source* exists in exactly two places, and neither can run in this venv: five
  pygments lexer data-regeneration helpers (`_lua`/`_mysql`/`_php`/`_postgres`/`_sourcemod`
  `_builtins.py`), each behind `if __name__ == '__main__'`; and `textual/demo/`, two of whose modules
  `import httpx` and call `api.github.com`. **`httpx` is not installed here** (`find_spec` → `None`),
  and `textual.demo` is a namespace subpackage that `import textual` never loads.

That is a snapshot of one lockfile, not a property of this project: the privacy check
(`test_privacy.py`) scans **this repository's** production scripts, and the invariant is stated that
way on purpose. "We open no sockets" would be a claim about third-party packages we do not control
and about versions that have not been installed yet. Re-measure after any dependency change with:

```bash
./.venv/bin/python - <<'PY'
import ast, pathlib
NET = {"socket","ssl","http","urllib","requests","httpx","aiohttp","urllib3",
       "smtplib","ftplib","websockets","websocket","twisted"}
root = next(p for p in pathlib.Path(".venv").glob("lib/python*/site-packages"))
for f in sorted(root.rglob("*.py")):
    try: tree = ast.parse(f.read_text(errors="replace"))
    except (SyntaxError, UnicodeDecodeError): continue
    mods = {n.name.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import) for n in x.names}
    mods |= {x.module.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.ImportFrom) and x.module}
    if mods & NET: print(f"{str(f.relative_to(root)):56}", sorted(mods & NET))
PY
./.venv/bin/python -c "import importlib.util as u; print('httpx installed:', u.find_spec('httpx'))"
```

An `urllib` hit is fine only when the line reads `from urllib.parse import ...`; any `socket`, `ssl`,
`http`, `requests` or `httpx` hit must be read before the privacy sentence is repeated anywhere.

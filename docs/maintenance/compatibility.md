# Compatibility

What this tool claims to work against, and how much of that claim is actually checked. Every line
was read off this machine or out of a file on 2026-10-09 / 2026-10-10; nothing here is remembered.

## Python

| Where | Version | What it means |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.11` | the floor the project promises |
| `.github/workflows/ci.yml` | **3.12** | the only interpreter CI runs |
| `AGENTS.md` (CI section) | 3.12 | must keep matching the workflow above |
| `install.sh` | `uv venv --python 3.13` (falls back to `uv venv` / `python3 -m venv`) | what a fresh local install gets when `uv` is present |
| `README.md` | "3.11+ — the full suite run green on 3.11, 3.12, 3.13 and 3.14" | the claim a visitor reads |
| this machine, `.venv` | 3.13.14 | what the TUI actually runs on |
| this machine, `python3` | 3.14.4 | what the headless commands run on |

**Green CI does not certify the interpreter you run.** CI is 3.12; the two interpreters actually
installed here are 3.13 and 3.14, so a claim about "CI passed" and a claim about "it works on my
machine" are different claims and each needs its own run. `__pycache__` on this machine holds
`cpython-313` and `cpython-314` artefacts, which is how the gap showed up — 3.11 and 3.12 have since
been exercised in throwaway venvs (table below) rather than guessed at.

What has actually been executed, most recently on 2026-10-10 — re-run after the settings layer and
the narrow-terminal column plan landed, because the first imports `tomllib`, which is standard
library only from exactly the floor this table's bottom row sits on, and the second changes layout
code every interpreter draws:

| Interpreter | Suite on this machine | Notes |
|---|---|---|
| `.venv` 3.13.14 | **Ran 298, OK** (113.0 s) | what the TUI runs on |
| 3.14.4 (system build, sqlite 3.46.1) | **Ran 298, OK** (104.9 s) | throwaway venv on the system interpreter — a different SQLite build, same green |
| 3.12.14 (CI's version) | **Ran 298, OK** (123.1 s) | throwaway `uv venv --python 3.12` + `textual>=8.2,<9`, i.e. the way CI builds it — run *before* pushing, so CI is not the first place 3.12 sees this code |
| 3.11.15 (the promised floor) | **Ran 298, OK** (129.7 s) | throwaway `uv venv --python 3.11` in `/tmp`, `textual==8.2.8`, sqlite 3.53.1 — this is the row that would fail first if `tomllib` were not available at the floor. Not a project venv, delete it and nothing is lost |

All four interpreters this project can plausibly meet are therefore measured, and the CI version is
not an exception. What is still *not* certified: the exact runner image, `pip` resolution and Python
patch level GitHub Actions uses (`3.12.x` on `ubuntu-latest`), and any interpreter nobody installed
here at all. All three throwaway venvs were deleted afterwards; the project `.venv` was never touched.
Reproducing either run:

```bash
uv venv --python 3.12 /tmp/cbut312 && VIRTUAL_ENV=/tmp/cbut312 uv pip install -r requirements.txt
/tmp/cbut312/bin/python -m unittest discover -s scripts/tests     # ~2 min

uv venv --python 3.11 /tmp/cbut311 && VIRTUAL_ENV=/tmp/cbut311 uv pip install "textual>=8.2,<9"
/tmp/cbut311/bin/python -m unittest discover -s scripts/tests
```

Widening the CI matrix (`python-version: ["3.11", "3.12", "3.14"]`, ~2 minutes per job; the suite needs
`textual` installed because the TUI suites use its `run_test` harness) is still the only way to make
this continuous rather than a one-off. Adding a matrix changes CI, so `AGENTS.md` says to make it in
the same commit as this table.

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

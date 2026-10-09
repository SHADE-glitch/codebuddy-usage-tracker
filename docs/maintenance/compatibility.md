# Compatibility

What this tool claims to work against, and how much of that claim is actually checked. Every line
was read off this machine or out of a file on 2026-10-09; nothing here is remembered.

## Python

| Where | Version | What it means |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.11` | the floor the project promises |
| `.github/workflows/ci.yml` | **3.12** | the only interpreter CI runs |
| `AGENTS.md` (CI section) | 3.12 | must keep matching the workflow above |
| `install.sh` | `uv venv --python 3.13` (falls back to `uv venv` / `python3 -m venv`) | what a fresh local install gets when `uv` is present |
| `README.md` | "3.11+ — the full suite run green on 3.11, 3.13 and 3.14" | the claim a visitor reads |
| this machine, `.venv` | 3.13.14 | what the TUI actually runs on |
| this machine, `python3` | 3.14.4 | what the headless commands run on |

**Green CI does not certify the interpreter you run.** CI is 3.12 and neither local interpreter is
3.12, so the tested-elsewhere case is unverified in both directions: a 3.12-only failure would pass
locally, and a 3.13/3.14-only failure would pass CI. `__pycache__` on this machine holds both
`cpython-313` and `cpython-314` artefacts, which is how the gap showed up.

What has actually been executed, as of 2026-10-09:

| Interpreter | Suite on this machine | Notes |
|---|---|---|
| `.venv` 3.13.14 | **Ran 272, OK** (114.0 s) | what the TUI runs on |
| system `python3` 3.14.4 | **Ran 272, OK** (127.5 s) | different SQLite build (3.46.1), same green |
| 3.12 (CI) | green at `e299bf5` — **never at the current work** | this whole round is still uncommitted, so CI has not seen any of it |
| 3.11.15 (the promised floor) | **Ran 272, OK** (129.9 s) | throwaway `uv venv --python 3.11` in `/tmp`, `textual==8.2.8`, sqlite 3.53.1 — not a project venv, delete it and nothing is lost |

So the only unexercised interpreter is **3.12 — the one CI uses**, and CI has not seen this round of
work yet (everything here is uncommitted). Two ways to close that last gap: widen the matrix
(`python-version: ["3.11", "3.12", "3.14"]`, ~2 minutes per job; the suite needs `textual` installed,
because the TUI suites use its `run_test` harness) or keep 3.12 as "CI only" and push before trusting
it. Adding a matrix changes CI, so `AGENTS.md` says to make it in the same commit as this table.
Reproducing the 3.11 run without touching the project venv:

```bash
uv venv --python 3.11 /tmp/cbut311 && VIRTUAL_ENV=/tmp/cbut311 uv pip install "textual>=8.2,<9"
/tmp/cbut311/bin/python -m unittest discover -s scripts/tests
```

## SQLite

The two local interpreters **link different SQLite builds**, so "the SQLite here" is not one number:

| Interpreter | `sqlite3.sqlite_version` |
|---|---|
| `.venv` (3.13.14) — what the TUI and `install.sh` create | **3.53.1** |
| system `python3` (3.14.4) — what headless commands get without the venv | **3.46.1** |

No separate `sqlite3` binary is required or assumed; the library comes from whichever build Python's
`sqlite3` module links against. The journal is WAL and `PRAGMA user_version` is unused: the schema
version lives in the `meta` table as `schema_version` (currently **5**) and `ensure_schema()` refuses
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
| README badge "CodeBuddy 2.16x — schema verified against 2.161.4" | **source not machine-confirmable.** The CLI is launched through a shell alias here, its version is not readable from the logs, and no transcript line carries a version field. The number needs its origin stated before it can be kept as a claim |
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

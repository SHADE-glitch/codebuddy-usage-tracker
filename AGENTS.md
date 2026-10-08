# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A **passive** usage tracker for CodeBuddy Code: it parses the logs CodeBuddy already writes and
builds a local SQLite index, then shows it in a seven-tab Textual TUI (plus a headless `cbut stats`
CLI). Three layers, in dependency order:

1. `scripts/cbut-sync.py` — the **only writer**. Parses `~/.codebuddy/projects/**/*.jsonl` and
   `~/.codebuddy/traces/**/*.jsonl` read-only, classifies tools/skills/agents/plugins/MCP calls, and
   writes incrementally and idempotently into `~/.local/share/codebuddy-usage-tracker/usage.db`.
2. `scripts/cbut_db.py` — the shared data layer (schema, queries, calendar-day windows). Imported by the
   TUI, the stats CLI and the tests.
3. `scripts/cbut-tui.py` (Textual) and `scripts/cbut-stats.py` (headless CLI). Read-only apart from
   explicit maintenance subcommands.

`bin/cbut` is a bash dispatcher. **It is bash on purpose**: it must still run and print a useful
error when the TUI venv is missing or broken, so it cannot depend on any Python.

This repository is public.

## Commands

```bash
python3 -m unittest discover -s scripts/tests     # stdlib unittest; pytest discovers them too
./install.sh                                      # create .venv + link bin/cbut; optional systemd timer
bash -n bin/cbut                                  # syntax-check the dispatcher
```

Single file: `python3 -m unittest scripts.tests.test_sync` (or the matching `pytest` form).

## CI

GitHub Actions runs on every `push` and `pull_request` (`.github/workflows/ci.yml`, Python 3.12):

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s scripts/tests
```

The checkout must fetch full history (`fetch-depth: 0`): the record-coverage test walks
`git log <anchor>..HEAD` back to the coverage anchor, which a shallow clone cannot resolve.

**It must stay green.** A red CI is a broken contract, not a warning: this is the same command as
the L0 verification tier below, so a change that fails here fails everywhere. Run it locally before
pushing — do not leave the first run to CI.

- **Keep CI in step with the code.** Update `.github/workflows/ci.yml` in the *same change* that
  makes it stale — never as a later cleanup.
- **New or renamed tests need no CI edit** as long as CI runs the discovery command
  (`python3 -m unittest discover -s scripts/tests`); it does, so it picks them up automatically.
  Only touch CI if the *command itself* changes.
- **Environment changes** — a new dependency, a Python version bump, or a new system tool — mean
  updating the workflow's setup/install steps.
- **Renamed or moved code**: the record-coverage test watches a declared list of code paths. If a
  watched path moves, update that list; the test goes red until you do.
- **After a refactor**, confirm CI still exercises the real code and the declared paths still cover
  it. A green CI that no longer touches the changed code is worse than a red one.
- **A new verification tier** (e.g. a live/TUI layer) — decide explicitly whether CI runs it; do not
  add it silently.
- If what CI runs changes, update this section too. CI is a signal, not a gate, until branch protection
  is enabled — read the result after every push.

## Release / version

The project's version lives in `pyproject.toml` (`[project].version`) and nowhere else; it is
currently `0.1.0`. Bump it in that one file when a release is cut — the READMEs and `CHANGELOG.md`
describe behaviour and do not carry a version to keep in sync. There is no PyPI publish step: the
tool runs from a checkout, so the version only needs to move when a documented change ships.

## Hard rules

- **CodeBuddy's own files are never written.** No hook is installed, no `~/.codebuddy/**` file is
  opened for writing, no runtime overhead is added to the host. `cbut` reads and stops there.
- **Metadata only, never content.** Only structured metadata fields are read: counts, timings,
  statuses, identifiers. Prompt/response text, tool argument values and file contents are never
  read and never stored. Nothing leaves the machine — there is no network code path in this project.
- **Tests never touch real state.** Each suite builds its own throwaway database in a temp
  directory; the real `usage.db` and the real `~/.codebuddy` logs are never opened. A test that
  reads a live store is an instrument error, not a finding.
- **Sync must stay incremental *and* idempotent.** Repeated syncs and schema migrations must not
  inflate `context tokens` or duplicate rows; `cbut sync --full` is the documented rebuild path.
- **No provider field, by decision.** CodeBuddy transcripts carry no provider / account / site /
  endpoint, so the Usage page has no provider column at all. Do not "helpfully" add or infer one
  from the model name — guessing is worse than an honest omission.
- **Name is the primary key of every panel.** Each tab lists the union of what is installed
  (the static `inventory`) and what has *ever been used* (the usage tables), keyed by name
  alone, sorted by call count. A renamed, updated or removed entity keeps its history; a name
  that appears under several owners is merged into one row. Under a time window only the counts
  shrink — the list stays full. Add a new panel by name, never by a per-run surrogate id.
- **Windows are whole local calendar days, not "now minus N hours".** `window_bounds` starts at
  local 00:00 (`"1d"` = today, `"2d"` = from yesterday's 00:00, …) and ends at the current time;
  each tab keeps its own window. Do not switch to rolling hours or a shared range — the
  natural-day boundary and per-tab independence are both deliberate.
- **Keep `context tokens` and per-model token totals apart.** They are different measurements
  (turn-metrics `tokenDelta` vs per-response model tokens from transcripts) and summing them is
  wrong.
- **MCP detection follows the real call shape**: `mcp__<server>__<tool>`, including calls wrapped
  by the deferred-tool mechanism (`DeferExecuteTool`). A sparse MCP tab is honest, not broken.
- **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) live in OTel traces that are
  **not yet wired into v1**; do not fabricate their usage from transcript data.
- **Never commit runtime state**: `usage.db`, `.venv/`, `__pycache__/`, generated systemd units in
  user paths. See `.gitignore`.

## Conventions

- **Code and user-facing output are English** (identifiers, comments, CLI/TUI text).
- **Docs are bilingual**: `README.md` (English, landing page) and `README.zh-CN.md` (Chinese).
  Edit both together and keep section order aligned; a section added on one side alone is drift.
- Commit messages follow Conventional Commits (`feat:` / `fix:` / `docs:` / `chore:`), code before
  docs.

## Recording conventions
- Repairs, performance work, drift guards and withdrawals land in
  [`CHANGELOG.md`](CHANGELOG.md) as `D-###` entries; ids are monotonic and never reused, so a gap
  means an entry was deleted and the check fails rather than calling it cleanup.
- **`feat` commits are out of scope, by class.** This is an original project with no upstream, so a
  feature is the product, not a droppable deviation — features are documented in the READMEs. The
  exclusion is one regex over the commit subject inside the check; it must not decay into a
  per-commit skip flag.
- `kind` ∈ `fix` | `perf` | `taste` | `guard` | `revert` | `chore`, cut by **who may demand a
  revert**: bug → `fix`; measurable degradation only → `perf`; only my taste → `taste` (zero
  obligation, discardable on an upgrade); no behaviour change, detects drift → `guard`; withdraws
  earlier work → `revert`; cleanup owed nothing either way → `chore`.
- **An empty covered window is legal only when the header says so.** If the window contains covered
  commits but the record has no entries, the check fails: a pass over an empty set proves nothing.
- An entry is an assertion **as of its commit**, not current state. Never re-verify an old entry;
  never hand-copy an aggregate count here — the check and the test suite print them.
- **Verification tiers** (named by what the claim needs, not by the tool): **L0** =
  `python3 -m unittest discover -s scripts/tests` (temp database, no host), **L1** = a throwaway
  fixture store or a hand-run `cbut sync --full` against copied logs, **L2** = the real
  `~/.codebuddy` logs on this machine.
- `Symptom` names the mechanism, never the session: no prompt text, no command lines, no tool
  argument values, no real paths from `~/.codebuddy`. The record is held to the same
  metadata-not-content rule as the code.
- Run the coverage check before committing docs:
  `python3 -m unittest scripts.tests.test_record_coverage`. It is part of the suite.

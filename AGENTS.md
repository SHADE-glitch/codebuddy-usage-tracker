# AGENTS.md

Guidance for AI coding agents working in this repository.

> **Shared standard.** Root file names, the process-draft location (`docs/reports/`), the
> `CHANGELOG` entry format, CI version pinning and entry commands, the test entry command, and
> the runtime ignore list are defined once in the machine-wide `STANDARD.md` (outside this
> repository) and are not restated here.
>
> **Push over SSH, never HTTPS.** Verify `git remote get-url --push origin` starts with `git@`
> before pushing; if it starts with `https://`, fix it first — never push over HTTPS.

## What this is

A **passive** usage tracker for CodeBuddy Code: it parses the logs CodeBuddy already writes and
builds a local SQLite index, then shows it in an eight-tab Textual TUI (plus a headless `cbut stats`
CLI). Three layers, in dependency order:

1. `scripts/cbut-sync.py` — the parser and indexer. Reads **only**
   `~/.codebuddy/projects/**/*.jsonl`, read-only, and classifies tools/skills/agents/plugins/MCP
   calls into `~/.local/share/codebuddy-usage-tracker/usage.db`. `~/.codebuddy/traces/` is **not
   ingested** (see "Internal agents" below) — do not describe it as an input.
2. `scripts/cbut_db.py` — the shared data layer (schema, queries, calendar-day windows, snapshots).
   Imported by the TUI, the stats CLI and the tests.
3. `scripts/cbut-tui.py` (Textual) and `scripts/cbut-stats.py` (headless CLI).

**There are two writers, not one.** `cbut sync` is the only thing that indexes transcripts, but the
TUI also writes: `on_mount` runs `db.migrate()` (a read-write connection, DDL included) and its
30-second auto-sync worker calls the same indexer the CLI does. Anything that assumes "the TUI is
read-only" is wrong — reason about locks and migrations accordingly. The headless CLI is read-only
except for its explicit maintenance subcommands (`backup`, `restore`, `format --write`).

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

GitHub Actions runs on every `push` and `pull_request` (`.github/workflows/ci.yml`, Python matrix of **3.11 and 3.14** — the floor and the declared upper bound):

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s scripts/tests
```

The checkout must fetch full history (`fetch-depth: 0`): the record-coverage test walks
`git log <anchor>..HEAD` back to the coverage anchor, which a shallow clone cannot resolve.

**It must stay green.** A red CI is a broken contract, not a warning: this is the same command as
the L0 verification tier below, so a change that fails here fails everywhere. Run it locally before
pushing — do not leave the first run to CI.

**Green CI does not certify the interpreter you are using.** CI runs Python 3.11 and 3.14; this repository's
local venv is created at 3.13 (`install.sh`) and the system `python3` may be newer — the version
matrix and what each number means live in
[`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md). If a change depends on
interpreter behaviour, say which version you actually ran, and consider widening the CI matrix
rather than assuming those two speak for all of them.

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
  statuses, identifiers. Prompt/response text and tool argument *values* are never stored. One
  exception is load-bearing and easy to misread: to recover a slash-command name the parser does
  look at a message's text blocks, and only the string inside the `<command-name>` marker is kept.
  This rule is enforced by checks, not by good intentions — `scripts/tests/test_privacy.py`
  (an import allowlist over the production scripts; sentinel values scanned against **every text
  column of every table**; a tree hash proving `~/.codebuddy` is not written) and
  `scripts/tests/test_format_registry.py` (the columns stay what the code reads). Two prose columns
  — `agent_usage.description` and `sessions.title` — were live in v4 and are gone as of v5: the
  promise had drifted behind the code. Do not re-add a column that holds free text.
- **Nothing leaves the machine.** No production script imports a network module, and the check
  scans *this repository's* scripts — say it that way. Measured on the installed set (9 distributions):
  no package imports `socket`/`ssl` at all; every `urllib` use is `urllib.parse` (string work); the two
  network-capable spots are pygments regeneration helpers under `if __name__ == '__main__'` and
  `textual/demo/`, which needs `httpx` — **not installed**. "We cannot make a request" is still not a
  claim we own: it is about versions we have not installed. Re-measure (command in
  `docs/maintenance/compatibility.md`) before restating any of it.
- **Tests never touch real state.** Each suite builds its own throwaway database in a temp
  directory; the real `usage.db` and the real `~/.codebuddy` logs are never opened. A test that
  reads a live store is an instrument error, not a finding. This has already happened once: a
  dispatcher test stopped defaulting `CBUT_SCRIPTS` to its stub directory and ran a real
  `cbut sync --full` against the live database. Any test that shells out to `bin/cbut` must pin
  `CBUT_SCRIPTS`, `CBUT_DB` **and** `CBUT_CODEBUDDY_DIR` into its temp directory — all three,
  because the launcher resolves them independently.
- **Sync must stay incremental *and* idempotent.** Repeated syncs and schema migrations must not
  inflate `context tokens` or duplicate rows; `cbut sync --full` is the documented rebuild path.
  The property is pinned by `test_a_real_process_kill_mid_transaction_leaves_no_partial_state`
  (a child that `os._exit`s with the transaction open — not a simulated close): rows and offset
  are lost together, so a retry counts a turn exactly once.
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
- **A table never shows a half column.** Every `DataTable` in the app — nine: the seven that live in
  the eight tabs (Dashboard draws panels, not a table) plus one on each pushed detail screen — is
  filled through `_fill_wide`, which keeps the longest **prefix** of its
  columns and names the dropped ones on the note line above the table. A cell past the right edge used
  to be cut mid-label with no keyboard way to reach it, so "it scrolls" is not an acceptable answer.
  The mechanism lives in `WideTableMixin`; a host declares `WIDE_TABLES` = table id → (columns, note
  id, cells lost to its surroundings), and `on_mount` builds the columns **from that registry**, so a
  table cannot gain a column the plan does not know about. "Every" is checked against the source, not
  against a registry: `test_every_datatable_in_the_source_is_registered` walks `cbut-tui.py` for
  `DataTable(id=…)` and fails on any id no host registers (a layout test can only loop over what is
  already registered, so an unplanned table would clip in silence). Chrome and the 2-cell per-column
  cost are measured, not derived; `MIN_SHOWN = 2` is the floor, and a table that cannot fit even those
  two says `even these are cut` on its note instead of pretending it fits.
- **Long cells are capped at `name_cap` (default 28), and only when a column would otherwise be dropped.**
  The default is a measurement, not a preference: on this machine's data every entity name fits (tool 22,
  model 24, agent 20, plugin 19, skill 28) and the only column that runs past it is the project path
  (162 of 482 rows over 24, longest 74). `_cap_cell` keeps the **end** of anything containing `/` — a
  path's head is the prefix every row shares — and the **head** of everything else. If a table fits at
  its real widths the cap stays out, so a wide terminal never loses characters; when it engages the
  note prints the number in force, because a silent character cut is the same lie as a silent column
  drop. A row is therefore keyed from the **uncapped** value (`key_rows` in `_fill`): two entities that
  render identically must still route to their own detail screen. Do not cap by truncating the key.
  A host reads the cap through the `name_cap` **property**, never the `NAME_CAP` attribute: `NAME_CAP`
  is only the shipped default, which `TrackerApp.__init__` shadows from the settings file. A pushed
  screen has no config of its own, so it must override the property to read the app — and because
  forgetting that is invisible on every screen the suite knows today,
  `test_every_pushed_screen_resolves_the_cap_from_the_app` enumerates the mixin's `Screen` hosts and
  fails on any that does not define the override itself.
- **Keep `context tokens` and per-model token totals apart.** They are different measurements
  (turn-metrics `tokenDelta` vs per-response model tokens from transcripts) and summing them is
  wrong.
- **MCP detection follows the real call shape**: `mcp__<server>__<tool>`, including calls wrapped
  by the deferred-tool mechanism (`DeferExecuteTool`). A sparse MCP tab is honest, not broken.
- **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) live only in the OTel traces
  under `~/.codebuddy/traces/`, which this tool does not read (see "What this is": traces are not
  an input). Their counts are therefore **0 by construction**, not missing data — do not fabricate
  them from transcripts, and do not describe trace ingestion as part of v1. Wiring traces in is a
  design task: the tree holds both `.jsonl` and `.json` files and needs a session-linking decision
  before it can be counted honestly.
- **Never commit runtime state or working notes**: `usage.db`, `.venv/`, `__pycache__/`, generated
  systemd units in user paths, and the session reports — `STATE.md`, `PROFILE.md`, `AUDIT.md`,
  `PLAN.md`, `VERIFY.md` — which live under `docs/reports/` and are ignored by a non-anchored
  `reports/` rule. Those are working documents, not artifacts: the repository is public, and they
  contain real paths, real counts and open defects. If a check needs to change and it starts
  matching a file that should stay local, fix the ignore list in the same commit. Long-lived
  maintenance material goes in `docs/maintenance/` and *is* committed.

## Conventions

- **Docs are bilingual**: `README.md` (English, landing page) and `README.zh-CN.md` (Chinese).
  Edit both together and keep section order aligned; a section added on one side alone is drift.
- **Settings are read once, in one place.** `db.load_config()` resolves environment > file >
  `DEFAULTS`. Adding a knob means: one entry in `DEFAULTS`, one in `ENV_NAMES`, one in `_BOUNDS`, and
  one assignment in the app — `test_config.py` pins the default against the constant it replaces, so a
  settings layer cannot quietly retune shipped behaviour. The READMEs are part of the surface, not
  documentation someone does later: `test_both_readmes_document_every_setting` requires every
  `"<key> = <default>"` and every override name to appear in both languages, because the loader is the
  only complete copy and three hand-typed lists of this same set have now been found wrong (D-029
  `cbut health`, D-030 the test suite's isolation, D-031 the READMEs). The assignment has to land where the value is
  *used*: a class constant read by a mixin is shadowed per instance, and every host that inherits it
  needs the same resolution (`name_cap` above) — a key the loader returns and no renderer consults is
  a setting the user can edit with no effect. `cbut health` prints whatever `load_config()` returned,
  keyed by the names the file uses, so a knob cannot be added and go unreported
  (`test_health_prints_every_key_the_loader_knows` enumerates `DEFAULTS` against the output).
  A malformed file raises `ConfigError` and the entry point turns it into a message naming the path;
  **never fall back silently** — a user editing a file that is not being read is the worst failure this
  surface can have. Nothing writes the file, and no setting lives in the database.
- **A behaviour change ships with a case that fails without it.** Not a style preference — the rule
  that made this round's worst bug visible: the parser change that emptied the Commands panel passed
  every test, because every command fixture took the *other* branch. If no test goes red when you undo
  the change, the change is not verified yet (and provoke it: remove the fix on purpose and read the
  failure).
- **A settings case must pass in someone else's shell.** Anything asserting the file or the defaults
  is asserting them *under* the environment, which outranks both — so the case reads as a settings-layer
  regression the moment a developer exports one `CBUT_*` value. `test_config.py` wraps those cases in
  `no_settings_env()`, which hides **every name derived from `db.ENV_NAMES`** (a hand-copied list is how
  the next key gets forgotten here too), and the check is that the whole suite is run twice: once clean,
  once with every setting set to a hostile value. Five cases were red that way before the helper existed.
- Commit messages are **English** and follow Conventional Commits (`feat:` / `fix:` / `docs:` /
  `chore:`), code before docs.

## Recording conventions
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
- **Verification tiers** (named by what the claim needs, not by the tool): **L0** =
  `python3 -m unittest discover -s scripts/tests` (temp database, no host), **L1** = a throwaway
  store outside the repository (`--db /tmp/…`) fed by the real logs or by copied logs, **L2** = the
  real `~/.codebuddy` logs and the real `usage.db` on this machine.
  - **L1 is where parser changes are proved.** Claiming a parsing fix works without running it
    against real transcript bytes is how a rewrite filtered on a block type that carries no
    markers emptied the Commands panel while every test stayed green.
  - **L2 needs the owner's go-ahead**, and a destructive L2 step needs a snapshot first.
    `cbut sync --full` and any schema migration take one automatically (last five, under
    `backups/`); `cbut restore` lists and rolls them back. Treat "I ran `--full` on the real
    database" as an action requiring permission, not as verification.
  - An L1/L2 result is reported per table, not for the tables you expected to change: compare
    **every** table's row count before and after a rebuild, or a regression in a table you did
    not touch stays invisible.
- `Symptom` names the mechanism, never the session: no prompt text, no command lines, no tool
  argument values, no real paths from `~/.codebuddy`. The record is held to the same
  metadata-not-content rule as the code.
- Run the coverage check before committing docs:
  `python3 -m unittest scripts.tests.test_record_coverage`. It is part of the suite.

## Invariants and how to check them

Three promises are load-bearing enough that prose is not proof. Each has a command that fails when
the promise breaks — a rule without a check is a preference.

**1. Nothing leaves the machine.** No production script imports a socket, HTTP client, mail,
process-spawning or thread module.

```bash
python3 -m unittest scripts.tests.test_privacy      # AST import allowlist over the 4 production scripts
```

The check has an allowlist of roots per file and names the forbidden ones broadly (raw sockets,
`subprocess`, `multiprocessing`, `threading`), so adding one is a test failure rather than a quiet
convention change. **Scope it honestly:** it inspects this repository's code. Say "our code has no
network path", never "this tool cannot make a request" — the second is a claim about third-party
packages and about versions not yet installed. What is measured here today: no installed package
imports `socket`/`ssl`, every `urllib` use is `urllib.parse`, and the only network-capable source in
the venv is pygments regeneration helpers under `if __name__ == '__main__'` plus `textual/demo/`,
whose `httpx` is not installed. Re-measure before restating; the command is in
[`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md#textual).

**2. CodeBuddy's own files are never written.** `~/.codebuddy` is opened for reading only, no hook
is installed, no configuration is touched.

```bash
python3 -m unittest scripts.tests.test_privacy      # fingerprints a fixture CodeBuddy tree around a real index run
```

The test builds a fixture `~/.codebuddy`-shaped tree, fingerprints every path, runs the actual
indexer over it, and re-hashes: one changed byte fails it. It also asserts nothing new appeared next
to the database (no stray journal or side file). The proof is about the code path, not about the
machine — which is the right target, since the same `index_file()` is what runs against the real
logs. This is the invariant most likely to be broken by accident (a stray `open(..., "w")` on a log
path), so it is proven by content hash rather than by review.

**3. Metadata only, never content.** No free-text value from a transcript may reach a column.

```bash
python3 -m unittest scripts.tests.test_privacy      # sentinels in every argument slot, scanned across every text column
python3 -m unittest scripts.tests.test_format_registry
```

The first test feeds sentinel strings into every place the parser has to look (agent description,
skill args, message body, session titles, tool argument values) and asserts none of them appears in
**any text column of any table** — a column-scoped check, because a leak moved once the moment it
was guarded in a named column. It also carries a positive control (names that *must* be stored):
without it the scan would pass on a database that indexed nothing. The second keeps a new free-text
column from being added silently: a usage table gaining a prose column is a privacy change, so
`test_no_prose_column_exists_in_a_usage_table` fails until the column is declared as metadata.

Maintenance material — the format surface itself, the version matrix, the size and latency
baseline — lives in [`docs/maintenance/`](docs/maintenance/), and `test_maintenance_docs.py` keeps
the generated part of it tied to the code.

## Format coupling

CodeBuddy owns the data; this tool only reads it. **The main maintenance risk is upstream
changing the format**, and the failure mode is silent: a renamed field reads as `NULL`, a renamed
record type is simply unclaimed, the panels empty out, `cbut sync` prints "done", CI stays green.

Two defenses, and both are mechanical:

- **Unclaimed records are counted.** `scripts/cbut-sync.py` ends its dispatch chain with an `else`
  that records *why* a record was not claimed (`type:<name>`, `unparseable_line`,
  `unreadable_file:<OSError>`) into the `unparsed` table. `cbut health`, the sync summary and the
  TUI status line all surface it. A number that cries wolf is not a signal, so types the code
  understands but does not need (a `model-usage` echo whose tokens are already stored) get an
  explicit claim branch instead of padding the count.
- **The format surface is registered in one place.** The block at the top of
  `scripts/cbut-sync.py` (`# --- CodeBuddy format registry`) declares every field name, path,
  glob, separator, tool name and regex the parser depends on. `cbut format` prints it, and
  `docs/maintenance/codebuddy-format.md` carries that output.

Two enforcement styles, deliberately: layout values, separators and tool names are **source** (the
code reads the constant, so a rename is one edit); record **field names** stay inline in the
handlers — a parser is easier to trust when the field it grabs is visible — and are **mirrored** by
the `*_FIELDS` sets. `scripts/tests/test_format_registry.py` locks the mirror both ways: a field
read and not registered fails, a registered field no longer read fails, and no bare literal may be
passed to a layout method. That is what makes a half-applied rename impossible to hide: change
`providerData` in the registry and the test reports both halves of the mismatch, with line numbers.

When upstream changes: follow the checklist in
[`docs/maintenance/codebuddy-format.md`](docs/maintenance/codebuddy-format.md#when-a-panel-goes-empty),
then change the registry and the handler **in the same commit**, and re-index at L1 before touching
a real database. `AGENTS.md` does not list the fields — the registry does, and the test keeps it
true.

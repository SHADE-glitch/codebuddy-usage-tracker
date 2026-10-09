# CHANGELOG — codebuddy-usage-tracker

Original project, **no upstream**: nothing here is a deviation from somebody else's code, so this
record is not a divergence list. It records the repairs, the performance work, the drift guards and
the withdrawals — the things an upgrade of CodeBuddy's log format, of Textual, or of the transcript
schema can invalidate.

Coverage: f62eb1f..HEAD
Check with `python3 -m unittest scripts.tests.test_record_coverage`. Entries are `D-###`, monotonic,
never reused. An entry states what was true **as of its commit**, not current state, and aggregate
counts are printed by the check, never copied into this file.

> **Why the record is short.** Most production commits carry a `feat` subject, which is out of scope
> by class: a feature is the product, not a droppable deviation, and features are documented in the
> READMEs. Only the non-feature commits that touch a production path appear below — D-001 is a
> `docs:` commit that also corrected stale CLI help, D-002 a `fix:` — coverage follows the touched
> paths and the `feat` exclusion, not the author's intent.
>
> **The one exception, and why it is honest.** D-006…D-020 all cite `673255c`, whose subject is `feat:`
> and which the class rule above would therefore skip. It was landed as a single bundled commit, and
> inside it are repairs and guards, not product: a promise the code had already broken, an upstream
> change that emptied panels silently, destructive steps with no rollback, and a regression that one
> commit introduced and another recorded. The exclusion is by commit **class**, and a bundled commit is
> exactly the case where the class stops describing the content — so the entries are recorded against
> the one hash, and each names the specific test or measurement that proves its own half. Future rounds
> should commit by concern instead of making this note necessary.

`kind` ∈ `fix` | `perf` | `taste` | `guard` | `revert` | `chore` — see AGENTS.md § Recording
conventions for the cut.

---

### D-001 · 2026-10-07 · fix
Symptom  `cbut --help` still advertised "five tabs" after the TUI grew to seven, so the entry point
         described a version of the app that no longer exists
Change   Correct the dispatcher's help text while adding the four missing README sections
Evidence L0 2026-10-08 re-run: `python3 -m unittest discover -s scripts/tests` (suite green, and
         `bash -n bin/cbut` parses); L1/L2 not applicable to help text
Cost     `bin/cbut` stays bash-on-purpose (it must print a useful error when the venv is broken), so
         this text is not generated from the TUI and will go stale again unless checked by hand
Commit   cfcccf4

### D-002 · 2026-10-08 · fix
Symptom  An `Agent` call carrying only `description`/`prompt` (no `subagent_type`) was recorded
         under the literal name `?`, splitting real general-purpose usage into an unknown row
         instead of counting it as the tool's documented default
Change   Record `general-purpose` when the caller omits `subagent_type`, and add a regression test;
         `cbut sync --full` reclassifies the rows already indexed under `?`
Evidence L2 2026-10-08: after `cbut sync --full` against the real logs, `q_agents` reports no `?`
         row and the general-purpose count matches the previously-split total; L0 `test_sync.py`
         green
Cost     None — the default is the Agent tool's own contract, not a guess; a CodeBuddy build that
         changes that default would need this line revisited
Commit   19c0b60

### D-003 · 2026-10-08 · fix
Symptom  CI failed on the Dashboard commit: `test_tab_cycle_wraps` raised `NoMatches: No nodes match
         '#dash-note'` — the 5s auto-refresh timer fired once more during `run_test` teardown, after
         the widgets were unmounted, so `query_one` blew up from inside the timer
Change   `_auto_refresh_tick` returns early when `self.is_running` is false, and a regression test
         pins that a tick outside a running app is a no-op (it fails, with the same `NoMatches`,
         if the guard is removed)
Evidence L0 2026-10-08: forcing the timer to fire every 50ms while cycling tabs failed 2–3 times
         in 6 runs before the guard and 0 in 6 after; the new test fails without the guard and
         passes with it; full suite 172 tests green
Cost     None — the guard only skips work there is nothing to render anyway; it does not change
         timer cadence while the app is live
Commit   4ff3243

### D-004 · 2026-10-08 · guard
Symptom  The TUI read "now" straight from the wall clock, so the two Dashboard window tests only
         passed when the suite ran outside the first hour after local midnight — a `now - 1h`
         fixture otherwise lands on yesterday and drops out of a Today window. A time-of-day flake
         that CI (UTC) can hit for any runner's local midnight
Change   `TrackerApp` takes an optional `clock` (default `time.time`); `_bounds_for`, `_refresh_usage`
         and `_on_sync_done` now read "now" through `_now_ms()`. The Dashboard tests pin the clock
         to local noon, and a new test pins the seam itself
Evidence L0 2026-10-08: with the clock pinned to noon the two tests no longer depend on the run
         hour; `test_injected_clock_drives_the_window` asserts the window end equals the pinned
         instant and its start equals `window_bounds("1d", pinned)` (so it fails if `time.time` is
         read directly again); full suite 173 tests green
Cost     One level of indirection for "now"; the default leaves a real run identical (no behaviour
         change), and any future call site must go through `_now_ms()` to stay pinnable
Commit   fe2d2c6

### D-005 · 2026-10-08 · fix
Symptom  The Dashboard's Top tools list read "1 calls" for a single-call tool
Change   Pick the singular "call" when the count is 1 (the noun is padded so the trailing "ms"
         column stays aligned), and have `test_dashboard_extras_render` assert both forms — "2 calls"
         present, "1 calls" absent
Evidence L0 2026-10-08: the test now asserts "2 calls" is present and "1 calls" is not; full suite
         177 tests green
Cost     None — a display-only noun; the numeric column and ordering are unchanged
Commit   d1de6a1

### D-006 · 2026-10-09 · fix
Symptom  The dispatch chain had no `else`: a record type CodeBuddy stops sending, or starts sending,
         was dropped without a trace. Panels emptied, `cbut sync` printed done, CI stayed green — the
         main maintenance risk of this tool had zero observability
Change   Count every unclaimed record with its reason (`type:<name>`, `unparseable_line`,
         `unreadable_file:<OSError>`) into an `unparsed` table, surfaced in `cbut health`, the sync
         summary and the TUI status line. Types the code understands but does not need get an explicit
         claim branch instead, because a counter that cries wolf is not a signal
Evidence L0 `test_sync.py` asserts the counter and the claim branch; read against the real logs the
         counter reported three reasons totalling ≈ 20k records. The first run also produced a false
         alarm (`type:model-usage`), which was real instrumentation feedback: that type is handled
         inside the model-response path, so it now has a claim branch rather than a permanent count
Cost     One table and one counter; a new upstream type costs one line in `HANDLED_RECORD_TYPES`
Commit   673255c

### D-007 · 2026-10-09 · fix
Symptom  The published promise "metadata only, never content" was already false: `agent_usage` carried
         a model-written `description` and `sessions` a model-written `title`, and tool arguments were
         serialised whole into a column
Change   Schema v5 drops both columns and stops storing argument values; the agent name, skill name and
         command name are parsed as identifiers instead of recovered from a blob. Migration keeps rows
         and re-indexes names, and the database snapshots first (D-009)
Evidence L0 v4→v5 migration tests assert the columns are gone, the rows survive, and a fresh index
         reports the same counts; a sentinel scan (D-008) fails if any free text reaches a column
Cost     Anyone reading those two columns loses them — they were the violation, not a feature;
         a rollback exists as a snapshot, not as a compatibility shim
Commit   673255c

### D-008 · 2026-10-09 · guard
Symptom  Two of the three privacy promises were prose. A leak would have been found by review, and
         review had already missed one (D-007)
Change   `test_privacy.py`: an AST import allowlist per production script; a sentinel fed into every
         slot the parser reads, asserted absent from **every text column of every table** with a
         positive control that proves something was indexed; and a per-file hash of a fixture
         CodeBuddy tree before and after a real index run
Evidence L0 — the suite; each half was provoked red by re-introducing the fix it guards (a prose
         column, a stray write into the input tree)
Cost     Scope is stated in the test and in AGENTS.md: it proves **this repository's** code has no
         network or write path. It does not prove the tool cannot make a request — that claim belongs
         to third-party packages and to versions not yet installed
Commit   673255c

### D-009 · 2026-10-09 · fix
Symptom  `--full` and a migration destroy indexed state, and the only recovery was a copy the owner had
         made by hand. That contradicted the stated rule "back up before a migration, keep it
         reversible"
Change   `db.snapshot/list_backups/restore` plus `cbut backup` and `cbut restore`; a snapshot is taken
         automatically before a migration and before `--full`, and the last five are kept
Evidence L0 `test_snapshots.py`; the path was exercised for real when an accidental `--full` hit the
         live database during this round and was rolled back with these files
Cost     Five copies of the index on disk; restoring needs the TUI closed (a locked file is reported,
         not retried silently)
Commit   673255c

### D-010 · 2026-10-09 · fix
Symptom  `schema_version` was written and never read, so older code opening a newer database silently
         no-op'd its `CREATE TABLE IF NOT EXISTS` set and then queried columns that did not exist
Change   `ensure_schema()` reads the stored version and picks its path; `migrate()` returns
         `current` / `migrated` / `newer` / `failed: <reason>`, and the TUI shows the reason instead of
         rendering an empty page
Evidence L0 three branches covered in `test_usage.py`; the note is rendered by `test_tui.py`
Cost     A newer-version database now refuses to open rather than half-working — intentional, and the
         message says what to do
Commit   673255c

### D-011 · 2026-10-09 · fix
Symptom  The Skills panel listed directory **categories** ("backend", "frontend") as skills and hid the
         real ones; the Agents panel showed only built-ins because user agent specs were never scanned;
         plugin-owned entities had no attribution
Change   A skill is the parent of its `SKILL.md`, `agents/*.md` are indexed, and skill/agent/command
         names are mapped back to the plugin that installed them (keys split on `@`)
Evidence L0 fixtures shaped like the real layout; L1 against the real logs the Skills panel lists only
         manifests' parents and the user agents appear
Cost     Depends on CodeBuddy keeping a manifest inside each skill directory — registered in the
         format registry, so a layout change is a test failure rather than a quiet empty panel
Commit   673255c

### D-012 · 2026-10-09 · fix
Symptom  **A regression this round introduced.** Command names were read only from content blocks of
         type `text`; measured transcript blocks carry the marker in `input_text`/`output_text`, and no
         `text` block carries one — the Commands panel emptied to zero rows while every test stayed
         green, because all command fixtures used the bare-string branch
Change   Read block `text` fields without filtering on block type, and add tests for the real shapes
         plus a control that a marker outside a text field is not invented; the measured distribution
         is recorded in the registry so the assumption is not re-derived silently
Evidence L0 three tests, each provoked red by putting the type filter back; L1 a full re-index of the
         real logs restores the command rows
Cost     Old databases cannot recover those rows incrementally — the bytes were consumed already, so
         recovery is `cbut sync --full`. This entry records the violation of the project's own rule
         ("do not assert CodeBuddy's format from memory") as well as the fix
Commit   673255c

### D-013 · 2026-10-09 · guard
Symptom  About forty literals describe CodeBuddy's format and were scattered across the parser. A
         half-applied rename was invisible, and there was no single place to re-check when upstream
         moved
Change   A registry block at the top of `cbut-sync.py` declares layout, separators, tool names, regexes
         and field mirrors; `cbut format` prints it and `docs/maintenance/codebuddy-format.md` carries
         that output. Layout values are **source** (code reads the constant); field names stay inline in
         handlers and are **mirrored**, deliberately
Evidence L0 `test_format_registry.py` — no field read without being registered, no registered field
         left unread, no bare literal passed to a layout method, every raw-usage field landing on a
         column; four destructive edits were provoked and all four failed
Cost     Two places per field name (registry and handler). That is the point: the test makes a
         half-applied rename impossible to hide
Commit   673255c

### D-014 · 2026-10-09 · fix
Symptom  The indexer closed its connection only on the success path, and `sync_state.size` stored an
         offset, so a truncated or rewritten log was indistinguishable from one already consumed
Change   `try/finally` around the connection; `size` stores the real `st_size`; one unreadable file is
         counted and skipped instead of aborting the run
Evidence L0 covers the offset/size split and a single-file failure; L1 an incremental run over the real
         logs reports no re-read
Cost     A file whose size shrinks below the stored offset is re-read from zero — that is the detection,
         not a bug
Commit   673255c

### D-015 · 2026-10-09 · guard
Symptom  A static read claimed a crash mid-transaction would double-count on the next run. It was an
         inference about WAL rollback that had never been executed
Change   Replace the inference with a behaviour test: a child process that `os._exit`s inside the
         transaction (no commit, no close, no checkpoint), then a recovery run asserting rows and
         offsets moved together
Evidence L0/L1 the test itself; **the original claim is disproved** — nothing partial survived. Verified
         by moving a commit into the per-record handler and watching the test fail
Cost     A test that spawns a process is slower and platform-tolerant only in the sense that it skips
         nothing: it must keep failing if atomicity goes
Commit   673255c

### D-016 · 2026-10-09 · fix
Symptom  Session rows were created with a NULL project for records whose working directory was unknown,
         so the Sessions count was inflated by sub-agent and tail records — visible as rows no project
         could own
Change   A session row requires `sessionId` **and** a timestamp **and** a project; the tail-of-file
         insert that lacked them is gone
Evidence L1 a full re-index of the real logs drops the NULL-project rows to zero while the real session
         count stays; L0 asserts the refusal
Cost     A session that genuinely has no project is not listed. It was indistinguishable from noise
Commit   673255c

### D-017 · 2026-10-09 · fix
Symptom  `bin/cbut` had three ways to fail without saying why: a scripts directory that did not exist
         fell through to a confusing Python error, `CBUT_VENV` accepted only one shape, and `install.sh`
         on a venv without pip failed at the install step. `AGENTS.md` also claimed the sync module was
         the only writer while the TUI migrates and syncs every 30 s
Change   Dispatcher errors out with the path it looked for, accepts a directory or a binary, and
         generates help from its own comments; `install.sh` bootstraps pip with `ensurepip`; the
         `--auto_sync_tick` guard matches the one `D-003` added to its sibling timer; the runtime
         facts were rewritten into AGENTS.md
Evidence L0 `test_dispatcher.py` (each path pinned to a temp directory after one test run escaped to
         the real environment); `bash -n` on both scripts; L0 the tick guard
Cost     Help text is generated from comments, so it still needs the README to stay in step by hand
Commit   673255c

### D-018 · 2026-10-09 · fix
Symptom  A failed query left the previous page on screen and looked like fresh data; the status line was
         written from several places, so "Syncing…" stuck after a zero-file sync and `s` overwrote the
         counts
Change   One `_status_line()` builder (data + markers + warnings); query errors are reported in the
         status bar and in the affected panel, and recovery is decided by "nothing complained this
         round" rather than by matching text
Evidence L0 `test_tui.py`, including an open-ended assertion that scans the rendered status line for
         any singular-with-1 construction rather than listing the five places that had the bug;
         removing either report site fails a test
Cost     Error text is composed in one place, so a new panel must go through the same helper to be
         heard — which is the behaviour the guard enforces
Commit   673255c

### D-019 · 2026-10-09 · taste
Symptom  The 8 tabs disagreed with each other: Usage built its empty state by hand while the others used
         the shared helper, an empty sparkline window drew nothing, durations were formatted with a
         token formatter, and labels read "Incomplete: complete"
Change   Summary panels reflow to 2×2 below `COMPACT_WIDTH` and to a single column below
         `STACKED_WIDTH`; all 8 tabs share one empty-state path; the sparkline draws a baseline when the
         window is empty; counts, durations and labels swept by class, not one at a time
Evidence L0 `test_tui.py` layout and rendering assertions at three widths, with the grid class and the
         stacked class each provoked separately; eight-tab smoke at 60×24
Cost     Widths are measured on a Textual harness — a widget that was never laid out reports width 0 and
         manufactures a fake overflow, so any new width test must activate its tab first. Whether 80
         columns should hide the cache columns is **not** decided here: that is a product call and
         remains open
Commit   673255c

### D-020 · 2026-10-09 · guard
Symptom  The maintenance surface lived only in the maintainer's head: no format dependency list, no
         version matrix, no way to re-measure anything the same way twice. README printed a hand-typed
         test count that had drifted, claimed a narrow-terminal result that had never been measured, and
         stated a dependency fact about Textual's networking from hearsay
Change   `docs/maintenance/` — the generated format list, a version matrix that says which interpreter
         has actually been run rather than which is promised, and a measurement page whose figures are
         rounded because exact volumes of a personal index are personal data. `AGENTS.md` gained
         "Invariants and how to check them" and "Format coupling", and its stale rules were deleted or
         rewritten rather than appended to. README's counts, both languages, are pinned by
         `test_readme_counts.py`
Evidence L0 `test_maintenance_docs.py` fails if the generated block drifts from the registry;
         `test_readme_counts.py` compares every README row against the loader; every command published
         in these files was run, including the dependency scan whose output corrected the earlier
         "Textual imports socket/ssl" sentence — measured: no installed package imports `socket`/`ssl`
Cost     More documentation, each tied to a command. A page that is not generated or checked is a page
         this round refuses to add
Commit   673255c


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
> **When a bundled `feat:` commit still needs entries.** D-006…D-020 all cite `673255c`, whose subject is `feat:`
> and which the class rule above would therefore skip. It was landed as a single bundled commit, and
> inside it are repairs and guards, not product: a promise the code had already broken, an upstream
> change that emptied panels silently, destructive steps with no rollback, and a regression that one
> commit introduced and another recorded. The exclusion is by commit **class**, and a bundled commit is
> exactly the case where the class stops describing the content — so the entries are recorded against
> the one hash, and each names the specific test or measurement that proves its own half.
>
> The condition, stated so it can be checked rather than counted: **a `feat` commit gets entries when it
> carries something a reader would have to revert on its own** — a repair to a path that had already
> shipped, a guard for a promise the code had broken, a claim being withdrawn. `20ffd24` (D-022, D-023),
> `86d4220` (D-024) and `7ae28ae` (D-025) meet it; the other `feat` commits in `git log` do not, and are
> not recorded. Earlier versions of this heading said "the one exception", then "the exceptions", then
> carried a count — each rotted the moment the next commit landed, which is why it is now a rule with
> named examples instead of a number.

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
         `test_readme_counts.py`. The same rounding applies inside the code: the unclaimed-types comment
         in the registry printed per-machine counts, and now states the order of magnitude and points at
         `cbut health` for whoever owns the real figures
Evidence L0 `test_maintenance_docs.py` fails if the generated block drifts from the registry;
         `test_readme_counts.py` compares every README row against the loader; every command published
         in these files was run, including the dependency scan whose output corrected the earlier
         "Textual imports socket/ssl" sentence — measured: no installed package imports `socket`/`ssl`
Cost     More documentation, each tied to a command. A page that is not generated or checked is a page
         this round refuses to add
Commit   673255c 4d0f8ab

### D-021 · 2026-10-09 · perf
Symptom  Two indexes — `idx_tool_session` and `idx_model_resp_session` — existed to answer
         `WHERE session_id = ?`, and no query in the data layer ever did. They were maintained on
         every insert and read by nobody: together about a seventh of the whole database file
Change   Schema v6. `SCHEMA_SQL` no longer creates them, and a migration drops exactly those two and
         leaves every live index alone. Ruled out rather than merely unbuilt: panels are keyed on the
         entity **name** and a window shrinks counts but never the list, so a session-keyed read
         contradicts the panel model instead of extending it
Evidence L0 three cases: a new database carries neither index; a hand-rebuilt v5 database loses those
         two and keeps `idx_tool_name` / `idx_model_resp_model_ts`; and the justification itself is a
         test that reads the real source (`0 of 81` queries filter by `session_id`) with a floor on how
         many queries the extractor must find, so a broken extractor cannot pass as a clean result.
         L2 on the real database: index count 12 → 10, `integrity_check ok`, freelist ≈ 20 → ≈ 570
         pages
Cost     The file did not get smaller — dropping an index returns pages to the freelist, and only
         `VACUUM` hands them back, which is not worth running for 2.2 MiB. If a session-detail screen
         is ever built, the guard above fails first and the index is one `CREATE INDEX` away
Commit   d75ad24

### D-022 · 2026-10-09 · fix
Symptom  `cbut health` printed the **default** database path whatever `--db` was handed. Pointed at a
         throwaway file it answered under `db path` with
         `~/.local/share/codebuddy-usage-tracker/usage.db` — while every line below it (transcript
         files, indexed rows, last activity, unparsed) came from the connection `--db` had opened. The
         one line that says *which database you are reading* was the one that did not know
Change   print `args.db`. It landed inside a `feat:` commit rather than as its own `fix:` because it
         was found while writing the settings line that now sits next to it in the same function; the
         class exclusion would have hidden a repair to something that had already shipped, so it is
         recorded against that hash
Evidence L0 `test_config.py::HealthSurfaceTest.test_health_reports_the_database_it_was_handed` runs the
         real CLI in a child process and asserts both halves — the given path appears **and** the
         default path does not. Red was proved after the fact, not assumed: the old line was
         re-introduced, the case failed with `'/tmp/tmpfo6p7qb_/u.db' not found in '… db path
         ~/.local/share/codebuddy-usage-tracker/usage.db'` (the leading home directory is elided here,
         as everywhere else in this record — the failure printed it in full), and `git diff --exit-code`
         then confirmed the file back at its committed bytes
Cost     Nothing structural. The point of recording it is that `health` is the command used to find
         out which database is being read, so a wrong path there casts doubt on every number printed
         above it
Commit   20ffd24

### D-023 · 2026-10-09 · guard
Symptom  The settings layer was dead on arrival in the app it was written for: `main()` built
         `TrackerApp` without ever calling `load_config()`. A written `config.toml` changed nothing, a
         syntactically broken one stopped nothing — the TUI started on defaults and printed the escape
         codes of a live interface. The suite was green throughout, because the test that called
         itself "wiring" handed the constructor a **dict** and so never walked the path a user takes
Change   `main()` passes `config=db.load_config()`. The case covering it drives `main()` with
         `TrackerApp.run` replaced — not as a mocking preference but because the unhypothetical
         alternative in a test is a real event loop that blocks forever, and a regression that ignored
         the file would then show up as a hang rather than as a failure
Evidence L0 `CliWiringTest`, run red first and observed red (`(5, 100, 200) != (3, 25, 50)`, and
         `0 != 2` where exit 2 was required) before the one-line change made it green: a file's
         `top_n` / `log_limit` / `detail_limit` reach the app through `main()`; no file at all keeps
         the shipped defaults; a broken file returns 2 naming the path and never reaches `run()`.
         Also proved outside the harness — `CBUT_CONFIG=<broken file>` against a real database exits 2
         with `your settings are not usable: /tmp/bad-cbut.toml: not valid TOML (Invalid value (at
         line 1, column 9))` in 166 bytes of stderr, no terminal escape codes, i.e. nothing started
Cost     AGENTS.md already requires a failing case for every behaviour change; what this adds is where
         the case has to sit — on the path the user takes, not on the constructor. A dict-injection
         test stays useful and simply cannot be the only one
Commit   20ffd24

### D-024 · 2026-10-10 · taste
Symptom  The two wide tables were cut rather than planned. Measured at 80×24: Tokens rendered **6 of 9**
         columns, Usage **5 of 10**, the rest clipped past the right edge mid-label — and unreachable,
         because `right`, `ctrl+right`, `shift+right` and `end` each left `scroll_x` at 0.0 (only a
         programmatic `scroll_right()` moved it). Both READMEs asserted the opposite: the cache columns
         are "reachable by horizontal scroll". That claim is withdrawn, not softened
Change   A column plan: fill the longest **prefix** of the table's columns that fits, and name the
         dropped ones on a line above the table, with the width that would show everything. Prefix and
         not re-ordering because both tables already put the essential columns leftmost, so the tail is
         exactly what a narrow terminal should not be paying for. Widths are measured from the rows (a
         column costs its content width + a 2-cell gutter; the table loses 2 cells — Tokens — or 4 —
         Usage — to its surroundings), never from a constant baked in for the current data. A resize
         re-plans from cached rows instead of re-querying
Evidence L0 four cases, red before green: at 80×24 each table shows fewer columns than it has **and**
         what it shows fits its own region **and** the note names every hidden column with matching
         numbers; at 160×30 nothing is dropped and the note is empty (the control that keeps the note
         from becoming decoration); widening hands the columns back with the row count intact; and a
         rebuilt row still routes to its model's detail screen, because the row key is the thing a
         cut-and-rebuilt table could silently lose. The note's own honesty was proved the same way as
         D-022's: with `height: 1` the case fails on `'Cache write' not found in '… Cache hit, Cache
         miss, Cache'`, so `height: auto` is a measured answer and not a preference
Cost     The note costs a row when it wraps (at 80×24 the Usage table keeps 9 rows instead of 10, with
         a test floor of 6), and a width-band change resets the table cursor, because restoring a
         highlight across `clear(columns=True)` would need the key re-derived. Four value tests that
         read cells by position now ask for a 160-column terminal — on a narrow one those columns are
         legitimately absent, and asserting against a column that was never drawn proves nothing
Commit   86d4220


### D-025 · 2026-10-10 · taste
Symptom  The rule written one commit earlier — "a table never shows a half column" — was true for two
         of the nine tables in the app. Measured with long names at 80×24: `t-tools` needs 90 cells
         for a 78-wide region, `t-mcp` needs 137, and the widest table anywhere in the tool is the
         pushed **Model responses** screen — 12 columns needing 135 cells, which had never been said a
         word about. Both detail screens also predated the empty-state convention: no rows meant a
         blank table, the one thing the seven tab tables had been fixed not to do
Change   The mechanism moved out of `TrackerApp` into `WideTableMixin`; each host declares its own
         `WIDE_TABLES` and `on_mount` now builds columns from that registry instead of a second inline
         list, so a table cannot gain a column the plan does not know how to name. Chrome measured per
         host: 2 cells inside a tab pane, 4 on the padded Usage page, 0 on a pushed screen that spans
         the terminal. Both screens fill through `_fill_wide`, which gives them the empty row as well
Evidence L0 five cases (303 total, all four interpreters re-run): an every-tab loop at 60 and 80 that
         asserts per table "shown is a prefix of the registry, it fits, and every dropped name appears
         on the note", with a non-vacuous floor (`t-mcp` and `t-usage` must show up in the dropped set or
         the loop proved nothing); the two screens at 80 and at 220 wide; registry-equality; and the MCP
         row key surviving a drop to exactly two columns. One case had to be **widened** to keep its
         meaning: the message-id masking test ran at the default 80, where that column is not shown at
         all — `assertNotIn(raw_id, cells)` was passing because nothing was rendered, not because
         anything was masked
Cost     The floor is where this stops being a fix: two name columns that alone exceed the screen (MCP
         server + tool = 143 cells at 78) are still cut, and what the change guarantees is that the
         note ends with `even these are cut` instead of implying the table fits. A per-column cap on
         name columns is the next candidate and is deliberately not in this commit. The first version of
         the every-tab assertion said "what is shown always fits"; it went red on that MCP case, and the
         product was right — the assertion was the bug
Commit   7ae28ae


### D-026 · 2026-10-10 · fix
Symptom  Two numbers in the shipped prose were written from memory instead of measured. The
         `WideTableMixin` docstring, D-025 and AGENTS.md all said the plan covers "the eleven
         tables"; a walk of `cbut-tui.py` finds **nine** `DataTable(id=…)` — seven inside the eight
         tabs (Dashboard draws panels, not a table) and one on each pushed detail screen. Both
         READMEs also promised "the Tokens page keeps **6 of 9**" at 80×24. Against a copy of this
         machine's own database the plan keeps **5 of 9**, and the 6 was never a plan reading at
         all: it is D-024's count of columns visible *before* the plan existed, a different
         quantity reused as the plan's output. The English README said the nine share "the same
         registry" when each host declares its own `WIDE_TABLES`
Change   The census is corrected to nine everywhere it appeared, with the counting method named
         beside it. The READMEs (both languages) keep the one reading that re-measured true (Usage
         5 of 10) and label it "readings from this machine's own database at 80×24, yours will
         differ" rather than presenting it as a property of the page; the borrowed Tokens count is
         deleted instead of replaced, because a number that rots in a README is worse than no
         number. "Same registry" became "the same plan"
Evidence `test_every_datatable_in_the_source_is_registered` parses the source, collects every
         `DataTable(id=…)` literal, and fails in both directions — a table no host registers, or a
         registry entry naming a table the source never builds. It is the check the prose never had:
         every layout test loops over a registry, so an unplanned table would clip in silence while
         the claim stayed true of the tables that were known. Provoked by deleting the `t-plugins`
         line: `AssertionError: {'t-plugins'} is not false : unplanned tables, free to clip:
         ['t-plugins']`, and the file restored so `git diff` showed only the docstring line. The
         readings above are the app under `run_test` at 80×24 and 60×24 against a **copy** of the
         real database in `/tmp`, Usage switched to all-time; the real file was never opened by them
         and the copy is deleted
Cost     Suite 303 → 304. The guard pins coverage, not the count: no test asserts "nine", because a
         tenth table should fail a test for being unplanned, not for being one past a number in some
         prose. What changed is where a future count has to come from — the source walk, not memory
Commit   57190c4


### D-027 · 2026-10-10 · taste
Symptom  The column plan had a floor it could not raise: when two name columns alone were wider than
         the terminal, the honest note still described a table that was cut (`even these are cut`),
         and on real data one column caused it. Measured on this machine's database, read-only from a
         `/tmp` copy since deleted — tool median 9 / max 22, model max 24, agent max 20, plugin max 19,
         skill max 28, **project path median 16 but max 74, with 162 of 482 rows over 24**. So the
         cap is not for names at all: it is for the one column whose beginning is the same in every
         row and whose end is the only part that distinguishes them
Change   `_cap_cell` fits a cell to `NAME_CAP = 28`, keeping the **end** of anything containing `/`
         and the **head** of everything else; the cap is chosen as a measurement, with the per-column
         numbers written next to it in the source. It engages only when the table would otherwise lose
         a column — the plan tries the real widths first, so a wide terminal never loses characters —
         and when it engages the note says `cells capped at 28` rather than going quiet. A row keeps
         being keyed from the **uncapped** value (`key_rows`), because two entities sharing the visible
         prefix must still route apart
Evidence L0 four cases, three of them red before the code existed: a 74-cell path no longer pushes
         columns off an 80-wide History screen and each cell ends `…` with the distinguishing tail
         intact; a name without a slash keeps its head; the note says `cells capped at 28` when it cut
         characters but hid no column; and two tools whose names share 28 visible characters still open
         their own detail screen. The last case was proved non-vacuous without editing the product:
         an in-memory variant that keyed rows from the capped cells was run against the real app and
         Textual refused the table outright — `DuplicateKey: The row key … already exists.` Two further
         reds were the product's fault and stayed red until fixed: the cap first fired on wide
         terminals (`t-skills hides nothing and still warns`), and it compared rendered cells against
         raw ints, reporting a phantom cap on every numeric column
Cost     Suite 308 on the project venv (the four-interpreter matrix is re-run at the final HEAD, so
         no number here is promised against an interpreter that has not run it). Real-data effect at 80×24:
         History 2/5 → 5/5 columns,
         nothing else changes (Tokens 5/9, Usage 5/10, Model responses 6/12); at 60×24 History goes
         2/5 → 3/5 and that page no longer needs the floor clause either. What the cap does **not** fix
         is a table whose label row alone exceeds the terminal, or a reader who needs the whole path:
         the full value is on disk and in the row key, not on the screen — widening past the plan's
         `widen to N` figure brings back columns, and only an uncapped cell brings back characters
Commit   8aa6987


### D-028 · 2026-10-10 · guard
Symptom  A privacy check described itself as covering more than it did. The needle test in
         `test_privacy.py` carried the comment "and any dynamic import built from a string", but the
         body scans seven fixed literals — so `__import__("socket")`, `importlib.import_module(name)`
         and `exec(source)` would have passed an allowlist whose entire purpose is to be passed. The
         claim was not in the product, so nothing broke when it went stale; that is exactly why no
         check caught it
Change   `dynamic_module_calls()` walks the AST for calls that reach a module by name: bare
         `__import__`/`eval`/`exec`/`compile`, and `import_module`/`load_module` under any receiver.
         Two edges are deliberate rather than thorough. Attribute calls match on the final name only,
         so `re.compile(...)` is not mistaken for code generation — a check that fires on a regex
         teaches everyone to ignore it. And `cbut_db.load_sync()`'s
         `spec_from_file_location` of `cbut-sync.py` is named inside the docstring as the reviewed
         dynamic load this guard does not flag: it resolves a path built from `__file__`, not a name
Evidence L0 two cases. `test_no_script_reaches_a_module_by_name` runs it over all four production
         scripts (clean — no finding, this is not a bug report).
         `test_the_dynamic_import_detector_actually_detects` feeds the detector the four shapes it
         claims to catch and asserts each is reported, then asserts `re.compile("a+")` is **not**; the
         guard therefore has to show it fires without anybody editing production code to make it fire.
         Suite 310; re-run with `python3 -m unittest scripts.tests.test_privacy`
Cost     The comment is now shorter than it was, because the sentence it carried was a promise this
         file cannot keep: a call built through `getattr(builtins, "ev" + "al")` still walks past. It
         closes the realistic accident — an import by string — not a determined obfuscation, and the
         entry says so instead of the comment pretending otherwise
Commit   c480625

### D-029 · 2026-10-10 · fix
Symptom  Two gaps of one shape, both opened up by making D-027's cap configurable.
         (1) `_cap_cell` was a `@classmethod` reading `NAME_CAP` off the **class**, so a
         per-instance value could only ever reach the host that shadows the attribute. The tabs
         would honour `name_cap` and the two pushed detail screens would keep rendering 28 — and
         the pushed screens are where the long values live (they carry the project path column,
         whose census D-027 recorded: 162 of 482 rows over 24, longest 74). A setting that reaches
         half the surface is worse than no setting: the half it misses looks like it works.
         (2) `cbut health` hand-listed five settings and renamed two of them — it printed
         `refresh=5s sync=30s` for keys the file spells `refresh_secs`/`sync_secs` — so three of
         the six knobs a user may edit did not appear under the name that works in the file
Change   The cap is resolved through a `name_cap` **property**. `NAME_CAP` stays as the shipped
         default, `TrackerApp.__init__` shadows it from `config["name_cap"]` the way it already
         shadows the three limits, and each pushed screen overrides the property to read
         `self.app.name_cap`. `name_cap` joins `DEFAULTS` (28), `ENV_NAMES` (`CBUT_NAME_CAP`) and
         `_BOUNDS` (8..200): below 8 nothing readable survives the cut, above 200 it is not a cap.
         `cmd_health` prints whatever `load_config()` returned, keyed the way the file spells it,
         so the report cannot fall behind the layer one key at a time. No new import and no network
         path — a dict comprehension inside a print that already ran
Evidence L0 three new cases. `test_a_configured_cap_reaches_the_detail_screen_too` builds the app
         at `name_cap = 12` and reads 12 back off the tab cells, the note text, the pushed screen's
         own `name_cap`, its project column and `_cap_cell`. It went red twice before it was right,
         and only the first red was the product's: then `AttributeError: 'TrackerApp' object has no
         attribute 'name_cap'`, then my own fixture asserted a cap on a tab whose long values were
         all on the *other* screen.
         `test_every_pushed_screen_resolves_the_cap_from_the_app` enumerates the mixin's `Screen`
         hosts and fails on any that does not define the override in its own `__dict__`; provoked
         without touching a product file — an in-memory `del HistoryScreen.name_cap` turned both it
         (`['HistoryScreen'] is not false`) and the behaviour case (`AssertionError: 28 != 12`) red.
         `test_health_prints_every_key_the_loader_knows` compares `DEFAULTS` against the health
         output and was red naming all three: `['name_cap', 'refresh_secs', 'sync_secs']`. A real
         run on a throwaway database with `CBUT_NAME_CAP=40` prints `detail_limit=200 log_limit=100
         name_cap=40 refresh_secs=5s sync_secs=30s top_n=5`
Cost     Suite 313, four interpreters, one serial pass at this state (the four timings and any
         SQLite difference are in docs/maintenance/compatibility.md; that table is what makes this
         line checkable rather than remembered). What this does **not** close: every new screen
         still has to override the property by hand — the guard names the offender, it cannot
         install the override. And `NAME_CAP` stays on the class as the default, so reading the
         attribute directly still compiles and still ignores the settings file, which is why the
         check is an enumeration of hosts rather than a type
Commit   dd2ef52

### D-030 · 2026-10-10 · guard
Symptom  The settings suite passed on a clean shell and failed on a configured one. Five cases assert
         what the file or `DEFAULTS` produce, but the loader resolves environment > file > default, so
         an exported `CBUT_TOP_N` outranks the very value such a case is checking. Reproduced rather
         than reasoned about — `CBUT_TOP_N=9 CBUT_NAME_CAP=40 CBUT_REFRESH_SECS=2 CBUT_LOG_LIMIT=7` gave
         5 failures, `AssertionError: 9 != 5` and `Tuples differ: (9, 25, 50) != (3, 25, 50)`. From
         outside, that reads as a regression in the settings layer, and only the person who actually
         configured the tool can see it
Change   `no_settings_env()` hides every name **derived from `db.ENV_NAMES`** around the cases that
         assert a file-or-default value, and restores exactly what it took (a name that was unset
         stays unset rather than becoming an empty string). Derived rather than listed: the
         hand-copied `CBUT_*` set in `HealthSurfaceTest._env` is the same mistake D-029 corrected, and
         a second copy here would be forgotten by the next key the layer grows. Two environment
         branches the code had and no case claimed are now covered — an out-of-range environment value
         is refused with the **variable** named in the message, and a fractional interval reaches
         `refresh_secs` through the environment instead of quietly falling back to the default
Evidence L0, three readings. The 21-case settings suite is green **with the hostile environment**,
         after being red 5 ways in it. The whole suite was then run twice — clean, and with all six
         settings exported — `Ran 315` both times with the **same** two failures in both (the README
         count guards, which this round's numbers then satisfied): the identical failure set is the
         claim, not the count. `AGENTS.md` records the rule and the check, which is the hostile run
         itself rather than a review of the cases
Cost     One helper and a `with` clause; no production file moved, so nothing a user sees changes.
         What it does **not** buy: isolation is per-case, so a new case can still read the ambient
         shell by simply not entering the block — the catch for that is re-running the suite hostile,
         which is why it is written down as a check instead of being left to review
Commit   141ab25

### D-031 · 2026-10-10 · guard
Symptom  The READMEs are the only place a user meets the settings surface, and both halves of it — the
         `key = default` block and the sentence naming `CBUT_TOP_N` … `CBUT_SYNC_SECS` — are hand-typed,
         with nothing comparing them against the loader. This is the third hand-copied list of the same
         set found in one session: `cbut health` printed five of six keys and renamed two of those five
         (D-029), and the test suite was isolating its own hand-copied list of variable names (D-030)
Change   `test_both_readmes_document_every_setting` reads `db.DEFAULTS` and `db.ENV_NAMES` and requires
         every `"<key> = <default>"` line and every override name to appear in `README.md` **and** in
         `README.zh-CN.md`. It lives in the README check because that file's subject is exactly the
         distance between the page and the reality. No production code moved
Evidence L0, provoked without editing a tracked file: injecting `panel_gap = 3` + `CBUT_PANEL_GAP` into
         the loader's own dicts turns the case red with `['panel_gap = 3'] is not false`; renaming
         `CBUT_TOP_N` turns the second assertion red (`never names the variable that overrides these:
         ['CBUT_TOP_N_RENAMED']`); removing both turns it green again — the control that proves neither
         branch is decoration. Probe script: `/tmp/provoke_readme_settings_guard.py`
Cost     Suite 316 (this file 4 → 5). What it does not buy: the case proves the page *contains* the
         names, not that the prose around them is correct, and it cannot tell whether a documented
         default is described honestly — that stays a reviewer's job. What it does stop is a fourth key
         shipping while invisible in both languages
Commit   6459bb7

### D-032 · 2026-10-10 · guard
Symptom  AGENTS.md promises "Tests never touch real state" — each suite builds a throwaway database in a
         temp directory and the live `usage.db` is never opened — but the promise had no check. It has
         been broken once (a dispatcher test ran a real `cbut sync --full` against the live store), and
         on 2026-10-10 a moved `usage.db` mtime cost an hour: nothing in the repository could say whether
         a test had opened it (it was the maintainer's own TUI session). A rule with no check is a
         preference, and a wrong guess about it costs an hour
Change   `_hermetic.py` installs a process-wide `sys.addaudithook` that refuses a `sqlite3.connect`
         naming the real database (the read-only `file:...?mode=ro` form included — the rule is "never
         opened", not "never written") and a `subprocess.Popen` that would run one of our entry points
         without `CBUT_DB` or `--db` steering it away. Every `test_*.py` imports it, and
         `test_hermetic.py` fails on any module that stops. The launch test reads argv, not raw text: a
         `git` pathspec (`git log -- bin/cbut`) and a `bash -n bin/cbut` syntax check name an entry point
         without executing one, and refusing those would be a false positive the suite would answer with
         meaningless `CBUT_DB` pins. No production code moved
Evidence L0 2026-10-10: `python3 -m unittest discover -s scripts/tests` — `Ran 326`, `OK`. The coverage
         case is red before the twelve modules import the tripwire and green after; the enforcement cases
         carry their own controls (a `CBUT_DB`-pinned child and an unrelated `python -c` are allowed, so
         a hook that refused every subprocess would fail the pair). With the text rule instead of argv
         the same run was `failures=3, errors=4` — the four errors came from a `git` call (twice), the
         `bash -n` check and the crash child, none of which runs our code
Cost     Suite 316 → 326 (test_hermetic.py: new, 10 cases). What it does not buy: the hook is in-process,
         so a child that opens the real database directly is caught only by the launch test, and that
         test decides from argv — an inline `bash -c "bin/cbut sync"` is out of scope, named in the
         module docstring
Commit   e6e25ef


### D-033 · 2026-10-11 · fix
Symptom  A model response's cache hit lives in two spellings of one number:
         `rawUsage.prompt_cache_hit_tokens` and `rawUsage.prompt_tokens_details.cached_tokens`.
         Built-in routes write both, holding the identical value in each; a custom endpoint writes
         only the nested one. The parser read the top level alone, so every response served through a
         custom endpoint persisted `NULL` — and `NULL` is what the interface then showed as no cache
         usage. The number existed in the source the whole time. The miss was the instrument, not the
         data: the first probe over real transcripts counted the *top-level keys* of `rawUsage` and
         concluded the provider sent no cache fields at all, which was written up as a hard limit
         before anything had descended one level
Change   `_nested_cache_hit()` reads `prompt_tokens_details` → `cached_tokens`, and
         `_record_model_response` substitutes it only where the top-level field is absent. Never added:
         across built-in records the two spellings sum to the same figure, so a sum would have doubled
         every cache number the tool has ever reported. An explicit `0` also stays `0`, because `0` is a
         measurement and absence is not — which is why the case is three tests rather than one, each red
         under a different wrong implementation (sum, falsy test, no read at all). The nested path is
         registered as `NESTED_USAGE_FIELDS` and printed by `cbut format`, so the generated format
         document carries it; `test_no_registered_field_is_dead` exempts it as a dynamic read, and its
         proof is behavioural rather than static
Evidence L1 A/B on identical input, same indexer, nested read disabled then enabled into two throwaway
         databases: the custom endpoint's rows go `NULL → 38,634,240` (425 rows) and `NULL → 29,030,400`
         (288 rows), recovered `67,664,640` cache-hit tokens — 96% of that route's prompt tokens. All
         five built-in models' sums and NULL counts unchanged (`deepseek-v4.1-flash` stayed
         `2,536,817,403`), and `model_responses` row count unchanged at 20,119. L0 `Ran 330, OK`.
         `prompt_cache_miss_tokens` and `prompt_cache_write_tokens` stay `NULL` for this route: it
         genuinely sends neither, and an empty cell is the honest answer where no number exists
Cost     Suite 327 → 330 (test_sync.py: 82 → 85). No schema change — the column has existed since v3 and
         was simply never filled for this route. No backfill without a rebuild: incremental sync follows
         file offsets and does not re-read rows it already stored, so existing custom rows keep their
         `NULL` until an L2 `cbut sync --full`. One figure in commit f3e5b3f conflates the two
         quantities: the equal pair was measured at `2,614,796,037` over transcript records, and the
         stored built-in total is `2,536,817,403` after `messageId` dedup — the claim (equal, therefore
         never summed) holds; the single number quoted for it was the wrong one of the two
Commit   f3e5b3f

### D-034 · 2026-10-11 · guard
Symptom  `docs/maintenance/compatibility.md` is the document whose job is the version matrix, and it
         carried a row saying the AGENTS.md CI section "must keep matching the workflow above". The
         workflow then became a `3.11` + `3.14` matrix and AGENTS.md was updated in the same change,
         while the maintenance document went on stating **3.12** and "the only interpreter CI runs" in
         four places. All 330 cases stayed green and CI reported success throughout, so nothing in the
         repository could tell a reader that a description of a check had stopped describing the check —
         the `schema_version` failure mode again: true once, never re-read
Change   `CiMatrixIsDocumentedAsItRuns` in `test_maintenance_docs.py` reads `python-version` out of
         `.github/workflows/ci.yml` and compares it, per line, against every line of AGENTS.md and
         compatibility.md that makes a CI claim (`ci.yml`, or CI runs/is/matrix/section). A set
         comparison over the lines that claim, not a list of lines to inspect: the next stale sentence is
         caught without editing the check. Scoping is by claim rather than by file, so prose about the
         local venv (3.13, which CI does not run and which the TUI uses) is out of scope by construction.
         The document's other CI sentences were rewritten to match, including the note that CI now covers
         the two bounds and therefore still misses 3.13, and the reproduce commands were replaced with
         the ones actually run
Evidence Red against the document as it stood, naming line 11 and the rest. Provoked from the workflow
         side on a copy of the file: matrix widened to three, narrowed to one, and the version list
         removed — each turned the check red, so it reads the workflow rather than reciting today's
         answer. A synthetic page still saying 3.12 is caught while a `requires-python >=3.11` line on
         the same page is left alone, which is the scoping claim being tested rather than asserted.
         L0 `Ran 334, OK`
Cost     Suite 330 → 334 (test_maintenance_docs.py: 9 → 13). What it does not buy: it compares version
         tokens on claim lines, so a document can still misdescribe CI in prose that carries no version
         number, and it cannot say whether the matrix is the right choice — only that the documents agree
         with it
Commit   39b608d

### D-035 · 2026-10-12 · fix
Symptom  The date picker's calendar drew one digit per cell — October 2026 read "3 3 1 2 3 4 5 / 6 7 8 9 1 1 1
         …" — and the two time fields cut their value ("12:12 P"), while clicking a card to aim the calendar at
         START or END often did nothing. All three looked like one bug and were three: the numbers were correct
         and the day clicks landed on the right date throughout, so nothing in the suite could see it. Textual
         renders a Button's two-digit label as a single digit at width 3 ("30" -> " 3 "); an Input spends 6 cells
         on its border plus its 0-2 padding, so a 13-cell field holds 7 — one short of "12:12 PM"; and while
         "End follows current time" was ticked the end inputs were `disabled`, and a disabled Input swallows the
         click, so neither the click handler nor focus could switch the target and the calendar was unusable for
         END
Change   Day cells are 4 wide; the time fields are 16 (10 cells of content) and the date fields take the slack
         (`#dr-left` 44, box 80). The end inputs are never disabled; editing one drops the follow switch by itself,
         told apart from a prefill/preset write by matching the value last written into that field (`_written`).
         Three cases: every cell renders its whole number, clicking an end field switches the target, and an edit
         drops follow while a programmatic write does not
Evidence Reproduced before the change and re-measured after on a copy of the live store: all 42 cells render their
         full number (was 11 of 42 clipped), "10/12/2026" and "12:19 PM" fit whole (content 17 and 10), and clicking
         the END date field moves `_active` to "end" so the next day click lands in END. A click on a disabled end
         input was measured to post no event at all, which is why the switch had been unreliable rather than wrong.
         L0 `Ran 376, OK`
Cost     Suite 373 → 376 (test_tui.py: 97 → 100). What it does not buy: the cell width is asserted through the
         rendered text, not through Textual's layout rules, so a future Textual that renders two digits at width 3
         would leave the guard green while a wider cell stayed — harmless, but the number is pinned by observation
         rather than by contract
Commit   dfb1e17

### D-036 · 2026-10-12 · fix
Symptom  Two leftovers from the calendar rebuild. The six shortcut chips did not fit their column: at an
         80-column terminal the row ran from x=2 to x=53 (52 cells) while `#dr-left` was 47 wide, so the last
         chip, "All time", was cut to "All tim". And after picking a custom range the top bar's 📅 button kept
         the width it had been built with (10 cells, "📅 Today") and drew a truncated span — "📅 10-06" — until
         some later full relayout; switching a tab away and back was what made it correct.
Change   Day cells are Statics, not Buttons: a Button renders "30" as " 3 " at a 3-cell width, and widening
         every cell to 4 would not leave room for the calendar beside the cards, so a day click is read from
         the cell id in `on_click` instead of a `Button.Pressed`. The freed cells take `#dr-left` to 53 (one
         cell spare) and the box budget to 79 columns. `TopBar.set_range_label` now calls `refresh(layout=True)`,
         since a Button does not re-lay-out when its label grows.
Evidence Measured on the running app, not derived: the six chips span x=2..53 (52 cells) and the column is 53,
         so the last chip's right edge (54) sits inside the row's (55); before the change the row ended at 49
         and the chip at 54, six cells past. The label guard drives a custom range, switches to another tab and
         back, and finds "10-01 00:00 → 10-03 00:00" whole in the drawn line. L0 `Ran 378, OK`
Cost     Suite 376 → 378 (test_tui.py: 100 → 102). What it does not buy: the chip fit is asserted against the
         rendered regions at one terminal size, so a terminal narrower than 79 columns would still clip the
         picker (it is centred and does not scroll horizontally); and the relayout guard proves the label is
         whole after a tab switch, not that a label change repaints without one.
Commit   7442b59

### D-037 · 2026-10-12 · fix
Symptom  `cbut mcp`, `cbut skills` and `cbut agents` — and the Skills/Agents sections of `cbut stats`,
         and the recent-responses table of `cbut models` — printed a table header with nothing under
         it whenever the database held no rows for that section, instead of the `(none)` notice each
         line was written to show. A reader saw a bare header and no way to tell "none recorded" from
         "the report broke".
Change   The cause was the idiom, not a missing branch: `table(...) or "(none)"` could never reach its
         right-hand side, because `table()` always returns at least a header line and a separator line,
         so the string is always truthy. A `table_or(headers, rows, empty=…)` helper now branches on
         the rows. Fallback text is unchanged per call site (`cbut stats` keeps its indented
         `"  (none)"`). The new `test_stats.py` suite drives the real CLI and pins the empty case.
Evidence L0 2026-10-12: `python3 -m unittest discover -s scripts/tests` → `Ran 432 tests ... OK`;
         `test_stats.py::test_empty_reports_say_none_not_a_bare_header` runs `cbut mcp/skills/agents`
         against an empty temp database and finds `(none)` and no `---` separator.
Cost     None. The `(none)` string was already the stated intent; the helper only makes it reachable.
         What it does not buy: the notice is a fixed string per call site, so a future section added
         with `print(table(...))` (no helper) would go back to printing a bare header with no guard.
Commit   6788a70

### D-038 · 2026-10-13 · guard
Symptom  `AGENTS.md` has said, since the D-030 isolation work, that any test shelling out to
         `bin/cbut` must pin `CBUT_SCRIPTS`, `CBUT_DB` **and** `CBUT_CODEBUDDY_DIR` "all three,
         because the launcher resolves them independently". Two of the three were checked by
         `scripts/tests/_hermetic.py`; the third was prose. A rule with no check is a preference,
         and the sibling `ocut` shipped exactly this hole — a fixture read the developer's real
         plugin-events sidecar, so its tests were red only on the machines that had one.
Change   The audit hook now refuses three things instead of two. Any `open`, `os.scandir`/`os.listdir`
         or `sqlite3.connect` naming a file under `~/.codebuddy` raises `Refusal`; the listing events
         are included because a glob reaches the host's files with no `open` at all, which is how
         `cbut health` walks `projects/**/*.jsonl`. A child that runs one of our entry points without
         `CBUT_CODEBUDDY_DIR` pinned is refused as well. The `subprocess.Popen` branch gained an
         early `return` when argv names no entry point, hoisting the check
         `_child_would_open_the_real_db` already made on its first line, so the host refusal applies
         to the same set of children and `bash -n bin/cbut` stays legal.
Evidence L0 2026-10-13: `python3 -m unittest discover -s scripts/tests` → `Ran 435 tests ... OK` —
         and, for the first time, the same run under an empty `HOME`
         (`HOME=/tmp/… PYTHONPATH=/home/shade/.local/lib/python3.14/site-packages python3 -m unittest
         discover -s scripts/tests`) also reports `Ran 435 tests ... OK`. Before this the suite's
         answer depended on which machine ran it. Reversal: restoring only
         `test_sync.py` to its previous form turns the case red with
         `Refusal: a test tried to read a file under the real host footprint /home/shade/.codebuddy`,
         naming the path and the variable to pin.
Cost     One audit-hook event per `open`, so the suite pays a `Path.resolve()` on file opens.
         The negative control is part of the cost of trusting it: `test_an_ordinary_test_path_is_left_alone`
         proves a temp path, `usage.db` and `:memory:` are untouched, because a tripwire that refuses
         ordinary work trains the suite to disable it. What it does not buy: `~/.codebuddy` is the only
         protected host, and the check reads argv and the environment — a child that hardcodes the real
         path inside an inline script is out of reach, the same limit the database check already had.
Commit   dcd1ee3

### D-039 · 2026-10-13 · fix
Symptom  `test_sync.py::test_inventory_owner_empty_string_not_null` called `scan_inventory(self.conn)`
         three times with nothing pinning the host directory, so its baseline `n1` was whatever the
         developer's real `~/.codebuddy` contained — measured here at 140 rows (52 builtin, 28 plugin,
         60 user), against 52 on a machine with no such directory. The case asserted that re-scanning
         does not grow `inventory`; what it actually measured was this machine. Two more cases had the
         same hole, because `cbut health` stats and globs the host dir:
         `test_config.HealthSurfaceTest._env` handed the child the ambient `CBUT_CODEBUDDY_DIR`, and
         `test_usage.py::OldDatabaseOnAReadOnlyCliTest` ran `health` against it.
Change   All three now point somewhere throwaway. The inventory case builds a fixture host
         (`skills/ai/add-tests/SKILL.md`) under its own temp directory, repoints
         `sync.db.CODEBUDDY_DIR`, restores it in a `finally`, and asserts `n1 > 0` so the fixture is
         proven to have produced something. The settings helper hides `CBUT_CODEBUDDY_DIR` alongside
         every name derived from `db.ENV_NAMES` and pins it to a path that does not exist; the
         old-database case repoints `db.CODEBUDDY_DIR` the same way.
Evidence L0 2026-10-13: `Ran 432 tests ... OK` at this commit and `Ran 435 ... OK` after the tripwire
         that then made the fix necessary landed. The honest limit, measured rather than assumed: the
         old case was **not** vacuous. `scan_inventory` always inserts the builtin tool, agent and
         command rows, so `n1` was 52 even with an empty home and the dedup assertion did run — over
         those builtin rows, which are exactly the rows whose `owner_plugin` is the empty string the
         case is named for. The defect is hermeticity, not an assertion that never failed.
Cost     The inventory case now depends on the fixture layout it creates, so a change to
         `SKILL_MANIFEST_GLOB` or `DIR_SKILLS` can make it fail for the right reason at last.
         `assertGreater(n1, 0)` is weak on its own — it cannot tell "one row" from "all of them" —
         and it is kept only so a silently empty fixture does not read as a passing dedup check.
Commit   d7fa4bf

### D-040 · 2026-10-13 · guard
Symptom  The root-file layer was a naming convention with nothing behind it. No check in this
         repository asserted that the finalized files sit at the repository root, that `LICENSE`
         exists, or that `AGENTS.md` points at the machine-wide standard instead of restating it.
         A renamed or deleted root file would only be noticed by a person reading the diff.
Change   Add `scripts/tests/test_conventions.py` (9 cases, standard library only, and it installs
         the same hermetic tripwire as its siblings). It asserts the finalized layer is present at
         the root and that `pyproject.toml` is the only manifest; that `AGENTS.md` carries the
         one-line dimension pointer and reproduces none of the standard's own numbered section
         headings; that `.gitignore` still covers the runtime state the standard lists and that
         `git ls-files` tracks none of it; and that no process draft is tracked anywhere. `AGENTS.md`
         gains the dimension line above the shared-standard pointer it already had. The README pair
         moves from 435 to 444 tests, because `test_readme_counts.py` compares the table against
         `scripts/tests/` by set equality and a new suite has to be counted, not ignored. Nothing
         is fetched — every assertion reads this working tree.
Evidence `python3 -m pytest scripts/tests -q` 444 passed (142 subtests), run with the final content
         of this entry in the tree. Provoked for real: moving `LICENSE` out of the tree turns the
         root-layer case red with `AssertionError: Lists differ: [] != ['LICENSE: create this file
         at the repository root']`, and restoring it turns the suite green again. Two earlier
         drafts of the guard were themselves red and were corrected rather than suppressed — a
         heading match that also caught this repository's own `## CI`, and an ignore-list spelling
         that only matched `.venv/`. No test was skipped, widened or moved to make a check pass.
Cost     The manifest and ignore lists are hard-coded, so a deliberate second manifest or a new
         runtime artefact is a change to this file rather than a surprise failure. The README
         aggregate is a second place to update for every new suite; that is the repository's own
         rule, not something this entry introduces.

Commit   ca82a0b

### D-041 · 2026-10-13 · fix
Symptom  The Skills tab listed 55 skills and not one of them was installed. `scan_inventory`
         globbed `skills/**/SKILL.md` (`scripts/cbut-sync.py:279`), and pathlib's `**` does not
         descend into a symlinked directory — while all 27 skills on this machine are
         symlinks into `~/.agents/skills`. Measured, not reasoned: the glob matched exactly one
         file, `.codebuddy/skills/.skill/SKILL.md`, a dot-directory that
         `INVENTORY_HIDDEN_PREFIX` never filtered because the filter was applied only inside
         plugin subdirectories. What the tab showed instead was the union's other half — 55
         rows from a retired `skills/<category>/<skill>/` layout whose directories are gone.
         Two more rows in the same table were wrong the other way: `scan_inventory` only ever
         ran `INSERT OR REPLACE`, so an uninstalled plugin, a deleted skill and five deleted
         agents each kept their row forever, and `BUILTIN_TOOLS` was 35 names typed by hand
         against an installed `product.json` carrying 61 — with `fork` in the constant and not
         in the package, and `cli` the other way round.
Change   Skill discovery walks the direct children of `skills/`, follows one level of symlink
         (npm-style: the link *is* the entry) and never descends through one; the dot filter
         now applies to the same walk. Reading and writing are split: `collect_inventory`
         returns rows, `replace_inventory` writes `DELETE` + `INSERT` inside one transaction
         afterwards, so inventory means "installed now" and a failed scan leaves the previous
         snapshot alone. The catalogue comes from the installed package's `product.json`,
         located from the `codebuddy` executable on `PATH` and found by walking up to the
         package root — pure `os`/`pathlib`; the checked-in constants stay as the fallback, and
         `format_doc()` now reports which of the two it used instead of restating a count. An
         `CBUT_PRODUCT_JSON` override naming a non-file resolves to nothing.
Evidence L0 2026-10-13: `python3 -m unittest discover -s scripts/tests` → `Ran 485 tests ...
         OK`. L1, real `~/.codebuddy` logs into a throwaway store at `/tmp/…` (L2 not touched —
         the live `usage.db` was not rebuilt): every table is row-identical to the live one
         except `inventory`, `141 → 133`. Inside it, `skill|user 55 → 27` (the 27 are the
         symlinked skills, previously 0), `tool|builtin 35 → 61`, `agent|user 5 → 0`,
         `plugin|plugin 10 → 9` (the uninstalled one is gone), zero rows whose name starts
         with a dot. Provoked, each with the failure named: restoring `skills/**/SKILL.md` turns
         five inventory cases red; dropping the `DELETE` turns three red; returning `None` from
         the resolver turns two red; inserting `subprocess.run(["which", "codebuddy"])` turns
         two error and makes two `test_privacy` cases red, which is the reason the resolver
         walks `PATH` itself.
Cost     The one-level symlink rule is a decision, not a limit discovered: a skill installed by
         symlinking a directory into a category folder is not found, and neither is one nested
         two deep. That is the layout this repository retired, so the old reading has to stay
         readable — it does, one level down and only through real directories. The drift guard
         is the other cost: `product.json` is upstream's to change, and the checked-in snapshot
         is compared against it whenever a `codebuddy` is on `PATH`, so a CodeBuddy upgrade that
         adds a tool fails here until `scripts/tests/product_snapshot.py` is re-run. Without an
         installed CLI the comparison is skipped, but the snapshot is still parsed first, so an
         emptied fixture is caught on every machine.
Commit   2fb070a

### D-042 · 2026-10-13 · fix
Symptom  An installed skill and one uninstalled last month rendered identically — same row,
         same columns, and a `-` where `last used` should be. The `UNION` that lets a removed
         skill keep its history also erased the evidence that it is gone: `inventory.source`
         was read by `cbut inventory` and by no panel query, and no column anywhere carried
         installed/removed, so the only tell was a call count of zero. The complaint this
         started from — "I deleted the skill, why is it still listed" — had no honest answer
         in the UI, because the database could not tell either.
Change   `installed` and `ever_used` are derived in the query layer, aggregated over the
         inventory side and the history side of each names CTE, and `db.entity_state()`
         turns them into `in use` / `installed` / `removed` for both front ends to render.
         No column and no migration: under snapshot semantics, a missing inventory row *is*
         the removed state, so storing it would duplicate a fact the query can derive. `removed`
         outranks `in use`, and neither flag follows the time window — `ever_used` comes from
         the un-windowed history scan, because a tool last used last week is still in use.
Evidence L0 2026-10-13: `Ran 485 tests ... OK`. L1 (same throwaway store as D-041):
         `cbut skills` reads `removed` for `docx` and `add-tests` — names with history and no
         inventory row — and `in use` for `playwright-cli`; `cbut agents` reads `removed` for
         `code-reviewer`, whose file was deleted while its two calls stayed. Provoked: removing
         the inner `GROUP BY name` doubles every count (`cbut plugins` reports 4 uses for 2)
         and turns three cases red; hardcoding `ever_used = 1` marks an unused configured MCP
         server as in use and turns two red; narrowing the renderable check to `isinstance(c, str)`
         flattens every `Text` cell, and the dimming case goes red.
Cost     `rich` becomes a second non-stdlib import in `cbut-tui.py`, admitted to the privacy
         allowlist by measurement rather than by assertion: `import socket|ssl|http|urllib`
         matches nothing anywhere in rich 15.0.0, not even `urllib`. The scope of that claim
         is unchanged from the one already in AGENTS.md — it is about a version installed here.
         The state column is last on purpose, because the column plan drops the tail first: a
         narrow terminal loses `last used` before it loses the answer to "is this still here".
         Dimming covers the state cell only, since Textual's `DataTable` has no per-row style
         hook — a row is therefore not uniformly greyed, which reads as a deliberate choice
         rather than a missing one only if you know it.
Commit   9764332

### D-043 · 2026-10-13 · fix
Symptom  D-041 made the catalogue come from the installed package and left a checked-in
         fallback for a machine that cannot resolve one — but nothing said which of the two a
         given run had used. The two disagree about 27 tool names, and D-041 left
         `builtin_catalog()` documenting that "`cbut health` prints which of the two was used"
         without adding the line, so the commit that introduced the catalogue also shipped the
         claim it made about it.
Change   `cmd_health` resolves the catalogue again and prints it: `product.json @ <path>` with
         the counts, or `fallback constants (…)`. The parenthetical keeps three states
         apart — resolved, found but carrying no catalogue (the four variant product configs
         have no `tools` key), and unresolved — because "the file is missing" and "the file
         says nothing" lead a user to delete different things.
Evidence L0 2026-10-13: `Ran 485 tests ... OK`. The two health cases pin the resolved line to
         the snapshot's own two arrays and assert the counts it implies, then pin two
         throwaway files — one absent, one carrying empty arrays — and require the matching
         phrase. `HealthSurfaceTest._env` now hides `CBUT_PRODUCT_JSON` with the other two path
         overrides, because without it the child's line depended on whose machine ran the
         suite. Provoked: dropping `resolve_product_json`'s `is_file()` turns the resolver case
         red with `PosixPath('…/absent.json') is not None`.
Cost     `cbut health` now reads the package's install prefix on every run, on a path derived
         from `PATH`. It is the same walk the indexer already does on every sync, and it
         happens after the settings line, so a slow `PATH` shows up in a report that used to
         be a pure database check. The registry escape hatch this file still carries —
         `cmd_health` re-declaring `"projects"` and `"**/*.jsonl"` outside the format registry,
         unguarded because `test_format_registry.py` parses only `cbut-sync.py` — is untouched
         and still owed.
Commit   d6f4e7b
### D-044 · 2026-10-13 · fix
Symptom  D-042's `removed` column was right about most names and wrong about three, and the
         three were the ones a user was looking at. `docx` read `removed` with 8 calls on it
         while the Plugins panel read the same plugin as `installed` — the panel contradicted
         itself two tabs apart. A plugin can *be* the skill: it ships `<installPath>/SKILL.md`
         and nothing else this tool knows how to count. `collect_inventory` only walked
         `PLUGIN_SUBDIRS`, so docx, pptx and xlsx never reached `inventory` at all, and a name
         with history but no inventory row is precisely what the union renders as `removed`.
         Three plugins, three silently missing skills, and the gap scaled with the plugin: one
         skill's worth of uses was also unattributed, so the Plugins panel undercounted them.
Change   A plugin holding `SKILL.md` at its install root is inventoried as a skill under the
         plugin's own name, owned by that plugin. The name comes from the plugin key, not the
         directory: `installPath` ends in the version (`unknown`, `1.3.0`), so the directory
         would file the skill under a name nothing can call it by — which is the same row, still
         removed, just renamed. The frontmatter `name:` is deliberately not read either: a
         plugin installed here declares `skills安全审计` for the directory `skills-sec-audit`,
         so the manifest is not the invocable name. `inventory` gains no column and the schema
         does not move; the owner map gains the entry, which is what `skill_usage.plugin` and
         therefore `plugin_usage` are built from.
Evidence L0 2026-10-13: `Ran 492 tests ... OK`. L1 (throwaway store, real logs): the Skills
         panel reads `docx` as `in use` where it read `removed`, and Plugins goes from
         `docx uses=0 skills=0` to `uses=8 skills=1`; `pptx` and `xlsx` appear as installed.
         The `removed` rows that remain — `commit`, `review`, `security-and-hardening`,
         `add-tests`, `systematic-debugging` and a dozen more — are correct: they belong to the
         retired `skills/<category>/<skill>` layout that the `~/.agents/skills` symlinks
         replaced. Provoked: deleting the new branch turns 6 of the 7 new cases red, including
         the one that reads the symptom back through `db.q_skills`.
Cost     The two surfaces overlap by name and are read as a union, so a plugin shipping both a
         root manifest and a `skills/` directory contributes two rows where one plugin might
         have meant one skill; nothing installed does, and both being real is better than one
         being invented. Plugin **tools** and plugin **MCP servers** remain uninventoried — no
         installed plugin ships either, `plugin.json` declares neither, and there is no rule to
         read without guessing at one, so the format document now names the gap instead. If
         either appears, the failure mode is this entry's, and that line is where to look.
Commit   6af151f

### D-045 · 2026-10-16 · chore
Symptom  The pointer in AGENTS.md is followed only by an agent that chooses to follow it: it named
         `~/.agents/standards/`, but nothing made that text reach the context, and the section list
         it carried had drifted — §5 (the conflict rule) was missing.
Change   End the pointer block with two bare `@` imports (`@~/.agents/standards/STANDARD.md`,
         `@~/.agents/standards/dimensions/a.md`) and add `opencode.json`, whose `instructions` array
         names the same two files. Add `§5 conflict rule` to the section list. `test_conventions.py`'s
         `ROOT_FILES` gains `opencode.json`.
Evidence 2026-10-16: CodeBuddy CLI (`-p --tools ''`, every tool disabled) in a directory whose only
         memory file was an `AGENTS.md` carrying the two imports printed the tokens they carried — the
         text is injected, not read by a tool. opencode `run` printed the token its `instructions`
         named, with no tool call. `python3 -m pytest scripts/tests/test_conventions.py -q`
         9 passed.
Cost     Both hooks name `~/.agents`, absent in another clone, so they are skipped there — the
         pointer stays the portable part. Nothing is copied, so nothing can drift.
Commit   795730d

### D-046 · 2026-10-16 · chore
Symptom  The checked-in `fixtures/product.json.snapshot` lagged the installed CodeBuddy CLI:
         `MemAnswer` and `MemCurate` had been added upstream, so
         `test_the_snapshot_has_not_drifted_from_the_installed_package` failed and the two real
         builtin tools were read as unknown.
Change   Regenerate the snapshot with `scripts/tests/product_snapshot.py` (61 -> 63 tools) and drop
         an unused `import importlib.util` from `scripts/cbut-tui.py`.
Evidence L2 2026-10-16: the drift guard was red before the regeneration;
         `python3 -m unittest discover -s scripts/tests` 492 passed after it.
Cost     The snapshot is a copy of upstream's tool list; the next CLI upgrade drifts it again, and
         this guard is the only thing that will say so.
Commit   3face03

### D-047 · 2026-10-17 · perf
Symptom  `idx_tool_cat` keyed `tool_calls.category`, and nothing in this tool compares that column —
         not a query, and not one of the five views either. It came from the first schema and
         outlived the v6 sweep, which retired the two session indexes: nobody had looked at it. What
         is worth recording is *how* the v6 pair was found — by grepping the source for a `WHERE` on
         the column — because that method cannot see an f-string, and that is the only reason this
         one survived. The lesson is sharpened by the sibling project `opencode-usage-tracker`,
         which has the query shape this index serves (`WHERE category = 'builtin'` in its MCP-shape
         check) and whose copy of the index is genuinely reached, in one plan of 1,452. Same index
         name, two repositories, opposite verdicts: neither can be decided by the name.
Change   Drop the index, and migrate it out of databases that already hold it — `SCHEMA_VERSION`
         7 -> 8 with a fourth `MIGRATIONS` step, `_drop_dead_category_index`, alongside the v6 one
         rather than folded into it, so the v6 docstring keeps describing v6. Three cases added,
         and the query extractor they share with the session-index guard had to be rewritten (see
         Evidence — the first attempt at each of the two guards was wrong in a different way).
         README counts moved with the suite: `test_usage.py` 100 -> 103, `Ran 492` -> `Ran 495`,
         which the repository's own `test_readme_counts` demanded rather than tolerated.
Evidence L2 2026-10-17: reachability was decided on statements that really ran, not on source text
         — `open_db` was wrapped with `set_trace_callback` for the whole suite, capturing 8,339
         executable statements, each re-planned with `EXPLAIN QUERY PLAN` against a real-size
         database. `idx_tool_cat` is named in **0** of them; the indexes that are kept are named in
         13 to 703. All five views were read back out of `sqlite_master` and none mentions the
         column. Read path, on real-size copies: all ten headless reports (`stats`, `tools`,
         `skills`, `agents`, `mcp`, `plugins`, `recent`, `inventory`, `export`, `models`) printed
         byte-identical output with and without the index, with matching exit status, and each output
         was checked to be a report rather than an error — the
         first pass at this had compared two usage-error messages for `cbut stats tools`, which is
         not a subcommand, and was discarded. No existing query's plan can change at all: a plan
         that never names the index cannot. Write path, at the real row count: re-inserting the real
         37,345 `tool_calls` rows took **145.03 ms with the index and 121.87 ms without** (+19.0%),
         and the database 9,539,584 B against 8,892,416 B. This index is **not** dead the way the v6
         pair were — it is a covering index for `WHERE category = ?` and the planner does choose it
         for that shape, statistics or not — so the only argument for the cut is that no such query
         exists here, and the cost of carrying one that does not is measurable at this size.
         Re-adding it is one `CREATE INDEX` line and no migration, because `CREATE INDEX IF NOT
         EXISTS` runs on every open. Guards, provoked: planting
         `SELECT COUNT(*) FROM tool_calls WHERE category = 'builtin'` into `cbut_db.py` turns
         `test_no_query_filters_by_category` red, planting `WHERE session_id = ?` still turns the
         v6 guard `test_no_query_filters_by_session_id` red, and re-declaring the index turns
         `test_the_category_index_is_neither_declared_nor_present` red; each was restored and
         re-checked green. The first version of that last one asserted the index was absent from a
         fresh database and **was provably empty** — the migration drops it in the same open, so a
         re-declared line is invisible to it; that is what the negative check caught, and the
         assertion now reads the declaration. The extractor both guards share had a second defect:
         it scraped double-quoted spans out of the raw source, where `re.I` counts the English word
         "with" as the SQL keyword `WITH`, so it reported a *docstring about* a query as a query; it
         now reads string constants out of the AST with docstrings removed. Real file — snapshot
         `usage-20261017-082233-manual.db` (18,350,080 B) taken first: `migrate` returned `migrated`
         then `current`, `idx_*` went 10 -> 9 with only `idx_tool_cat` gone and nothing unexpected
         added, `schema_version` 8, `quick_check`/`integrity_check` ok, 0 foreign-key violations, and
         `cbut health` differs by the schema-version line alone. `python3 -m unittest discover -s
         scripts/tests` 495 tests, OK (492 before the three new cases).
Cost     The cut is cheap to reverse — one line, no migration — which is the whole reason it is safe
         to make on "no caller today". The cost is that the reversal has to stay honest: if a query
         of that shape is ever added, `test_no_query_filters_by_category` fails and the index comes
         back with it, and that test sees only string constants it can read, so a query assembled
         where it cannot look would not warn anybody. The second cost is about the guards, not the
         index: of the tests written for this entry, **two were green while guarding nothing** — a
         name-based absence check that the migration itself made vacuous, and an extractor that went
         red on prose — and both were only found by trying to make them fail. A guard is not
         evidence until it has been made to go red on the thing it claims to catch.
Commit   9917a95

### D-048 · 2026-10-17 · fix
Symptom  The status line and `cbut health` reported one unparsed total, and that total could not be
         read. Three record types this build understands and deliberately indexes nothing from —
         `reasoning` (22,146 records on this database), `file-history-snapshot` (7,315) and
         `summary` (528) — held 29,989 of the 29,989, so `! 29,989 unparsed` sat on screen every
         day. A record type nothing here has been taught about would have looked exactly like it,
         which is the one thing the number exists to say. The file already names this failure for
         the `model-usage` echo — a number that cries wolf is not a signal — and then left three
         types holding 100% of the count unclaimed.
Change   Give them a name rather than removing them from the tally: removal would hide the day one
         of them starts carrying something we index. `UNCLAIMED_RECORD_TYPES` sits beside
         `HANDLED_RECORD_TYPES` in the format registry, `KNOWN_UNCLAIMED_REASONS` is the spelling
         the `unparsed` table uses, and `split_unparsed` is the one filter `cbut health` and the
         status line both call — two copies would be two answers to "is this drift?" and the
         disagreement would be invisible, because both show a number. Health marks each reason
         `NEW` or not and says which case it found; the status line raises its `!` only for a
         reason outside the set, prints the familiar total without it otherwise, and counts the
         *new* records in the alarm rather than the familiar ones it would otherwise be buried
         under.
Evidence L0 2026-10-17: the split was provoked both ways. Replacing the status line's
         `if o.get("unparsed_unexpected"):` with `if False:` turns
         `test_the_status_line_only_shouts_about_a_reason_it_has_not_seen` red; restoring it turns
         it green. The same test then fails if the alarm count is the familiar total instead of
         the new records, and `test_health_splits_the_tally_into_known_and_new` fails if a known
         reason is marked `NEW`. Against the real database, `cbut health` prints
         `29989 records across 3 reason(s) no handler claims (29989 known, 0 not)`, all three rows
         unmarked, and `(every reason here is one this build already declines on purpose, so
         nothing has moved)`; the same command against a fixture carrying `type:totally-new-thing`
         (4) and `unparseable_line` (2) prints `(29989 known, 6 not)`, marks both rows `NEW`, and
         names them under `that is the format moving`. Three unit cases pin the vocabulary itself:
         the three expected types, the loss reasons that must never join them (`unparseable_line`,
         `type:<missing>`, `unreadable_file:OSError`), `split_unparsed`'s arithmetic on a mixed
         list, and that no type is in both `HANDLED_RECORD_TYPES` and `UNCLAIMED_RECORD_TYPES`.
         README counts moved with the suite (test_sync 126 -> 129, test_tui 120 -> 121,
         test_stats 28 -> 29) because `test_readme_counts` compares them to the loader.
         `python3 -m unittest discover -s scripts/tests` 500 tests, OK (495 before this).
Cost     `UNCLAIMED_RECORD_TYPES` is a hand-kept list of record type names, and the day CodeBuddy
         renames one the views will say NEW — which is the correct answer, but it means a genuine
         upstream rename and a genuine upstream addition look the same until somebody reads the
         reason name. A type moved into that set without a matching entry here would silence a
         real signal one commit at a time. The split also reads the whole `unparsed` table in
         Python; that is one row per reason (three, here), and it would not stay cheap if a host
         ever wrote one row per record.
Commit   ec2ad52

### D-049 · 2026-10-17 · perf
Symptom  `TABLES_SQL` declared `idx_model_resp_model` on `model_responses(model)` — the leading
         column of `idx_model_resp_model_ts` — so every insert paid for an index whose key a wider
         one already starts with. The `v9` docstring first justified the cut with "no statement
         this build runs reaches it", and that sentence is false: put the index back on a copy of
         the live file and **3 of the 87 plans the report surface produces name it**
         (`q_model_summary`'s `SEARCH … (model=?)` and the two per-model roll-ups'
         `SCAN … USING INDEX`). So the reachability check that landed with the cut would have
         passed this index, and the record that says it found the index was wrong about its own
         method.
Change   Keep the drop (a wider key answers every lookup the narrow one can) and give the real
         reason a rule of its own in `scripts/tests/test_indexes.py`: an index whose key is a
         **strict prefix** of another declared index's key on the same table can never win a plan,
         and `PRAGMA index_info` answers that from the schema `ensure_schema` builds — no rows,
         no DDL parsing. `_drop_dead_model_index`'s docstring, `measurements.md` §2, AGENTS.md
         invariant 4 and the README's per-suite row now say what was measured rather than the
         shortcut. The migration itself is unchanged: a bare `DROP INDEX`, undoable with one
         `CREATE INDEX` line, no row re-read.
Evidence L0 2026-10-17: `python3 -m unittest discover -s scripts/tests` 507 tests, OK (505 before
         the two prefix cases), and `test_readme_counts` holds the README rows to the loader.
         Read side measured on copies of the live file — all **87** statements the report surface
         runs, planned in a fresh connection on each side: **3 plans differ**, all moving to
         `idx_model_resp_model_ts` (one equality lookup, two covering scans that sort in a temp
         B-tree either way); 300 timed runs of each query function read **12.15 against 13.19 ms**
         (`q_model_summary`), **28.70 against 28.36 ms** (`q_model_tokens`) and **22.98 against
         23.78 ms** (`q_usage_model_stats`), the sign flipping between rows. The guard's own
         provocation: `CREATE INDEX idx_model_resp_model ON model_responses(model)` added to a
         fresh schema makes `prefix_duplicates` return exactly `['idx_model_resp_model']`, and
         the synthetic case pins that a wider key, the same key on another table and a different
         column order are **not** reported. Write side and file size, from `measurements.md`:
         one full insert pass over the real 27,114 rows 265.4 ms with the index against 232.4 ms
         without, and 0.70 MiB of a 16.91 MiB file.
Cost     Two rules, two blind spots: a redundant index that is not a prefix is only caught when no
         plan names it, and a prefix duplicate is caught even if some plan names it — which is the
         case here. The corpus is statements that pass through `db.open_db`, so a SQL string
         executed on a connection built inside a helper is invisible to it, and that gap stays
         safe only while every reader keeps using the one door. Re-adding the index means writing
         the query the wider key cannot serve.
Commit   3c28e40 68609d4

### D-050 · 2026-10-17 · fix
Symptom  `skill_usage.plugin` is decided once, when the row is written, from the inventory
         snapshot of that moment — and nothing ever revisits it. Three installed plugins *are*
         their own skill, with the manifest at `<installPath>/SKILL.md` instead of under
         `skills/`, and the inventory could not see that shape until it learned to. So the eight
         `docx` calls (seven on 2026-10-07, one on 2026-10-12) carry `plugin` NULL while the
         inventory has carried `docx -> docx` ever since, and the Plugins panel counted none of
         them: `plugin_usage` held 4 rows, all `playwright-cli`.
Change   `attribute_plugin_skills` fills the owner on `skill_usage` rows that are still NULL,
         from `inventory` rows that are plugin-sourced, and `backfill_plugin_usage` runs it
         before deriving — so every sync is a catch-up, not a one-shot. Name-based like
         write-time attribution and NULL-only, so a row that already names an owner is never
         overwritten and a skill no plugin owns (the user's own library) stays NULL. Three
         cases in `test_sync.py` pin the fill, the untouched library row, and the no-overwrite
         rule.
Evidence L0 2026-10-17: `python3 -m unittest discover -s scripts/tests` 510 passed (507
         before the three cases). On the live database, before the change: 8 `docx` rows with
         `plugin` NULL and 4 `plugin_usage` rows; after one `cbut sync`: the same 8 rows carry
         `plugin='docx'` and `plugin_usage` holds 12 (8 `docx` + 4 `playwright-cli`), while the
         other 37 skill calls stay NULL. The pre-change snapshot `usage-20261017-124734-manual.db`
         still reads `(NULL, 8)` and 4, which is what makes the new statement the only thing that
         moved those rows.
Cost     Name-based, so a personal skill that reuses a plugin skill's name is attributed to the
         plugin — the same ambiguity write-time attribution already has (`_plugin_owner` looks
         the name up in a map that only plugin-owned skills populate). The fill cannot rewrite
         history twice: it only ever fills a NULL. A plugin removed after its calls were
         recorded keeps them, which is intended — the panel's job is what happened, not what is
         installed now.
Commit   2ca3f3e

### D-051 · 2026-10-17 · fix
Symptom  The Plugins tab ignored the date picker. `_fill_plugins` passed no bounds and
         `q_plugins` accepted none, so at a 7-day range the same `docx` calls read 8 there and 1
         under Skills, and moving the range on that tab changed nothing. The plugin list is
         all-time on purpose — the inventory carries no timestamps, and an installed plugin with
         no use in range must not vanish — but the *counts* had no reason to be.
Change   `q_plugins` takes `start_ts`/`end_ts` and puts them in the LEFT JOIN's ON clause, the
         same shape `q_skills`/`q_agents`/`q_mcp` already use: `uses` and `last_used` follow the
         window, the row set stays installed ∪ ever used, and `installed`/`ever_used` stay
         unwindowed so the state column describes the entity and not the range. `_fill_plugins`
         passes the pane's bounds. One case in `test_usage.py` pins windowed counts, a zero-use
         installed plugin still listed, and an unwindowed state.
         The query half alone did not move the tab: `tab-plugins` was still absent from
         `tab_range`, so `_bounds_for` returned `(None, None)`, `refresh_data` still called the
         fill without bounds, and the range Select and the 📅 button kept their Plugins special
         cases. The second half adds the 7d default, passes bounds from both call sites, drops
         the three special cases and lets the drill-down follow the same bounds. Test 39 now
         asserts the range control is visible there, the row survives at 0 outside the window
         and reads 1 at all-time. The picker helper waits for `is_mounted` before returning: a
         caller that assigns fields before `on_mount`/`_prefill` runs had them overwritten, which
         surfaced as a later test failing on a date it had just typed.
Evidence L0 2026-10-17: `python3 -m unittest discover -s scripts/tests` 511 passed (510 before
         the new case), and `test_tui` alone 121 passed. On the live database at a 7-day window
         `q_plugins` now returns `docx uses=1 last=2026-10-12 12:17` and nothing else, which is
         the same row the Skills tab shows for `docx`; unwindowed it still returns `docx 8` and
         `playwright-cli 4`. The old behaviour is the same query without bounds, so the two
         readings come from one statement. Cross-checked against the tabs: `docx` reads 1/1/1
         (Skills list, its drill-down, its summary) and 1/1/1 under Plugins at 7d, 8 everywhere
         at all-time; the Dashboard's `plugins` KPI is `COUNT(DISTINCT plugin)` (2 at all-time),
         a different quantity from the tab's `uses` (12) by design.
Cost     Two tabs can still disagree on *scope* (Skills lists skills, Plugins lists plugins), and
         the Plugins tab's `skills`/`agents`/`commands` columns remain inventory counts — how many
         the plugin ships, not how many were used — because the inventory has no per-item
         timestamps. Only `uses` and `last used` are windowed, which is exactly the pair the Skills
         tab windows under the name `calls` / `last used`. And the first commit recorded here
         described the fix as complete when the visible half was still missing; the entry was
         corrected rather than given a second number, because it is one defect, not two.
Commit   57b2627 58d2369

### D-052 · 2026-10-19 · chore

Symptom  README.md said "the 334 tests above" in its layout table while the command block on the
         same page said "Ran 511 tests"; AGENTS.md also cited the standard by bare filename.
Change   Both mentions now say 511; the bare `STANDARD.md` references carry the full
         `~/.agents/standards/STANDARD.md` path.
Evidence 2026-10-19: `python3 -m unittest discover -s scripts/tests` — 511 tests, OK.
Cost     None; wording only.
Commit   27b4016

### D-053 · 2026-10-20 · fix

Symptom  The Tools tab's `state` column read `in use` beside a 0-call row (the label was all-time
         while `calls` was windowed), a malformed transcript name containing a newline
         (`REPO<…\n<… parameter name=`) rendered as a broken name, and a narrow terminal dropped
         `state` before `last used`. Models and commands the host offers existed only after their
         first call, because only tools/agents were discovered from `product.json`.
Change   `db.entity_state` now takes the row's windowed call count, so the label agrees with the
         row; `db.clean_cell` flattens control characters for both the TUI and the headless
         reports; ingest rejects an unusable tool name as `invalid_tool_name` instead of storing
         it; the narrow-terminal plan protects column 0 and the state column (and both MCP key
         columns); `product.json` models/commands and `~/.codebuddy/models.json` customs are
         inventoried, and the Tokens tab lists an available model with 0 requests and NULL
         tokens. The six entity tabs are one registry (`ENTITY_TABS`), so a new kind no longer
         needs six separate edits.
Evidence L0 2026-10-20: `.venv/bin/python -m pytest scripts/tests -q` — 520 passed, 165
         subtests passed; each new case fails when its fix is undone (window-state, clean-cell row
         height, model-inventory zero row). L1 the same day against this machine's own database:
         `cbut tools` prints `in use` only on rows with calls, and the four malformed names render
         on one line.
Cost     Existing databases keep the four malformed rows already indexed; they render flush now,
         and `cbut sync --full` drops them. Route tiers (`default-model`, `fast-model`, …) appear
         in Tokens as available models with 0 requests because `product.json` lists them as
         models; that is the host's own catalogue, not a claim that each tier served a response.
Commit   8dc91c8

### D-054 · 2026-10-21 · fix

Symptom  Selecting **All time** on the Tokens tab raised `DuplicateKey`: the row key was the model
         name alone, and a response whose `requestModelId` was absent produced a second row with
         the same name as the built-in one. The list also carried rows nobody had called —
         `default-model` / `fast-model` route tiers from `product.json`, and `ark-code-latest` /
         `kilo-auto/free` route ids from `models.json` whose answers are recorded under the real
         model (`deepseek-v4-1-flash`). D-053's Cost said those rows were the catalogue; they were
         not measurements.
Change   Tokens is usage-only and keyed by `(model, provider)`: one row per pair that answered,
         no catalogue union. `models.json` is no longer inventoried as models; it is still read by
         `load_model_hosts` for the route label. A stored NULL provider is displayed as `Built-in`
         (`_PROVIDER_DISPLAY_EXPR`) in Tokens, Usage and the drill-down, without rewriting the
         column. The drill-down (`q_model_responses` / `q_model_summary`, `ModelResponsesScreen`)
         filters on the same pair, so two routes serving one name open different screens. The
         Tokens row key is built from the raw columns (`key_columns`), not from the display cells.
Evidence L0 2026-10-21: `.venv/bin/python -m unittest discover -s scripts/tests` — 523 tests, OK;
         each new case fails when its half is undone (duplicate-key/all-time, usage-only list,
         catalogue turnover keeping history, provider merge, drill-down scope). L1 the same day
         against this machine's own database: All time returns 11 rows with 11 unique keys (was a
         crash), the first Tokens row opens 200 Volcano Ark responses, and no inventory-only model
         appears. `cbut inventory` still lists the built-in catalogue.
Cost     An installed-but-unused model no longer appears in Tokens (the previous table's feature,
         withdrawn by request); `cbut inventory --kind model` is where that catalogue still lives.
         The NULL-to-`Built-in` merge is a display rule — the `provider` column keeps its NULL, and
         `q_call_detail` still shows it as unknown for that one call. The catalogue snapshot was
         refreshed in `3e08c20`, a separate chore for the installed CLI's tool rename.
Commit   87e985e

### D-055 · 2026-10-21 · fix

Symptom  The Tools tab listed `REPO`, `REPO_env`, `REPO_PLACEHOLDER` (10 calls) and
         `REPO_TOTAL_DOCS_PLACEHOLDER` in state `removed` while their `last used` was the previous
         day. Nothing by those names was ever installed: they came from subagent transcripts where
         the model emitted a template placeholder in place of a tool name, and Code Mode answered
         each with "is not a direct tool". D-053's guard caught only the whitespace shape
         (`REPO<…\n<… parameter name=`), so a placeholder without a newline passed it, and
         `removed` — which means "was installed, is gone" — was the only state such a row could
         ever read.
Change   Ingest withdraws a call when its result carries Code Mode's refusal *and* the installed
         catalogue does not hold the name. The refusal alone is not enough: the same message
         answers a real tool called from the wrong layer (Bash ×4, SendMessage ×4, REPL ×2, Read,
         Write, AskUserQuestion on this machine), and those rows stay. A withdrawn call is counted
         as `invalid_tool_name`, which already means "a call whose name cannot be a tool". The
         verdict only exists on the result record, so the row is deleted there rather than skipped
         at insert; a re-read repeats the same no-op withdrawal. `output`,
         `providerData.toolResult` and `toolResult.error` are registered in the format registry
         (read for the marker only, never stored) and the generated block is refreshed.
Evidence L0 2026-10-21: `.venv/bin/python -m unittest discover -s scripts/tests` — 524 tests, OK;
         the new case fails when the withdrawal is disabled
         (`['Bash', 'REPO_PLACEHOLDER'] != ['Bash']`). L1 the same day: two throwaway stores
         (`--db /tmp/…`) built from this machine's logs, one with the previous code and one with
         this change. `tool_calls` −13 and `unparsed.invalid_tool_name` +14; every other table
         identical, apart from `model_responses` −3 and `type:reasoning` −2, which the call-id diff
         showed to be three REPL calls the live session wrote between the two builds. The 14th
         placeholder row was already refused at insert by D-053's guard, and its result now bumps
         the same counter. L2 with the owner's go-ahead: the snapshot taken immediately before
         held 45,262 calls, 14 of them with a name outside the catalogue; after a targeted delete
         the live table has none, and every other table is untouched. `cbut sync --full` was not
         used — it rebuilds from the transcripts still on disk and would have dropped ~2,984 calls
         whose logs CodeBuddy has since pruned.
Cost     A refused call to a name the catalogue does not hold is now invisible in every panel and
         in the headless reports. A name that leaves the catalogue later keeps its rows (no
         refusal ever marked them), so `removed` still means what it says. The filter reads
         CodeBuddy's English message text: if the CLI rewords it the rows come back, silently,
         because nothing fails — the two-store L1 comparison above is the check to re-run.
Commit   583594b

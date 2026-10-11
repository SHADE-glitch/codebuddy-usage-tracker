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

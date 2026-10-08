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

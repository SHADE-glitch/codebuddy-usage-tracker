# CHANGELOG — codebuddy-usage-tracker

Original project, **no upstream**: nothing here is a deviation from somebody else's code, so this
record is not a divergence list. It records the repairs, the performance work, the drift guards and
the withdrawals — the things an upgrade of CodeBuddy's log format, of Textual, or of the transcript
schema can invalidate.

Coverage: f62eb1f..HEAD
Check with `python3 -m unittest scripts.tests.test_record_coverage`. Entries are `D-###`, monotonic,
never reused. An entry states what was true **as of its commit**, not current state, and aggregate
counts are printed by the check, never copied into this file.

> **Why the record is so short.** This repository is one day old (all commits are dated 2026-10-07)
> and every production commit so far carries a `feat` subject, which is out of scope by class: a
> feature is the product, not a droppable deviation, and features are documented in the READMEs. The
> one covered commit below is a `docs:` commit that also corrected stale CLI help — the subject
> prefix does not decide coverage, the touched paths do.

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

# Maintenance notes

Everything here exists because **CodeBuddy upgrading its logs is the main maintenance risk** for
this tool: a renamed field or record type produces no error — the panels empty out, `cbut sync`
prints "done", and CI stays green.

| Document | Read it when |
|---|---|
| [`codebuddy-format.md`](codebuddy-format.md) | CodeBuddy shipped a new version, or a panel shows nothing it used to show |
| [`compatibility.md`](compatibility.md) | Choosing what to claim is supported, or a version string needs updating |
| [`measurements.md`](measurements.md) | Judging a size or performance change, or comparing against the recorded baseline |

Three standing checks cover this area — all part of `python3 -m unittest discover -s scripts/tests`:

- `test_format_registry.py` — the parser and the registry in `scripts/cbut-sync.py` agree, in both
  directions (nothing read unregistered, nothing registered unread, no inlined layout literal).
- `test_maintenance_docs.py` — the generated block in `codebuddy-format.md` still equals
  `cbut format` output. Edit the registry, run `cbut format --write`.
- `test_privacy.py` — nothing is read that could leave the machine, and no free-text value lands in
  any column of any table.

# MAINTENANCE.md — codebuddy-usage-tracker

Procedure file. This is a **router**: the long-form maintenance assets live under
[`docs/maintenance/`](docs/maintenance/README.md) (committed), and the rules for working in this
repository live in [`AGENTS.md`](AGENTS.md). Do not duplicate their tables here — a duplicated
fact is the thing that drifts.

The one maintenance risk that matters: **CodeBuddy changing its log format**. A renamed field
reads as `NULL`, a renamed record type is silently unclaimed, the panels empty out, `cbut sync`
prints "done", and CI stays green. The checklist is in
[`docs/maintenance/codebuddy-format.md`](docs/maintenance/codebuddy-format.md).

## Verification tiers
- **L0** — `python3 -m unittest discover -s scripts/tests` (temp database, no host).
- **L1** — a throwaway store outside the repository (`--db /tmp/…`), fed by real or copied logs.
- **L2** — the real `~/.codebuddy` logs and the real `usage.db`; needs the owner's go-ahead.

## CI
`.github/workflows/ci.yml` runs the offline layer on a Python 3.11 / 3.14 matrix. See `AGENTS.md` § CI.

## Assets
| Document | Read it when |
|---|---|
| [`codebuddy-format.md`](docs/maintenance/codebuddy-format.md) | CodeBuddy shipped a new version, or a panel shows nothing it used to show |
| [`compatibility.md`](docs/maintenance/compatibility.md) | Choosing what to claim is supported, or a version string needs updating |
| [`measurements.md`](docs/maintenance/measurements.md) | Judging a size or performance change |

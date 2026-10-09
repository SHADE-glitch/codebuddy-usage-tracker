# Size and performance: how to measure it the same way twice

Numbers in here are a **baseline to diff against**, not a target. They came from the commands below on
**2026-10-09**, on the real database, opened read-only. Re-run the commands before comparing anything:
the log set grows daily and the shape of the data moves the cost.

**Every figure is rounded on purpose.** The row counts and byte sizes of this index are a picture of one
person's activity level, and this repository is public — so the baseline keeps one or two significant
digits: enough to notice a 2× drift, not enough to read a schedule. The exact numbers are one command
away, on the machine that owns them.

Two honesty rules: every measurement is warm-cache and taken in sequence (a cold start is slower,
especially the first query), and `cbut` has no server — the only performance that matters is TUI
refresh latency and one-off re-index time. And a measurement is worth one order of magnitude: re-running
the query table on the same database ten minutes apart moved `overview()` from ≈ 27 ms to ≈ 18 ms, so
**only a 5× change is a signal here**, not a 30% one.

## The database

```bash
DB=~/.local/share/codebuddy-usage-tracker/usage.db
./.venv/bin/python - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for k in ("page_size", "page_count", "freelist_count", "journal_mode",
          "synchronous", "auto_vacuum"):
    print(f"{k:16}", c.execute(f"PRAGMA {k}").fetchone()[0])
print("integrity      ", c.execute("PRAGMA quick_check").fetchone()[0])
print("schema_version ", c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
PY
```

| | 2026-10-09 |
|---|---|
| file size | ≈ 15 MiB |
| page size / count | 4 KiB × ≈ 3.8k pages |
| freelist | ≈ 20 pages (tens of KiB reclaimable by `VACUUM`) |
| journal / synchronous | `wal` / `2` (FULL) |
| auto_vacuum | `0` (none) |
| `PRAGMA user_version` | `0` — unused by design; the schema version lives in `meta` |
| `quick_check` | `ok` |
| rows, all tables | ≈ 44k (`tool_calls` ≈ 25k · `model_responses` ≈ 18k · `sessions` ≈ 500 · `sync_state` ≈ 500 · every other table ≈ 300 or less) |
| span | ≈ 3 weeks of active days → **≈ 2.3k rows/day** |
| source logs indexed | `~/.codebuddy/projects` (≈ 500 transcripts) |

## Where the bytes go, and which indexes earn their keep

```bash
./.venv/bin/python - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for name, size in c.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 12"):
    print(f"{size:>12,}  {name}")
print("indexes total:", c.execute("SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'idx%'").fetchone()[0], "B")
PY
```

| 2026-10-09 | share of a ≈ 15 MiB file |
|---|---|
| `tool_calls` | ≈ 4 MiB |
| `model_responses` | ≈ 3.5 MiB |
| **`idx_tool_session`** | **≈ 1.3 MiB** |
| `sqlite_autoindex_tool_calls_1` | ≈ 1.1 MiB |
| **`idx_model_resp_session`** | **≈ 0.9 MiB** |
| `sqlite_autoindex_model_responses_1` | ≈ 0.8 MiB |
| `idx_model_resp_model_ts` | ≈ 0.7 MiB |
| `idx_model_resp_model` | ≈ 0.5 MiB |
| all explicit `idx_*` | ≈ 5 MiB — **about a third of the file** |

**Resolved as of schema v6**: the two bolded indexes existed to serve `WHERE session_id = ?`, and
**no query in `cbut_db.py` has a
`session_id` predicate** (a handful of its query strings mention the column at all, and only as a
projection, a `GROUP BY`, or an insert target), so they were paid for on every insert and never used
for a read. They are now dropped by a migration; the table above stays as the "before". Re-running the
dbstat query on a migrated database should show ≈ 2.2 MiB less in `idx_*` — and if it does not, the
migration did not run. The justification is itself guarded now
(`test_no_query_filters_by_session_id` reads the real source), because this is exactly the kind of
claim that goes stale: build a session-keyed screen and that test fails, telling you the index is due
back. The same check is one command
— it prints the ratio, and today it prints `0 of 81`:

```bash
./.venv/bin/python -c "
import re, pathlib
src = pathlib.Path('scripts/cbut_db.py').read_text()
q = re.findall(r'\"((?:[^\"]|\\\\.)*?(?:SELECT|WITH)[^\"]*)\"', src, re.I)
print(sum(1 for s in q if re.search(r'session_id\s*(?:=|IN|IS|LIKE)', s, re.I)), 'of', len(q), 'queries filter by session_id')"
```

Dropping an index is a schema change: it needed a `D-###` entry, and it only went in once the
session-detail screens (the obvious future consumer) were **ruled out** rather than merely unbuilt —
`AGENTS.md` fixes every panel's key as the entity *name*, and windows shrink counts but never the list,
so a session-keyed read contradicts the panel model instead of extending it. If that decision is ever
reversed, the index is one `CREATE INDEX` away and the guard above says so before you ship it.

## Query cost

```bash
./.venv/bin/python - "$DB" <<'PY'
import sqlite3, sys, time, statistics
sys.path.insert(0, "scripts"); import cbut_db as db
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); c.row_factory = sqlite3.Row
now = int(time.time() * 1000); s, e = db.window_bounds("30d", now)
def timeit(fn, n=5):
    t = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); t.append((time.perf_counter() - t0) * 1000)
    return f"{min(t):5.1f} / {statistics.median(t):5.1f} ms"
print("overview (all-time)      ", timeit(lambda: db.overview(c)))
print("q_tools (window, top 5)  ", timeit(lambda: db.q_tools(c, limit=5, start_ts=s, end_ts=e)))
print("q_usage_daily (30d)      ", timeit(lambda: db.q_usage_daily(c, s, e)))
print("q_model_tokens (30d)     ", timeit(lambda: db.q_model_tokens(c, start_ts=s, end_ts=e)))
print("q_usage_request_logs     ", timeit(lambda: db.q_usage_request_logs(c, s, e, limit=100)))
print("q_usage_kpi (30d)        ", timeit(lambda: db.q_usage_kpi(c, s, e)))
PY
```

| query | min / median (n=5), 2026-10-09 | note |
|---|---|---|
| `overview()` all-time | ≈ 16 / 18 ms | ~21 round-trips in one status line |
| `q_tools()` window, top 5 | ≈ 24 ms | windowed is as expensive as unwindowed — the same ≈ 25k rows are scanned either way |
| `q_usage_daily()` 30d | **≈ 41 ms**, the slowest one | `strftime` per row, so no index helps |
| `q_model_tokens()` 30d | ≈ 16 / 27 ms | the widest run-to-run spread in the set |
| `q_usage_request_logs()` 100 | ≈ 0.5 ms | first page only |
| `q_usage_kpi()` 30d | ≈ 1 ms | |

Order of magnitude is the point: every dashboard query costs tens of milliseconds, so a refresh is
imperceptible and latency work is not the next thing to do. If a re-run puts any of these into the
hundreds of milliseconds, that is the drift worth chasing.

A full Dashboard refresh is ~40 statements; the 5-second timer re-queries **only the visible tab**,
and hidden tabs refresh when opened — which is why these costs are tolerable rather than urgent.

## Re-index time

```bash
time ./.venv/bin/python scripts/cbut-sync.py --db /tmp/reindex.db   # L1: throwaway store
```

≈ 500 transcripts (≈ half a GiB of `projects/` on disk) into a fresh ≈ 15 MiB database: **~7 s**, once.
An incremental `cbut sync` with nothing new is dominated by the inventory scan and takes well under
that; the TUI runs one every 30 s in a worker thread.

## Growth, and what it implies

Roughly **0.8 MiB/day**, so **≈ 280 MiB/year** if the rate holds — that is the whole file divided by
the span the data covers, an approximation that treats the database as if it started empty three weeks
ago, which it roughly did. The transcripts it reads from are a separate, already-paid-for half GiB and
growing. Nothing prunes today: no retention window, no `VACUUM`, `auto_vacuum=0`, and rows deleted from
the transcripts are never reclaimed from the index. The decision this feeds is
**"is a few hundred MiB a year acceptable, and if not, what ages out?"** — answered in `AGENTS.md`
terms, that is a data-retention policy question for the owner, not a silent default. Measure again
before deciding; the numbers above are the "before".

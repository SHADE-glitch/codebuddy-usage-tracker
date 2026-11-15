#!/usr/bin/env python3
"""Regenerate docs/screenshots/demo.gif: synthetic index -> TUI frames -> GIF.

Nothing here reads your real index. The script points HOME at a throwaway
directory, builds this repo's own schema there (`<key>_db.TABLES_SQL`), fills it
with made-up rows, drives the TUI with Textual's `run_test`, and assembles the
frames.

Needs, on top of the tracker's own deps: Pillow, and a headless Chrome or
Chromium on PATH for the SVG -> PNG step (`--chrome PATH` to point elsewhere).

    python3 scripts/demo_gif.py                 # write docs/screenshots/demo.gif
    python3 scripts/demo_gif.py --keep-frames   # leave the PNGs under build/demo-frames
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
OUT_DEFAULT = ROOT / "docs" / "screenshots" / "demo.gif"
COLS, ROWS = 132, 36
DAY = 86_400_000

MODELS = ["claude-sonnet-4.5", "claude-opus-4.1", "gpt-5-codex", "gemini-2.5-pro",
          "qwen3-coder", "kimi-k2"]
PROVIDERS = ["anthropic", "openai", "google", "qwen"]
PROJECTS = ["/home/dev/platform-api", "/home/dev/shell-extensions",
            "/home/dev/usage-trackers", "/home/dev/notes", "/home/dev/infra"]
SKILLS = ["code-review", "tdd", "writing-plans", "design-md", "sql-review",
          "diagnosing-bugs", "gh", "pr", "performance-optimization", "spring-boot",
          "postgresql-optimization", "domain-modeling", "branch-workflow",
          "open-source-conventions", "app-conventions", "personal-conventions",
          "repo-fleet-maintenance", "git-release", "grilling", "writing-for-agents",
          "playwright-cli", "session-mining", "repo-about-sync", "fork-maintenance"]
TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "code_search", "Task"]
SESSIONS = [f"sess{i:02d}" for i in range(1, 15)]
KINDS = ["skill", "plugin", "mcp", "agent"]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def value(col: str, table: str, i: int, decl: str, rng, now: int):
    """One plausible value for one column, by name first and type second."""
    c = col.lower()
    if c in ("session_id", "session", "sessionid"):
        return rng.choice(SESSIONS)
    if c == "model" or c.endswith("_model"):
        return rng.choice(MODELS)
    if c in ("provider", "vendor"):
        return rng.choice(PROVIDERS)
    if c in ("project", "directory", "cwd", "path", "workdir", "workspace"):
        return rng.choice(PROJECTS)
    if c in ("skill", "skill_name", "tool", "tool_name", "name", "entity", "custom"):
        return rng.choice(SKILLS if "skill" in c or c in ("name", "entity", "custom")
                          else TOOLS)
    if c == "kind":
        return rng.choice(KINDS)
    if c == "category":
        return "builtin"
    if "plugin" in c:
        return rng.choice(["core", "dev-tools", "shell-utils", "review-pack"])
    if c in ("state", "install_state", "inventory_state"):
        return rng.choices(["installed", "removed"], [9, 1])[0]
    if c in ("source", "origin"):
        return rng.choice(["marketplace", "local", "builtin"])
    if c in ("owner", "author"):
        return rng.choice(["anthropics", "community", "in-house"])
    if c in ("status", "outcome", "result"):
        return rng.choices(["completed", "error"], [19, 1])[0]
    if c.endswith("_id") or c == "id":
        return f"{table[:4]}{i:05d}"
    if c in ("ts", "timestamp", "started_at", "ended_at", "created_at", "updated_at",
             "last_used", "last_seen", "time", "date", "when"):
        if rng.random() < 0.35:
            return now - rng.randint(0, 20 * 3600 * 1000)
        return now - rng.randint(1, 6) * DAY - rng.randint(0, DAY - 1)
    if "duration" in c or c.endswith("_ms"):
        return rng.randint(60, 9000)
    if "cost" in c or "credit" in c:
        return round(rng.uniform(0.001, 0.9), 4)
    if "token" in c or c in ("chars", "bytes", "count", "size"):
        return rng.randint(200, 90_000)
    if c in ("title", "subject", "topic", "summary", "text", "prompt", "message"):
        return rng.choice(["refactor the sync path", "review the migration",
                           "debug the flaky test", "summarise the release notes"])
    d = (decl or "").upper()
    if "INT" in d:
        return rng.randint(0, 1000)
    if "REAL" in d or "FLOA" in d or "NUM" in d or "DEC" in d:
        return round(rng.uniform(0, 1), 3)
    return rng.choice(SESSIONS)


def seed(db_path: Path, scripts: list[str], rng, now: int) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(db_path)
    for script in scripts:
        if script:
            conn.executescript(script)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in sorted(tables, key=lambda t: ("session" not in t, t)):
        cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if not cols:
            continue
        n = 240 if any(k in table for k in ("call", "response", "message", "event")) else 90
        if any(k in table for k in ("setting", "meta", "config", "sync_state", "backup")):
            n = 3
        sql = (f'INSERT INTO "{table}" ('
               + ",".join(f'"{c}"' for c, _ in cols)
               + f') VALUES ({",".join("?" * len(cols))})')
        for i in range(n):
            row = tuple(value(c, table, i, t, rng, now) for c, t in cols)
            for bump in range(6):
                try:
                    conn.execute(sql, row)
                    break
                except sqlite3.IntegrityError:
                    row = tuple(f"{v}{bump}" if isinstance(v, str) and bump else v
                                for v in row)
                except sqlite3.Error:
                    break
    conn.commit()
    conn.close()


def seed_board(home: Path, rng, now: int) -> None:
    """all-usage-trackers reads three indexes; its schemas live in the tests."""
    fixtures = load(SCRIPT_DIR / "tests" / "test_aut.py", "demo_schemas")
    for db_name, ddl, entities in (
            ("codebuddy-usage-tracker", fixtures.CBUT_DDL,
             fixtures.ENTITY_DDL["codebuddy-usage-tracker"]),
            ("opencode-usage-tracker", fixtures.OCUT_DDL,
             fixtures.ENTITY_DDL["opencode-usage-tracker"]),
            ("qoder-cn-usage-tracker", fixtures.QCUT_DDL,
             fixtures.ENTITY_DDL["qoder-cn-usage-tracker"])):
        seed(home / ".local" / "share" / db_name / "usage.db", [ddl, entities],
             rng, now)


def build_app():
    found = sorted(SCRIPT_DIR.glob("*-tui.py")) or sorted(SCRIPT_DIR.glob("*_app.py"))
    tui = found[0]
    mod = load(tui, "demo_tui")
    from textual.app import App
    classes = [o for _, o in inspect.getmembers(mod, inspect.isclass)
               if issubclass(o, App) and o is not App and o.__module__ == mod.__name__]
    classes.sort(key=lambda c: len(inspect.getsource(c)), reverse=True)
    app_cls = classes[0]
    kwargs = {}
    for prm in list(inspect.signature(app_cls.__init__).parameters.values())[1:]:
        if prm.name in ("db_path", "path", "db"):
            kwargs[prm.name] = str(Path(sys.modules["demo_db"].DB_PATH))
    return app_cls(**kwargs)


async def record(app, frames: Path) -> list[Path]:
    from textual.widgets import TabbedContent, TabPane
    shots: list[Path] = []

    def snap(tag: str) -> None:
        p = frames / f"{len(shots):02d}-{tag}.svg"
        app.save_screenshot(str(p))
        shots.append(p)

    async with app.run_test(size=(COLS, ROWS)) as pilot:
        await pilot.pause(1.5)
        snap("dashboard")
        try:
            tabs = app.query_one(TabbedContent)
            panes = [p.id for p in tabs.query(TabPane)]
        except Exception:                                  # noqa: BLE001
            panes = []
        for pid in panes[1:]:
            tabs.active = pid
            await pilot.pause(0.45)
            snap(str(pid))
        if panes:
            tabs.active = panes[min(2, len(panes) - 1)]
            await pilot.pause(0.4)
            try:
                await pilot.press("enter")
                await pilot.pause(0.6)
                snap("detail")
                await pilot.press("escape")
                await pilot.pause(0.5)
                snap("back")
            except Exception:                              # noqa: BLE001
                pass
    return shots


def rasterise(shots: list[Path], chrome: str) -> list[Path]:
    pngs = []
    for svg in shots:
        png = svg.with_suffix(".png")
        subprocess.run([chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--window-size=1120,760",
                        f"--screenshot={png}", svg.as_uri()],
                       check=True, capture_output=True)
        pngs.append(png)
    return pngs


def assemble(pngs: list[Path], out: Path) -> None:
    from PIL import Image, ImageChops
    frames = []
    for png in pngs:
        im = Image.open(png).convert("RGB")
        white = Image.new("RGB", im.size, (255, 255, 255))
        box = (ImageChops.difference(im, white).convert("L")
               .point(lambda v: 255 if v > 10 else 0).getbbox())
        if box:
            l, t, r, b = box
            im = im.crop((max(0, l - 6), max(0, t - 6),
                          min(im.width, r + 6), min(im.height, b + 6)))
        frames.append(im)
    width = 1100
    height = int(frames[0].height * width / frames[0].width)
    frames = [f.resize((width, height), Image.LANCZOS) for f in frames]
    durations = [1400] + [1100] * (len(frames) - 2) + [1700]
    palette = [f.convert("P", palette=Image.ADAPTIVE, colors=180) for f in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    palette[0].save(out, save_all=True, append_images=palette[1:],
                    duration=durations, loop=0, optimize=True)
    try:
        label = out.relative_to(ROOT)
    except ValueError:
        label = out
    print(f"{label}  {width}x{height}  {len(palette)} frames  "
          f"{out.stat().st_size / 1024:.0f} KB  {sum(durations) / 1000:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--chrome", default=shutil.which("google-chrome")
                    or shutil.which("chromium") or shutil.which("chromium-browser"))
    ap.add_argument("--keep-frames", action="store_true")
    args = ap.parse_args()
    if not args.chrome:
        print("no Chrome/Chromium on PATH; pass --chrome", file=sys.stderr)
        return 2

    home = Path(tempfile.mkdtemp(prefix="demo-home-"))
    os.environ["HOME"] = str(home)         # before the db module is imported
    try:
        rng = random.Random(7)
        now = int(time.time() * 1000)
        db_mods = sorted(SCRIPT_DIR.glob("*_db.py"))
        db_mod = load(db_mods[0], "demo_db") if db_mods else None
        if db_mod is not None and hasattr(db_mod, "DB_PATH"):
            seed(Path(db_mod.DB_PATH),
                 [getattr(db_mod, k, None) for k in ("TABLES_SQL", "VIEWS_SQL")],
                 rng, now)
        else:
            seed_board(home, rng, now)

        frames_dir = Path(tempfile.mkdtemp(prefix="demo-frames-"))
        import asyncio
        shots = asyncio.run(record(build_app(), frames_dir))
        pngs = rasterise(shots, args.chrome)
        assemble(pngs, args.out)
        if args.keep_frames:
            keep = ROOT / "build" / "demo-frames"
            keep.mkdir(parents=True, exist_ok=True)
            for p in pngs:
                shutil.copy2(p, keep / p.name)
            print("frames kept in", keep.relative_to(ROOT))
    finally:
        os.environ["HOME"] = os.path.expanduser("~")
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

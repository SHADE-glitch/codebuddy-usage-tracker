#!/usr/bin/env python3
"""Regenerate `fixtures/product.json.snapshot` from the installed CodeBuddy CLI.

The snapshot exists so the drift guard has something to compare against on a
machine with no CodeBuddy installed — CI, for one. It is *not* a copy of
product.json: only the two keys `cbut-sync` reads are kept, so a fixture cannot
quietly become a second description of an upstream file nobody here maintains.

Run it after a CodeBuddy upgrade, in the same change that re-reads the surface:

    python3 scripts/tests/product_snapshot.py

It refuses rather than writing when no package can be resolved: an empty
snapshot would make the guard pass on nothing, which is the same failure as a
fixture that was emptied by hand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cbut_db as db  # noqa: E402

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "product.json.snapshot"

HEADER = (
    "Trimmed copy of the installed CodeBuddy package product.json: only the two "
    "keys cbut-sync reads. Regenerate with scripts/tests/product_snapshot.py "
    "after a CLI upgrade."
)


def build(raw: dict) -> dict:
    sync = db.load_sync()
    return {
        "_comment": HEADER,
        sync.PRODUCT_TOOLS_FIELD: sorted(set(sync._catalog_names(raw.get(sync.PRODUCT_TOOLS_FIELD)))),
        sync.PRODUCT_AGENTS_FIELD: sorted(set(sync._catalog_names(raw.get(sync.PRODUCT_AGENTS_FIELD)))),
    }


def main() -> int:
    sync = db.load_sync()
    path = sync.resolve_product_json()
    if path is None:
        print("no CodeBuddy package resolved — refusing to write an empty snapshot",
              file=sys.stderr)
        return 1
    raw = sync._load_json(path)
    if not isinstance(raw, dict):
        print(f"{path} is not a JSON object — refusing to snapshot it", file=sys.stderr)
        return 1
    snapshot = build(raw)
    for key in (sync.PRODUCT_TOOLS_FIELD, sync.PRODUCT_AGENTS_FIELD):
        if not snapshot[key]:
            print(f"{path} carries no {key} — refusing to snapshot an empty list",
                  file=sys.stderr)
            return 1
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"updated   {SNAPSHOT}  ({len(snapshot[sync.PRODUCT_TOOLS_FIELD])} tools, "
          f"{len(snapshot[sync.PRODUCT_AGENTS_FIELD])} agents)  from {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
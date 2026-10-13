"""_catalog.py — the builtin catalogue is pinned for the whole test run.

``scan_inventory`` resolves the CodeBuddy package's ``product.json`` by walking
up from the ``codebuddy`` executable on ``PATH``. That is right in production and
wrong in a test: the result would be whatever CLI the developer happens to have
installed, so a suite could be green on one machine and red on the next upgrade
while proving nothing about this repository.

So every test that scans inventory runs against
``fixtures/product.json.snapshot`` instead, pinned through ``CBUT_PRODUCT_JSON``
— the same override a user has. ``setUpModule``/``tearDownModule`` do it for the
whole module rather than per test: a per-test pin is a convention the next test
forgets, and forgetting it does not fail, it just measures the host.

The drift guard in ``test_builtin_catalog`` is the one deliberate exception; it
reads the real package to say whether the snapshot is still current.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "product.json.snapshot"


class catalog_pinned:
    """Pin the builtin catalogue to the snapshot fixture for a block of tests."""

    def __enter__(self):
        self._patch = mock.patch.dict(
            os.environ, {"CBUT_PRODUCT_JSON": str(SNAPSHOT)})
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

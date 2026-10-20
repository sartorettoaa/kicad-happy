#!/usr/bin/env python3
"""Unit tests for toolkit/deps.py — optional dependency detection contract.

Run with:
    python3 skills/kicad/toolkit/test_deps.py
    python3 -m unittest skills/kicad/toolkit/test_deps.py
"""

import os
import subprocess
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from toolkit import deps  # noqa: E402


class TestDeps(unittest.TestCase):
    def test_has_shapely_returns_bool_and_is_cached(self):
        first = deps.has_shapely()
        self.assertIsInstance(first, bool)
        self.assertEqual(deps.has_shapely(), first)  # cached, stable across calls

    def test_shapely_geometry_matches_has_shapely(self):
        if deps.has_shapely():
            mod = deps.shapely_geometry()
            self.assertTrue(hasattr(mod, "Polygon"))
        else:
            with self.assertRaises(ImportError):
                deps.shapely_geometry()

    def test_no_module_level_shapely_import(self):
        # The whole point of lazy detection: importing toolkit.deps (alone,
        # in a fresh interpreter) must not pull shapely into sys.modules.
        code = (
            f"import sys; sys.path.insert(0, {PARENT_DIR!r}); "
            "import toolkit.deps; "
            "print('shapely' in sys.modules)"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()

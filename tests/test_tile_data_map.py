"""Validate dashboard-tiles.yaml :: tiles[].data_keys per Q10 (2026-05-03).

Every tile in dashboard-tiles.yaml must declare a `data_keys` list (possibly
empty). Track D's per-user filter reads this map at runtime; a missing
`data_keys` field would silently strip all data for that user. Fail loudly here.
"""
import unittest
from pathlib import Path

import yaml


TILES_PATHS = [
    Path.home() / "dashboards" / "config" / "dashboard-tiles.yaml",
]


def _load_tiles():
    for p in TILES_PATHS:
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            return data.get("tiles", []), p
    raise FileNotFoundError(f"no dashboard-tiles.yaml at any of: {TILES_PATHS}")


class TileDataKeys(unittest.TestCase):
    def setUp(self):
        self.tiles, self.path = _load_tiles()

    def test_tiles_yaml_has_tiles(self):
        self.assertGreater(len(self.tiles), 0, f"no tiles in {self.path}")

    def test_every_tile_has_id(self):
        for t in self.tiles:
            self.assertIn("id", t, f"tile missing id: {t}")
            self.assertTrue(t["id"], f"tile id is falsy: {t}")

    def test_every_tile_declares_data_keys(self):
        missing = [t.get("id") for t in self.tiles if "data_keys" not in t]
        self.assertEqual(
            missing, [],
            f"tiles missing required `data_keys` field in {self.path}: {missing}. "
            "Per Q10/Track D, every tile must declare its data-key list (use [] for none)."
        )

    def test_data_keys_is_list_of_strings(self):
        for t in self.tiles:
            tid = t.get("id")
            dk = t.get("data_keys")
            self.assertIsInstance(dk, list, f"tile {tid!r}: data_keys must be a list, got {type(dk).__name__}")
            for k in dk:
                self.assertIsInstance(k, str, f"tile {tid!r}: data_keys entry {k!r} must be a string")
                self.assertTrue(k, f"tile {tid!r}: data_keys contains empty string")

    def test_no_duplicate_keys_within_a_tile(self):
        for t in self.tiles:
            tid = t.get("id")
            dk = t.get("data_keys") or []
            self.assertEqual(len(dk), len(set(dk)), f"tile {tid!r}: duplicate entries in data_keys: {dk}")


if __name__ == "__main__":
    unittest.main()

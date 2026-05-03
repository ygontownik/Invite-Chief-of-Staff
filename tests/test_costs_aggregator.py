"""
Unit tests for ~/cos-pipeline/next/track-G/costs_aggregator.py.

Run:
    python3 ~/cos-pipeline/tests/test_costs_aggregator.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TRACK_G = Path(os.path.expanduser("~/cos-pipeline/next/track-G/costs_aggregator.py"))
_spec = importlib.util.spec_from_file_location("costs_aggregator", _TRACK_G)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["costs_aggregator"] = _mod
_spec.loader.exec_module(_mod)


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc).isoformat()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class CostsAggregatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.tenant = "testfirm"
        self.costs = self.root / f"data-{self.tenant}" / "costs"
        # Patch the module's pipeline root to the temp dir.
        self._patch = mock.patch.object(_mod, "_PIPELINE_ROOT", self.root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_missing_dir_returns_empty(self):
        agg = _mod.aggregate_costs("does-not-exist", lookback_days=30)
        self.assertEqual(agg["total_usd"], 0.0)
        self.assertEqual(agg["by_model"], {})
        self.assertEqual(agg["by_pass"], {})
        self.assertEqual(agg["by_routine"], {})
        self.assertEqual(agg["daily"], [])
        self.assertEqual(agg["jsonl_files_seen"], 0)
        self.assertEqual(agg["lines_read"], 0)
        self.assertEqual(agg["lookback_days"], 30)
        # format_for_tile must accept the empty shape
        tile = _mod.format_for_tile(agg)
        self.assertEqual(tile["totalUsd"], 0.0)
        self.assertEqual(tile["topModels"], [])

    def test_totals_and_rollups(self):
        today = date.today()
        d_today = today.isoformat()
        d_yest = (today - timedelta(days=1)).isoformat()
        _write_jsonl(self.costs / f"{d_today}.jsonl", [
            {"ts": _iso(today), "task_type": "pass2_pipeline_analyst",
             "model": "claude-opus-4-7", "input_tokens": 1000, "output_tokens": 500,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 0.50},
            {"ts": _iso(today), "task_type": "pass1_source_scanner",
             "model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 500,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 0.10},
            # Subscription mode: 0 dollars but should still be counted in line count.
            {"ts": _iso(today), "task_type": "pass3_ic_memo",
             "model": "claude-sonnet-4-6", "input_tokens": 800, "output_tokens": 200,
             "cached_input_tokens": 600, "mode": "subscription", "est_usd": 0.0},
        ])
        _write_jsonl(self.costs / f"{d_yest}.jsonl", [
            {"ts": _iso(today - timedelta(days=1)), "task_type": "pass2_pipeline_analyst",
             "model": "claude-opus-4-7", "input_tokens": 2000, "output_tokens": 100,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 0.40},
        ])

        agg = _mod.aggregate_costs(self.tenant, lookback_days=30)
        self.assertAlmostEqual(agg["total_usd"], 1.00, places=4)
        self.assertEqual(agg["jsonl_files_seen"], 2)
        self.assertEqual(agg["lines_read"], 4)

        # by_model
        self.assertAlmostEqual(agg["by_model"]["claude-opus-4-7"], 0.90, places=4)
        self.assertAlmostEqual(agg["by_model"]["claude-sonnet-4-6"], 0.10, places=4)

        # by_pass uses task_type fallback
        self.assertAlmostEqual(agg["by_pass"]["pass2_pipeline_analyst"], 0.90, places=4)
        self.assertAlmostEqual(agg["by_pass"]["pass1_source_scanner"], 0.10, places=4)
        self.assertIn("pass3_ic_memo", agg["by_pass"])  # zero-cost still keyed

        # daily: two entries, sorted ascending
        self.assertEqual(len(agg["daily"]), 2)
        self.assertEqual(agg["daily"][0]["date"], d_yest)
        self.assertEqual(agg["daily"][1]["date"], d_today)

        # tile-shape sanity
        tile = _mod.format_for_tile(agg, top_n=2)
        self.assertEqual(tile["summary"], "$1.00 last 30d")
        self.assertEqual(len(tile["topModels"]), 2)
        self.assertEqual(tile["topModels"][0][0], "claude-opus-4-7")

    def test_lookback_window_excludes_old_files(self):
        today = date.today()
        old_day = today - timedelta(days=60)
        recent_day = today - timedelta(days=2)
        _write_jsonl(self.costs / f"{old_day.isoformat()}.jsonl", [
            {"ts": _iso(old_day), "task_type": "pass1_source_scanner",
             "model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 99.99},
        ])
        _write_jsonl(self.costs / f"{recent_day.isoformat()}.jsonl", [
            {"ts": _iso(recent_day), "task_type": "pass1_source_scanner",
             "model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 0.25},
        ])
        agg = _mod.aggregate_costs(self.tenant, lookback_days=30)
        self.assertAlmostEqual(agg["total_usd"], 0.25, places=4)
        self.assertEqual(agg["jsonl_files_seen"], 1)  # old file pre-filtered

    def test_explicit_pass_and_routine_fields(self):
        # Forward-compat: if router ever emits pass_name / routine_name, use them.
        today = date.today()
        _write_jsonl(self.costs / f"{today.isoformat()}.jsonl", [
            {"ts": _iso(today), "task_type": "pass2_pipeline_analyst",
             "pass_name": "Deal Ideation", "routine_name": "deal_pipeline_weekly",
             "model": "claude-opus-4-7", "input_tokens": 1, "output_tokens": 1,
             "cached_input_tokens": 0, "mode": "api", "est_usd": 1.23},
        ])
        agg = _mod.aggregate_costs(self.tenant, lookback_days=30)
        self.assertIn("Deal Ideation", agg["by_pass"])
        self.assertIn("deal_pipeline_weekly", agg["by_routine"])
        self.assertNotIn("pass2_pipeline_analyst", agg["by_pass"])

    def test_malformed_lines_skipped(self):
        today = date.today()
        path = self.costs / f"{today.isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            fh.write("not-json\n")
            fh.write("\n")
            fh.write(json.dumps({
                "ts": _iso(today), "task_type": "x", "model": "m",
                "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                "mode": "api", "est_usd": 0.05,
            }) + "\n")
        agg = _mod.aggregate_costs(self.tenant, lookback_days=30)
        self.assertAlmostEqual(agg["total_usd"], 0.05, places=4)
        self.assertEqual(agg["lines_read"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

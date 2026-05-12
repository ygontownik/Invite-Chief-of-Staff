"""test_costs_subscription.py — tests for costs.py.next subscription extension.

Loads the .next draft via SourceFileLoader (resilient to cutover —
falls back to live `costs.py` if `.next` is gone). The .next adds:
  - load_subscription_dispatch(tenant, days)
  - aggregate_subscription(rows)
  - _queue_depth(tenant) / _dead_letter_count(tenant)
  - print_subscription_panel(summary, tenant)
  - --tenant / --no-subscription CLI flags

Run:
    python3 tests/test_costs_subscription.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


def _load_costs_module() -> types.ModuleType:
    from importlib.machinery import SourceFileLoader
    import importlib.util

    next_path = ROOT / "costs.py.next"
    live_path = ROOT / "costs.py"
    target = next_path if next_path.exists() else live_path

    name = "_costs_under_test"
    sys.modules.pop(name, None)
    loader = SourceFileLoader(name, str(target))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tenant = "qcosts"
        self.c = _load_costs_module()
        # Skip if running against the live (pre-cutover) costs.py — it
        # doesn't have the new helpers.
        if not hasattr(self.c, "load_subscription_dispatch"):
            self.skipTest("live costs.py does not yet have subscription helpers")
        self.c._PIPELINE_ROOT = Path(self.tmp.name)  # type: ignore[attr-defined]

    def _data_dir(self) -> Path:
        p = Path(self.tmp.name) / f"data-{self.tenant}"
        p.mkdir(parents=True, exist_ok=True)
        return p


class TestLoadAndAggregate(_Base):
    def test_no_data_returns_empty_summary(self):
        rows = self.c.load_subscription_dispatch(self.tenant, days=7)
        self.assertEqual(rows, [])
        s = self.c.aggregate_subscription(rows)
        self.assertEqual(s["total_calls"], 0)
        self.assertEqual(s["ok_calls"], 0)
        self.assertEqual(s["failed_calls"], 0)
        self.assertIsNone(s["latest_rate_limit_status"])

    def test_ok_and_failure_counts(self):
        ts = datetime.now(timezone.utc).isoformat()
        path = self._data_dir() / "dispatch.jsonl"
        with path.open("w") as f:
            for r in [
                {"ts": ts, "task_type": "briefing-morning",
                 "model": "claude-sonnet-4-6", "outcome": "ok",
                 "usage": {"input_tokens": 100, "output_tokens": 50},
                 "rate_limit_status": "allowed", "rate_limit_resets_at": None},
                {"ts": ts, "task_type": "briefing-morning",
                 "model": "claude-sonnet-4-6", "outcome": "ok",
                 "usage": {"input_tokens": 80, "output_tokens": 40},
                 "rate_limit_status": "allowed", "rate_limit_resets_at": None},
                {"ts": ts, "task_type": "tomac-deal-compile",  # noqa: tenant-leak (task_type key test)
                 "model": "claude-opus-4-7",
                 "outcome": "failure:ProcessError",
                 "usage": {}, "rate_limit_status": "exceeded",
                 "rate_limit_resets_at": "2026-05-04T03:00:00+00:00"},
            ]:
                f.write(json.dumps(r) + "\n")
        rows = self.c.load_subscription_dispatch(self.tenant, days=7)
        s = self.c.aggregate_subscription(rows)
        self.assertEqual(s["total_calls"], 3)
        self.assertEqual(s["ok_calls"], 2)
        self.assertEqual(s["failed_calls"], 1)
        self.assertEqual(s["by_task_type"]["briefing-morning"], 2)
        self.assertEqual(s["by_model"]["claude-opus-4-7"], 1)
        self.assertEqual(s["tokens"]["input"], 180)
        self.assertEqual(s["tokens"]["output"], 90)
        self.assertEqual(s["latest_rate_limit_status"], "exceeded")

    def test_window_filter(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        path = self._data_dir() / "dispatch.jsonl"
        with path.open("w") as f:
            f.write(json.dumps({
                "ts": old_ts, "task_type": "x", "model": "claude-sonnet-4-6",
                "outcome": "ok", "usage": {}, "rate_limit_status": "allowed",
                "rate_limit_resets_at": None,
            }) + "\n")
            f.write(json.dumps({
                "ts": new_ts, "task_type": "y", "model": "claude-sonnet-4-6",
                "outcome": "ok", "usage": {}, "rate_limit_status": "allowed",
                "rate_limit_resets_at": None,
            }) + "\n")
        rows = self.c.load_subscription_dispatch(self.tenant, days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "y")


class TestQueueAndDead(_Base):
    def test_counts_zero_when_files_absent(self):
        self.assertEqual(self.c._queue_depth(self.tenant), 0)
        self.assertEqual(self.c._dead_letter_count(self.tenant), 0)

    def test_counts_lines(self):
        d = self._data_dir()
        with (d / "queue.jsonl").open("w") as f:
            f.write('{"task_type":"a","queue_until":null}\n')
            f.write('{"task_type":"b","queue_until":null}\n')
            f.write("\n")  # blank lines should not count
        with (d / "queue.dead.jsonl").open("w") as f:
            f.write('{"task_type":"c","attempts":25}\n')
        self.assertEqual(self.c._queue_depth(self.tenant), 2)
        self.assertEqual(self.c._dead_letter_count(self.tenant), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""test_subscription_health.py — tests for `_subscription_health.py.next`.

Loads the .next draft via SourceFileLoader (resilient to cutover —
falls back to live `_subscription_health.py` if `.next` is gone).

Run:
    python3 tests/test_subscription_health.py
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


def _load_health_module() -> types.ModuleType:
    from importlib.machinery import SourceFileLoader
    import importlib.util

    next_path = ROOT / "_subscription_health.py.next"
    live_path = ROOT / "_subscription_health.py"
    target = next_path if next_path.exists() else live_path

    name = "_subhealth_under_test"
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
        self.tenant = "qhealth"
        self.h = _load_health_module()
        self.h._PIPELINE_ROOT = Path(self.tmp.name)  # type: ignore[attr-defined]

    def _data_dir(self) -> Path:
        p = Path(self.tmp.name) / f"data-{self.tenant}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _write_dispatch(self, rows):
        path = self._data_dir() / "dispatch.jsonl"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _write_queue(self, rows):
        path = self._data_dir() / "queue.jsonl"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


class TestEmptyState(_Base):
    def test_no_data_returns_zeros(self):
        s = self.h.summarize(self.tenant)
        self.assertEqual(s["calls"]["total"], 0)
        self.assertEqual(s["queue"]["pending"], 0)
        self.assertEqual(s["queue"]["dead_letter"], 0)
        self.assertIsNone(s["rate_limit"]["latest_status"])


class TestCallAggregation(_Base):
    def test_outcome_counts(self):
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        self._write_dispatch([
            {"ts": ts, "task_type": "briefing-morning", "model": "claude-sonnet-4-6",
             "mode": "subscription", "outcome": "ok",
             "usage": {"input_tokens": 100, "output_tokens": 50},
             "rate_limit_status": "allowed", "rate_limit_resets_at": None,
             "project_id": "proj-x"},
            {"ts": ts, "task_type": "briefing-morning", "model": "claude-sonnet-4-6",
             "mode": "subscription", "outcome": "ok",
             "usage": {"input_tokens": 80, "output_tokens": 40},
             "rate_limit_status": "allowed", "rate_limit_resets_at": None,
             "project_id": "proj-x"},
            {"ts": ts, "task_type": "tomac-deal-compile", "model": "claude-opus-4-7",
             "mode": "subscription", "outcome": "failure:ProcessError",
             "usage": {}, "rate_limit_status": "exceeded",
             "rate_limit_resets_at": "2026-05-04T03:00:00+00:00",
             "project_id": None},
        ])
        s = self.h.summarize(self.tenant)
        self.assertEqual(s["calls"]["total"], 3)
        self.assertEqual(s["calls"]["by_outcome"]["ok"], 2)
        self.assertEqual(s["calls"]["by_outcome"]["failure:ProcessError"], 1)
        self.assertEqual(s["calls"]["by_task_type"]["briefing-morning"], 2)
        self.assertEqual(s["projects"]["with_project_id"], 2)
        self.assertEqual(s["projects"]["preamble_inlined"], 1)
        self.assertEqual(s["tokens"]["input"], 180)
        self.assertEqual(s["tokens"]["output"], 90)
        # Latest rate_limit signal is the exceeded one (last in file).
        self.assertEqual(s["rate_limit"]["latest_status"], "exceeded")


class TestWindowFilter(_Base):
    def test_old_rows_excluded(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        self._write_dispatch([
            {"ts": old, "task_type": "x", "model": "claude-sonnet-4-6",
             "outcome": "ok", "usage": {}, "rate_limit_status": "allowed",
             "rate_limit_resets_at": None},
            {"ts": new, "task_type": "y", "model": "claude-sonnet-4-6",
             "outcome": "ok", "usage": {}, "rate_limit_status": "allowed",
             "rate_limit_resets_at": None},
        ])
        s = self.h.summarize(self.tenant, hours=24)
        self.assertEqual(s["calls"]["total"], 1)
        self.assertEqual(list(s["calls"]["by_task_type"].keys()), ["y"])


class TestQueueDepth(_Base):
    def test_queue_and_dead_counts(self):
        self._write_queue([
            {"task_type": "briefing-morning", "queue_until": None, "attempts": 1,
             "system": "ctx", "messages": []},
            {"task_type": "tomac-deal-compile", "queue_until": None, "attempts": 1,
             "system": "ctx", "messages": []},
        ])
        # Dead letter file
        dead = self._data_dir() / "queue.dead.jsonl"
        with dead.open("w") as f:
            f.write(json.dumps({"task_type": "x", "attempts": 25}) + "\n")
        s = self.h.summarize(self.tenant)
        self.assertEqual(s["queue"]["pending"], 2)
        self.assertEqual(s["queue"]["dead_letter"], 1)


class TestJsonOutput(_Base):
    def test_summarize_serializable(self):
        ts = datetime.now(timezone.utc).isoformat()
        self._write_dispatch([
            {"ts": ts, "task_type": "x", "model": "claude-sonnet-4-6",
             "outcome": "ok", "usage": {}, "rate_limit_status": "allowed",
             "rate_limit_resets_at": None},
        ])
        s = self.h.summarize(self.tenant)
        # Round-trip JSON.
        round_trip = json.loads(json.dumps(s, default=str))
        self.assertEqual(round_trip["calls"]["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

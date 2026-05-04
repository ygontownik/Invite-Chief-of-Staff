"""test_subscription_queue.py — tests for `_subscription_queue.py.next`.

Loads the .next draft by file path (mirrors test_model_router_subscription.py).
Mocks `_model_router.call_claude` so the daemon never makes real calls.

Run:
    python3 tests/test_subscription_queue.py
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


def _load_queue_module() -> types.ModuleType:
    """Pre-cutover: load .next draft. Post-cutover: live module."""
    from importlib.machinery import SourceFileLoader
    import importlib.util

    next_path = ROOT / "_subscription_queue.py.next"
    live_path = ROOT / "_subscription_queue.py"
    target = next_path if next_path.exists() else live_path

    name = "_subq_under_test"
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
        self.tenant = "qtest"
        self.q = _load_queue_module()
        self.q._PIPELINE_ROOT = Path(self.tmp.name)  # type: ignore[attr-defined]

        # Install a fake _model_router so drain() can call it without
        # importing the real one (which itself would try to instantiate
        # the SDK at dispatch time).
        self.fake_calls: list[dict] = []
        fake = types.SimpleNamespace(
            call_claude=lambda **kw: self.fake_calls.append(kw) or {"text": "ok"},
        )
        sys.modules["_model_router"] = fake
        self.addCleanup(lambda: sys.modules.pop("_model_router", None))

    def _row(self, *, task_type="cos-personal-briefing",
             queue_until=None, attempts=1,
             messages=None, system="ctx") -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "model": "claude-sonnet-4-6",
            "package": "briefing",
            "queue_until": queue_until,
            "attempts": attempts,
            "error_type": "ProcessError",
            "error_msg": "rate_limit_exceeded",
            "system": system,
            "messages": messages or [{"role": "user", "content": "x"}],
        }

    def _write_rows(self, rows):
        path = self.q._queue_path(self.tenant)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


class TestEmptyQueue(_Base):
    def test_no_op_when_queue_missing(self):
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["loaded"], 0)
        self.assertEqual(stats["fired"], 0)


class TestDueRow(_Base):
    def test_past_queue_until_fires_and_drops(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write_rows([self._row(queue_until=past)])
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["fired"], 1)
        self.assertEqual(stats["succeeded"], 1)
        # Queue file should now be empty (rewritten with no surviving rows).
        path = self.q._queue_path(self.tenant)
        self.assertEqual(path.read_text().strip(), "")
        # Fake call_claude was invoked once with the row's payload.
        self.assertEqual(len(self.fake_calls), 1)
        kw = self.fake_calls[0]
        self.assertEqual(kw["task_type"], "cos-personal-briefing")
        self.assertEqual(kw["mode"], "subscription")

    def test_null_queue_until_treated_as_due(self):
        self._write_rows([self._row(queue_until=None)])
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["fired"], 1)
        self.assertEqual(stats["succeeded"], 1)


class TestFutureRow(_Base):
    def test_future_queue_until_is_skipped(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        row = self._row(queue_until=future)
        self._write_rows([row])
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["fired"], 0)
        self.assertEqual(stats["skipped"], 1)
        # Row preserved in the queue.
        rows = list(self.q._load_queue(self.tenant))
        self.assertEqual(len(rows), 1)


class TestFailureBackoff(_Base):
    def test_failure_bumps_attempts_and_queue_until(self):
        # Replace the fake call_claude with a raising version.
        sys.modules["_model_router"] = types.SimpleNamespace(
            call_claude=lambda **kw: (_ for _ in ()).throw(RuntimeError("still rate-limited")),
        )
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write_rows([self._row(queue_until=past, attempts=3)])
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["dead"], 0)

        rows = list(self.q._load_queue(self.tenant))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 4)
        # queue_until should be in the future (now + 1h backoff).
        new_dt = self.q._to_datetime(rows[0]["queue_until"])
        self.assertGreater(new_dt, datetime.now(timezone.utc))

    def test_max_attempts_moves_to_dead(self):
        sys.modules["_model_router"] = types.SimpleNamespace(
            call_claude=lambda **kw: (_ for _ in ()).throw(RuntimeError("still failing")),
        )
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # 24 + 1 will cross _MAX_ATTEMPTS -> dead letter.
        self._write_rows([self._row(queue_until=past, attempts=self.q._MAX_ATTEMPTS)])
        stats = self.q.drain(self.tenant)
        self.assertEqual(stats["dead"], 1)
        # Live queue is now empty; dead letter file got the row.
        self.assertEqual(self.q._load_queue(self.tenant), [])
        dead = self.q._dead_path(self.tenant)
        self.assertTrue(dead.exists())
        rows = [json.loads(l) for l in dead.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)


class TestDryRun(_Base):
    def test_dry_run_does_not_call_or_rewrite(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [self._row(queue_until=past)]
        self._write_rows(rows)
        stats = self.q.drain(self.tenant, dry_run=True)
        self.assertEqual(stats["fired"], 1)
        # call_claude not invoked.
        self.assertEqual(self.fake_calls, [])
        # Queue file unchanged.
        path = self.q._queue_path(self.tenant)
        self.assertEqual(
            len([l for l in path.read_text().splitlines() if l.strip()]),
            1,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""test_firm_context_subscription.py — tests for the new subscription
accessors added to `_firm_context.py.next`.

Covers `load_auth_mode()` and `load_claude_projects()`. Loads the
`.next` draft via SourceFileLoader (cutover-resilient — falls back to
live `_firm_context.py` if `.next` is gone, and skips if the live
module doesn't yet have the accessors).

Each test stamps a fresh tmp config dir and points the loader at it
via the COS_CONFIG_DIR env var, which `_find_config_dir()` honors as
the highest-priority lookup.

Run:
    python3 tests/test_firm_context_subscription.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


def _load_fc_module() -> types.ModuleType:
    from importlib.machinery import SourceFileLoader
    import importlib.util

    next_path = ROOT / "_firm_context.py.next"
    live_path = ROOT / "_firm_context.py"
    target = next_path if next_path.exists() else live_path

    name = "_fc_under_test"
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
        self.cfg_dir = Path(self.tmp.name)

        self.fc = _load_fc_module()
        if not hasattr(self.fc, "load_auth_mode"):
            self.skipTest("live _firm_context.py does not yet have subscription accessors")

        # Point the loader at our tmp dir.
        self._env_ctx = mock.patch.dict(
            os.environ, {"COS_CONFIG_DIR": str(self.cfg_dir)}, clear=False,
        )
        self._env_ctx.start()
        self.addCleanup(self._env_ctx.stop)

    def _write_yaml(self, body: str) -> None:
        # Use a real YAML file (PyYAML is available per costs.py imports).
        (self.cfg_dir / "firm_context.yaml").write_text(body)

    def _write_json(self, data: dict) -> None:
        (self.cfg_dir / "firm_config.json").write_text(json.dumps(data))


class TestLoadAuthMode(_Base):
    def test_subscription(self):
        # Minimal valid firm_context with the field set.
        self._write_yaml(
            "schema_version: 2\n"
            "principal:\n  name: Test\n  email: t@x.com\n  role: x\n"
            "  investment_focus: [\"a\"]\n"
            "firm:\n  name: Test\n"
            "team: []\n"
            "owner_whitelist: [\"x\"]\n"
            "workstream_categories:\n  deal: x\n  recruiting: x\n  other: x\n"
            "key_people: []\n"
            "counterparty_aliases: []\n"
            "peer_firms: []\n"
            "auth_mode: subscription\n"
        )
        self.assertEqual(self.fc.load_auth_mode(), "subscription")

    def test_api(self):
        self._write_yaml(
            "schema_version: 2\n"
            "principal:\n  name: T\n  email: t@x.com\n  role: x\n"
            "  investment_focus: [\"a\"]\n"
            "firm:\n  name: T\n"
            "team: []\n"
            "owner_whitelist: [\"x\"]\n"
            "workstream_categories:\n  deal: x\n  recruiting: x\n  other: x\n"
            "key_people: []\n"
            "counterparty_aliases: []\n"
            "peer_firms: []\n"
            "auth_mode: api\n"
        )
        self.assertEqual(self.fc.load_auth_mode(), "api")

    def test_field_absent_returns_none(self):
        self._write_yaml(
            "schema_version: 2\n"
            "principal:\n  name: T\n  email: t@x.com\n  role: x\n"
            "  investment_focus: [\"a\"]\n"
            "firm:\n  name: T\n"
            "team: []\n"
            "owner_whitelist: [\"x\"]\n"
            "workstream_categories:\n  deal: x\n  recruiting: x\n  other: x\n"
            "key_people: []\n"
            "counterparty_aliases: []\n"
            "peer_firms: []\n"
        )
        self.assertIsNone(self.fc.load_auth_mode())

    def test_invalid_value_returns_none(self):
        # Anything other than 'subscription' / 'api' is treated as None
        # (defensive — bad config shouldn't force an unintended mode).
        self._write_yaml(
            "schema_version: 2\n"
            "principal:\n  name: T\n  email: t@x.com\n  role: x\n"
            "  investment_focus: [\"a\"]\n"
            "firm:\n  name: T\n"
            "team: []\n"
            "owner_whitelist: [\"x\"]\n"
            "workstream_categories:\n  deal: x\n  recruiting: x\n  other: x\n"
            "key_people: []\n"
            "counterparty_aliases: []\n"
            "peer_firms: []\n"
            "auth_mode: bogus\n"
        )
        self.assertIsNone(self.fc.load_auth_mode())

    def test_missing_file_returns_none(self):
        # No firm_context.yaml at all → load_firm_context() raises;
        # load_auth_mode swallows it and returns None.
        self.assertIsNone(self.fc.load_auth_mode())


class TestLoadClaudeProjects(_Base):
    def test_full_dict(self):
        self._write_json({
            "firm_name": "Test",
            "claude_projects": {
                "briefing": "proj-b",
                "capture":  "proj-c",
                "research": "proj-r",
                "deals":    "proj-d",
            },
        })
        out = self.fc.load_claude_projects()
        self.assertEqual(out, {
            "briefing": "proj-b",
            "capture":  "proj-c",
            "research": "proj-r",
            "deals":    "proj-d",
        })

    def test_partial_dict(self):
        # v1 fallback shape: some packages have IDs, others empty.
        self._write_json({
            "firm_name": "Test",
            "claude_projects": {
                "briefing": "proj-b",
                "capture":  "",
                "research": "",
                "deals":    "proj-d",
            },
        })
        out = self.fc.load_claude_projects()
        self.assertEqual(out["briefing"], "proj-b")
        self.assertEqual(out["capture"], "")

    def test_field_absent_returns_empty(self):
        self._write_json({"firm_name": "Test"})
        self.assertEqual(self.fc.load_claude_projects(), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(self.fc.load_claude_projects(), {})

    def test_non_dict_value_returns_empty(self):
        # Defensive: someone wrote claude_projects: "x" (string) by mistake.
        self._write_json({"firm_name": "Test", "claude_projects": "oops"})
        self.assertEqual(self.fc.load_claude_projects(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

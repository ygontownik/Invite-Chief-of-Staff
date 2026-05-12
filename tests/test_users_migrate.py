#!/usr/bin/env python3
"""test_users_migrate.py — unit tests for the F-now per-user prefs migration
and the per-user /data filter logic.

Run:
    python3 ~/cos-pipeline/tests/test_users_migrate.py

Uses a temp dir as the config root. Touches no live files.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# Load users_migrate.py.next as a module despite the non-standard suffix.
HERE = Path(__file__).resolve().parent
MIGRATE_PATH = HERE.parent / "users_migrate.py.next"
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("users_migrate_next", str(MIGRATE_PATH))
spec = importlib.util.spec_from_loader("users_migrate_next", loader)
um = importlib.util.module_from_spec(spec)
sys.modules["users_migrate_next"] = um
loader.exec_module(um)


# ──────────────────────────────────────────────────────────────────────
# Inline reimplementation of the filter logic from server-data-filter.delta.md
# (the live server isn't being mutated; we test the logic at the contract level
# documented in the delta file).
_NON_OWNER_FORBIDDEN_KEYS = ("recruiting", "personalActions", "briefingLog")


def _load_user_prefs(config_root: Path, email: str) -> dict:
    p = config_root / "users" / email / "preferences.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _filter_data_for_user(data: dict, user: str, email: str,
                          config_root: Path, enabled: bool = True) -> dict:
    if user == "owner" or not enabled:
        return data
    out = dict(data)
    for k in _NON_OWNER_FORBIDDEN_KEYS:
        out.pop(k, None)
    prefs = _load_user_prefs(config_root, email)
    hidden = set(prefs.get("hiddenItems") or [])
    if hidden:
        # Map list-key -> hiddenItems prefix used in the schema.
        LIST_TO_PREFIX = {
            "followUps":     "followUp",
            "upcomingCalls": "upcomingCall",
            "emailQueue":    "emailQueue",
        }
        for list_key, prefix in LIST_TO_PREFIX.items():
            v = out.get(list_key)
            if isinstance(v, list):
                out[list_key] = [
                    x for x in v
                    if not (isinstance(x, dict) and
                            f"{prefix}:{x.get('id', '')}" in hidden)
                ]
    return out


# ──────────────────────────────────────────────────────────────────────
class TestUsersMigrate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "cos-pipeline-config-test"
        self.dashboard_path = self.root / "dashboard-data.json"
        self.users_path = self.root / "users.json"

        self.dashboard_path.write_text(json.dumps({
            "_pinnedItems": ["deal:cholla"],  # noqa: tenant-leak (test fixture — backward-compat path)
            "_hiddenItems": ["followUp:legacy-noise"],
            "_dismissedFollowUps": ["fu-001", "fu-002"],
            "_dismissedEmailIds": ["em-aaa"],
            "recruiting": {"active": [{"id": "r1"}]},
            "personalActions": [{"id": "p1"}],
            "briefingLog": [{"id": "b1"}],
            "followUps": [
                {"id": "fu-001", "text": "stale"},
                {"id": "fu-keep", "text": "live"},
            ],
        }))

        self.users_path.write_text(json.dumps({
            "users": [
                {"username": "yoni", "email": "yoni@example.com",
                 "tiles": ["/", "/tomac-cove/"]},  # noqa: tenant-leak (test fixture)
                {"username": "mark", "email": "mark@example.com",
                 "tiles": ["/tomac-cove/"]},  # noqa: tenant-leak (test fixture)
            ]
        }))

    def tearDown(self):
        self.tmp.cleanup()

    # ── migration ────────────────────────────────────────────────────
    def test_dry_run_writes_nothing(self):
        report = um.migrate(
            tenant="test", config_root=self.config_root,
            dashboard_path=self.dashboard_path, users_path=self.users_path,
            apply=False, verbose=False,
        )
        self.assertEqual(report["summary"]["written"], 0)
        self.assertEqual(report["summary"]["would_write"], 2)
        self.assertFalse((self.config_root / "users").exists())

    def test_apply_writes_files(self):
        um.migrate(
            tenant="test", config_root=self.config_root,
            dashboard_path=self.dashboard_path, users_path=self.users_path,
            apply=True, verbose=False,
        )
        owner_prefs = json.loads(
            (self.config_root / "users" / "yoni@example.com" / "preferences.json").read_text())
        mark_prefs = json.loads(
            (self.config_root / "users" / "mark@example.com" / "preferences.json").read_text())

        # Owner gets pinned + dismissed* migrated into hiddenItems.
        self.assertIn("deal:cholla", owner_prefs["pinnedItems"])  # noqa: tenant-leak (test fixture check)
        self.assertIn("followUp:fu-001", owner_prefs["hiddenItems"])
        self.assertIn("emailQueue:em-aaa", owner_prefs["hiddenItems"])
        # Owner-only legacy keys NOT migrated to non-owner.
        self.assertNotIn("followUp:fu-001", mark_prefs["hiddenItems"])
        # Tiles are passed through.
        self.assertEqual(mark_prefs["tilesVisible"], ["/tomac-cove/"])  # noqa: tenant-leak (test fixture check)
        # Schema defaults applied.
        self.assertEqual(mark_prefs["theme"], "paper")
        self.assertFalse(mark_prefs["notificationsMuted"])

    def test_idempotent_rerun(self):
        um.migrate(tenant="test", config_root=self.config_root,
                   dashboard_path=self.dashboard_path, users_path=self.users_path,
                   apply=True, verbose=False)
        path = self.config_root / "users" / "mark@example.com" / "preferences.json"
        first_mtime = path.stat().st_mtime
        first_content = path.read_text()
        # Re-run: should not produce a diff.
        report = um.migrate(tenant="test", config_root=self.config_root,
                            dashboard_path=self.dashboard_path, users_path=self.users_path,
                            apply=True, verbose=False)
        self.assertEqual(report["summary"]["unchanged"], 2)
        self.assertEqual(report["summary"]["written"], 0)
        self.assertEqual(path.read_text(), first_content)
        self.assertEqual(path.stat().st_mtime, first_mtime)


class TestPerUserFilter(unittest.TestCase):
    """Mirrors patch in server-data-filter.delta.md."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_root = Path(self.tmp.name) / "cfg"
        # Mark's prefs: hide one followUp.
        mark_dir = self.config_root / "users" / "mark@example.com"
        mark_dir.mkdir(parents=True)
        (mark_dir / "preferences.json").write_text(json.dumps({
            "pinnedItems": [],
            "hiddenItems": ["followUp:fu-stale"],
            "tilesVisible": [],
            "defaultLanding": "/",
            "theme": "paper",
            "notificationsMuted": False,
            "lastUpdated": "2026-05-03T07:00:00Z",
        }))
        self.payload = {
            "today": "2026-05-03",
            "recruiting": {"active": [{"id": "r1"}]},
            "personalActions": [{"id": "p1"}],
            "briefingLog": [{"id": "b1"}],
            "followUps": [
                {"id": "fu-stale", "text": "drop me"},
                {"id": "fu-keep", "text": "keep me"},
            ],
            "tomac": [{"id": "deal-1"}],  # noqa: tenant-leak (backward-compat key test)
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_login_filters_recruiting_personal_briefing(self):
        out = _filter_data_for_user(
            self.payload, user="mark", email="mark@example.com",
            config_root=self.config_root, enabled=True,
        )
        self.assertNotIn("recruiting", out)
        self.assertNotIn("personalActions", out)
        self.assertNotIn("briefingLog", out)
        # Allowed keys still present.
        self.assertIn("today", out)
        self.assertIn("tomac", out)  # noqa: tenant-leak (backward-compat key test)
        # hiddenItems applied to followUps.
        ids = [x["id"] for x in out["followUps"]]
        self.assertIn("fu-keep", ids)
        self.assertNotIn("fu-stale", ids)

    def test_owner_unfiltered(self):
        out = _filter_data_for_user(
            self.payload, user="owner", email="",
            config_root=self.config_root, enabled=True,
        )
        self.assertIn("recruiting", out)
        self.assertIn("personalActions", out)
        self.assertIn("briefingLog", out)

    def test_flag_off_is_passthrough(self):
        out = _filter_data_for_user(
            self.payload, user="mark", email="mark@example.com",
            config_root=self.config_root, enabled=False,
        )
        self.assertIn("recruiting", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

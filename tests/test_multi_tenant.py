"""Unit tests for ``cos-pipeline/multi_tenant.py``.

Run::

    python3 ~/cos-pipeline/tests/test_multi_tenant.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Allow `python3 tests/test_multi_tenant.py` from any cwd.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

import multi_tenant as mt  # noqa: E402


class TestSlugValidation(unittest.TestCase):
    def test_accepts_legal_slugs(self):
        for s in ("tomac", "re-dev", "abc", "a1", "foo-bar-baz"):
            mt.validate_slug(s)  # must not raise

    def test_rejects_uppercase(self):
        with self.assertRaises(ValueError):
            mt.validate_slug("Tomac")

    def test_rejects_whitespace(self):
        with self.assertRaises(ValueError):
            mt.validate_slug("re dev")

    def test_rejects_leading_digit(self):
        with self.assertRaises(ValueError):
            mt.validate_slug("1tenant")

    def test_rejects_reserved(self):
        for s in ("shared", "all", "default", "common", "template"):
            with self.assertRaises(ValueError):
                mt.validate_slug(s)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            mt.validate_slug("")

    def test_rejects_underscore_and_dot(self):
        with self.assertRaises(ValueError):
            mt.validate_slug("re_dev")
        with self.assertRaises(ValueError):
            mt.validate_slug("re.dev")


class TestPortAllocation(unittest.TestCase):
    def test_reserved_ports(self):
        self.assertEqual(mt.slug_to_port("tomac"), 7777)
        self.assertEqual(mt.slug_to_port("re-dev"), 7778)

    def test_dynamic_port_in_range(self):
        port = mt.slug_to_port("acme-cap")
        self.assertGreaterEqual(port, mt.DYNAMIC_PORT_START)
        self.assertLessEqual(port, mt.DYNAMIC_PORT_END)

    def test_dynamic_port_deterministic(self):
        # Two calls with the same slug return the same port (assuming no
        # registry mutation between them).
        a = mt.slug_to_port("acme-cap")
        b = mt.slug_to_port("acme-cap")
        self.assertEqual(a, b)

    def test_dynamic_port_avoids_reserved(self):
        # Reserved ports must never be returned for a non-reserved slug.
        for slug in ("acme-cap", "newco", "x-firm", "longer-tenant-slug"):
            self.assertNotIn(
                mt.slug_to_port(slug),
                set(mt.RESERVED_PORTS.values()),
                f"slug {slug!r} collided with a reserved port",
            )

    def test_port_registry_path(self):
        p = mt.port_registry_path()
        self.assertTrue(str(p).endswith("data-shared/tenant-ports.json"))
        # Verify it's a Path and is anchored under the user's home dir.
        self.assertIsInstance(p, Path)
        self.assertIn("cos-pipeline", str(p))


class TestLabelFormat(unittest.TestCase):
    def test_label_format(self):
        self.assertEqual(
            mt.launchagent_label("tomac", "morning-briefing"),
            "com.cos.tomac.morning-briefing",
        )
        self.assertEqual(
            mt.launchagent_label("re-dev", "cos-capture"),
            "com.cos.re-dev.cos-capture",
        )

    def test_label_rejects_bad_routine(self):
        with self.assertRaises(ValueError):
            mt.launchagent_label("tomac", "")
        with self.assertRaises(ValueError):
            mt.launchagent_label("tomac", "morning briefing")
        with self.assertRaises(ValueError):
            mt.launchagent_label("tomac", "morning.briefing")

    def test_label_rejects_bad_slug(self):
        with self.assertRaises(ValueError):
            mt.launchagent_label("Tomac", "x")


class TestKeychainAndPaths(unittest.TestCase):
    def test_keychain_service(self):
        self.assertEqual(mt.keychain_service("tomac"), "cos-pipeline-tomac")
        self.assertEqual(mt.keychain_service("re-dev"), "cos-pipeline-re-dev")

    def test_data_dir(self):
        self.assertTrue(str(mt.tenant_data_dir("tomac")).endswith("/cos-pipeline/data-tomac"))

    def test_logs_dir(self):
        self.assertTrue(str(mt.tenant_logs_dir("re-dev")).endswith("/cos-pipeline/logs-re-dev"))

    def test_config_repo(self):
        self.assertTrue(str(mt.tenant_config_repo("re-dev")).endswith("/cos-pipeline-config-re-dev"))


class TestListKnownTenants(unittest.TestCase):
    def test_finds_tomac_and_re_dev(self):
        # These were scaffolded by run-2 phase 1.x; the test expects them
        # present on the dev machine. If running in a sandbox without them,
        # this assertion is the documented signal that the env is wrong.
        slugs = mt.list_known_tenants()
        self.assertIn("tomac", slugs, f"expected 'tomac' in {slugs}")
        self.assertIn("re-dev", slugs, f"expected 're-dev' in {slugs}")

    def test_returns_sorted(self):
        slugs = mt.list_known_tenants()
        self.assertEqual(slugs, sorted(slugs))


if __name__ == "__main__":
    unittest.main(verbosity=2)

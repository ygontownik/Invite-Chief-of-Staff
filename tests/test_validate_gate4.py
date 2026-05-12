"""test_validate_gate4.py — pin the rewritten Gate 4 in setup.sh --validate.

Original Gate 4 checked phantom keychain entries (`<prefix>/DASHBOARD_USERNAME`,
`/DASHBOARD_PASSWORD`) that no consumer reads. Rewritten 2026-05-03 (session 4)
to check the real auth surfaces:
  - keychain `<prefix>/ANTHROPIC_API_KEY` (read by _secrets.load_secret)
  - keychain `<prefix>/ASSEMBLYAI_API_KEY` (read by podcast/transcript hooks)
  - dashboard plist env OWNER_PASSWORD + PARTNER_PASSWORD
    (read at cos-dashboard-server.py:31-32)
  - $CONFIG_DIR/config/users.json with at least one valid user

These tests run setup.sh --instance=<slug> --validate and grep the output. They
do not modify any state.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / 'setup.sh'


def _run_validate(slug='tomac'):  # noqa: tenant-leak (integration test default slug)
    return subprocess.run(
        ['bash', str(SETUP), f'--instance={slug}', '--validate'],
        capture_output=True, text=True, timeout=60,
    )


class Gate4ChecksRealSurfaces(unittest.TestCase):
    """The new Gate 4 must check the surfaces that actually drive auth."""

    @classmethod
    def setUpClass(cls):
        cls.r = _run_validate('tomac')  # noqa: tenant-leak (integration test)
        cls.out = cls.r.stdout + cls.r.stderr

    def test_validate_runs_and_emits_output(self):
        # Should not crash, and should produce gate output (PASS or FAIL).
        self.assertIn('VALIDATE:', self.out)

    def test_checks_anthropic_keychain_entry(self):
        # Either ✓ Keychain or ✗ Keychain MISSING — but it must be checked.
        self.assertRegex(
            self.out,
            r'Keychain.*cos-pipeline-tomac/ANTHROPIC_API_KEY',  # noqa: tenant-leak (keychain pattern check)
        )

    def test_checks_assemblyai_keychain_entry(self):
        self.assertRegex(
            self.out,
            r'Keychain.*cos-pipeline-tomac/ASSEMBLYAI_API_KEY',  # noqa: tenant-leak (keychain pattern check)
        )

    def test_checks_owner_password_in_plist_or_env(self):
        self.assertRegex(
            self.out,
            r'Dashboard env.*OWNER_PASSWORD',
        )

    def test_checks_partner_password_in_plist_or_env(self):
        self.assertRegex(
            self.out,
            r'Dashboard env.*PARTNER_PASSWORD',
        )

    def test_checks_users_json(self):
        self.assertRegex(self.out, r'users\.json')

    def test_no_longer_checks_phantom_dashboard_username_keychain(self):
        # The phantom checks must NOT appear — they would lie about reality.
        self.assertNotRegex(
            self.out,
            r'Keychain.*DASHBOARD_USERNAME',
        )
        self.assertNotRegex(
            self.out,
            r'Keychain.*DASHBOARD_PASSWORD',
        )


class Gate4PassesOnHealthyTomac(unittest.TestCase):  # noqa: tenant-leak (class name — slug integration test)
    """On the live default-tenant install, all 5 Gate 4 checks should pass."""

    @classmethod
    def setUpClass(cls):
        cls.r = _run_validate('tomac')  # noqa: tenant-leak (integration test)
        cls.out = cls.r.stdout + cls.r.stderr

    def test_anthropic_keychain_present(self):
        self.assertRegex(
            self.out,
            r'✓.*Keychain.*cos-pipeline-tomac/ANTHROPIC_API_KEY',  # noqa: tenant-leak (keychain pattern check)
        )

    def test_assemblyai_keychain_present(self):
        self.assertRegex(
            self.out,
            r'✓.*Keychain.*cos-pipeline-tomac/ASSEMBLYAI_API_KEY',  # noqa: tenant-leak (keychain pattern check)
        )

    def test_owner_password_present(self):
        self.assertRegex(self.out, r'✓.*Dashboard env.*OWNER_PASSWORD')

    def test_partner_password_present(self):
        self.assertRegex(self.out, r'✓.*Dashboard env.*PARTNER_PASSWORD')

    def test_users_json_has_valid_user(self):
        # "✓ users.json: N user(s) with username+password" where N >= 1
        m = re.search(r'users\.json:\s*(\d+)\s+user', self.out)
        self.assertIsNotNone(m, msg=f'no user count in output: {self.out[:500]}')
        self.assertGreaterEqual(int(m.group(1)), 1)


if __name__ == '__main__':
    unittest.main()

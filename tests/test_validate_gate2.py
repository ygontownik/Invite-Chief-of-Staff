"""test_validate_gate2.py — pin the rewritten Gate 2 in setup.sh --validate.

Original Gate 2 REQUIRED keys: google_docs, principal, team, owner_whitelist,
keychain_service_prefix, domain. Two of those were checking the wrong file:
  - google_docs: drive-docs.yaml is canonical per C8; firm_context.yaml :: google_docs
    is OPTIONAL bootstrap fallback. Should not be required.
  - keychain_service_prefix: lives in firm_config.json per C11, not firm_context.yaml.
    Must be checked separately.

Rewritten 2026-05-03 (session 4) to:
  - REQUIRED in firm_context.yaml: principal, team, owner_whitelist, domain
  - SEPARATE check for firm_config.json :: keychain_service_prefix
  - SEPARATE check rejecting deprecated firm_config.json :: docs (C8)

These tests run setup.sh --instance=<slug> --validate and assert on the output.
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


class Gate2RequiredKeysAreCorrect(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _run_validate('tomac')  # noqa: tenant-leak (integration test)
        cls.out = cls.r.stdout + cls.r.stderr

    def test_checks_principal(self):
        self.assertRegex(self.out, r'firm_context\.yaml :: principal')

    def test_checks_team(self):
        self.assertRegex(self.out, r'firm_context\.yaml :: team')

    def test_checks_owner_whitelist(self):
        self.assertRegex(self.out, r'firm_context\.yaml :: owner_whitelist')

    def test_checks_domain(self):
        self.assertRegex(self.out, r'firm_context\.yaml :: domain')

    def test_does_not_check_google_docs_in_firm_context(self):
        # google_docs in firm_context.yaml is optional per C8 (drive-docs.yaml is canonical).
        self.assertNotRegex(self.out, r'firm_context\.yaml :: google_docs MISSING')

    def test_does_not_check_keychain_service_prefix_in_firm_context(self):
        # keychain_service_prefix lives in firm_config.json per C11, not firm_context.yaml.
        self.assertNotRegex(
            self.out,
            r'firm_context\.yaml :: keychain_service_prefix MISSING',
        )

    def test_checks_keychain_service_prefix_in_firm_config(self):
        # The right place to check it.
        self.assertRegex(self.out, r'firm_config\.json :: keychain_service_prefix')

    def test_rejects_deprecated_docs_schema(self):
        # The gate must still flag firm_config.json :: docs as deprecated.
        # We can't directly assert it appears (because we just removed it from tomac),
        # but we can check the gate didn't accidentally drop the check.
        # Test: setup.sh source contains the regex check.
        src = SETUP.read_text()
        self.assertIn('Old schema detected: firm_config.json :: docs', src)


class TomacValidatePassesClean(unittest.TestCase):
    """The whole point: the default-tenant validate should PASS after session-4 fixes."""

    @classmethod
    def setUpClass(cls):
        cls.r = _run_validate('tomac')  # noqa: tenant-leak (integration test)
        cls.out = cls.r.stdout + cls.r.stderr

    def test_exits_zero(self):
        self.assertEqual(self.r.returncode, 0,
                         msg=f'expected PASS but got non-zero. Output:\n{self.out}')

    def test_emits_pass_marker(self):
        self.assertIn('VALIDATE: PASS', self.out)

    def test_does_not_emit_fail_marker(self):
        self.assertNotIn('VALIDATE: FAIL', self.out)


if __name__ == '__main__':
    unittest.main()

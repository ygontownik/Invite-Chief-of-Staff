"""test_uninstall.py — safety + idempotency tests for setup.sh --uninstall.

These tests run the live setup.sh against a sandbox slug ("uninst-test") that
has no real installed agents. They verify:
  - Guard: --uninstall without --instance refuses to run
  - Empty case: uninstalling an unused slug exits 0 with 0 items removed
  - Idempotency: two runs in a row both succeed cleanly
  - Slug isolation: a fake-slug uninstall does NOT touch the default tenant's
    LaunchAgents, keychain entries, or data directories.

The sandbox slug is never written to and never used in the real port registry.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / 'setup.sh'
SANDBOX_SLUG = 'uninst-test'


def _run_setup(*args, env=None):
    """Run setup.sh with args; return CompletedProcess."""
    cmd = ['bash', str(SETUP), *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )


class UninstallGuards(unittest.TestCase):

    def test_uninstall_without_instance_refuses(self):
        r = _run_setup('--uninstall', '--yes')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--uninstall requires --instance', r.stdout + r.stderr)

    def test_uninstall_unknown_slug_exits_clean(self):
        r = _run_setup(f'--instance={SANDBOX_SLUG}', '--uninstall', '--yes')
        self.assertEqual(r.returncode, 0,
                         msg=f'stdout={r.stdout!r}\nstderr={r.stderr!r}')
        self.assertIn('UNINSTALL: complete', r.stdout)
        self.assertIn('0 items removed', r.stdout)

    def test_uninstall_is_idempotent(self):
        r1 = _run_setup(f'--instance={SANDBOX_SLUG}', '--uninstall', '--yes')
        r2 = _run_setup(f'--instance={SANDBOX_SLUG}', '--uninstall', '--yes')
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        # Both runs report 0 items because there's nothing to remove.
        self.assertIn('0 items removed', r1.stdout)
        self.assertIn('0 items removed', r2.stdout)

    def test_uninstall_skips_data_when_purge_data_not_passed(self):
        r = _run_setup(f'--instance={SANDBOX_SLUG}', '--uninstall', '--yes')
        self.assertIn('Skipping data/log dir removal', r.stdout)
        self.assertIn('Skipping config dir removal', r.stdout)


class SlugIsolation(unittest.TestCase):
    """Uninstalling slug X must not affect slug Y's footprint."""

    def _tomac_inventory(self):  # noqa: tenant-leak (slug isolation test)
        """Snapshot the parts of the default-tenant footprint that uninstall could touch."""
        la = sorted(
            p.name for p in (Path.home() / 'Library' / 'LaunchAgents').glob('com.cos.tomac.*')  # noqa: tenant-leak (slug isolation test)
        )
        # Keychain: count cos-pipeline-tomac/* entries  # noqa: tenant-leak (slug isolation test)
        try:
            dump = subprocess.run(
                ['security', 'dump-keychain',
                 str(Path.home() / 'Library' / 'Keychains' / 'login.keychain-db')],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            dump = ''
        kc_count = sum(
            1 for line in dump.splitlines()
            if '"svce"<blob>="cos-pipeline-tomac/' in line  # noqa: tenant-leak (keychain slug check)
        )
        # Dirs
        data_exists = (REPO / 'data-tomac').exists()  # noqa: tenant-leak (dir slug check)
        logs_exists = (REPO / 'logs-tomac').exists()  # noqa: tenant-leak (dir slug check)
        return {
            'launchagents': la,
            'keychain_count': kc_count,
            'data_dir': data_exists,
            'logs_dir': logs_exists,
        }

    def test_uninstalling_sandbox_does_not_touch_tomac(self):  # noqa: tenant-leak (method name — slug isolation test)
        before = self._tomac_inventory()  # noqa: tenant-leak (slug isolation test)
        r = _run_setup(
            f'--instance={SANDBOX_SLUG}', '--uninstall', '--yes',
            '--purge-data', '--purge-config',
        )
        self.assertEqual(r.returncode, 0,
                         msg=f'stdout={r.stdout!r}\nstderr={r.stderr!r}')
        after = self._tomac_inventory()  # noqa: tenant-leak (slug isolation test)
        self.assertEqual(before, after,
                         msg='default-tenant footprint changed after sandbox uninstall')


class CliFlags(unittest.TestCase):
    """The new flags must round-trip through arg parsing without crashing."""

    def test_setup_sh_passes_bash_syntax_check(self):
        r = subprocess.run(
            ['bash', '-n', str(SETUP)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r.returncode, 0,
                         msg=f'syntax error in setup.sh: {r.stderr}')

    def test_help_flag_still_works(self):
        r = _run_setup('--help')
        self.assertEqual(r.returncode, 0)
        self.assertIn('Usage:', r.stdout)


if __name__ == '__main__':
    unittest.main()

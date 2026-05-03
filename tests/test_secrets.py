"""test_secrets.py — tests for the _secrets credential abstraction."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _secrets


class BackendDetection(unittest.TestCase):

    def test_explicit_override_keychain(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'keychain'}):
            self.assertEqual(_secrets._detect_backend(), 'keychain')

    def test_explicit_override_env(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'env'}):
            self.assertEqual(_secrets._detect_backend(), 'env')

    def test_explicit_override_invalid_falls_through(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'invalid'}):
            backend = _secrets._detect_backend()
            self.assertIn(backend, ('keychain', 'env'))

    def test_auto_detect_returns_known_backend(self):
        # Drop the override if present.
        env = {k: v for k, v in os.environ.items() if k != 'COS_SECRETS_BACKEND'}
        with mock.patch.dict(os.environ, env, clear=True):
            backend = _secrets._detect_backend()
            self.assertIn(backend, ('keychain', 'env'))


class EnvLookup(unittest.TestCase):

    def test_literal_key_match(self):
        with mock.patch.dict(os.environ, {'my-test-key': 'literal-value'}, clear=True):
            self.assertEqual(_secrets._load_env('my-test-key'), 'literal-value')

    def test_upper_snake_fallback(self):
        with mock.patch.dict(os.environ, {'MY_TEST_KEY': 'upper-value'}, clear=True):
            self.assertEqual(_secrets._load_env('my-test-key'), 'upper-value')

    def test_literal_wins_over_upper(self):
        with mock.patch.dict(os.environ, {
            'my-test-key': 'literal-value',
            'MY_TEST_KEY': 'upper-value',
        }, clear=True):
            self.assertEqual(_secrets._load_env('my-test-key'), 'literal-value')

    def test_missing_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_secrets._load_env('nonexistent-key-12345'))


class LoadSecret(unittest.TestCase):

    def test_keychain_backend_finds_via_env_fallback(self):
        # When keychain returns None, cross-backend fallback should hit env.
        with mock.patch.dict(os.environ, {
            'COS_SECRETS_BACKEND': 'keychain',
            'MY_KEY': 'env-fallback-val',
        }, clear=True):
            with mock.patch.object(_secrets, '_load_keychain', return_value=None):
                self.assertEqual(_secrets.load_secret('my-key'), 'env-fallback-val')

    def test_env_backend_finds_via_keychain_fallback(self):
        # When env returns None, cross-backend fallback should hit keychain.
        with mock.patch.dict(os.environ, {
            'COS_SECRETS_BACKEND': 'env',
        }, clear=True):
            with mock.patch.object(_secrets, '_load_keychain', return_value='keychain-val'):
                self.assertEqual(_secrets.load_secret('my-key'), 'keychain-val')

    def test_default_when_neither_backend_finds_it(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'env'}, clear=True):
            with mock.patch.object(_secrets, '_load_keychain', return_value=None):
                self.assertEqual(
                    _secrets.load_secret('missing-key', default='dflt'),
                    'dflt',
                )

    def test_default_is_none_by_default(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'env'}, clear=True):
            with mock.patch.object(_secrets, '_load_keychain', return_value=None):
                self.assertIsNone(_secrets.load_secret('missing-key'))

    def test_keychain_value_preferred_when_keychain_active(self):
        with mock.patch.dict(os.environ, {
            'COS_SECRETS_BACKEND': 'keychain',
            'MY_KEY': 'env-val',
        }, clear=True):
            with mock.patch.object(_secrets, '_load_keychain', return_value='keychain-val'):
                self.assertEqual(_secrets.load_secret('my-key'), 'keychain-val')


class ServicePrefix(unittest.TestCase):

    def test_falls_back_to_cos_pipeline_when_firm_config_missing(self):
        with mock.patch('_firm_context.load_firm_config', return_value={}):
            self.assertEqual(_secrets._service_prefix(), 'cos-pipeline')

    def test_uses_firm_config_value_when_present(self):
        with mock.patch('_firm_context.load_firm_config',
                        return_value={'keychain_service_prefix': 'cos-pipeline-tomac'}):
            self.assertEqual(_secrets._service_prefix(), 'cos-pipeline-tomac')

    def test_handles_firm_config_load_failure(self):
        with mock.patch('_firm_context.load_firm_config',
                        side_effect=Exception('config unreachable')):
            self.assertEqual(_secrets._service_prefix(), 'cos-pipeline')


class KeychainEntryShape(unittest.TestCase):
    """Pin the service/account shape so we can't drift from setup_keychain.sh.

    Canonical entry: service="<prefix>/<KEY>", account=current $USER.
    setup_keychain.sh writes this shape; _secrets must read/write the same.
    """

    def test_load_uses_prefix_slash_key_service_and_user_account(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            r = mock.Mock(); r.returncode = 0; r.stdout = 'val\n'
            return r

        with mock.patch.dict(os.environ, {'USER': 'alice'}, clear=False):
            with mock.patch.object(_secrets, '_service_prefix',
                                   return_value='cos-pipeline-tomac'):
                with mock.patch('subprocess.run', side_effect=fake_run):
                    _secrets._load_keychain('ANTHROPIC_API_KEY')

        cmd = captured['cmd']
        # ['security', 'find-generic-password', '-s', svc, '-a', acct, '-w']
        self.assertEqual(cmd[3], 'cos-pipeline-tomac/ANTHROPIC_API_KEY')
        self.assertEqual(cmd[5], 'alice')

    def test_store_uses_prefix_slash_key_service_and_user_account(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            r = mock.Mock(); r.returncode = 0
            return r

        with mock.patch.dict(os.environ, {'USER': 'bob'}, clear=False):
            with mock.patch.object(_secrets, '_service_prefix',
                                   return_value='cos-pipeline-re-dev'):
                with mock.patch('subprocess.run', side_effect=fake_run):
                    _secrets._store_keychain('ASSEMBLYAI_API_KEY', 'xyz')

        cmd = captured['cmd']
        # ['security', 'add-generic-password', '-s', svc, '-a', acct, '-w', val, '-U']
        self.assertEqual(cmd[3], 'cos-pipeline-re-dev/ASSEMBLYAI_API_KEY')
        self.assertEqual(cmd[5], 'bob')

    def test_explicit_service_argument_overrides_prefix(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            r = mock.Mock(); r.returncode = 1; r.stdout = ''
            return r

        with mock.patch.dict(os.environ, {'USER': 'carol'}, clear=False):
            with mock.patch('subprocess.run', side_effect=fake_run):
                _secrets._load_keychain('MY_KEY', service='other-prefix')

        self.assertEqual(captured['cmd'][3], 'other-prefix/MY_KEY')


class StoreSecret(unittest.TestCase):

    def test_env_backend_raises_not_implemented(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'env'}, clear=True):
            with self.assertRaises(NotImplementedError):
                _secrets.store_secret('foo', 'bar')

    def test_keychain_backend_returns_subprocess_result(self):
        with mock.patch.dict(os.environ, {'COS_SECRETS_BACKEND': 'keychain'}, clear=True):
            with mock.patch.object(_secrets, '_store_keychain', return_value=True):
                self.assertTrue(_secrets.store_secret('foo', 'bar'))


if __name__ == '__main__':
    unittest.main()

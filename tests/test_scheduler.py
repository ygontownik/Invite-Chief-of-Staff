"""test_scheduler.py — tests for the _scheduler abstraction."""
import os
import plistlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _scheduler


class BackendDetection(unittest.TestCase):

    def test_explicit_override_launchd(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}):
            self.assertEqual(_scheduler._detect_backend(), 'launchd')

    def test_explicit_override_systemd(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'systemd'}):
            self.assertEqual(_scheduler._detect_backend(), 'systemd')

    def test_invalid_override_falls_through_to_auto(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'invalid'}):
            backend = _scheduler._detect_backend()
            self.assertIn(backend, ('launchd', 'cron'))


class PlistRendering(unittest.TestCase):

    def test_keepalive_schedule(self):
        body = _scheduler.render_plist(
            label='com.test.daemon',
            program=['/usr/bin/python3', '/srv/foo.py'],
            schedule={'keepalive': True},
        )
        d = plistlib.loads(body)
        self.assertEqual(d['Label'], 'com.test.daemon')
        self.assertEqual(d['ProgramArguments'], ['/usr/bin/python3', '/srv/foo.py'])
        self.assertTrue(d['KeepAlive'])
        self.assertTrue(d['RunAtLoad'])
        self.assertEqual(d['LimitLoadToSessionType'], 'Aqua')

    def test_interval_schedule(self):
        body = _scheduler.render_plist(
            label='com.test.poll',
            program=['/bin/bash', '/srv/poll.sh'],
            schedule={'interval_sec': 900},
            env={'PATH': '/usr/bin'},
        )
        d = plistlib.loads(body)
        self.assertEqual(d['StartInterval'], 900)
        self.assertEqual(d['EnvironmentVariables'], {'PATH': '/usr/bin'})
        self.assertNotIn('KeepAlive', d)
        self.assertNotIn('StartCalendarInterval', d)

    def test_calendar_single_entry(self):
        body = _scheduler.render_plist(
            label='com.test.daily',
            program=['/usr/bin/python3', '/srv/x.py'],
            schedule={'calendar': [{'hour': 7, 'minute': 30}]},
        )
        d = plistlib.loads(body)
        self.assertEqual(d['StartCalendarInterval'], [{'Hour': 7, 'Minute': 30}])

    def test_calendar_multiple_entries_twice_daily(self):
        body = _scheduler.render_plist(
            label='com.test.bi',
            program=['/x'],
            schedule={'calendar': [
                {'hour': 7, 'minute': 30},
                {'hour': 18, 'minute': 0},
            ]},
        )
        d = plistlib.loads(body)
        self.assertEqual(d['StartCalendarInterval'], [
            {'Hour': 7, 'Minute': 30},
            {'Hour': 18, 'Minute': 0},
        ])

    def test_calendar_weekday_expansion(self):
        body = _scheduler.render_plist(
            label='com.test.weekdays',
            program=['/x'],
            schedule={'calendar': [
                {'hour': 7, 'minute': 22, 'weekday': [1, 2, 3, 4, 5]},
            ]},
        )
        d = plistlib.loads(body)
        # Five entries, one per weekday.
        self.assertEqual(len(d['StartCalendarInterval']), 5)
        for i, entry in enumerate(d['StartCalendarInterval'], start=1):
            self.assertEqual(entry, {'Weekday': i, 'Hour': 7, 'Minute': 22})

    def test_invalid_schedule_raises(self):
        with self.assertRaises(ValueError):
            _scheduler.render_plist(
                label='com.test.bad',
                program=['/x'],
                schedule={'unknown': True},
            )

    def test_session_type_can_be_disabled(self):
        body = _scheduler.render_plist(
            label='com.test.no-session',
            program=['/x'],
            schedule={'interval_sec': 60},
            session_type=None,
        )
        d = plistlib.loads(body)
        self.assertNotIn('LimitLoadToSessionType', d)

    def test_stdout_stderr_paths(self):
        body = _scheduler.render_plist(
            label='com.test.logs',
            program=['/x'],
            schedule={'interval_sec': 60},
            stdout='/tmp/out.log',
            stderr='/tmp/err.log',
        )
        d = plistlib.loads(body)
        self.assertEqual(d['StandardOutPath'], '/tmp/out.log')
        self.assertEqual(d['StandardErrorPath'], '/tmp/err.log')


class RegisterFlow(unittest.TestCase):
    """Exercise register/unregister with mocked launchctl + temp plist dir."""

    def setUp(self):
        self.tmp = Path(os.environ.get('TMPDIR', '/tmp')) / 'cos-scheduler-test'
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.label = 'com.test.cos-scheduler.unit'
        self.plist_path = self.tmp / f'{self.label}.plist'
        if self.plist_path.exists():
            self.plist_path.unlink()

    def tearDown(self):
        if self.plist_path.exists():
            self.plist_path.unlink()

    def test_register_writes_plist_and_bootstraps(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
            with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp), \
                 mock.patch.object(_scheduler, '_is_loaded_launchd', return_value=False), \
                 mock.patch.object(_scheduler, '_bootstrap_launchd') as boot:
                path = _scheduler.register(
                    label=self.label,
                    program=['/usr/bin/python3', '/x'],
                    schedule={'interval_sec': 60},
                )
                self.assertEqual(path, self.plist_path)
                self.assertTrue(self.plist_path.exists())
                boot.assert_called_once()

    def test_register_replaces_loaded_copy(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
            with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp), \
                 mock.patch.object(_scheduler, '_is_loaded_launchd', return_value=True), \
                 mock.patch.object(_scheduler, '_bootout_launchd') as bootout, \
                 mock.patch.object(_scheduler, '_bootstrap_launchd') as boot:
                _scheduler.register(
                    label=self.label,
                    program=['/x'],
                    schedule={'keepalive': True},
                )
                bootout.assert_called_once_with(self.label)
                boot.assert_called_once()

    def test_register_activate_false_writes_only(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
            with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp), \
                 mock.patch.object(_scheduler, '_bootstrap_launchd') as boot:
                _scheduler.register(
                    label=self.label,
                    program=['/x'],
                    schedule={'interval_sec': 60},
                    activate=False,
                )
                boot.assert_not_called()
                self.assertTrue(self.plist_path.exists())

    def test_unregister_removes_plist_and_bootouts(self):
        # Pre-create the plist so unregister has something to remove.
        self.plist_path.write_bytes(b'<?xml version="1.0"?><plist><dict/></plist>')
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
            with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp), \
                 mock.patch.object(_scheduler, '_is_loaded_launchd', return_value=True), \
                 mock.patch.object(_scheduler, '_bootout_launchd') as bootout:
                changed = _scheduler.unregister(self.label)
                self.assertTrue(changed)
                bootout.assert_called_once()
                self.assertFalse(self.plist_path.exists())

    def test_unregister_idempotent_when_absent(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
            with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp), \
                 mock.patch.object(_scheduler, '_is_loaded_launchd', return_value=False):
                changed = _scheduler.unregister(self.label)
                self.assertFalse(changed)

    def test_list_registered_filters_by_prefix(self):
        # Create three plist files with different prefixes.
        (self.tmp / 'com.cos.tomac.foo.plist').write_bytes(b'<plist/>')
        (self.tmp / 'com.cos.tomac.bar.plist').write_bytes(b'<plist/>')
        (self.tmp / 'com.unrelated.baz.plist').write_bytes(b'<plist/>')
        try:
            with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'launchd'}, clear=False):
                with mock.patch.object(_scheduler, '_launchagents_dir', return_value=self.tmp):
                    matches = _scheduler.list_registered('com.cos.tomac.')
                    self.assertEqual(matches, ['com.cos.tomac.bar', 'com.cos.tomac.foo'])
                    all_three = _scheduler.list_registered('')
                    self.assertIn('com.unrelated.baz', all_three)
        finally:
            for f in ('com.cos.tomac.foo.plist', 'com.cos.tomac.bar.plist',
                      'com.unrelated.baz.plist'):
                p = self.tmp / f
                if p.exists():
                    p.unlink()

    def test_unsupported_backend_raises(self):
        with mock.patch.dict(os.environ, {'COS_SCHEDULER_BACKEND': 'systemd'}, clear=False):
            with self.assertRaises(NotImplementedError):
                _scheduler.register(
                    label='com.test.foo',
                    program=['/x'],
                    schedule={'interval_sec': 60},
                )


if __name__ == '__main__':
    unittest.main()

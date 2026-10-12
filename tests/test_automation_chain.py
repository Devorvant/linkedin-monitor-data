"""Synthetic processes and mocked shutdown: never operate a browser or power off."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runtime = module('automation_runtime')
entry = module('linkedin_worker_entry')
dispatcher = module('dispatcher_v4_linkedin_profi')
NOW = datetime(2026, 10, 12, 8, 10)
FINISH = NOW.replace(minute=20)
CFG = {'enabled': True, 'schedule': {'start_time': '8:10', 'finish_time': '8:20'},
       'shutdown_after_finish': True, 'shutdown_when_disabled': True,
       'run_if_started_late': True}


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=tz)


class RunnerTests(unittest.TestCase):
    def test_success_and_nonzero_are_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            runner = runtime.StageRunner(root, logger=lambda _: None)
            for code in (0, 7):
                result = runner.run([sys.executable, '-c', f'print("hello"); raise SystemExit({code})'], 'synthetic', 5)
                self.assertEqual(result['exit_code'], code)
                self.assertEqual(result['state'], 'ok' if not code else 'error')
                self.assertIn('hello', Path(result['log_path']).read_text())
                self.assertEqual(json.loads((Path(root) / 'logs/synthetic_status.json').read_text())['exit_code'], code)

    def test_hung_process_is_stopped_and_reported(self):
        with tempfile.TemporaryDirectory() as root:
            result = runtime.StageRunner(root, logger=lambda _: None).run(
                [sys.executable, '-c', 'import time; time.sleep(30)'], 'hung', 1)
            self.assertEqual((result['state'], result['exit_code'], result['safe_to_continue']), ('timeout', 124, True))
            self.assertEqual(result['timeout_reason'], 'stage timeout')

    def test_finish_overrides_long_stage_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            result = runtime.StageRunner(root, datetime.now() + timedelta(seconds=0.3), lambda _: None).run(
                [sys.executable, '-c', 'import time; time.sleep(30)'], 'deadline', 60)
            self.assertEqual(result['timeout_reason'], 'FINISH')
            self.assertEqual(result['exit_code'], 124)

    def test_expired_finish_never_starts(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime.subprocess, 'Popen') as launch:
            result = runtime.StageRunner(root, datetime.now() - timedelta(seconds=1)).run(['unused'], 'expired', 20)
            launch.assert_not_called()
            self.assertEqual(result['state'], 'skipped_finish')

    def test_failed_stop_blocks_following_gui(self):
        process = Mock(pid=123, returncode=None)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as root, patch.object(runtime.subprocess, 'Popen', return_value=process), \
             patch.object(runtime, 'WindowsJob', return_value=Mock()), \
             patch.object(runtime.time, 'monotonic', side_effect=[0, 2]), \
             patch.object(runtime, 'stop_tree', return_value=False):
            result = runtime.StageRunner(root, logger=lambda _: None).run(['unused'], 'blocked', 1)
            self.assertFalse(result['safe_to_continue'])

    def test_job_creation_error_still_stops_process(self):
        process = Mock(pid=123)
        with tempfile.TemporaryDirectory() as root, patch.object(runtime.subprocess, 'Popen', return_value=process), \
             patch.object(runtime, 'WindowsJob', side_effect=OSError('no job')), \
             patch.object(runtime, 'stop_tree', return_value=True) as stop:
            result = runtime.StageRunner(root, logger=lambda _: None).run(['unused'], 'job_error', 1)
            stop.assert_called_once_with(process, None, unittest.mock.ANY)
            self.assertEqual(result['state'], 'exception')

    def test_windows_timer_command_is_mocked_and_bounded(self):
        with patch.object(runtime.sys, 'platform', 'win32'), patch.object(runtime, 'seconds_left', return_value=600), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            self.assertTrue(runtime.arm_windows_shutdown(FINISH, lambda _: None))
            self.assertEqual(command.call_args.args[0][0:5], ['shutdown', '/s', '/f', '/t', '600'])
            self.assertEqual(command.call_args.kwargs['timeout'], 5)

    def test_pause_refuses_insufficient_time(self):
        with patch.object(runtime, 'seconds_left', return_value=179), patch.object(runtime.time, 'sleep') as sleep:
            self.assertFalse(runtime.pause_before_stage(60, FINISH, 120, lambda _: None))
            sleep.assert_not_called()

    def test_pause_sleeps_requested_minute(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(runtime.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(runtime.time, 'sleep', side_effect=sleep):
            self.assertTrue(runtime.pause_before_stage(60))
            self.assertEqual(clock[0], 60)


class ChainTests(unittest.TestCase):
    def chain(self, failures=None, safe=True, pause_ok=True, secret=True, device='2', download_error=False):
        calls = []
        failures = failures or {}
        class FakeRunner:
            def __init__(self, *args):
                pass
            def run(self, command, name, timeout):
                calls.append(('stage', name, command))
                code = failures.get(name, 0)
                return {'stage': name, 'exit_code': code, 'safe_to_continue': safe,
                        'state': 'error' if code else 'ok'}
        def pause(seconds, *args, **kwargs):
            calls.append(('pause', seconds))
            return pause_ok
        fake = SimpleNamespace(StageRunner=FakeRunner, seconds_left=lambda _: 600, pause_before_stage=pause)
        status = {'stages': []}
        def download(url, *args):
            if download_error and url == entry.WORKER_URL:
                raise OSError('offline')
        with patch.object(entry, 'download_python', side_effect=download), \
             patch.object(entry, 'write_chain_status'), patch.object(entry, 'log'), \
             patch.object(entry, 'executor_secret_available', return_value=secret), \
             patch.object(entry, 'detect_executor_device', return_value=device), \
             patch.dict(os.environ, {'DISPATCHER_MANAGED': '0'}), \
             patch.object(sys, 'argv', ['entry.py', '-a']):
            code = entry.execute_chain(fake, CFG, FINISH, status)
        return code, status, calls

    def test_sequence_and_one_minute_delay(self):
        code, status, calls = self.chain()
        self.assertEqual(code, 0)
        self.assertEqual([(x[0], x[1]) for x in calls], [('stage', 'linkedin'), ('pause', 7),
                         ('stage', 'actions'), ('pause', 60), ('stage', 'profi')])
        self.assertEqual(calls[-1][2][-1], '-a')

    def test_linkedin_failure_skips_actions_but_allows_safe_profi(self):
        code, status, calls = self.chain({'linkedin': 3})
        self.assertEqual(code, 3)
        self.assertEqual([x[1] for x in calls if x[0] == 'stage'], ['linkedin', 'profi'])
        self.assertEqual(status['stages'][1]['state'], 'skipped')

    def test_action_failure_does_not_drop_profi(self):
        code, status, calls = self.chain({'actions': 4})
        self.assertEqual(code, 4)
        self.assertEqual(status['stages'][-1]['stage'], 'profi')

    def test_unsafe_process_blocks_profi(self):
        code, status, calls = self.chain({'linkedin': 124}, safe=False)
        self.assertEqual(code, 124)
        self.assertEqual(status['stages'][-1]['state'], 'blocked_process_alive')
        self.assertNotIn(('pause', 60), calls)

    def test_no_time_skips_profi(self):
        code, status, calls = self.chain(pause_ok=False)
        self.assertEqual(code, 124)
        self.assertEqual(status['stages'][-1]['state'], 'skipped_finish')

    def test_download_error_is_diagnostic(self):
        code, status, calls = self.chain(download_error=True)
        self.assertEqual(code, 1)
        self.assertEqual(status['stages'][0]['error'], 'offline')
        self.assertEqual(status['stages'][-1]['stage'], 'profi')

    def test_laptop_does_not_start_profi(self):
        code, status, calls = self.chain(device='1')
        self.assertEqual(status['stages'][-1]['state'], 'disabled')

    def test_cleanup_protects_profi_page(self):
        with tempfile.TemporaryDirectory() as root, patch.object(entry, 'SAVED_PAGES', Path(root)):
            for name in ('linkedin_feed.mhtml', 'profi_orders.mhtml', 'unrelated.html'):
                (Path(root) / name).write_text('synthetic')
            self.assertEqual(entry.cleanup_saved_pages(), (1, 0))
            self.assertTrue((Path(root) / 'profi_orders.mhtml').exists())

    def test_interrupt_before_runtime_load_cancels_owned_timer(self):
        with patch.object(entry, 'load_chain_config', return_value=CFG), patch.object(entry, 'run_deadline', return_value=FINISH), \
             patch.object(entry, 'arm_early_shutdown', return_value=True), patch.object(entry, 'write_chain_status'), \
             patch.object(entry, 'cleanup_saved_pages', side_effect=KeyboardInterrupt), \
             patch.object(entry.sys, 'platform', 'win32'), patch.object(entry.subprocess, 'run') as cancel, \
             tempfile.TemporaryDirectory() as root, patch.object(entry, 'LOG_DIR', Path(root)), \
             patch.dict(os.environ, {'DISPATCHER_SHUTDOWN_ARMED': '0'}):
            self.assertEqual(entry.main(), 130)
            self.assertEqual(cancel.call_args.args[0], ['shutdown', '/a'])


class DispatcherTests(unittest.TestCase):
    def execute(self, worker_error=False, cancel=False, refreshed=None, timer_error=False):
        events = []
        def arm(*args):
            events.append('arm')
            if timer_error:
                raise OSError('timer denied')
            return True
        def download(*args):
            events.append('download')
            if worker_error:
                raise RuntimeError('worker download failed')
            if cancel:
                raise KeyboardInterrupt()
            return Path('synthetic.py')
        def wait(target, label):
            events.append((label, target))
        with tempfile.TemporaryDirectory() as root, patch.object(dispatcher, 'LOG_DIR', Path(root)), \
             patch.object(dispatcher, 'datetime', Frozen), patch.object(dispatcher, 'log'), \
             patch.object(dispatcher, 'load_control', side_effect=[CFG, refreshed or CFG]), \
             patch.object(dispatcher, 'arm_windows_shutdown', side_effect=arm), \
             patch.object(dispatcher, 'download_worker', side_effect=download), \
             patch.object(dispatcher, 'run_worker', return_value=0), \
             patch.object(dispatcher, 'publish_diagnostics', side_effect=lambda *args: events.append('diagnostics')), \
             patch.object(dispatcher, 'wait_until', side_effect=wait), \
             patch.object(dispatcher, 'shutdown_windows', side_effect=lambda: events.append('shutdown')), \
             patch.object(dispatcher, 'cancel_owned_shutdown', side_effect=lambda *args: events.append('cancel')):
            dispatcher.main()
        return events

    def test_timer_precedes_work_and_finish_stays_fixed(self):
        events = self.execute(worker_error=True)
        self.assertEqual(events[0:2], ['arm', 'download'])
        self.assertIn(('FINISH', FINISH), events)
        self.assertEqual(events[-1], 'shutdown')
        self.assertNotIn('cancel', events)

    def test_timer_setup_failure_still_runs_and_shuts_down(self):
        events = self.execute(timer_error=True)
        self.assertIn('download', events)
        self.assertEqual(events[-1], 'shutdown')

    def test_user_cancel_cancels_only_own_timer(self):
        events = self.execute(cancel=True)
        self.assertIn('cancel', events)
        self.assertNotIn('shutdown', events)

    def test_disabled_without_shutdown_cancels_timer(self):
        events = self.execute(refreshed={'enabled': False, 'shutdown_when_disabled': False})
        self.assertIn('cancel', events)
        self.assertNotIn('download', events)
        self.assertNotIn('shutdown', events)

    def test_disabled_with_shutdown_powers_off(self):
        events = self.execute(refreshed={'enabled': False, 'shutdown_when_disabled': True})
        self.assertIn('shutdown', events)
        self.assertNotIn('download', events)

    def test_after_finish_does_not_roll_to_tomorrow(self):
        start, finish = dispatcher.resolve_times(NOW.replace(hour=9), CFG)
        self.assertEqual(finish, FINISH)

    def test_overnight_keeps_previous_start_and_today_finish(self):
        cfg = {**CFG, 'schedule': {'start_time': '23:00', 'finish_time': '01:00'}}
        now = NOW.replace(hour=0, minute=30)
        start, finish = dispatcher.resolve_times(now, cfg)
        self.assertEqual(start, now)
        self.assertEqual(finish, now.replace(hour=1, minute=0))

    def test_network_diagnostics_have_a_deadline(self):
        runner = Mock()
        with tempfile.TemporaryDirectory() as root, patch.object(dispatcher, 'LOG_DIR', Path(root)), \
             patch.object(dispatcher, 'StageRunner', return_value=runner), \
             patch.object(dispatcher, 'seconds_left', return_value=10):
            dispatcher.publish_diagnostics(CFG, 124, NOW, NOW, FINISH)
            self.assertEqual(runner.run.call_args.args[2], 8)
            self.assertTrue((Path(root) / 'dispatcher_status.json').exists())


if __name__ == '__main__':
    unittest.main()

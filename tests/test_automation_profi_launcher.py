"""Launcher regression checks: no real network, Chrome, upload or shutdown."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('profi_launcher', SCRIPTS / 'profi_worker_entry.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
SOURCE = b'print("synthetic collector")\n'


class LauncherTests(unittest.TestCase):
    def test_raw_download_succeeds_even_when_api_is_rate_limited(self):
        requests = []
        def download(url):
            requests.append(url)
            if 'api.github.com' in url:
                raise HTTPError(url, 403, 'rate limit exceeded', {}, None)
            return SOURCE
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root, \
                 patch.object(launcher, 'ROOT', Path(root)), \
                 patch.object(launcher, 'download', side_effect=download), \
                 patch.object(launcher.sys, 'argv', ['entry.py', '-a']), \
                 patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=code)) as run, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.main(), code)
                worker = Path(root) / 'cache' / 'profi_worker_unified_runtime.py'
                self.assertEqual(worker.read_bytes(), SOURCE)
                self.assertFalse(worker.with_suffix('.tmp').exists())
                self.assertIn(hashlib.sha256(SOURCE).hexdigest()[:12], output.getvalue())
                self.assertEqual(run.call_args.args[0],
                                 [sys.executable, str(worker), '--root', root, '-a'])
                self.assertEqual(run.call_args.kwargs['cwd'], root)
        self.assertEqual(requests, [launcher.WORKER_URL, launcher.WORKER_URL])
        self.assertTrue(all(url.startswith('https://raw.githubusercontent.com/') for url in requests))

    def test_invalid_source_preserves_previous_worker_and_does_not_launch(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / 'cache'
            cache.mkdir()
            worker = cache / 'profi_worker_unified_runtime.py'
            worker.write_bytes(SOURCE)
            with patch.object(launcher, 'ROOT', Path(root)), \
                 patch.object(launcher, 'download', return_value=b'def broken(:'), \
                 patch.object(launcher.subprocess, 'run') as run:
                with self.assertRaises(SyntaxError):
                    launcher.main()
                self.assertEqual(worker.read_bytes(), SOURCE)
                run.assert_not_called()

    def test_download_failure_does_not_launch_or_replace_cached_worker(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / 'cache'
            cache.mkdir()
            worker = cache / 'profi_worker_unified_runtime.py'
            worker.write_bytes(SOURCE)
            with patch.object(launcher, 'ROOT', Path(root)), \
                 patch.object(launcher, 'download', side_effect=OSError('offline')), \
                 patch.object(launcher.subprocess, 'run') as run:
                with self.assertRaises(OSError):
                    launcher.main()
                self.assertEqual(worker.read_bytes(), SOURCE)
                run.assert_not_called()

    def test_bat_bootstrap_updates_old_launcher_before_run(self):
        bat = (SCRIPTS / 'profi_run_now.bat').read_text(encoding='utf-8')
        bootstrap = re.search(r' -u -c "([^"\r\n]+)" "%ENTRY_FILE%"', bat).group(1)
        compile(bootstrap, 'BAT bootstrap', 'exec')
        self.assertLess(bat.index(bootstrap), bat.index('"%PYTHON_EXE%" -u "%ENTRY_FILE%" -a'))
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'path with spaces' / 'profi_worker_entry_runtime.py'
            target.parent.mkdir()
            target.write_text('raise RuntimeError("old API launcher")\n')
            response = SimpleNamespace(__enter__=lambda self: self, read=lambda: SOURCE)
            with patch('urllib.request.urlopen', return_value=response) as request, \
                 patch.object(sys, 'argv', ['-c', str(target)]):
                exec(bootstrap, {})
            self.assertEqual(target.read_bytes(), SOURCE)
            self.assertEqual(request.call_args.args[0].full_url,
                             'https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/profi_worker_entry.py')

    def test_bat_bootstrap_invalid_source_preserves_old_launcher(self):
        bat = (SCRIPTS / 'profi_run_now.bat').read_text(encoding='utf-8')
        bootstrap = re.search(r' -u -c "([^"\r\n]+)" "%ENTRY_FILE%"', bat).group(1)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'entry.py'
            target.write_bytes(SOURCE)
            response = SimpleNamespace(read=lambda: b'def broken(:')
            with patch('urllib.request.urlopen', return_value=response), \
                 patch.object(sys, 'argv', ['-c', str(target)]):
                with self.assertRaises(SyntaxError):
                    exec(bootstrap, {})
            self.assertEqual(target.read_bytes(), SOURCE)


if __name__ == '__main__':
    unittest.main()

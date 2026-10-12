import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = load("profi_worker_test", "profi_worker_unified.py")
        self.entry = load("profi_entry_test", "profi_worker_entry.py")
        self.fixture = json.loads((ROOT / "tests" / "fixtures" / "profi_capture.json").read_text(encoding="utf-8"))

    def test_existing_local_parser_extracts_synthetic_fields(self):
        capture = self.worker.datetime.fromisoformat(self.fixture["captured_at"])
        rows = self.worker.extract_orders(self.fixture["html"], "synthetic.html", capture)
        rows = {r["order_id"]: r for r in rows}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows["900002"]["budget_min_rub"], 1500)
        self.assertEqual(rows["900002"]["budget_max_rub"], 2500)
        self.assertEqual(rows["900003"]["title"], "Тест карусели")
        self.assertFalse(rows["900004"]["remote"])

    def test_parse_only_roots_and_json_only_upload(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "synthetic.html"
            source.write_text(self.fixture["html"], encoding="utf-8")
            with patch.object(w.sys, "argv", ["worker.py", "--root", str(folder), "--parse-only", str(source)]), patch.object(w, "get_github_token", return_value="test"), patch.object(w, "upload_to_github") as upload, patch.object(w, "capture_profi_page") as capture:
                w.main()
            capture.assert_not_called()
            expected = folder / "reports" / "profi_latest.json"
            upload.assert_called_once_with(expected)
            result = json.loads(expected.read_text(encoding="utf-8"))
            self.assertEqual(result["parser_version"], "4.4")
            self.assertEqual(result["summary"]["orders"], 6)
            self.assertEqual(w.GITHUB_PATH, "profi_latest.json")
            self.assertNotIn("html", result)
            self.assertEqual(w.GITHUB_TOKEN_FILES[0], folder / "github_token.txt")

    def test_window_close_before_parse_and_upload(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "synthetic.html"
            source.write_text(self.fixture["html"], encoding="utf-8")
            events = []
            extract = w.extract_orders
            def parse(*args):
                events.append("parse")
                return extract(*args)
            with patch.object(w.sys, "argv", ["worker.py", "--root", str(folder), "-a"]), patch.object(w, "get_github_token", return_value="test"), patch.object(w, "capture_profi_page", side_effect=lambda: events.append("capture") or source), patch.object(w, "close_profi_window_by_mouse", side_effect=lambda: events.append("close")), patch.object(w, "extract_orders", side_effect=parse), patch.object(w, "upload_to_github", side_effect=lambda _: events.append("upload")):
                w.main()
            self.assertEqual(events, ["capture", "close", "parse", "upload"])

    def test_launcher_pins_code_and_forwards_root_and_arguments(self):
        entry = self.entry
        sha = "a" * 40
        urls = []
        def download(url):
            urls.append(url)
            return json.dumps({"object": {"sha": sha}}).encode() if "api.github.com" in url else b"# valid collector\n"
        with tempfile.TemporaryDirectory() as directory, patch.object(entry, "ROOT", Path(directory)), patch.object(entry, "download", side_effect=download), patch.object(entry.sys, "argv", ["entry.py", "--close-y", "0.02"]), patch.object(entry.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(entry.main(), 0)
            self.assertTrue((Path(directory) / "cache" / "profi_worker_unified_runtime.py").exists())
            command = run.call_args.args[0]
            self.assertIn("--root", command)
            self.assertEqual(command[-2:], ["--close-y", "0.02"])
        self.assertEqual(len(urls), 2)
        self.assertIn("/" + sha + "/scripts/profi_worker_unified.py", urls[1])

    def test_invalid_download_does_not_replace_cached_worker(self):
        entry = self.entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "cache" / "profi_worker_unified_runtime.py"
            worker.parent.mkdir()
            worker.write_text("# previous valid version\n")
            payloads = [json.dumps({"object": {"sha": "a" * 40}}).encode(), b"not Python !"]
            with patch.object(entry, "ROOT", root), patch.object(entry, "download", side_effect=payloads), patch.object(entry.subprocess, "run") as run:
                with self.assertRaises(SyntaxError):
                    entry.main()
            run.assert_not_called()
            self.assertEqual(worker.read_text(), "# previous valid version\n")

    def test_old_launcher_download_target_is_compatible(self):
        self.assertEqual((ROOT / "scripts" / "profi_capture_worker.py").read_bytes(),
                         (ROOT / "scripts" / "profi_worker_unified.py").read_bytes())


if __name__ == "__main__":
    unittest.main()

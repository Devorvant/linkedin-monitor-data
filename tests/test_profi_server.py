import ast
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import profi_capture_transport as transport
import profi_ingest as ingest
import profi_monitor as monitor

FIXTURE = ROOT / "tests" / "fixtures" / "profi_capture.json"


class ServerParserTests(unittest.TestCase):
    def setUp(self):
        self.capture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_synthetic_card_capture_fields_on_server(self):
        report = ingest.parse_capture(self.capture)
        self.assertEqual(report["parser_location"], "github_actions")
        self.assertEqual(report["summary"], {"orders": 6, "remote_orders": 5})
        rows = {r["order_id"]: r for r in report["orders"]}
        self.assertEqual(sum(bool(r["budget"]) for r in rows.values()), 5)
        self.assertEqual(rows["900001"]["client"], "Тестовый клиент")
        self.assertEqual(rows["900001"]["posted_at"], "2026-10-12T09:22:30+03:00")
        self.assertEqual(rows["900002"]["schedule"], "Ср 17:00-18:00")
        self.assertEqual(rows["900002"]["budget_max_rub"], 2500)
        self.assertEqual(rows["900003"]["title"], "Тест карусели")
        self.assertTrue(rows["900003"]["description"])
        self.assertFalse(rows["900004"]["remote"])

    def test_transport_removes_account_and_active_content(self):
        document = '''<html><header>ACCOUNT_SENTINEL</header><a data-testid="1_order-snippet" onclick="danger()" href="https://example.com/?token=PRIVATE"><div><h3>Тест</h3><span>до 1000 ₽</span></div><div><p>Описание</p><input value="PASSWORD"><script>SESSION_KEY</script><iframe>PRIVATE_FRAME</iframe><span style="background:url(secret)">Деталь</span></div></a></html>'''
        html = transport.card_document(document)
        for private in ("ACCOUNT_SENTINEL", "SESSION_KEY", "PASSWORD", "PRIVATE_FRAME", "onclick", "example.com", "secret"):
            self.assertNotIn(private, html)
        self.assertIn('href="https://profi.ru/backoffice/n.php?o=1"', html)
        self.assertIn("Описание", html)

    def test_checksum_mismatch_fails(self):
        self.capture["html"] += "changed"
        with self.assertRaises(ValueError):
            ingest.parse_capture(self.capture)

    def test_empty_capture_fails(self):
        self.capture["html"] = "<html></html>"
        self.capture["html_sha256"] = hashlib.sha256(self.capture["html"].encode()).hexdigest()
        with self.assertRaises(ValueError):
            ingest.parse_capture(self.capture)
        with self.assertRaises(ValueError):
            transport.card_document("<html>No cards</html>")

    def test_ingest_is_cached_and_reparse_is_one_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "profi_inputs" / "2026-10-12" / FIXTURE.name
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.capture), encoding="utf-8")
            self.assertEqual(ingest.ingest(root), 1)
            self.assertEqual(ingest.ingest(root), 0)
            self.assertEqual(ingest.ingest(root, force=True), 1)
            status = monitor.build_outputs(root, [])
            self.assertEqual(status["summary"]["scans"], 1)
            latest = monitor.read_json(root / "profi_latest.json")
            self.assertEqual(latest["parser_version"], "4.5-server")

    def test_older_test_capture_does_not_replace_newer_current(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "profi_inputs" / "2026-10-12" / FIXTURE.name
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.capture), encoding="utf-8")
            latest = {"source": {"modified_at": "2026-10-12T10:11:28"}, "orders": [{"order_id": "9"}]}
            monitor.write_json(root / "profi_latest.json", latest)
            ingest.ingest(root)
            self.assertEqual(monitor.read_json(root / "profi_latest.json"), latest)

    def test_capture_worker_has_no_field_parser_or_bs4_dependency(self):
        source = (ROOT / "scripts" / "profi_capture_worker.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn("extract_orders", {n.name for n in tree.body if isinstance(n, ast.FunctionDef)})
        self.assertNotIn("BeautifulSoup", source)
        self.assertNotIn("bs4", source)

    def test_capture_upload_retries_conflict_and_sends_html_not_orders(self):
        spec = importlib.util.spec_from_file_location("capture_worker", ROOT / "scripts" / "profi_capture_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        calls = []
        conflicts = [True, False]
        def api(url, method="GET", data=None):
            if method == "GET":
                return {"sha": "current"}
            calls.append(data)
            if conflicts.pop(0):
                raise RuntimeError("GitHub API error 409: conflict")
            return {}
        with patch.object(worker, "PUBLIC_CAPTURE_UPLOAD_ENABLED", True), patch.object(worker.transport, "prepare_capture", return_value=self.capture), patch.object(worker, "github_api_request", side_effect=api), patch.object(worker.time, "sleep"):
            remote = worker.upload_capture(Path("saved.mhtml"))
        self.assertTrue(remote.startswith("profi_inputs/2026-10-12/"))
        payload = json.loads(base64.b64decode(calls[-1]["content"]))
        self.assertIn("html", payload)
        self.assertNotIn("orders", payload)
        self.assertEqual(len(calls), 2)

    def test_public_upload_disabled_before_any_request(self):
        spec = importlib.util.spec_from_file_location("capture_worker_disabled", ROOT / "scripts" / "profi_capture_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with patch.object(worker, "github_api_request") as api:
            with self.assertRaises(RuntimeError):
                worker.upload_capture(Path("saved.mhtml"))
        api.assert_not_called()

    def test_launcher_pins_all_downloads_and_preserves_root(self):
        spec = importlib.util.spec_from_file_location("worker_entry", ROOT / "scripts" / "profi_worker_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        sha = "a" * 40
        downloads = []
        def download(url):
            downloads.append(url)
            return json.dumps({"object": {"sha": sha}}).encode() if "api.github.com" in url else b"# valid Python\n"
        with tempfile.TemporaryDirectory() as folder, patch.object(entry, "ROOT", Path(folder)), patch.object(entry, "download", side_effect=download), patch.object(entry.sys, "argv", ["entry.py", "--close-y", "0.02"]), patch.object(entry.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(entry.main(), 0)
            self.assertTrue((Path(folder) / "cache" / "profi_capture_transport.py").exists())
            self.assertIn("--root", run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][-2:], ["--close-y", "0.02"])
        self.assertTrue(all("/" + sha + "/" in u for u in downloads[1:]))


if __name__ == "__main__":
    unittest.main()

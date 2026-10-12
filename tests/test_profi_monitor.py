import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess
import json

SPEC = importlib.util.spec_from_file_location("profi_monitor", Path(__file__).resolve().parents[1] / "scripts" / "profi_monitor.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def report(stamp, ids, version="4.4"):
    return {"parser_version": version,
            "source": {"filename": "profi_" + stamp + ".mhtml", "captured_at": stamp},
            "orders": [{"order_id": str(i), "title": "MATLAB: модель системы управления",
                        "description": "Разработка алгоритма стабилизации", "remote": True,
                        "budget": "от 5000 ₽", "budget_min_rub": 5000,
                        "budget_max_rub": None, "budget_kind": "lower_bound"} for i in ids]}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, raw):
        m.store_snapshot(self.root, raw)
        return m.build_outputs(self.root, [])

    def test_repeated_capture_is_one_scan(self):
        data = report("2026-10-12T09:47:30+03:00", [1, 2])
        self.ingest(data)
        status = self.ingest(data)
        self.assertEqual(status["summary"]["scans"], 1)
        self.assertEqual(status["summary"]["unique_observed"], 2)
        self.assertEqual([r["times_seen"] for r in m.read_json(self.root / "profi_history.json")["orders"]], [1, 1])

    def test_reparse_updates_snapshot_without_increment(self):
        data = report("2026-10-12T09:47:30+03:00", [1], version="legacy")
        self.ingest(data)
        data["parser_version"] = "4.4"
        data["orders"][0]["description"] = "Corrected"
        status = self.ingest(data)
        self.assertEqual(status["summary"]["scans"], 1)
        self.assertEqual(status["parser_version"], "4.4")
        self.assertIsNone(status["warning"])

    def test_collector_filename_is_stable_when_capture_time_is_corrected(self):
        data = report("2026-10-12T00:00:01+03:00", [1])
        data["source"]["filename"] = "profi_orders_2026-10-12_00-00-01.mhtml"
        self.ingest(data)
        data["source"]["captured_at"] = "2026-10-11T23:59:59+03:00"
        status = self.ingest(data)
        self.assertEqual(status["summary"]["scans"], 1)
        self.assertEqual(status["captured_at"], "2026-10-11T23:59:59+03:00")

    def test_new_ids_and_repeated_ids_daily_unique(self):
        self.ingest(report("2026-10-12T09:00:00+03:00", [1, 2]))
        status = self.ingest(report("2026-10-12T12:00:00+03:00", [2, 3]))
        self.assertEqual(status["summary"]["new_observed"], 1)
        self.assertEqual(status["daily"][0]["orders"], 3)
        self.assertEqual(status["daily"][0]["new_observed"], 3)
        self.assertEqual(status["daily"][0]["scans"], 2)

    def test_first_last_seen_and_missing_not_closed(self):
        self.ingest(report("2026-10-11T09:00:00+03:00", [1, 2]))
        self.ingest(report("2026-10-12T12:00:00+03:00", [2, 3]))
        rows = {r["order_id"]: r for r in m.read_json(self.root / "profi_history.json")["orders"]}
        self.assertFalse(rows["1"]["is_current"])
        self.assertNotIn("closed", rows["1"])
        self.assertEqual(rows["2"]["first_seen"], "2026-10-11T09:00:00+03:00")
        self.assertEqual(rows["2"]["last_seen"], "2026-10-12T12:00:00+03:00")
        self.assertEqual(rows["2"]["observed_dates"], ["2026-10-11", "2026-10-12"])

    def test_out_of_order_upload_keeps_newest_current(self):
        self.ingest(report("2026-10-12T12:00:00+03:00", [3]))
        status = self.ingest(report("2026-10-11T09:00:00+03:00", [1]))
        self.assertEqual(status["captured_at"], "2026-10-12T12:00:00+03:00")
        self.assertEqual(m.read_json(self.root / "profi_current.json")["orders"][0]["order_id"], "3")

    def test_timezone_day_is_moscow(self):
        status = self.ingest(report("2026-10-11T22:00:00Z", [1]))
        self.assertEqual(status["daily"][0]["date"], "2026-10-12")
        self.assertEqual(status["captured_at"], "2026-10-12T01:00:00+03:00")

    def test_no_valid_orders_does_not_replace_snapshot(self):
        self.ingest(report("2026-10-12T09:00:00+03:00", [1]))
        before = (self.root / "profi_current.json").read_bytes()
        with self.assertRaises(ValueError):
            m.store_snapshot(self.root, report("2026-10-12T12:00:00+03:00", []))
        self.assertEqual((self.root / "profi_current.json").read_bytes(), before)

    def test_profile_tags_are_explained_and_teaching_is_secondary(self):
        row = m.classify({"title": "MATLAB и Simulink", "description": "Модель системы управления"})
        self.assertEqual(row["profile_fit"], "high")
        self.assertTrue(row["match_terms"])
        row = m.classify({"title": "MATLAB", "description": "Подготовка к экзамену"})
        self.assertEqual(row["profile_fit"], "medium")
        self.assertEqual(m.classify({"title": "Сайт на Tilda"})["profile_fit"], "other")

    def test_same_id_twice_in_source_counts_once(self):
        status = self.ingest(report("2026-10-12T09:00:00+03:00", [1, 1]))
        self.assertEqual(status["summary"]["orders"], 1)

    def test_snapshot_keeps_budget_bounds_and_safe_order_url(self):
        data = report("2026-10-12T09:00:00+03:00", [1])
        data["orders"][0]["url"] = "javascript:alert(1)"
        self.ingest(data)
        row = m.read_json(self.root / "profi_current.json")["orders"][0]
        self.assertIsNone(row["budget_max_rub"])
        self.assertEqual(row["budget_min_rub"], 5000)
        self.assertEqual(row["url"], "https://profi.ru/backoffice/n.php?o=1")

    def test_legacy_report_remains_readable_and_marked(self):
        data = report("2026-10-12T09:00:00+03:00", [1])
        del data["parser_version"]
        data["source"] = {"filename": "old.mhtml", "modified_at": "2026-10-12T09:00:00"}
        status = self.ingest(data)
        self.assertEqual(status["parser_version"], "legacy")
        self.assertIn("старым парсером", status["warning"])

    def test_git_backfill_preserves_uploads_between_pipeline_runs(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  capture_output=True, text=True)
        git("init")
        git("config", "user.name", "Monitor Test")
        git("config", "user.email", "monitor@example.test")
        for stamp, ids in (("2026-10-11T09:00:00+03:00", [1]), ("2026-10-12T12:00:00+03:00", [2])):
            (self.root / "profi_latest.json").write_text(json.dumps(report(stamp, ids)), encoding="utf-8")
            git("add", "profi_latest.json")
            git("commit", "-m", "Capture")
        commits = m.git_backfill(self.root, [])
        self.assertEqual(len(commits), 2)
        status = m.build_outputs(self.root, commits)
        self.assertEqual(status["summary"]["scans"], 2)
        self.assertEqual(status["summary"]["unique_observed"], 2)
        self.assertEqual(m.git_backfill(self.root, commits), commits)


if __name__ == "__main__":
    unittest.main()

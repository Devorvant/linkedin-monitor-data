import copy
import gzip
import base64
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import action_planner as planner
import recommendation_rebuilder as rebuilder
import relationship_watchlist as watchlist
from person_identity import person_profile, with_feed_profiles


def signal(sid="s1", author="Alice", **kwargs):
    return {"signal_id": sid, "author": author, "author_type": "person",
            "source_url": "https://www.linkedin.com/in/alice/", "priority": "high",
            "confidence": 95, "career_relevance": 90, "technical_relevance": 85,
            "people": [{"name": author, "relation": "author", "confidence": "confirmed"}],
            **kwargs}


class PersonIdentityTests(unittest.TestCase):
    def test_mentioned_profile_is_not_selected_person(self):
        row = signal(author="Sarla Aviation", author_type="company",
                     source_url="https://www.linkedin.com/company/sarla-aviation/",
                     people=[{"name": "Dr. H. Sudarshan Ballal", "confidence": "confirmed"}],
                     links=[{"url": "https://www.linkedin.com/in/rakesh-gaonkar/", "relation": "mentioned"}])
        row = with_feed_profiles(row, {"s1": [{"name": "Rakesh Gaonkar", "url": row["links"][0]["url"], "source": "feed_mention"}]})
        self.assertIsNone(person_profile(row, "Dr. H. Sudarshan Ballal")[0])
        self.assertEqual(rebuilder.entity_key(row), "person:dr. h. sudarshan ballal")
        self.assertIsNone(watchlist.person_profile_url("Dr. H. Sudarshan Ballal", row))

    def test_named_mention_can_use_nonmatching_slug(self):
        row = signal(author_type="company", source_url="https://www.linkedin.com/company/example/",
                     people=[{"name": "Nate Glasscock", "confidence": "confirmed"}])
        row = with_feed_profiles(row, {"s1": [{"name": "Nate Glasscock", "url": "https://www.linkedin.com/in/nathan-glasscock/", "source": "feed_mention"}]})
        self.assertEqual(person_profile(row, "Nate Glasscock")[0], "https://www.linkedin.com/in/nathan-glasscock/")

    def test_conflicting_and_non_linkedin_profiles_stay_unknown(self):
        row = signal(source_url="https://example.org/in/alice/",
                     people=[{"name": "Alice", "profile_url": "https://example.org/in/alice/"}])
        self.assertIsNone(person_profile(row, "Alice")[0])
        row["people"] = [{"name": "Alice", "profile_url": "https://www.linkedin.com/in/alice-one/"},
                         {"name": "Alice", "profile_url": "https://www.linkedin.com/in/alice-two/"}]
        self.assertIsNone(person_profile(row, "Alice")[0])

    def test_mentioned_connection_does_not_leak(self):
        known = planner.load_known_relationships(ROOT / "linkedin_relationships")
        known["connection_slugs"].add("bob")
        known["follower_slugs"].add("bob")
        row = signal(people=[{"name": "Alice", "relation": "author"},
                             {"name": "Bob", "linkedin_id": "bob-id"}],
                     links=[{"url": "https://www.linkedin.com/in/bob/", "relation": "mentioned"}])
        known["connection_ids"].add("bob-id")
        labels = planner.known_relationship_state(row, planner.target_info(row), known)["labels"]
        self.assertNotIn("CONNECTION", labels)
        self.assertNotIn("FOLLOWER", labels)

    def test_crm_matches_target_kind_and_preserves_duplicate_contact_guards(self):
        row = signal(author="Alice • 1-й", company="Example",
                     people=[{"name": "Bob", "confidence": "confirmed"}])
        company = {"id": "company:example", "kind": "company", "name": "Example"}
        alice = {"id": "person:alice", "kind": "person", "name": "Alice",
                 "profile_url": "https://www.linkedin.com/in/alice/", "status": "WATCH"}
        displayed = {**alice, "id": "person:alice degree", "name": "Alice • 1-й"}
        crm = {"items": [company, alice, displayed]}
        self.assertEqual(planner.match_crm(row, crm)["id"], displayed["id"])
        alice["do_not_contact"] = True
        self.assertTrue(planner.match_crm(row, crm)["do_not_contact"])

    def test_repair_preserves_manual_fields_and_removes_guessed_url(self):
        item = {"id": "person:ballal", "name": "Dr. H. Sudarshan Ballal", "kind": "person",
                "profile_url": "https://www.linkedin.com/in/adrnschm/", "status": "CONTACTED",
                "notes": "Keep this note", "tags": ["manual"], "do_not_contact": True,
                "last_contacted_at": "2026-09-01", "next_follow_up_at": "2026-11-01"}
        existing = {"items": [item]}
        repaired = watchlist.repair_person_profiles(existing, [signal()])["items"][0]
        self.assertIsNone(repaired["profile_url"])
        for field in watchlist.MANUAL_FIELDS:
            self.assertEqual(item[field], repaired[field])
        self.assertEqual(existing["items"][0], item)

    def test_company_bucket_does_not_use_unrelated_mentioned_company(self):
        row = signal(author="Example", author_type="company", company="Actual target",
                     source_url="https://www.linkedin.com/company/example/", people=[],
                     links=[{"url": "https://www.linkedin.com/company/unrelated/"}])
        self.assertEqual(rebuilder.entity_key(row), "company:actual target")


class HistoryTests(unittest.TestCase):
    def test_repeated_signal_keeps_dates_without_counting_latest_twice(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder); history = root / "history" / "feed_signals"; history.mkdir(parents=True)
            for day in ("2026-09-28", "2026-10-02"):
                (history / (day + ".json")).write_text(json.dumps({"signals": [signal()]}))
            latest = root / "feed_signals_latest.json"
            latest.write_text(json.dumps({"generated_at": "2026-10-02T08:00:00Z", "signals": [signal()]}))
            rows, _ = rebuilder.collect_signals(history, latest)
            self.assertEqual(len(rows), 1)
            reps, _ = rebuilder.representative_signals(rows)
            stats = reps[0]["_history_stats"]
            self.assertEqual(stats["dates"], ["2026-09-28", "2026-10-02"])
            self.assertEqual(stats["occurrences"], 1)
            self.assertEqual(stats["signal_dates"][0]["dates"], stats["dates"])

    def test_representative_prefers_freshness_then_priority_and_relevance(self):
        older = signal("old", _history_date="2026-09-28")
        fresh = signal("fresh", _history_date="2026-10-02", priority="medium", career_relevance=70)
        reps, _ = rebuilder.representative_signals([older, fresh])
        self.assertEqual(reps[0]["signal_id"], "fresh")
        high = signal("high", _history_date="2026-10-02", confidence=90)
        reps, _ = rebuilder.representative_signals([fresh, high])
        self.assertEqual(reps[0]["signal_id"], "high")
        lower_fit = signal("lower-fit", _history_date="2026-10-02", confidence=99, career_relevance=50)
        reps, _ = rebuilder.representative_signals([high, lower_fit])
        self.assertEqual(reps[0]["signal_id"], "high")

    def test_ties_are_deterministic(self):
        first = signal("a", _history_date="2026-10-02")
        second = signal("b", _history_date="2026-10-02")
        left, _ = rebuilder.representative_signals([first, second])
        right, _ = rebuilder.representative_signals([second, first])
        self.assertEqual(left[0]["signal_id"], right[0]["signal_id"])


class AnalyzerTests(unittest.TestCase):
    def test_llm_keeps_parser_profile_and_ignores_invented_url(self):
        packed = ROOT / "scripts/feed_signal_analyzer.py.gz.b64"
        source = gzip.decompress(base64.b64decode(packed.read_text())).decode("utf-8")
        module = types.ModuleType("test_analyzer")
        exec(compile(source, str(packed), "exec"), module.__dict__)
        rule = signal(people=[{"name": "Nate Glasscock", "profile_url": "https://www.linkedin.com/in/nathan-glasscock/"}],
                      author_type="company", source_url="https://www.linkedin.com/company/example/")
        raw = {"people": [{"name": "Nate Glasscock", "profile_url": "https://www.linkedin.com/in/invented/"},
                          {"name": "Unknown Person", "profile_url": "https://www.linkedin.com/in/invented-too/"}]}
        people = module.normalize_llm_result(raw, rule)["people"]
        self.assertEqual(people[0]["profile_url"], rule["people"][0]["profile_url"])
        self.assertNotIn("profile_url", people[1])


if __name__ == "__main__":
    unittest.main()

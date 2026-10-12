from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import profi_recommendation_history as history


def report(capture='2026-10-12T10:00:00+03:00', generated='2026-10-12T13:00:00Z', action='clarify', source='ai'):
    return {'captured_at': capture, 'generated_at': generated, 'analysis_version': 'test',
            'source_generated_at': generated, 'status': 'AI', 'model': 'test',
            'items': [{'order_id': '123', 'title': 'Private title must not be copied', 'recommendation': action,
                       'reason': 'Private reason', 'draft': 'Private draft', 'analysis_source': source,
                       'analyzed_at': generated, 'last_seen': capture}]}


class RecommendationHistoryTests(unittest.TestCase):
    def test_cached_rerun_is_one_version_and_retains_original_date(self):
        out = history.build_index([(report(), 'a'*40),
            (report(generated='2026-10-12T15:00:00Z', source='ai_cache'), 'main')])
        self.assertEqual(out['summary']['snapshots'], 1)
        self.assertEqual(out['runs'][0]['ref'], 'a'*40)
        self.assertEqual(out['runs'][0]['analyzed_at'], '2026-10-12T16:00:00+03:00')

    def test_new_capture_and_changed_advice_are_indexed(self):
        out = history.build_index([(report(), 'a'*40),
            (report(action='respond', generated='2026-10-12T14:00:00Z'), 'b'*40),
            (report(capture='2026-10-13T10:00:00+03:00', generated='2026-10-13T13:00:00Z'), 'main')])
        self.assertEqual(out['summary']['snapshots'], 3)
        self.assertEqual(out['summary']['unique_orders'], 1)
        self.assertEqual(out['summary']['days'], 2)
        self.assertEqual(out['daily'][0]['unique_orders'], 1)

    def test_index_contains_no_titles_conditions_reasons_or_drafts(self):
        out = history.build_index([(report(), 'a'*40)])
        self.assertNotIn('Private', str(out))
        self.assertTrue(all(r['path']=='profi_recommendations.json' for r in out['runs']))
        self.assertNotIn('items', out)

    def test_invalid_ids_actions_and_refs_are_rejected(self):
        r = report();r['items'][0]['recommendation'] = 'closed'
        with self.assertRaises(ValueError): history.version_metadata(r, 'main')
        r = report();r['items'] *= 2
        with self.assertRaises(ValueError): history.version_metadata(r, 'main')
        with self.assertRaises(ValueError): history.version_metadata(report(), '../secret')

    def test_empty_input_cannot_replace_history(self):
        with self.assertRaises(ValueError): history.build_index([])

    def test_capture_days_use_moscow_clock(self):
        meta, _ = history.version_metadata(report(capture='2026-10-11T22:00:00Z'), 'main')
        self.assertEqual(meta['date'], '2026-10-12')


if __name__ == '__main__': unittest.main()

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import profi_analyzer as ai


def row(ident='123', **kw):
    return {'order_id': ident, 'title': 'Моделирование в MATLAB',
            'description': 'Разработать модель системы управления и провести анализ устойчивости.',
            'remote': True, 'budget': 'Договорная', 'budget_unit': 'project', **kw}


def advice(r, **kw):
    return {'order_id': r['order_id'], 'technical_fit': 90, 'recommendation': 'respond',
            'commercial_fit': 'suitable', 'confidence': 'high', 'task_kind': 'engineering',
            'reason': 'Задача связана с моделированием систем управления.', 'gaps': [],
            'questions': ['Какие исходные данные доступны?'], 'evidence': ['Моделирование в MATLAB'],
            'budget_assessment': 'Бюджет согласуется отдельно.', 'format_assessment': 'Дистанционно.',
            'draft': 'Здравствуйте! Заинтересовала задача моделирования. Какие исходные данные доступны?', **kw}


PROFILE = {'headline': 'Control Systems Engineer', 'core_skills': ['MATLAB', 'Simulink']}
CFG = {'enabled': True, 'data_transfer_approved': True, 'batch_size': 2, 'max_new_orders': 120, 'max_runtime_seconds': 480}


def current(rows):
    return {'generated_at': '2026-10-12T11:00:00Z', 'captured_at': '2026-10-12T10:00:00+03:00', 'orders': rows}


class AnalyzerTests(unittest.TestCase):
    def test_valid_ai_output_and_priority(self):
        r = row()
        call = Mock(return_value=({'items': [advice(r)]}, {'input_tokens': 10, 'output_tokens': 20}))
        output, cache = ai.analyze(current([r]), PROFILE, CFG, api_key='synthetic', caller=call)
        self.assertEqual(output['status'], 'AI')
        self.assertEqual(output['items'][0]['analysis_source'], 'ai')
        self.assertEqual(output['items'][0]['recommendation'], 'respond')
        self.assertEqual(output['items'][0]['priority_score'], 93)
        self.assertEqual(output['usage_this_run']['input_tokens'], 10)
        self.assertEqual(len(cache['entries']), 1)

    def test_cache_reuses_without_api_and_ignores_capture_date(self):
        r = row()
        call = Mock(return_value=({'items': [advice(r)]}, {}))
        _, cache = ai.analyze(current([r]), PROFILE, CFG, api_key='synthetic', caller=call)
        newer = current([{**r, 'last_seen': '2026-10-13', 'client': 'Not sent to model'}])
        newer['generated_at'] = '2026-10-13T11:00:00Z'
        output, _ = ai.analyze(newer, PROFILE, CFG, cache=cache, caller=Mock(side_effect=AssertionError))
        self.assertEqual(output['items'][0]['analysis_source'], 'ai_cache')
        self.assertEqual(output['source_generated_at'], newer['generated_at'])

    def test_changed_price_description_profile_and_model_invalidate_cache(self):
        context = ai.profile_input(PROFILE, CFG)
        original = ai.fingerprint(row(), context, 'test-model')
        for r, p, model in [(row(budget='1000 рублей'), context, 'test-model'),
                             (row(description='Изменённое описание'), context, 'test-model'),
                             (row(), {'core_skills': ['different']}, 'test-model'),
                             (row(), context, 'other-model')]:
            self.assertNotEqual(ai.fingerprint(r, p, model), original)

    def test_missing_api_is_preliminary_not_ai(self):
        call = Mock(side_effect=AssertionError)
        output, cache = ai.analyze(current([row()]), PROFILE, CFG, caller=call)
        call.assert_not_called()
        self.assertEqual(output['status'], 'RULES_ONLY')
        self.assertIsNone(output['items'][0]['technical_fit'])
        self.assertEqual(output['items'][0]['draft'], '')
        self.assertEqual(cache['entries'], {})

    def test_missing_budget_downgrades_confident_offer(self):
        r = row(budget=None)
        result = ai.apply_guards(advice(r), r)
        self.assertEqual(result['recommendation'], 'clarify')
        self.assertIn('бюджет', result['draft'])
        self.assertTrue(any('Бюджет' in x for x in result['gaps']))

    def test_incomplete_description_and_parser_warning_require_questions(self):
        r = row(description='', parser_warnings=['missing_unit'])
        result = ai.apply_guards(advice(r), r)
        self.assertEqual(result['recommendation'], 'clarify')
        self.assertGreaterEqual(len(result['questions']), 2)

    def test_skip_has_no_outbound_draft(self):
        result = ai.apply_guards(advice(row(), recommendation='skip'), row())
        self.assertEqual(result['draft'], '')

    def test_validation_rejects_fabricated_id_duplicate_and_evidence(self):
        for items in [[advice(row(), order_id='999')], [advice(row()), advice(row())],
                      [advice(row(), evidence=['Invented quote'])], [],
                      [advice(row(), technical_fit=True)], [advice(row(), technical_fit=101)]]:
            with self.assertRaises(ValueError):
                ai.validate_batch({'items': items}, [row()])

    def test_api_failure_does_not_poison_cache_or_abort_history(self):
        call = Mock(side_effect=RuntimeError('do not publish synthetic-secret'))
        rows = [row(str(i)) for i in range(5)]
        output, cache = ai.analyze(current(rows), PROFILE, CFG, api_key='synthetic', caller=call)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(output['summary']['orders'], 5)
        self.assertEqual(output['status'], 'RULES_ONLY')
        self.assertEqual(cache['entries'], {})
        self.assertNotIn('synthetic-secret', str(output))

    def test_runtime_limit_prevents_api_work(self):
        call = Mock(side_effect=AssertionError)
        output, _ = ai.analyze(current([row()]), PROFILE, CFG, api_key='synthetic', caller=call,
                               clock=Mock(side_effect=[0, 500]))
        call.assert_not_called()
        self.assertEqual(output['status'], 'RULES_ONLY')

    def test_cap_preserves_all_orders_with_partial_result(self):
        rows = [row('123'), row('124')]
        call = Mock(side_effect=lambda rows, *args: ({'items': [advice(r) for r in rows]}, {}))
        output, _ = ai.analyze(current(rows), PROFILE, {**CFG, 'max_new_orders': 1}, api_key='synthetic', caller=call)
        self.assertEqual(output['status'], 'PARTIAL')
        self.assertEqual(output['summary']['ai_orders'], 1)
        self.assertEqual(len(output['items']), 2)

    def test_unapproved_transfer_never_calls_api_even_with_key(self):
        call = Mock(side_effect=AssertionError('API must remain disabled'))
        output, cache = ai.analyze(current([row()]), PROFILE,
            {**CFG, 'enabled': True, 'data_transfer_approved': False}, api_key='synthetic', caller=call)
        call.assert_not_called()
        self.assertEqual(output['status'], 'RULES_ONLY')
        self.assertEqual(output['summary']['ai_orders'], 0)

    def test_input_minimizes_identity_and_redacts_contacts(self):
        r = row(description='Почта name@example.com, сайт https://example.com, тел +7 (999) 123-45-67',
                client='Client Name', client_id='99')
        sent = ai.order_input(r)
        self.assertNotIn('client', sent)
        self.assertNotIn('name@example.com', sent['description'])
        self.assertNotIn('+7 (999)', sent['description'])
        self.assertNotIn('education', ai.profile_input({'education': ['personal']}, CFG))

    def test_request_uses_structured_output_and_does_not_store(self):
        body = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': '{"items":[]}'}]}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock()
        import json
        response.read.return_value = json.dumps(body).encode()
        with patch.object(ai, 'urlopen', return_value=response) as urlopen:
            ai.call_openai([row()], PROFILE, 'test-model', 'synthetic', 30)
            request = urlopen.call_args.args[0]
            payload = json.loads(request.data)
            self.assertFalse(payload['store'])
            self.assertTrue(payload['text']['format']['strict'])
            self.assertEqual(urlopen.call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()

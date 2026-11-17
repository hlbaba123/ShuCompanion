"""Offline catalogue contracts: no live stages, models, game or credentials."""
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from shu.copilot_catalog import (
    MAX_BYTES, MAX_CANDIDATES, TIMEOUT_SECONDS, CopilotCatalogError,
    _NoRedirect, query_copilots,
)


def row(identifier=1, **changes):
    content = {
        'stage_name': 'unit_test_stage',
        'doc': {'title': '离线测试标题', 'details': '测试提示'},
        'opers': [{'name': '测试干员', 'skill': 2,
                   'requirements': {'elite': 2, 'level': 60, 'secret': 'not exposed'}}],
        'groups': [{'name': '替代组', 'opers': [{'name': '替代干员', 'skill': 1}]}],
        'actions': [{'type': 'Deploy', 'name': 'never return this action'}],
    }
    return {'id': identifier, 'type': 'PRTS', 'content': json.dumps(content), **changes}


def envelope(rows, **changes):
    return {'statusCode': 200, 'data': {'data': rows, 'hasNext': False}, **changes}


class FakeTransport:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def __call__(self, req, **kwargs):
        self.calls.append((req, kwargs))
        if isinstance(self.value, Exception):
            raise self.value
        if isinstance(self.value, bytes):
            return self.value
        return json.dumps(self.value).encode('utf-8')


class CopilotCatalogTests(unittest.TestCase):
    def test_bulk_page_accepts_fifty_preserves_requirements_and_reports_raw_count(self):
        network = FakeTransport({'status_code': 200,
                                 'data': {'total': 63, 'page': 1, 'has_next': True,
                                          'data': [row(i) for i in range(1, 51)]}})
        result = query_copilots('unit_test_stage', limit=50, full_summary=True,
                               transport=network)
        self.assertEqual(len(result['candidates']), 50)
        self.assertEqual(result['raw_count'], 50)
        self.assertEqual(result['total'], 63)
        self.assertEqual(parse_qs(urlsplit(network.calls[0][0].full_url).query)['limit'], ['50'])
        self.assertNotIn('Deploy', json.dumps(result))

    def test_bulk_summary_preserves_every_group_operator_without_actions(self):
        body = {'stage_name': 'unit_test_stage',
                'opers': [{'name': f'unit{i}', 'skill': 2,
                           'requirements': {'elite': 2, 'skill_level': 10}} for i in range(30)],
                'groups': [{'name': f'group{i}', 'opers': [{'name': f'alt{n}'} for n in range(12)]}
                           for i in range(15)], 'actions': [{'type': 'Deploy'}]}
        result = query_copilots('unit_test_stage', limit=50, full_summary=True,
                               transport=FakeTransport(envelope([row(1, content=json.dumps(body))])))
        candidate = result['candidates'][0]
        self.assertEqual(len(candidate['operators']), 30)
        self.assertEqual(len(candidate['groups']), 15)
        self.assertEqual(len(candidate['groups'][14]['opers']), 12)
        self.assertEqual(candidate['operators'][29]['requirements']['skill_level'], 10)
        self.assertNotIn('Deploy', json.dumps(candidate))

    def test_bulk_limit_rejects_out_of_range_without_request(self):
        network = FakeTransport(envelope([]))
        for limit in (0, 51, True, '50', -1):
            with self.subTest(limit=limit), self.assertRaises(CopilotCatalogError):
                query_copilots('unit_test_stage', limit=limit, transport=network)
        self.assertEqual(network.calls, [])

    def test_bulk_can_use_stable_id_order_and_rejects_unvalidated_sort_fields(self):
        network = FakeTransport(envelope([]))
        query_copilots('unit_test_stage', order_by='id', transport=network)
        self.assertEqual(parse_qs(urlsplit(network.calls[0][0].full_url).query)['orderBy'], ['id'])
        for value in ('unsafe sort', 'hot&extra=true', None):
            with self.subTest(value=value), self.assertRaises(CopilotCatalogError):
                query_copilots('unit_test_stage', order_by=value, transport=network)

    def test_bulk_continuation_is_not_limited_to_legacy_ten_pages(self):
        network = FakeTransport(envelope([]))
        result = query_copilots('unit_test_stage', page=11, limit=50, full_summary=True, transport=network)
        self.assertEqual(result['page'], 11)
        self.assertEqual(parse_qs(urlsplit(network.calls[0][0].full_url).query)['page'], ['11'])

    def test_bulk_requirements_preserve_unknown_fields_for_later_validation(self):
        requirements = {'elite': 2, 'module_id': 'fixture-module', 'future_field': {'minimum': 1}}
        body = {'stage_name': 'unit_test_stage', 'opers': [{'name': 'fixture', 'requirements': requirements}],
                'groups': [{'name': 'group', 'opers': [{'name': 'alternative', 'requirements': requirements}]}]}
        result = query_copilots('unit_test_stage', full_summary=True,
                               transport=FakeTransport(envelope([row(1, content=json.dumps(body))])))
        item = result['candidates'][0]
        self.assertEqual(item['operators'][0]['requirements'], requirements)
        self.assertEqual(item['groups'][0]['opers'][0]['requirements'], requirements)


    def test_fixed_credential_free_single_get_and_explicit_source(self):
        network = FakeTransport(envelope([row(1)]))
        result = query_copilots(' unit_test_stage ', transport=network)
        self.assertEqual(len(network.calls), 1)
        req, options = network.calls[0]
        parts = urlsplit(req.full_url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ('https', 'prts.maa.plus', '/copilot/query'))
        self.assertEqual(parse_qs(parts.query),
                         {'page': ['1'], 'limit': ['5'], 'levelKeyword': ['unit_test_stage'],
                          'type': ['PRTS'], 'orderBy': ['hot']})
        self.assertNotIn('level_keyword', parse_qs(parts.query))
        self.assertNotIn('document', parse_qs(parts.query))
        self.assertEqual(req.get_method(), 'GET')
        self.assertIsNone(req.data)
        self.assertIsNone(req.get_header('Authorization'))
        self.assertIsNone(req.get_header('Cookie'))
        self.assertEqual(options, {'timeout': TIMEOUT_SECONDS, 'max_bytes': MAX_BYTES})
        candidate = result['candidates'][0]
        self.assertEqual(candidate['source'], 'https://prts.maa.plus/copilot/get/1')
        self.assertEqual(candidate['title'], '离线测试标题')
        self.assertEqual(candidate['operators'][0]['requirements'], {'elite': 2, 'level': 60})
        self.assertEqual(candidate['groups'][0]['opers'][0]['skill'], 1)
        self.assertNotIn('"actions":', json.dumps(result))
        # Generic warning mentions actions; there must be no action payload.
        self.assertNotIn('Deploy', json.dumps(result))
        self.assertTrue(result['untrusted_content'])
        self.assertFalse(result['content_is_instruction'])
        self.assertTrue(result['requires_download'])

    def test_invalid_non_stage_inputs_never_reach_network(self):
        network = FakeTransport(envelope([]))
        for value in (None, 12, '', ' ', 'https://evil.invalid/', '../file',
                      '1234', 'stage?token=secret', 'stage\nheader', 'A' * 65,
                      'sk-fake-credential', 'Bearer-secret', {'stage': 'x'}):
            with self.subTest(value=value), self.assertRaises(CopilotCatalogError):
                query_copilots(value, transport=network)
        self.assertEqual(network.calls, [])

    def test_chinese_stage_name_encoded_as_query_data(self):
        network = FakeTransport(envelope([]))
        result = query_copilots('测试关卡', transport=network)
        self.assertEqual(parse_qs(urlsplit(network.calls[0][0].full_url).query)['levelKeyword'], ['测试关卡'])
        self.assertEqual(result['candidates'], [])
        self.assertTrue(result['hints'])

    def test_candidate_limit_and_no_followup_requests(self):
        network = FakeTransport(envelope([row(i) for i in range(1, 21)]))
        result = query_copilots('unit_test_stage', transport=network)
        self.assertEqual(len(result['candidates']), MAX_CANDIDATES)
        self.assertTrue(result['has_more'])
        self.assertEqual(len(network.calls), 1)

    def test_invalid_duplicate_video_and_foreign_source_are_not_exposed(self):
        rows = [row(1, source='https://evil.invalid'), row(1), row(True),
                row(2, type='VIDEO'), row(3, content='{bad json')]
        result = query_copilots('unit_test_stage', transport=FakeTransport(envelope(rows)))
        self.assertEqual([x['id'] for x in result['candidates']], [1])
        self.assertNotIn('evil.invalid', json.dumps(result))

    def test_response_size_and_json_schema_fail_closed(self):
        for value in (b'', b'x' * (MAX_BYTES + 1), b'not JSON', b'[]',
                      b'{"statusCode":200,"data":{"data":[]},"x":NaN}',
                      {'statusCode': 401, 'message': 'remote-secret'},
                      {'statusCode': True, 'data': {'data': []}},
                      {'statusCode': 200, 'data': {'data': 'wrong'}}):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(CopilotCatalogError) as caught:
                query_copilots('unit_test_stage', transport=FakeTransport(value))
            self.assertNotIn('remote-secret', str(caught.exception))

    def test_transport_errors_are_sanitized_and_not_retried(self):
        for exception in (TimeoutError('private detail'), RuntimeError('private detail'),
                          HTTPError('https://private.invalid', 429, 'private detail', {}, None)):
            network = FakeTransport(exception)
            with self.subTest(exception=type(exception).__name__), self.assertRaises(CopilotCatalogError) as caught:
                query_copilots('unit_test_stage', transport=network)
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(len(network.calls), 1)

    def test_redirects_are_disabled(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_deployed_snake_case_response_and_pagination_are_supported(self):
        network = FakeTransport({'status_code': 200,
                                 'data': {'data': [row(1)], 'has_next': True}})
        result = query_copilots('unit_test_stage', transport=network)
        self.assertEqual(len(result['candidates']), 1)
        self.assertTrue(result['has_more'])
        self.assertEqual(len(network.calls), 1)

    def test_diagnostics_distinguish_http_timeout_and_schema_without_remote_text(self):
        cases = [(HTTPError('https://private.invalid', 400, 'private detail', {}, None), 'HTTP 400'),
                 (TimeoutError('private detail'), '超时'),
                 ({'status_code': 200, 'data': {'data': 'private detail'}}, '响应结构')]
        for value, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(CopilotCatalogError) as caught:
                query_copilots('unit_test_stage', transport=FakeTransport(value))
            self.assertIn(expected, caught.exception.public_message)
            self.assertNotIn('private', caught.exception.public_message)

    def test_conflicting_status_aliases_fail_closed(self):
        value = {'status_code': 500, 'statusCode': 200, 'data': {'data': [row(1)]}}
        with self.assertRaises(CopilotCatalogError):
            query_copilots('unit_test_stage', transport=FakeTransport(value))

    def test_unmatched_metadata_is_retained_as_unverified_read_only_research(self):
        body = {'stage_name': 'unrelated_stage', 'doc': {'title': 'unrelated title'}, 'opers': []}
        network = FakeTransport(envelope([row(1, content=json.dumps(body))]))
        result = query_copilots('SHUTEST-9999', transport=network)
        self.assertEqual(result['filter_status'], 'unsupported_filter')
        self.assertFalse(result['stage_match_verified'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['unverified_candidates'][0]['stage_name'], 'unrelated_stage')
        self.assertEqual(len(network.calls), 1)

    def test_document_filter_accepts_original_query_text_without_claiming_stage_alias_match(self):
        body = {'stage_name': 'internal_test_alias', 'doc': {'title': 'AB-9 测试资料'}, 'opers': []}
        result = query_copilots('AB-9', transport=FakeTransport(envelope([row(1, content=json.dumps(body))])))
        self.assertEqual(result['filter_mode'], 'stage_keyword')
        self.assertFalse(result['stage_match_verified'])
        self.assertEqual(len(result['candidates']), 1)

    def test_document_filter_does_not_confuse_longer_stage_code(self):
        body = {'stage_name': 'internal_test_alias', 'doc': {'title': 'AB-90 测试资料'}, 'opers': []}
        result = query_copilots('AB-9', transport=FakeTransport(envelope([row(1, content=json.dumps(body))])))
        self.assertEqual(result['filter_status'], 'unsupported_filter')
        self.assertEqual(result['candidates'], [])
        self.assertEqual(len(result['unverified_candidates']), 1)

    def test_document_is_sent_only_for_explicit_text_search(self):
        network = FakeTransport(envelope([]))
        query_copilots('unit_test_stage', document='显式文本', transport=network)
        self.assertEqual(parse_qs(urlsplit(network.calls[0][0].full_url).query),
                         {'page': ['1'], 'limit': ['5'], 'levelKeyword': ['unit_test_stage'],
                          'document': ['显式文本'], 'type': ['PRTS'], 'orderBy': ['hot']})

    def test_invalid_explicit_document_never_reaches_network(self):
        network = FakeTransport(envelope([]))
        for document in ('', ' ', 'a' * 129, 'header\ninjection', 12):
            with self.subTest(document=document), self.assertRaises(CopilotCatalogError):
                query_copilots('unit_test_stage', document=document, transport=network)
        self.assertEqual(network.calls, [])

    def test_large_nested_metadata_is_bounded_and_unknown_fields_removed(self):
        body = {'stage_name': 'x' * 1000, 'doc': {'title': 't' * 2000, 'details': 'unit_test_stage ' + 'd' * 9000},
                'opers': [{'name': 'n' * 1000, 'skill': {'actions': []},
                           'requirements': {'elite': True, 'level': -500, 'potential': 1}}] * 200,
                'groups': [{'name': 'g' * 1000, 'opers': [{'name': 'a'}] * 30}] * 20,
                'actions': [{'type': 'Deploy'}]}
        result = query_copilots('unit_test_stage', transport=FakeTransport(envelope([row(1, content=json.dumps(body))])))
        # A prose mention of an internal ID does not override its actual stage.
        item = result['unverified_candidates'][0]
        self.assertEqual(len(item['operators']), 24)
        self.assertEqual(len(item['groups']), 12)
        self.assertEqual(len(item['groups'][0]['opers']), 8)
        self.assertLessEqual(len(item['title']), 160)
        self.assertLessEqual(len(item['hints'][0]), 1000)
        self.assertNotIn('skill', item['operators'][0])
        self.assertEqual(item['operators'][0]['requirements'], {'potential': 1})
        self.assertNotIn('Deploy', json.dumps(result))


if __name__ == '__main__':
    unittest.main()

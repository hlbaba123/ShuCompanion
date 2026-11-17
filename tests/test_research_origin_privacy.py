"""Do not forward a custom provider credential to fixed DeepSeek tools."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.providers import ProviderConfig
from shu.research_tools import ResearchToolError, ResearchTools
from shu.network_dialogue import _safe_error_info


KEY = 'synthetic-provider-credential-not-a-real-key'
CODE = 'deepseek_research_credentials_required'


class ResearchOriginPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, req, **kwargs):
        self.requests.append(req)
        return json.dumps({'results': []}).encode()

    def make(self, endpoint):
        return ResearchTools(ProviderConfig(base_url=endpoint, api_key=KEY),
                             self.root, transport=self.transport)

    def test_custom_and_lookalike_providers_never_send_key_to_deepseek_tools(self):
        for endpoint in ('https://gateway.example.invalid',
                         'https://api.deepseek.com.evil.example.invalid',
                         'https://api.deepseek.com@evil.example.invalid',
                         'https://api.deepseek.com.', 'http://api.deepseek.com',
                         'https://api.deepseek.com:8443'):
            with self.subTest(endpoint=endpoint):
                tools = self.make(endpoint)
                with self.assertRaises(ResearchToolError) as caught:
                    tools.search('ordinary public query', include_station=False)
                self.assertEqual(caught.exception.code, CODE)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(self.requests, [])
                self.assertEqual(tools.stats['search'], 0)

    def test_native_deepseek_configuration_keeps_existing_search_authentication(self):
        for number, endpoint in enumerate(('https://api.deepseek.com',
                                            'https://api.deepseek.com/v1',
                                            'https://api.deepseek.com:443/v1')):
            with self.subTest(endpoint=endpoint):
                result = self.make(endpoint).search('public query '+str(number), include_station=False)
                self.assertTrue(result['ok'])
                request = self.requests[-1]
                self.assertEqual(request.full_url, 'https://api.deepseek.com/v1/tools/search')
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)

    def test_fetch_also_rejects_a_custom_provider_key_without_http(self):
        with self.assertRaises(ResearchToolError) as caught:
            self.make('https://gateway.example.invalid').fetch('https://prts.wiki/w/Fixture')
        self.assertEqual(caught.exception.code, CODE)
        self.assertEqual(self.requests, [])

    def test_public_prts_catalog_still_works_without_resolving_custom_gateway_key(self):
        tools = self.make('https://gateway.example.invalid')
        def key_must_stay_local():
            raise AssertionError('No research key may be read for a custom provider')
        tools.config.resolve_key = key_must_stay_local
        source = 'https://prts.maa.plus/copilot/query'
        tools.catalog_query = lambda _: {'source': source, 'candidates': []}
        result = tools.search('TL-1 MAA 作业')
        self.assertFalse(result['web_ok'])
        self.assertEqual(result['web_error_code'], CODE)
        self.assertEqual(result['catalog']['source'], source)
        self.assertEqual(self.requests, [])
        self.assertEqual(tools.stats['catalog'], 1)
        self.assertNotIn(KEY, json.dumps(result))

    def test_dialogue_error_has_a_structured_safe_reason(self):
        with self.assertRaises(ResearchToolError) as caught:
            self.make('https://gateway.example.invalid').search('public query', include_station=False)
        result = _safe_error_info(caught.exception)
        self.assertEqual(result['error_code'], CODE)
        self.assertIn('独立', result['error'])
        self.assertNotIn(KEY, json.dumps(result))


if __name__ == '__main__':
    unittest.main()

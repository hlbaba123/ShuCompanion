"""Knowledge provenance, persistence, lexical search, and context-budget checks.

No real game, remote pages, credentials, screenshots, or model calls are used.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.knowledge import KnowledgeStore, estimate_tokens, search_terms


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.now=1000000.0
        self.store=KnowledgeStore(Path(self.temp.name),clock=lambda:self.now)
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def reference(self,subject,text,**kwargs):
        return self.store.add(subject,text,source_url='https://prts.wiki/w/example',evidence_status='source_reported',**kwargs)

    def test_chinese_and_stage_code_retrieval_with_no_external_tokenizer(self):
        self.reference('罗德岛基建','在主页选择基建入口。')
        self.reference('1-7','代理作战的关卡需要先完成解锁条件。')
        self.assertEqual(self.store.retrieve('帮我打开基建')[0]['subject'],'罗德岛基建')
        self.assertEqual(self.store.retrieve('刷1-7')[0]['subject'],'1-7')
        self.assertIn('基建',search_terms('请打开基建'))

    def test_exact_subject_ranked_before_incidental_mentions(self):
        self.reference('其他页面','基建是这里的一条参考链接。')
        self.reference('基建','罗德岛设施管理。')
        rows=self.store.retrieve('基建')
        self.assertEqual(rows[0]['subject'],'基建');self.assertTrue(rows[0]['exact_match'])

    def test_provenance_preserves_revision_and_source_timestamp(self):
        self.reference('基建','资料中描述的规则。',version='revid:440697',updated_at='2026-10-11T02:50:35Z')
        row=self.store.retrieve('基建')[0]
        self.assertEqual(row['source_kind'],'reference')
        self.assertEqual(row['source_url'],'https://prts.wiki/w/example')
        self.assertEqual(row['version'],'revid:440697')
        self.assertEqual(row['updated_at'],'2026-10-11T02:50:35+00:00')
        self.assertFalse(row['verified']);self.assertEqual(row['evidence_status'],'source_reported')

    def test_expiry_is_marked_and_can_be_excluded(self):
        self.reference('基建','旧资料。',ttl_seconds=5)
        self.now+=6
        self.assertTrue(self.store.retrieve('基建')[0]['stale'])
        self.assertEqual(self.store.retrieve('基建',include_stale=False),[])

    def test_old_source_edit_time_is_not_cache_expiry_time(self):
        self.reference('基建','今天刚核查的旧页面。',updated_at=1,ttl_seconds=60)
        self.assertFalse(self.store.retrieve('基建')[0]['stale'])

    def test_model_inferences_cannot_claim_observed_or_source_reported(self):
        for status in ('observed','source_reported'):
            with self.subTest(status=status),self.assertRaises(ValueError):
                self.store.add('基建','模型猜测有一键换班。',source_kind='model_inference',evidence_status=status,evidence_id='fake')
        self.store.add('基建','这只是尚未检验的猜测。',source_kind='model_inference')
        self.assertFalse(self.store.retrieve('基建')[0]['verified'])

    def test_actual_game_experience_requires_visible_evidence_identifier(self):
        with self.assertRaises(ValueError):self.store.add_experience('基建入口','进入成功。',verified=True)
        self.store.add_experience('基建入口','点击 BASE 后新截图显示基建管理页面。',evidence_id='observation-42',verified=True)
        row=self.store.retrieve('基建')[0]
        self.assertEqual(row['source_kind'],'observed_user_game')
        self.assertEqual(row['evidence_id'],'observation-42');self.assertTrue(row['verified'])

    def test_preferences_are_explicit_persistent_and_update_in_place(self):
        first=self.store.add_preference('理智药','刷关不要使用理智药。')
        second=self.store.add_preference('理智药','仅在我明确要求时才使用理智药。')
        self.assertEqual(first['id'],second['id'])
        self.store.close();self.now+=365*86400
        self.store=KnowledgeStore(Path(self.temp.name),clock=lambda:self.now)
        rows=self.store.preferences()
        self.assertEqual(len(rows),1);self.assertIn('明确要求',rows[0]['text'])
        self.assertFalse(rows[0]['stale']);self.assertFalse(rows[0]['verified'])
        self.assertEqual(rows[0]['evidence_status'],'user_stated')

    def test_refresh_external_id_removes_old_search_tokens(self):
        first=self.reference('基建','贸易站旧说明。',external_id='page:123#section')
        second=self.reference('基建','制造站新说明。',external_id='page:123#section')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.store.retrieve('贸易站'),[])
        self.assertEqual(self.store.retrieve('制造站')[0]['id'],first['id'])

    def test_context_char_and_estimated_token_budgets_include_metadata(self):
        for index in range(8):self.reference('基建'+str(index),'基建'+('这是一段较长资料。'*200))
        rows=self.store.retrieve('基建',top_k=3,char_budget=1400,token_budget=700)
        encoded=[json.dumps(row,ensure_ascii=False,separators=(',',':')) for row in rows]
        self.assertTrue(rows);self.assertLessEqual(len(rows),3)
        self.assertLessEqual(sum(map(len,encoded)),1400)
        self.assertLessEqual(sum(estimate_tokens(text) for text in encoded),700)
        self.assertTrue(any(row['truncated'] for row in rows))
        self.assertEqual(self.store.retrieve('基建',char_budget=20,token_budget=10),[])

    def test_source_filters_do_not_mix_game_experience_with_reference_material(self):
        self.reference('基建规则','公开资料的基建描述。')
        self.store.add_experience('基建入口','本机入口记录。',evidence_id='obs-1',verified=True)
        rows=self.store.retrieve('基建',source_kinds={'observed_user_game'})
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_kind'],'observed_user_game')

    def test_long_page_excerpt_keeps_the_matching_fact_in_view(self):
        self.reference('基建完整资料','无关背景。'*500+'制造站的目标规则。'+'补充说明。'*300)
        rows=self.store.retrieve('制造站',char_budget=1000,token_budget=700)
        self.assertTrue(rows);self.assertIn('制造站',rows[0]['text']);self.assertTrue(rows[0]['truncated'])

    def test_search_syntax_is_data_not_sql_or_fts_instructions(self):
        self.reference('基建','基建入口。')
        self.assertIsInstance(self.store.retrieve('基建" OR * :; DROP TABLE knowledge_entries; --'),list)
        self.assertEqual(self.store.retrieve('基建')[0]['subject'],'基建')

    def test_sensitive_tokens_are_redacted_before_storage_and_indexing(self):
        secret='private-example-token'
        self.store.set_secrets([secret])
        self.store.add_preference('连接提示','我的令牌 '+secret+' API_KEY=exampleCredential sk-exampleSECRET123456')
        raw=self.store._db.execute('SELECT text FROM knowledge_entries').fetchone()['text']
        self.assertNotIn(secret,raw);self.assertNotIn('exampleCredential',raw);self.assertNotIn('sk-example',raw)
        self.assertEqual(self.store.retrieve(secret),[])

    def test_later_exact_redaction_removes_existing_index_terms(self):
        self.store.add_preference('连接','accidentallyPastedOpaqueValue')
        self.store.set_secrets(['accidentallyPastedOpaqueValue'])
        self.assertEqual(self.store.retrieve('accidentallyPastedOpaqueValue'),[])
        self.assertNotIn('accidentallyPastedOpaqueValue',self.store.preferences()[0]['text'])

    def test_image_hidden_reasoning_and_credential_urls_rejected(self):
        for text in ('data:image/png;base64,abcd','<think>hidden text</think>', '{"reasoning_content":"hidden"}'):
            with self.subTest(text=text),self.assertRaises(ValueError):self.reference('测试',text)
        for url in ('file:///C:/capture.png','https://name:password@example.org/page','https://example.org/page?api_key=secret'):
            with self.subTest(url=url),self.assertRaises(ValueError):self.store.add('来源','资料',source_url=url)

    def test_empty_queries_and_invalid_limits_do_not_read_entire_store(self):
        self.reference('基建','规则。')
        self.assertEqual(self.store.retrieve(''),[])
        for kwargs in ({'top_k':0},{'top_k':True},{'char_budget':0},{'token_budget':0},{'source_kinds':'reference'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.store.retrieve('基建',**kwargs)

    def test_closed_store_rejects_reads_and_writes(self):
        self.store.close();self.store.close()
        with self.assertRaises(RuntimeError):self.store.retrieve('基建')
        with self.assertRaises(RuntimeError):self.store.add_preference('测试','内容')


if __name__=='__main__':unittest.main()

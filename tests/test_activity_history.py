"""Portable request display history, never task execution authority."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.activity_history import ActivityHistory


class ActivityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.json'
        self.history = ActivityHistory(self.path)

    def test_stable_request_id_is_one_record_and_survives_restart(self):
        self.history.begin('request-1', '刷六次，不使用理智药', '2026-11-07T01:00:00+08:00')
        self.history.append('request-1', {'role':'leader','stage':'intent_started','summary':'理解目标'})
        self.history.begin('request-1', '不得覆盖原话')
        restored = ActivityHistory(self.path)
        self.assertEqual(len(restored.records()), 1)
        record = restored.get('request-1')
        self.assertEqual(record['text'], '刷六次，不使用理智药')
        self.assertEqual(record['events'][0]['summary'], '理解目标')

    def test_finish_updates_only_display_status_and_never_creates_task_fields(self):
        self.history.begin('r', '查找当前活动')
        self.history.finish('r', 'understood', '查询已返回')
        record = self.history.get('r')
        self.assertEqual(record['status'], 'understood')
        self.assertEqual(record['summary'], '查询已返回')
        self.assertNotIn('contract', record)
        self.assertNotIn('writes_enabled', record)

    def test_private_payload_and_credential_fields_never_persist(self):
        self.history.begin('r', '不要泄漏 sk-fake-secret-value')
        self.history.append('r', {'role':'research','stage':'tool_started',
            'summary':'Bearer fake-provider-token','query':'公开资料',
            'reasoning_content':'hidden-private-reasoning','api_key':'opaque-private-key',
            'result':{'secret':'private-tool-payload'},'url':'https://example.com/?token=private'})
        self.history.finish('r','failed','api_key=opaque-private-value')
        visible = '\n'.join(p.read_text(encoding='utf-8') for p in Path(self.temp.name).rglob('*.json'))
        for secret in ('sk-fake-secret-value','fake-provider-token','hidden-private-reasoning',
                       'opaque-private-key','private-tool-payload','opaque-private-value'):
            self.assertNotIn(secret, visible)
        self.assertNotIn('url',self.history.get('r')['events'][0])

    def test_each_request_is_bounded_but_older_record_files_are_not_deleted(self):
        self.history.begin('old', '旧要求')
        for i in range(205): self.history.append('old', {'stage':'notice','summary':str(i)})
        self.assertEqual(len(self.history.get('old')['events']),200)
        first_files = set(Path(self.temp.name).rglob('*.json'))
        for i in range(101): self.history.begin('r'+str(i), '要求'+str(i))
        self.assertEqual(len(self.history.records()),100)
        self.assertNotIn('old',[r['request_id'] for r in self.history.records()])
        self.assertTrue(all(p.exists() for p in first_files))

    def test_unknown_request_cannot_silently_receive_late_results(self):
        self.history.begin('known','当前要求')
        self.assertFalse(self.history.append('unknown',{'summary':'迟到的结果'}))
        self.assertEqual(self.history.get('known')['events'],[])
        self.assertIsNone(self.history.get('unknown'))

    def test_malformed_index_does_not_load_unregistered_files(self):
        self.path.write_text('{broken',encoding='utf-8')
        unexpected = self.path.with_name('history-records')
        unexpected.mkdir()
        (unexpected/'private.json').write_text('{"text":"unregistered-private-fixture"}',encoding='utf-8')
        restored=ActivityHistory(self.path)
        self.assertEqual(restored.records(),[])
        self.assertTrue((unexpected/'private.json').exists())

    def test_returned_records_are_detached_from_persistent_state(self):
        self.history.begin('r','原始要求')
        record=self.history.get('r');record['text']='改写';record['events'].append({'summary':'伪造'})
        self.assertEqual(self.history.get('r')['text'],'原始要求')
        self.assertEqual(self.history.get('r')['events'],[])

    def test_reusing_an_id_outside_recent_index_never_overwrites_its_old_file(self):
        self.history.begin('old','旧原话')
        old_files=set(Path(self.temp.name).rglob('history-records/*.json'))
        for i in range(100):self.history.begin('r'+str(i),'要求'+str(i))
        original={path:path.read_bytes() for path in old_files}
        self.assertIsNone(self.history.begin('old','不能覆盖旧记录'))
        self.assertIsNone(self.history.get('old'))
        self.assertEqual({path:path.read_bytes() for path in old_files},original)

    def test_proven_task_can_review_and_update_its_archived_request(self):
        self.history.begin('old','旧任务原话')
        self.history.bind_request('old','task-a')
        self.history.append('old',{'task_id':'task-a','stage':'tool_started','summary':'原工具证据'})
        for i in range(100): self.history.begin('r'+str(i),'新消息')
        restored=ActivityHistory(self.path)
        self.assertIsNone(restored.load_bound('old','task-b'))
        self.assertFalse(restored.append('old',{'task_id':'task-b','stage':'tool_finished'}))
        self.assertTrue(restored.append('old',{'task_id':'task-a','stage':'tool_finished','summary':'任务晚到结果'}))
        self.assertEqual(len(restored.records()),100)
        groups,_=ActivityHistory(self.path).task_groups(task_id='task-a')
        self.assertEqual(groups['task-a'][0]['text'],'旧任务原话')
        self.assertIn('任务晚到结果',str(groups['task-a'][0]['events']))
        restored.delete_task('task-a')
        self.assertIsNone(restored.load_bound('old','task-a'))

    def test_archived_loading_requires_exact_registered_identity(self):
        self.history.begin('old','归档原话')
        for i in range(100): self.history.begin('r'+str(i),'新消息')
        restored=ActivityHistory(self.path)
        self.assertIsNone(restored.load_bound('old','task-a'))
        self.assertTrue(restored.bind_request('old','task-a'))
        self.assertEqual(restored.load_bound('old','task-a')['text'],'归档原话')
        self.assertIsNone(restored.load_bound('unregistered','task-a'))

    def test_real_followup_steps_replace_understood_status_without_claiming_task_complete(self):
        self.history.begin('r','已理解但还要执行')
        self.history.finish('r','understood','已理解要求')
        self.history.append('r',{'stage':'tool_started','role':'maa','status':'running','summary':'实际MAA步骤'})
        self.assertEqual(self.history.get('r')['status'],'running')
        self.history.append('r',{'stage':'dispatch_completed','role':'maa','status':'completed'})
        self.assertEqual(self.history.get('r')['status'],'running')
        self.history.append('r',{'stage':'task_waiting','status':'needs_input'})
        self.assertEqual(self.history.get('r')['status'],'needs_input')

    def test_terminal_task_status_survives_late_intent_and_tool_events(self):
        self.history.begin('r','查询作业')
        self.history.append('r',{'stage':'task_waiting','status':'needs_input','summary':'尚缺核实证据'})
        self.history.append('r',{'stage':'intent_started','status':'running','summary':'追问的理解'})
        self.history.append('r',{'stage':'tool_started','status':'running','summary':'旧工具回调'})
        record=self.history.get('r')
        self.assertEqual((record['status'],record['summary']),('needs_input','尚缺核实证据'))
        self.history.append('r',{'stage':'task_started','status':'running','summary':'明确恢复任务'})
        self.assertEqual(self.history.get('r')['status'],'running')

    def test_public_step_title_survives_restart_with_display_sanitization(self):
        self.history.begin('r','只读作业查询')
        self.history.append('r',{'stage':'dispatch_started','step_title':'核对TL-6候选与来源'})
        self.history.append('r',{'stage':'dispatch_started','step_title':'Bearer secret-token '+('长'*150)})
        restored=ActivityHistory(self.path).get('r')
        self.assertEqual(restored['events'][0].get('step_title'),'核对TL-6候选与来源')
        title=restored['events'][1].get('step_title','')
        self.assertTrue(title)
        self.assertLessEqual(len(title),120)
        self.assertNotIn('secret-token',title)


if __name__ == '__main__': unittest.main()

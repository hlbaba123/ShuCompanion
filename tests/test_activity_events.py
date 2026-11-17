"""Only small public facts may reach the desktop Agent activity surface."""
import json
import unittest

from shu.activity_events import make_activity_event


class ActivityEventTests(unittest.TestCase):
    def test_projection_drops_hidden_payloads_and_bounds_public_fields(self):
        event = make_activity_event('tool_finished', {
            'role': 'research', 'status': 'completed', 'task_id': 'task-1',
            'epoch': 4, 'dispatch_id': 'dispatch-2', 'tool': 'research_search',
            'summary': '可展示结果 sk-private-credential-value Bearer private-token ' + '长' * 1000,
            'reasoning_content': 'PRIVATE REASONING', 'api_key': 'PRIVATE KEY',
            'result': {'content': 'FULL TOOL PAYLOAD'}, 'query': '查 sk-private-query-value',
            'image_url': 'data:image/png;base64,PRIVATE IMAGE'})
        self.assertLessEqual(len(event['summary']), 300)
        self.assertEqual(event['epoch'], 4)
        self.assertEqual(event['role'], 'research')
        serialized = json.dumps(event)
        for forbidden in ('PRIVATE REASONING', 'PRIVATE KEY', 'FULL TOOL PAYLOAD',
                          'private-query-value', 'PRIVATE IMAGE', 'private-credential', 'private-token'):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(event['timestamp'].endswith('+08:00'))

    def test_actual_public_query_and_counts_survive_without_raw_result(self):
        event=make_activity_event('tool_finished',role='research',tool='research_candidates',
            query='TEST-6',url='https://prts.plus/?op=123',item_count=19,total=19,cached=True,
            elapsed_seconds=.8,tool_call_id='tool-1')
        self.assertEqual(event['query'],'TEST-6');self.assertEqual(event['item_count'],19)
        from shu.activity_history import event_fields
        self.assertEqual(event_fields(event)['tool_call_id'],'tool-1')
        self.assertEqual(event_fields(event)['elapsed_seconds'],.8)

    def test_unknown_stage_or_role_is_not_presented_as_real_agent_work(self):
        self.assertIsNone(make_activity_event('imaginary_agent_started', {'role': 'leader'}))
        self.assertIsNone(make_activity_event('dispatch_started', {'role': 'imaginary'}))

    def test_queue_step_preserves_target_identity_in_history(self):
        from shu.activity_history import event_fields
        event = make_activity_event('tool_finished',role='maa',tool='queue_item_verified',
            target_key='target-ordinary',task_id='one-task',epoch=3,
            summary='已核实本次目标入队。')
        self.assertEqual(event_fields(event)['target_key'],'target-ordinary')
        self.assertEqual(event['task_id'],'one-task')

    def test_arbitrary_objects_are_never_stringified_to_the_activity_log(self):
        class Secret:
            def __str__(self):
                raise AssertionError('untrusted objects must not be stringified')
        event = make_activity_event('task_finished', {'role': 'leader',
            'summary': Secret(), 'task_id': Secret(), 'epoch': True,
            'elapsed_seconds': float('nan')})
        self.assertEqual(event['summary'], '')
        self.assertNotIn('epoch', event)
        self.assertNotIn('elapsed_seconds', event)


if __name__ == '__main__':
    unittest.main()

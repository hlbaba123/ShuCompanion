"""Public activity projection preserves real episode and tool boundaries."""
import unittest

from shu.activity_timeline import project_timeline


class TimelineProjectionTests(unittest.TestCase):
    def test_dispatch_ids_keep_same_role_redelegations_separate(self):
        steps=project_timeline([
            {'stage':'dispatch_started','role':'research','dispatch_id':'first','summary':'首轮'},
            {'stage':'tool_started','role':'research','dispatch_id':'first','tool':'web_fetch','tool_call_id':'one'},
            {'stage':'dispatch_started','role':'research','dispatch_id':'second','summary':'补充'},
            {'stage':'tool_finished','role':'research','dispatch_id':'first','tool':'web_fetch','tool_call_id':'one',
             'status':'failed','summary':'原请求失败'},
            {'stage':'tool_started','role':'research','dispatch_id':'second','tool':'web_search','query':'补充来源'},
        ])
        self.assertEqual(len(steps),2)
        self.assertEqual(steps[0].items[0].status,'failed')
        self.assertEqual(steps[1].items[0].query,'补充来源')
        self.assertEqual(steps[0].failures,['原请求失败'])

    def test_tool_call_ids_pair_concurrent_calls_without_swapping_results(self):
        steps=project_timeline([
            {'stage':'tool_started','role':'research','tool':'web_fetch','tool_call_id':'a','query':'第一个网页'},
            {'stage':'tool_started','role':'research','tool':'web_fetch','tool_call_id':'b','query':'第二个网页'},
            {'stage':'tool_finished','role':'research','tool':'web_fetch','tool_call_id':'b','status':'completed','summary':'第二个返回'},
            {'stage':'tool_finished','role':'research','tool':'web_fetch','tool_call_id':'a','status':'failed','summary':'第一个失败'},
        ])
        self.assertEqual(len(steps),1)
        self.assertEqual([(x.query,x.summary,x.status) for x in steps[0].items],
            [('第一个网页','第一个失败','failed'),('第二个网页','第二个返回','completed')])

    def test_task_stop_does_not_convert_successful_tool_return_into_task_success(self):
        steps=project_timeline([
            {'stage':'dispatch_started','role':'research'},
            {'stage':'tool_finished','role':'research','tool':'web_search','status':'completed','summary':'搜索已返回'},
            {'stage':'task_stopped','role':'leader','status':'cancelled','summary':'本次已停止'},
        ])
        self.assertEqual(len(steps),1)
        self.assertEqual(steps[0].status,'cancelled')
        self.assertFalse(steps[0].completed)
        self.assertEqual(steps[0].items[0].status,'completed')

    def test_unknown_payload_never_becomes_public_tool_details(self):
        steps=project_timeline([{'stage':'tool_started','role':'research','tool':'web_fetch',
            'query':'公开查询','reasoning_content':'private-chain','result':{'private':'raw-output'},
            'url':'file:///C:/private.txt'}])
        self.assertEqual(steps[0].items[0].query,'公开查询')
        self.assertEqual(steps[0].items[0].url,'')
        self.assertNotIn('private-chain',str(steps))
        self.assertNotIn('raw-output',str(steps))

    def test_finished_tool_without_outcome_is_returned_without_claiming_success(self):
        steps=project_timeline([
            {'stage':'tool_started','role':'research','tool':'web_fetch','status':'running'},
            {'stage':'tool_finished','role':'research','tool':'web_fetch'},
        ])
        self.assertEqual(steps[0].items[0].status,'returned')
        self.assertFalse(steps[0].completed)

    def test_research_title_describes_only_tools_that_actually_occurred(self):
        cases=[(['research_candidates'],'收集作业候选'),
               (['research_candidates','research_download'],'收集并核对作业'),
               (['web_search','research_fetch','finish_role'],'查询公开资料'),
               ([],'研究 Agent')]
        for tools,want in cases:
            with self.subTest(tools=tools):
                events=[{'stage':'dispatch_started','role':'research'}]
                events += [{'stage':'tool_started','role':'research','tool':tool} for tool in tools]
                self.assertEqual(project_timeline(events)[0].title,want)

    def test_leader_title_uses_only_preceding_actual_research_return(self):
        steps=project_timeline([
            {'stage':'dispatch_started','role':'leader'},
            {'stage':'tool_started','role':'leader','tool':'delegate'},
            {'stage':'dispatch_completed','role':'leader','status':'completed'},
            {'stage':'dispatch_started','role':'research'},
            {'stage':'tool_started','role':'research','tool':'research_candidates'},
            {'stage':'dispatch_completed','role':'research','status':'researched'},
            {'stage':'dispatch_started','role':'leader'},
            {'stage':'tool_started','role':'leader','tool':'delegate'},
        ])
        self.assertEqual([step.title for step in steps],['分配任务','收集作业候选','审核结果'])
        self.assertTrue(steps[1].completed)

    def test_task_submission_does_not_reopen_completed_understanding_step(self):
        steps=project_timeline([
            {'stage':'intent_started','role':'leader','status':'running'},
            {'stage':'intent_finished','role':'leader','summary':'已解析只读目标'},
            {'stage':'task_started','role':'leader','status':'running','summary':'已交给 Leader'},
            {'stage':'dispatch_started','role':'leader'},
        ])
        self.assertEqual(steps[0].status,'understood')
        self.assertEqual(steps[0].summary,'已解析只读目标')

    def test_diagnosis_action_title_requires_real_tool_activity(self):
        steps=project_timeline([{'stage':'dispatch_started','role':'diagnosis'},
            {'stage':'tool_started','role':'diagnosis','tool':'read_evidence'}])
        self.assertEqual(steps[0].title,'排查问题')

    def test_real_dispatch_task_title_wins_over_role_and_tool_fallback(self):
        steps=project_timeline([
            {'stage':'dispatch_started','role':'research','dispatch_id':'d',
             'step_title':'补齐TL-6候选池的第二页'},
            {'stage':'tool_started','role':'research','dispatch_id':'d','tool':'research_candidates'},
            {'stage':'dispatch_completed','role':'research','dispatch_id':'d','status':'researched'},
        ])
        self.assertEqual(steps[0].title,'补齐TL-6候选池的第二页')


if __name__ == '__main__': unittest.main()

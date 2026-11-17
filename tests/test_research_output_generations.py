"""Display receipts survive needs-input, without acquiring execution authority."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.core import TaskEngine
from shu.runtime import RuntimeStore
from shu.workflow_store import WorkflowStore
from shu.maa_coordinator import MaaCoordinator
from shu.providers import ProviderConfig


class ResearchOutputGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.engine=TaskEngine(self.root);self.addCleanup(self.engine.close)
        self.runtime=RuntimeStore(self.root);self.addCleanup(self.runtime.close)
        self.store=WorkflowStore(self.engine)
        self.coordinator=MaaCoordinator(self.engine,object(),self.runtime,
            role_config=ProviderConfig(),task_config=ProviderConfig(),permissions=lambda:(False,False),
            store=self.store,directory=self.root,recover=False)
        self.contract={'goal':'只查TL-6作业，本轮不筛选','task_spec':{'kind':'lookup','stage_code':'TL-6'}}
        self.state=self.store.submit(self.contract['goal'],self.contract)
        self.task_id=self.state['task_id'];self.coordinator.current_task_id=self.task_id

    def publish(self,value,name):
        path=self.root/name
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def research(self):
        dispatch=self.store.ensure_dispatch(self.task_id,'research','first',{'contract':self.contract,'task':'查TL-6'})
        stage={'task_id':self.task_id,'epoch':self.state['epoch'],'goal':self.contract['goal'],
            'query':'TL-6','source':'https://prts.maa.plus/arknights/level','raw_sha256':'a'*64,
            'stage_id':'act55side_06','level_id':'level_act55side_06','code':'TL-6'}
        stage['evidence_ref']=self.publish(stage,'stage.json')
        body=self.publish({'stage_name':'act55side_06','actions':[{'type':'SpeedUp'}]},'copilot.json')
        record={'task_id':self.task_id,'epoch':self.state['epoch'],
            'research_dispatch_id':dispatch['dispatch_id'],'copilot_id':'108619',
            'goal_stage':stage,'path':body['path'],'sha256':body['sha256'],'stage_name':'act55side_06',
            'source_url':'https://prts.maa.plus/copilot/get/108619'}
        ref=self.publish(record,'candidate.json')
        self.store.finish_dispatch(dispatch['dispatch_id'],{'status':'unknown','candidate_refs':[ref]})
        return dispatch

    def test_needs_input_keeps_previous_run_outputs_for_display_only(self):
        dispatch=self.research()
        self.store.suspend(self.task_id,expected_epoch=1,message='还缺其他关卡')
        self.assertIsNone(self.store.dispatch_result(dispatch['dispatch_id']))
        self.assertEqual(self.store.research_results(self.task_id),[])
        output=self.coordinator.research_output(self.task_id)
        self.assertEqual([i['mystery_code'] for i in output['items']],['prts://108619'])
        self.assertIn('任务尚未完成',output['summary'])
        self.assertEqual(output['generation']['current_epoch'],2)
        self.assertEqual(output['generation']['source_epoch'],1)
        self.assertIs(output['generation']['display_only'],True)
        self.assertIs(output['generation']['execution_eligible'],False)

    def test_stop_and_new_contract_do_not_reuse_prior_run_outputs(self):
        self.research();self.store.suspend(self.task_id,expected_epoch=1,message='待处理')
        self.store.control(self.task_id,'stop')
        self.assertEqual(self.coordinator.research_output(self.task_id)['items'],[])
        self.store.submit('换查SL-1作业',{'goal':'换查SL-1作业','task_spec':{'kind':'lookup','stage_code':'SL-1'}},self.task_id)
        self.assertEqual(self.coordinator.research_output(self.task_id)['items'],[])

    def test_another_task_cannot_receive_a_different_tasks_candidate_reference(self):
        dispatch=self.research()
        foreign=self.store.dispatch_result(dispatch['dispatch_id'])
        other=self.store.submit(self.contract['goal'],self.contract)
        job=self.store.ensure_dispatch(other['task_id'],'research','cross-task',{'contract':self.contract})
        self.store.finish_dispatch(job['dispatch_id'],foreign)
        self.assertEqual(self.coordinator.research_output(other['task_id'])['items'],[])

    def test_same_epoch_redelegation_displays_registered_original_source(self):
        original=self.research();receipt=self.store.dispatch_result(original['dispatch_id'])
        later=self.store.ensure_dispatch(self.task_id,'research','later',{'contract':self.contract})
        self.store.finish_dispatch(later['dispatch_id'],receipt)
        output=self.coordinator.research_output(self.task_id)
        self.assertEqual([i['mystery_code'] for i in output['items']],['prts://108619'])
        self.assertEqual(output['generation']['source_dispatch_ids'],[original['dispatch_id']])
        self.store.suspend(self.task_id,expected_epoch=1,message='还缺其他关卡')
        output=self.coordinator.research_output(self.task_id)
        self.assertEqual([i['mystery_code'] for i in output['items']],['prts://108619'])
        self.assertIsNone(self.store.dispatch_result(original['dispatch_id']))

    def test_claimed_source_must_have_registered_the_exact_candidate_reference(self):
        original=self.research();receipt=self.store.dispatch_result(original['dispatch_id'])
        record=json.loads(Path(receipt['candidate_refs'][0]['path']).read_text(encoding='utf-8'))
        record.update(copilot_id='555',source_url='https://prts.maa.plus/copilot/get/555')
        unregistered=self.publish(record,'unregistered-candidate.json')
        later=self.store.ensure_dispatch(self.task_id,'research','later',{'contract':self.contract})
        self.store.finish_dispatch(later['dispatch_id'],{'status':'unknown','candidate_refs':[unregistered]})
        self.assertEqual(self.coordinator.research_output(self.task_id)['items'],[])


if __name__=='__main__':unittest.main()

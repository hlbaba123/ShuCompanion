"""Actual role adapter handoff reuses public pools across two dispatches."""
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_agents import MaaRoleAgents
from shu.providers import ProviderConfig
from tests.test_copilot_pool import page


class CandidateHandoffTests(unittest.TestCase):
    def test_two_role_dispatches_resume_pages_instead_of_restarting(self):
        with ExitStack() as stack:
            temporary=stack.enter_context(tempfile.TemporaryDirectory())
            root=Path(temporary);engine=TaskEngine(root);store=WorkflowStore(engine)
            stack.callback(engine.close)
            state=store.submit('只读查询TEST-1全部作业摘要',{'goal':'只读查询TEST-1全部作业摘要','task_spec':{'kind':'lookup','stage_code':'TEST-1'}})
            roles=MaaRoleAgents(store,root,ProviderConfig(),ProviderConfig(),None,lambda:False,lambda:False)
            stack.callback(roles.close);cancel=threading.Event()
            def context(key,results):
                dispatch=store.ensure_dispatch(state['task_id'],'research',key,{'contract':state['contract']})
                return {**{k:state[k] for k in ('task_id','epoch','revision','contract')},
                    'dispatch_id':dispatch['dispatch_id'],'role_results':results}
            levels={'source':'https://prts.maa.plus/arknights/level','raw_sha256':'a'*64,
                'levels':[{'code':'TEST-1','stage_id':'unit_stage','level_id':'level-unit','match':'exact_code_or_id'}]}
            requested=[]
            def query(stage,**options):
                number=options['page'];requested.append(number)
                return page(range(1+(number-1)*50,min(number*50+1,126)),total=125,has_more=number<3)
            with patch('shu.level_catalog.find_levels',return_value=levels),patch('shu.research_tools.query_copilots',side_effect=query):
                first=context('first',{});saved={}
                result=roles._execute_tool('research','research_candidates',{'stage_or_keyword':'TEST-1'},first,saved,cancel)
                self.assertEqual(result['coverage']['collected'],100)
                prior={'candidate_pools':list(saved['candidate_pools'].values())}
                second=context('second',{'research':prior});next_state={}
                local=roles._execute_tool('research','research_pool_page',{'pool_id':result['pool_ref']['pool_id']},second,next_state,cancel)
                self.assertEqual(len(local['candidates']),5)
                result=roles._execute_tool('research','research_candidates',{'stage_or_keyword':'TEST-1'},second,next_state,cancel)
                self.assertEqual(requested,[1,2,3]);self.assertTrue(result['coverage']['complete'])
                self.assertEqual(result['coverage']['collected'],125)


if __name__=='__main__':unittest.main()

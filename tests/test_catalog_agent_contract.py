from pathlib import Path
import tempfile
import unittest
from shu.maa_agents import _ROLE_TOOLS, _TOOLS
from shu.maa_graph import ROLES

class CatalogAgentContract(unittest.TestCase):
    def test_catalog_is_isolated(self):
        self.assertIn('catalog', ROLES)
        self.assertIn('catalog', _ROLE_TOOLS)
        names = set(_ROLE_TOOLS['catalog'])
        self.assertTrue({'research_activity_stages', 'research_search', 'research_fetch', 'research_levels', 'normalize_structure', 'finish_role'} <= names)
        self.assertFalse(names & {'research_download', 'maa_act', 'research_candidates', 'research_screen'})
        self.assertIn('catalog', _TOOLS['delegate']['function']['parameters']['properties']['next_role']['enum'])

class PlannedRoleTests(unittest.TestCase):
    def setUp(self):
        staging = Path(__file__).resolve().parents[2] / '.pet-temp'
        staging.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=staging)
        self.addCleanup(temporary.cleanup)
        self.fixture_root = Path(temporary.name)

    def test_catalog_completion_rejects_model_claim_without_evidence(self):
        from types import SimpleNamespace
        from shu.planned_agents import verify_planned_completion
        store = SimpleNamespace(engine=SimpleNamespace(data_dir='.'))
        state = {'task_id':'t', 'epoch':1, 'role_results':{'catalog':{'status':'completed', 'summary':'done'}}}
        self.assertFalse(verify_planned_completion(store, state, 'stage_catalog')['complete'])

    def test_typed_view_skips_legacy_json_precheck(self):
        from shu.maa_agents import _model_context
        value = _model_context({'contract': {'current_deliverable':'copilot_codes', 'goal':'查作业'},
                                'role_results': {'research': {'status':'researched', 'steps':[]}}})
        self.assertNotIn('material_delivery_precheck', value)
        self.assertEqual(value['role_results']['research']['steps'], [])

    def test_research_uses_only_claimed_steps_and_closes_in_order(self):
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        from shu.planned_agents import run_planned_research
        context = {'task_id':'t','epoch':1,'revision':1,'dispatch_id':'d'}
        lease = {'step_id':'s','resolved_stage':{'display_code':'X-1','stage_key':'k'}}
        plans = Mock()
        plans.current.return_value = {'activity_id':'fixture','ordered_stage_keys':['k'], 'groups':[{'label':'G','stage_keys':['k']}], 'steps':[{'step_id':'s','status':'primary_only','result':{}}]}
        plans.claim_next.side_effect = [lease, None]
        plans.retry_failed.return_value = []
        result = {'status':'primary_only', 'primary_ref':{'id':'p'}}
        worker = Mock()
        worker.find_pair.return_value = result
        store = Mock()
        store.engine.data_dir = self.fixture_root
        agents = SimpleNamespace(store=store, directory=self.fixture_root,
            clients={'research':SimpleNamespace(config=Mock())}, rosters=Mock(),
            _guard=Mock(), _progress=Mock())
        with patch('shu.planned_agents.PlanStore', return_value=plans), \
             patch('shu.planned_agents.ResearchTools') as research_factory, \
             patch('shu.code_delivery.CodeDeliveryStore'), \
             patch('shu.code_delivery.CodeTools', return_value=worker), \
             patch('shu.job_library.JobLibrary') as library_factory:
            library_factory.return_value.adopt_step.return_value = None
            output = run_planned_research(agents, context, Mock())
        plans.close_step.assert_called_once_with(lease, result)
        worker.find_pair.assert_called_once()
        self.assertEqual(output['steps'][0]['stage'], 'X-1')
        self.assertEqual(research_factory.call_args.kwargs['public_cache_root'],
                         self.fixture_root / 'public-cache')

    def test_catalog_gate_requires_matching_task_epoch_and_hash(self):
        import hashlib, json, tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from shu.planned_agents import verify_planned_completion
        with tempfile.TemporaryDirectory(dir=self.fixture_root) as d:
            def save(name, value):
                raw=json.dumps(value).encode(); path=Path(d)/name; path.write_bytes(raw)
                return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}
            catalog=save('catalog.json', {'catalog_complete':True,'event_name':'A','source':'public','raw_sha256':'abc'})
            structure=save('structure.json', {'ok':True,'catalog_complete':True,'activity_name':'A','catalog_ref':catalog,
                'stages':[{'stage_key':'a1','stage_id':'a1','display_code':'A-1','difficulty':1,'group_id':'g'}],
                'groups':[{'group_id':'g','label':'第一章','stage_keys':['a1']}]})
            obs=save('obs.json', {'task_id':'t','epoch':1,'tool':'research_activity_stages','result':{'ok':True,'stage_catalog_ref':catalog}})
            state={'task_id':'t','epoch':1,'role_results':{'catalog':{'structure_ref':structure,'evidence_refs':[obs]}}}
            store=SimpleNamespace(engine=SimpleNamespace(data_dir=d), current=lambda task_id:{'contract':{}})
            self.assertTrue(verify_planned_completion(store,state,'stage_catalog')['complete'])
            state['epoch']=2
            self.assertFalse(verify_planned_completion(store,state,'stage_catalog')['complete'])

    def test_public_facts_feed_structure_without_inventing_difficulty(self):
        import json, tempfile
        from pathlib import Path
        from shu.activity_public_facts import stage_facts
        from shu.activity_structure import normalize_structure, ordered_stage_keys
        catalog={'event_name':'A','catalog_complete':True,'total_stages':2,'stages':[
            {'stage_id':'normal','code':'X-1'}, {'stage_id':'challenge','code':'X-1'}]}
        table={'normal':{'stageId':'normal','code':'X-1','levelId':'l','difficulty':'NORMAL','zoneId':'z','unlockCondition':[]},
               'challenge':{'stageId':'challenge','code':'X-1','levelId':'l','difficulty':'FOUR_STAR','zoneId':'z','unlockCondition':[]}}
        def transport(url):
            return json.dumps({'stages':table} if 'stage_table' in url else {'zones':{'z':{'zoneNameSecond':'真实分区','zoneIndex':1}}}).encode()
        with tempfile.TemporaryDirectory(dir=self.fixture_root) as d:
            facts=stage_facts(catalog, Path(d), transport=transport)
            proposal={k:facts[k] for k in ('query_mappings','groups','phase_links','order_constraints')}
            result=normalize_structure(catalog, proposal)
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(ordered_stage_keys(result)),2)
            self.assertEqual({s['difficulty'] for s in result['stages']},{1,2})
            self.assertEqual({s['query_key'] for s in result['stages']},{'normal'})

    def test_structure_tool_rejects_model_mapping_and_uses_fact_result(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from shu.maa_agents import MaaRoleAgents
        catalog={'event_name':'A','catalog_complete':True,'total_stages':1,'stages':[{'stage_id':'s','code':'X-1'}]}
        factref={'path':'public.json','sha256':'a'}
        facts={'ok':True,'groups':[{'group_id':'z','label':'Zone','stage_ids':['s'],'source_refs':[factref]}],
               'query_mappings':[{'stage_id':'s','query_key':'s','difficulty':1,'source_refs':[factref]}],
               'phase_links':[],'order_constraints':[],'unknown':[]}
        with tempfile.TemporaryDirectory(dir=self.fixture_root) as d:
            agent=MaaRoleAgents.__new__(MaaRoleAgents); agent.directory=Path(d); agent._guard=Mock()
            agent._publish=Mock(return_value={'path':'result','sha256':'digest'})
            context={'task_id':'t','epoch':1,'dispatch_id':'d','contract':{'current_deliverable':'stage_catalog'}}
            state={'activity_catalog':{'ok':True,'stage_catalog_ref':{}},'stage_facts':facts}
            with patch('shu.activity_context.read_activity_catalog', return_value=catalog):
                with self.assertRaises(ValueError):
                    agent._execute_tool('catalog','normalize_structure',{'proposal':{'query_mappings':[]}},context,state,Mock())
                result=agent._execute_tool('catalog','normalize_structure',{'proposal':{}},context,state,Mock())
            self.assertTrue(result['ok'])
            self.assertEqual(agent._publish.call_args.args[1]['stages'][0]['difficulty'],1)

    def test_explicit_stage_scopes_plan_without_erasing_catalog(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch
        from shu.maa_agents import MaaRoleAgents
        catalog={'event_name':'A','catalog_complete':True,'total_stages':2,'stages':[
            {'stage_id':'s1','code':'X-1'}, {'stage_id':'s2','code':'X-2'}]}
        ref={'path':'public','sha256':'abc'}
        facts={'ok':True,'groups':[{'group_id':'z','label':'Z','stage_ids':['s1','s2'],'source_refs':[ref]}],
               'query_mappings':[{'stage_id':s,'query_key':s,'difficulty':1,'source_refs':[ref]} for s in ('s1','s2')],
               'phase_links':[],'order_constraints':[],'unknown':[]}
        with tempfile.TemporaryDirectory(dir=self.fixture_root) as d:
            agent=MaaRoleAgents.__new__(MaaRoleAgents); agent.directory=Path(d); agent._guard=Mock()
            agent._publish=Mock(return_value={'path':'result','sha256':'digest'})
            context={'task_id':'t','epoch':1,'dispatch_id':'d','contract':{'current_deliverable':'copilot_codes','goal':'查 X-2 作业', 'task_spec':{'stage_code':'X-2'}}}
            state={'activity_catalog':{'ok':True,'stage_catalog_ref':ref},'stage_facts':facts}
            with patch('shu.activity_context.read_activity_catalog', return_value=catalog):
                result=agent._execute_tool('catalog','normalize_structure',{'proposal':{}},context,state,Mock())
            self.assertTrue(result['ok'],result)
            structure=agent._publish.call_args.args[1]
            self.assertEqual(len(structure['stages']),2)
            self.assertEqual([s['display_code'] for s in structure['stages'] if s['stage_key'] not in structure['excluded_stage_keys']],['X-2'])

    def test_structure_schema_only_accepts_group_order(self):
        schema=_TOOLS['normalize_structure']['function']['parameters']['properties']['proposal']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']), {'group_order'})
        self.assertIn('proposal={}', _TOOLS['normalize_structure']['function']['description'])

    def test_catalog_search_disables_station_lookup(self):
        from pathlib import Path
        from unittest.mock import Mock
        from shu.maa_agents import MaaRoleAgents
        agent=MaaRoleAgents.__new__(MaaRoleAgents); agent._guard=Mock(); agent.directory=Path('.')
        agent._research_attempts={}; research=Mock(); research.search.return_value={'ok':False}
        state={'research':research}; context={'task_id':'t','epoch':1,'contract':{},'role_results':{}}
        agent._execute_tool('catalog','research_search',{'query':'公开资料'},context,state,Mock())
        research.search.assert_called_once_with(query='公开资料',include_station=False)

    def test_restore_only_persisted_same_generation(self):
        from pathlib import Path
        from unittest.mock import Mock, patch
        from shu.maa_agents import MaaRoleAgents
        agent=MaaRoleAgents.__new__(MaaRoleAgents); agent.directory=Path('.').resolve(); agent.store=Mock()
        ref1={'path':'catalog-observation'}; ref2={'path':'facts-observation'}; catalogref={'path':'catalog'}
        agent.store.get_dispatch.return_value={'role':'catalog','task_id':'t','revision':2,'epoch':3,'status':'completed'}
        agent.store.dispatch_result.return_value={'evidence_refs':[ref1,ref2]}
        observations=[{'task_id':'t','epoch':3,'dispatch_id':'d','read_only':True,'tool':'research_activity_stages','result':{'ok':True,'stage_catalog_ref':catalogref}},
            {'task_id':'t','epoch':3,'dispatch_id':'d','read_only':True,'tool':'catalog_stage_facts','catalog_ref':catalogref,'result':{'ok':True,'source_refs':[{'path':'source'}]}}]
        agent._read_ref=Mock(side_effect=observations)
        context={'task_id':'t','revision':2,'epoch':3,'handoff_history':[{'role':'catalog','dispatch_id':'d'}]}
        with patch('shu.activity_context.read_activity_catalog'),patch('shu.leader_plan.read_reference'):
            state={}; agent._restore_catalog(context,state)
        self.assertTrue(state['stage_facts']['ok'])
        agent.store.get_dispatch.return_value['revision']=1
        state={}; agent._restore_catalog(context,state)
        self.assertNotIn('activity_catalog',state)

    def test_typed_scope_ignores_goal_history(self):
        from shu.leader_policy import research_stage_codes
        c={'current_deliverable':'copilot_codes','goal':'原任务 TL-1，本次 SN-6',
           'user_request':'以前 TL-1','target_stage_codes':['SN-6'], 'task_spec':{}}
        self.assertEqual(research_stage_codes(c),['SN-6'])
        c['target_stage_codes']=[]
        self.assertEqual(research_stage_codes(c),[])

    def test_closed_missing_step_is_not_researched_or_retryable(self):
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        from pathlib import Path
        from shu.planned_agents import run_planned_research
        plan={'activity_id':'fixture','ordered_stage_keys':['k'],'steps':[{'step_id':'s','status':'search_incomplete','result':{'reason':'budget_exhausted'}}]}
        plans=Mock(); plans.claim_next.return_value=None; plans.current.return_value=plan
        store=Mock(); store.engine.data_dir=self.fixture_root
        agents=SimpleNamespace(store=store,directory=self.fixture_root,clients={'research':SimpleNamespace(config=Mock())},rosters=Mock(),_guard=Mock())
        with patch('shu.planned_agents.PlanStore',return_value=plans),patch('shu.planned_agents.ResearchTools') as research_factory,patch('shu.code_delivery.CodeDeliveryStore'),patch('shu.code_delivery.CodeTools'),patch('shu.job_library.JobLibrary'):
            out=run_planned_research(agents,{'task_id':'t','revision':1,'epoch':1,'dispatch_id':'d'},Mock())
        self.assertEqual(out['status'],'unknown')
        self.assertTrue(out['closed_not_retryable'])
        self.assertEqual(out['structural_gaps'][0]['reason'],'budget_exhausted')
        self.assertEqual(research_factory.call_args.kwargs['public_cache_root'],
                         self.fixture_root / 'public-cache')

    def test_reuse_catalog_is_current_task_observation(self):
        from pathlib import Path
        from unittest.mock import Mock, patch
        from shu.maa_agents import MaaRoleAgents
        agent=MaaRoleAgents.__new__(MaaRoleAgents); agent.directory=Path('.'); agent.store=Mock(); agent._guard=Mock()
        agent._publish=Mock(return_value={'path':'obs','sha256':'x'})
        context={'task_id':'new','epoch':2,'dispatch_id':'dispatch','contract':{'source_task_id':'old'}}
        source={'ok':True,'stage_catalog_ref':{'path':'copied','sha256':'h'}}
        with patch('shu.catalog_memory.linked_catalog',return_value=source):
            state={}; result=agent._execute_tool('catalog','reuse_catalog',{},context,state,Mock())
        self.assertEqual(state['activity_catalog'],source)
        obs=agent._publish.call_args.args[1]
        self.assertEqual((obs['task_id'],obs['epoch'],obs['tool']),('new',2,'research_activity_stages'))
        self.assertEqual(result['observation_ref']['path'],'obs')

    def test_catalog_facts_projection_includes_opening_evidence(self):
        from pathlib import Path
        from unittest.mock import Mock, patch
        from shu.maa_agents import MaaRoleAgents, _PROMPTS
        agent=MaaRoleAgents.__new__(MaaRoleAgents); agent.directory=Path('.'); agent._guard=Mock()
        agent._publish=Mock(return_value={'path':'obs','sha256':'x'})
        context={'task_id':'t','epoch':1,'dispatch_id':'d','contract':{}}
        state={'activity_catalog':{'ok':True,'stage_catalog_ref':{'path':'catalog'}}}
        phase={'phase_id':'z','stage_ids':['a','b'],'opening_status':'open','opens_at':'2026-01-01','ends_at':'2026-12-01','source_refs':[{'path':'p','sha256':'h'}]}
        facts={'ok':True,'groups':[],'phase_links':[phase],'query_mappings':[],'unknown':[]}
        with patch('shu.activity_context.read_activity_catalog',return_value={}),patch('shu.activity_public_facts.stage_facts',return_value=facts):
            result=agent._execute_tool('catalog','catalog_stage_facts',{},context,state,Mock())
        shown=result['phase_links'][0]
        self.assertEqual((shown['phase_id'],shown['stage_count'],shown['opening_status']),('z',2,'open'))
        self.assertEqual(shown['source_refs'],phase['source_refs'])
        self.assertEqual(shown['opens_at'],phase['opens_at'])
        self.assertEqual(shown['ends_at'],phase['ends_at'])
        self.assertIn('无新矛盾不重复网页查询',_PROMPTS['catalog'])

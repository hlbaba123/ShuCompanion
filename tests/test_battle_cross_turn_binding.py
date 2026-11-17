"""Cross-turn gameplay consumes only its explicit, verified single-import source."""
import hashlib
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_battle_import_binding as fixtures
from shu.battle_contract import authorize
from shu.battle_import_binding import ImportBindingError, ImportJobPin
from shu.core import TaskEngine
from shu.maa_battle import BattleRunner, loaded_target
from shu.workflow_store import WorkflowStore, _encode


class CrossTurnImportBindingTests(unittest.TestCase):
    def setup_import(self, difficulty=2, *, external_pin=False):
        fixture=fixtures.BattleImportBindingTests('runTest')
        fixture.import_difficulty=difficulty
        fixture.import_title='Unit UT-EX-1 普通/突袭兼容'
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if external_pin:
            from shu.battle_import_binding import load_import_binding
            fixture.acceptance_pin=load_import_binding(fixture.path)
        engine=TaskEngine(fixture.data)
        self.addCleanup(engine.close)
        store=WorkflowStore(engine)
        # Match the real non-compound Graph dispatch: execution_step is absent.
        dispatch=store.get_dispatch(fixture.import_dispatch_id)
        payload=dict(dispatch['payload']);payload.pop('execution_step')
        encoded,digest=_encode(payload)
        store._db.execute('UPDATE workflow_dispatches SET payload=?,payload_hash=? WHERE dispatch_id=?',
                          (encoded,digest,fixture.import_dispatch_id))
        return fixture,store

    def runner(self, fixture, store, *, source='fixture', stage='UT-EX-1', request=None):
        source=fixture.tid if source=='fixture' else source
        contract=authorize({'user_request':request or '现在开始MAA自动战斗，运行刚才导入的这关一次，不使用理智药或源石。',
            'current_deliverable':'gameplay','relation':'followup','source_task_id':source,
            'task_spec':{'kind':'repeat_stage','stage_code':stage,'repeat_count':1}})
        state=store.submit('Run the explicitly imported unit job',contract)
        dispatch=store.ensure_dispatch(state['task_id'],'maa','battle',{'contract':state['contract']})
        store.claim_dispatch(dispatch['dispatch_id'],'unit-battle')
        runner=BattleRunner.__new__(BattleRunner)
        runner.store=store
        runner.context={key:state[key] for key in ('task_id','revision','epoch','contract')}
        runner.context['dispatch_id']=dispatch['dispatch_id']
        runner.roles=SimpleNamespace(battle_expected_job=None)
        runner.guard=lambda:None
        runner.cancel=threading.Event();runner.stopping=threading.Event()
        runner.start_operation=None;runner.stop_result=None;runner.evidence=[];runner.sequence=0
        runner.supervisor=SimpleNamespace(target_identity=fixtures.IDENTITY)
        runner.publish=lambda *args:None
        return runner

    def run_before_native(self, runner):
        pins=[]
        def boundary(*args):
            pins.append(runner._expected_import_pin())
            raise RuntimeError('unit stop before any native window operation')
        with patch('shu.battle_windows.ensure_maa_window',side_effect=boundary), \
             patch('shu.battle_windows.ensure_game_window') as game:
            result=runner.run()
        game.assert_not_called()
        return result,pins

    def test_both_modes_of_same_compatible_code_follow_import_proof(self):
        for difficulty,mode in ((1,'normal'),(2,'raid')):
            with self.subTest(difficulty=difficulty):
                fixture,store=self.setup_import(difficulty)
                runner=self.runner(fixture,store)
                result,pins=self.run_before_native(runner)
                self.assertEqual(len(pins),1,result)
                self.assertIsInstance(pins[0],ImportJobPin)
                self.assertEqual((pins[0].source_task_id,pins[0].difficulty),(fixture.tid,mode))
                document={'doc':{'title':fixture.expected['title']},'actions':[{}],'stage_name':'unit'}
                raw=json.dumps(document).encode()
                view={'page':'copilot','running':False,'multi_mode':False,'start_box':
                    {'x':.2,'y':.2,'width':.1,'height':.1},'mystery_code':'prts://123',
                    'stage_code':'UT-EX-1','preview_title':fixture.expected['title']}
                target=loaded_target(view,document,runner.context['contract'])
                self.assertEqual(runner._import_bound_target(target,raw)['difficulty'],mode)

    def test_missing_source_never_uses_latest_verified_import_or_opens_window(self):
        fixture,store=self.setup_import()
        runner=self.runner(fixture,store,source=None)
        result,pins=self.run_before_native(runner)
        self.assertEqual(pins,[])
        self.assertEqual(result['code'],'battle_import_binding_unverified')

    def test_readonly_or_unverified_source_cannot_fall_back_to_real_import(self):
        for kind in ('copilot_codes','maa_import'):
            with self.subTest(kind=kind):
                fixture,store=self.setup_import()
                other=store.submit('A different task',{'current_deliverable':kind,'allow_start':False})
                runner=self.runner(fixture,store,source=other['task_id'])
                result,pins=self.run_before_native(runner)
                self.assertEqual(pins,[])
                self.assertEqual(result['code'],'battle_import_binding_unverified')

    def test_old_source_generation_recycled_source_or_wrong_target_are_rejected(self):
        for mutation in ('resume','trash','stage','proof'):
            with self.subTest(mutation=mutation):
                fixture,store=self.setup_import()
                runner=self.runner(fixture,store,stage='OTHER-1' if mutation=='stage' else 'UT-EX-1')
                if mutation=='resume':store.control(fixture.tid,'resume')
                elif mutation=='trash':store.management.trash(fixture.tid)
                elif mutation=='proof':
                    from pathlib import Path
                    path=Path(fixture.proof_ref['path']);path.write_bytes(path.read_bytes()+b' ')
                result,pins=self.run_before_native(runner)
                self.assertEqual(pins,[])
                self.assertEqual(result['code'],'battle_import_binding_unverified')

    def test_consumed_pin_rejects_replaced_code_and_later_source_revocation(self):
        fixture,store=self.setup_import()
        runner=self.runner(fixture,store)
        _,pins=self.run_before_native(runner)
        self.assertIsInstance(pins[0],ImportJobPin)
        target={'stage_code':'UT-EX-1','mystery_code':'prts://999','difficulty':'normal'}
        with self.assertRaises(ImportBindingError):runner._import_bound_target(target,b'cache')
        store.control(fixture.tid,'resume')
        before=store._db.execute('SELECT COUNT(*) FROM workflow_operations').fetchone()[0]
        with self.assertRaises(ImportBindingError):
            runner.input({'surface':'maa','action':'click','target':'battle_start'},starts=True)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM workflow_operations').fetchone()[0],before)

    def test_unreferenced_preloaded_single_job_retains_legacy_entry(self):
        fixture,store=self.setup_import()
        runner=self.runner(fixture,store,source=None,
            request='开始MAA自动战斗，运行当前已加载的单作业一次')
        _,pins=self.run_before_native(runner)
        self.assertEqual(pins,[None])

    def test_explicit_external_acceptance_pin_is_still_a_declared_source(self):
        fixture,store=self.setup_import(external_pin=True)
        runner=self.runner(fixture,store,source=None)
        runner.roles.battle_expected_job=fixture.acceptance_pin
        _,pins=self.run_before_native(runner)
        self.assertEqual(pins,[runner.roles.battle_expected_job])

    def test_explicit_raid_without_source_is_not_downgraded_by_compatible_title(self):
        fixture,store=self.setup_import()
        runner=self.runner(fixture,store,source=None,request='开始MAA自动战斗，运行当前已加载的UT-EX-1突袭一次')
        result,pins=self.run_before_native(runner)
        self.assertEqual(pins,[])
        self.assertEqual(result['code'],'battle_import_binding_unverified')

    def test_newer_source_dispatch_and_foreign_battle_dispatch_are_rejected(self):
        for change in ('source_dispatch','battle_dispatch'):
            with self.subTest(change=change):
                fixture,store=self.setup_import()
                runner=self.runner(fixture,store)
                if change=='source_dispatch':
                    store._db.execute('INSERT INTO workflow_dispatches '
                        '(dispatch_id,task_id,revision,epoch,role,dispatch_key,payload,payload_hash,created_at) '
                        "SELECT 'pending-newer',task_id,revision,epoch,role,'pending-newer',payload,payload_hash,created_at "
                        'FROM workflow_dispatches WHERE dispatch_id=?',(fixture.import_dispatch_id,))
                else:runner.context['dispatch_id']=fixture.import_dispatch_id
                result,pins=self.run_before_native(runner)
                self.assertEqual(pins,[])
                self.assertEqual(result['code'],'battle_import_binding_unverified')

    def test_start_action_carries_exact_source_generation_and_receipt_for_commit_gate(self):
        fixture,store=self.setup_import()
        runner=self.runner(fixture,store)
        self.run_before_native(runner)
        with patch.object(store,'prepare_operation',side_effect=RuntimeError('stop before input')) as prepare:
            with self.assertRaisesRegex(RuntimeError,'stop before input'):
                runner.input({'surface':'maa','action':'click','target':'battle_start'},starts=True)
        action=prepare.call_args.args[3]
        self.assertEqual(action['source_import_binding'],{'task_id':fixture.tid,'revision':1,'epoch':1,
            'dispatch_id':fixture.import_dispatch_id,'receipt_ref':fixture.proof_ref})
        self.assertEqual(action['source_import_task_id'],fixture.tid)
        self.assertEqual(action['import_proof_ref'],fixture.proof_ref)


if __name__=='__main__':unittest.main()

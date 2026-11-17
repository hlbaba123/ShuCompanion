"""Offline SQLite/PNG receipts bind an import to a later battle acceptance."""
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from types import SimpleNamespace

from shu.battle_import_binding import ImportJobPin, ImportBindingError, load_import_binding, bind_import_target
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_queue_receipt import _publish
from tests.test_battle_ledger import png


IDENTITY={'hwnd':41,'pid':42,'creation_time':43,'executable':r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe'}


class BattleImportBindingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.data=self.root/'data';self.data.mkdir()
        self.expected={'code':'prts://123','stage_code':'UT-EX-1',
                       'title':getattr(self,'import_title','Unit UT-EX-1'),
                       'difficulty':getattr(self,'import_difficulty',1),'operator_names':['Unit Operator']}
        engine=TaskEngine(self.data);store=WorkflowStore(engine)
        state=store.submit('Import unit job',{'current_deliverable':'maa_import','allow_start':False})
        self.tid=state['task_id'];dispatch=store.ensure_dispatch(self.tid,'maa','import',
            {'contract':state['contract'],'execution_step':'import'})
        self.import_dispatch_id=dispatch['dispatch_id']
        operation=store.prepare_operation(self.tid,dispatch['dispatch_id'],'code',
            {'kind':'maa_import','action':'replace_code','code':self.expected['code']})
        store.issue_operation(operation['operation_id']);store.enter_submission(operation['operation_id'])
        store.finish_operation(operation['operation_id'],'verified',{'input_dispatched':True})
        refs=[]
        for n,name in enumerate(('before','entry','first','last')):
            path=self.data/(name+'.png');path.write_bytes(png())
            loaded=name in ('first','last')
            frame={'screenshot_id':name,'ok':True,'page':'copilot','idle':True,'running':False,
                'field_empty':name=='before','expected_title_visible':loaded,'expected_stage_visible':loaded,
                'expected_operators_visible':['Unit Operator'] if loaded else [],'missing_operators':[],
                'operator_count':1 if loaded else None,'errors':[],
                'codes':[self.expected['code']] if name=='entry' else [],'code_id_readback':None,
                'preview_signature':'loaded' if loaded else 'empty','preview_sha256':'new' if loaded else 'old',
                'image_path':str(path),'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'window':{**IDENTITY,'client_rect':{'width':2,'height':2},'dpi':96}}
            refs.append(_publish(self.data,'frame-'+name,frame))
        # A real import may accumulate refresh/comparison/load observations.
        # a39 had 34 valid references and failed the former 32-reference cap.
        # Retain them in the flat durable protocol through final readback and
        # the later battle handoff, without requiring another GUI input.
        for n in range(29):
            refs.append(_publish(self.data,'refresh-'+str(n),{'diagnostic':'refresh','attempt':n}))
        self.proof={'transport':'screenshot_ocr','start_clicked':False,'code':self.expected['code'],
            'expected':self.expected,'before':'before','code_entry':'entry','after':['first','last'],
            'observation_refs':refs,'actions':[{'operation_id':operation['operation_id'],'action':'replace_code'}]}
        self.proof_ref=_publish(self.data,'proof',self.proof)
        self.receipt={'status':'import_verified','transport':'screenshot_keyboard',
            'mystery_code':self.expected['code'],'battle_start_submitted':False,
            'receipt_ref':self.proof_ref,'evidence_refs':[*refs,self.proof_ref]}
        store.finish_dispatch(dispatch['dispatch_id'],self.receipt)
        store.complete_import_verified(self.tid,self.receipt['evidence_refs'],expected_epoch=state['epoch'])
        from shu.conversation_acceptance import import_verification
        verified=import_verification(store,self.tid)
        self.assertTrue(verified['verified'])
        self.report={'passed':True,'verified_import_count':1,'battle_start_submissions':0,
            'turns':[{'contract':{'current_deliverable':'maa_import'},'task':{'id':self.tid},
                      'import_verification':verified}]}
        engine.close()
        self.path=self.root/'report.json';self.save_report()

    def save_report(self):self.path.write_text(json.dumps(self.report),encoding='utf-8')

    def test_completed_actual_import_becomes_frozen_non_authorizing_pin(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        pin=load_import_binding(self.path)
        self.assertEqual((pin.stage_code,pin.mystery_code,pin.difficulty),('UT-EX-1','prts://123','normal'))
        self.assertEqual(pin.maa_identity,tuple(IDENTITY[k] for k in ('hwnd','pid','creation_time','executable')))
        with self.assertRaises(FrozenInstanceError):pin.stage_code='OTHER-1'
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_modified_receipt_is_rejected_even_if_report_claims_success(self):
        p=Path(self.proof_ref['path']);p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaises(ImportBindingError):load_import_binding(self.path)

    def test_current_pin_reads_exact_import_dispatch_and_rejects_old_generation(self):
        from shu.battle_import_binding import load_current_import_binding
        engine=TaskEngine(self.data);self.addCleanup(engine.close)
        store=WorkflowStore(engine)
        pin=load_current_import_binding(store,self.tid,self.import_dispatch_id)
        self.assertEqual((pin.stage_code,pin.mystery_code,pin.difficulty),('UT-EX-1','prts://123','normal'))
        self.assertEqual((pin.proof_path,pin.proof_sha256),(self.proof_ref['path'],self.proof_ref['sha256']))
        with self.assertRaises(ImportBindingError):
            load_current_import_binding(store,self.tid,'unrelated-dispatch')
        store.control(self.tid,'resume')
        with self.assertRaises(ImportBindingError):
            load_current_import_binding(store,self.tid,self.import_dispatch_id)

    def test_false_or_incomplete_report_cannot_bind(self):
        for key,value in (('passed',False),('verified_import_count',0),('battle_start_submissions',1)):
            with self.subTest(key=key):
                saved=self.report[key];self.report[key]=value;self.save_report()
                with self.assertRaises(ImportBindingError):load_import_binding(self.path)
                self.report[key]=saved
        self.report['turns'][0]['import_verification']['mystery_code']='prts://999';self.save_report()
        with self.assertRaises(ImportBindingError):load_import_binding(self.path)

    def test_new_loaded_stage_code_or_process_cannot_replace_imported_job(self):
        pin=load_import_binding(self.path)
        target={'stage_code':'UT-EX-1','mystery_code':'prts://123','difficulty':'normal'}
        self.assertEqual(bind_import_target(pin,target,IDENTITY,'a'*64),target)
        for key,value in (('stage_code','OTHER-1'),('mystery_code','prts://999')):
            with self.subTest(key=key),self.assertRaises(ImportBindingError):
                bind_import_target(pin,{**target,key:value},IDENTITY,'a'*64)
        with self.assertRaises(ImportBindingError):bind_import_target(pin,target,{**IDENTITY,'pid':99},'a'*64)

    def test_mode_is_bound_to_verified_import_not_a_title_inference(self):
        pin=load_import_binding(self.path)
        target={'stage_code':'UT-EX-1','mystery_code':'prts://123','difficulty':'raid'}
        self.assertEqual(bind_import_target(pin,target,IDENTITY,'a'*64)['difficulty'],'normal')

    def test_actual_maa_cache_fingerprint_is_checked_when_receipt_has_it(self):
        pin=replace(load_import_binding(self.path),document_sha256='a'*64)
        target={'stage_code':'UT-EX-1','mystery_code':'prts://123','difficulty':'normal'}
        self.assertEqual(bind_import_target(pin,target,IDENTITY,'a'*64),target)
        with self.assertRaises(ImportBindingError):bind_import_target(pin,target,IDENTITY,'b'*64)

    def test_runner_bound_guard_rejects_changed_job_after_initial_check(self):
        from shu.maa_battle import BattleRunner
        runner=BattleRunner.__new__(BattleRunner)
        runner.roles=SimpleNamespace(battle_expected_job=load_import_binding(self.path))
        runner.supervisor=SimpleNamespace(target_identity=IDENTITY)
        target={'stage_code':'UT-EX-1','mystery_code':'prts://123','difficulty':'normal'}
        self.assertEqual(runner._import_bound_target(target,b'actual cache bytes'),target)
        with self.assertRaises(ImportBindingError):
            runner._import_bound_target({**target,'mystery_code':'prts://999'},b'actual cache bytes')

    def test_invalid_binding_writes_clean_zero_input_acceptance_report(self):
        from shu.maa_battle_acceptance import run_battle_acceptance
        output=self.root/'blocked-battle'
        result=run_battle_acceptance(self.root,self.root,output,self.root/'unused-key',self.data,
            version='unit',battle_import_report=self.root/'missing.json')
        self.assertEqual(result,2)
        report=json.loads((output/'report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['passed']);self.assertEqual(report['gui_inputs'],0)
        self.assertFalse(report['battle_start_submitted'])
        self.assertFalse((output/'data').exists())

    def test_no_pin_retains_current_loaded_job_behavior_without_granting_authority(self):
        target={'stage_code':'OTHER-1','mystery_code':'prts://99','difficulty':'normal'}
        self.assertEqual(bind_import_target(None,target,IDENTITY,'a'*64),target)
        from shu.battle_contract import authorized
        self.assertFalse(authorized({'current_deliverable':'maa_import'}))


class BattleCliBoundaryTests(unittest.TestCase):
    def test_import_binding_requires_battle_and_diagnostics_cannot_be_mixed(self):
        main=Path(__file__).resolve().parents[1]/'main.py'
        for args in (['--battle-import-report','missing.json'],
                     ['--maa-battle-test','output','--conversation-test','questions.json','--allow-maa-import-test'],
                     ['--maa-battle-test','output','--capture-probe','123']):
            with self.subTest(args=args):
                result=subprocess.run([sys.executable,str(main),*args],capture_output=True,text=True)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertIn('error:',result.stderr)


if __name__=='__main__':unittest.main()

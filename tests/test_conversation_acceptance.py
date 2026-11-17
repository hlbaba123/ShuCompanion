import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
import zlib

from shu.conversation_acceptance import assess_turn, import_verification


class AcceptanceBudgetTests(unittest.TestCase):
    def test_import_only_start_guard_precedes_original_ledger(self):
        from shu.conversation_acceptance import install_start_budget
        from shu.core import ActionRejected
        calls = []
        store = SimpleNamespace(
            prepare_operation=lambda *a, **k: calls.append(('prepare', a, k)),
            issue_operation=lambda *a, **k: calls.append(('issue', a, k)),
            enter_submission=lambda *a, **k: calls.append(('submit', a, k)),
            get_operation=lambda _: {'starts': True, 'action': {}})
        install_start_budget(store, allowed=lambda: False)
        for operation in (lambda: store.prepare_operation('task','dispatch','effect',{},starts=True),
                          lambda: store.issue_operation('operation'),
                          lambda: store.enter_submission('operation')):
            with self.assertRaises(ActionRejected):
                operation()
        self.assertEqual(calls, [])
        store.prepare_operation('task','dispatch','effect',{},starts=False)
        self.assertEqual(len(calls), 1)

    def test_budget_observes_current_turn_and_counts_attempts_across_tasks(self):
        from shu.conversation_acceptance import install_start_budget
        from shu.core import ActionRejected
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.execute('CREATE TABLE workflow_operations(starts,issued_at)')
        phase = {'final': False}
        calls = []
        store = SimpleNamespace(engine=SimpleNamespace(_lock=threading.RLock(), _db=database),
            prepare_operation=lambda *a, **k: calls.append('prepare'),
            issue_operation=lambda *a, **k: calls.append('issue'),
            enter_submission=lambda *a, **k: calls.append('submit'),
            get_operation=lambda _: {'starts': True, 'action': {}})
        install_start_budget(store, allowed=lambda: phase['final'])
        with self.assertRaises(ActionRejected): store.issue_operation('one')
        phase['final'] = True
        store.issue_operation('one')
        database.execute("INSERT INTO workflow_operations VALUES(1,'issued')")
        store.enter_submission('one')
        with self.assertRaises(ActionRejected): store.issue_operation('two')
        self.assertEqual(calls, ['issue','submit'])

    def test_last_turn_must_run_exact_preceding_import_and_authorized_stage(self):
        from shu.conversation_acceptance import install_start_budget
        from shu.core import ActionRejected
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.execute('CREATE TABLE workflow_operations(starts,issued_at)')
        actual = {'stage_code': 'TL-EX-1', 'difficulty': 'raid', 'mystery_code': 'prts://123'}
        preceding = {'mystery_code': 'prts://123'}
        calls = []
        store = SimpleNamespace(engine=SimpleNamespace(_lock=threading.RLock(), _db=database),
            _battle_reference=lambda _: {'target': dict(actual)},
            prepare_operation=lambda *a, **k: calls.append('prepare'),
            issue_operation=lambda *a, **k: calls.append('issue'),
            enter_submission=lambda *a, **k: calls.append('submit'),
            get_operation=lambda _: {'starts': True, 'action': {'ready_ref': 'verified-ref'}})
        install_start_budget(store, allowed=lambda: True, target=('TL-EX-1', 'raid'),
                             expected_import=lambda: preceding)
        for key, value in (('stage_code', 'TL-EX-2'), ('difficulty', 'normal'),
                           ('mystery_code', 'prts://999')):
            saved = actual[key]
            actual[key] = value
            with self.subTest(key=key), self.assertRaises(ActionRejected):
                store.enter_submission('one')
            actual[key] = saved
        saved_import = dict(preceding)
        preceding.clear()
        with self.assertRaises(ActionRejected): store.issue_operation('one')
        self.assertEqual(calls, [])
        preceding.update(saved_import)
        store.enter_submission('one')
        self.assertEqual(calls, ['submit'])

class AcceptanceTurnTests(unittest.TestCase):
    def test_single_run_requires_explicit_opt_in_and_verified_end_not_start_prose(self):
        turn = {'contract': {'current_deliverable': 'gameplay'},
                'task': {'status': 'completed'},
                'messages': [{'role': 'assistant', 'text': '已经开始并结束'}]}
        self.assertFalse(assess_turn(turn, allow_single_battle=True)['passed'])
        turn['battle_verification'] = {'verified': True}
        self.assertFalse(assess_turn(turn)['passed'])
        self.assertEqual(assess_turn(turn, allow_single_battle=True)['completion_scope'],
                         'maa_single_run_finished')
        turn['timed_out'] = True
        self.assertFalse(assess_turn(turn, allow_single_battle=True)['passed'])

    def test_preparation_acceptance_is_pending_and_not_a_gui_success(self):
        turn={'contract':{'current_deliverable':'maa_import'},'task':{'status':'needs_input'},
              'preparation_verification':{'verified':True,'items':[{'code':'prts://1'}],
                  'goal_complete':False,'maa_dispatches':0,'submitted_operations':0}}
        self.assertTrue(assess_turn(turn,prepare_import_only=True)['passed'])
        self.assertFalse(assess_turn(turn)['passed'])
        for field,value in (('verified',False),('items',[]),('goal_complete',True),
                            ('maa_dispatches',1),('submitted_operations',1)):
            original=turn['preparation_verification'][field]
            turn['preparation_verification'][field]=value
            self.assertFalse(assess_turn(turn,prepare_import_only=True)['passed'],field)
            turn['preparation_verification'][field]=original
        turn['task']['status']='completed'
        self.assertFalse(assess_turn(turn,prepare_import_only=True)['passed'])

    def test_chat_needs_no_task(self):
        self.assertTrue(assess_turn({'contract':{'current_deliverable':'chat'}, 'messages':[{'role':'assistant','text':'你好'}]})['passed'])
    def test_catalog_requires_completed_real_structure(self):
        turn={'contract':{'current_deliverable':'stage_catalog'},'task':{'status':'completed'},'catalog':{'verified':True,'structure':{'ok':True,'catalog_complete':True,'groups':[{'stage_keys':['a']}],'stages':[{'stage_key':'a'}]}}}
        self.assertTrue(assess_turn(turn)['passed'])
        turn['catalog']['verified']=False
        self.assertFalse(assess_turn(turn)['passed'])
    def test_codes_require_all_targets_and_real_refs(self):
        turn={'contract':{'current_deliverable':'copilot_codes'},'task':{'status':'completed'},'delivery':{'complete':True,'missing':[],'items':[{'primary_code':'prts://1'}],'code_refs':[{'receipt_id':'one'}]}}
        self.assertTrue(assess_turn(turn)['passed'])
        turn['delivery']['missing']=['b']
        self.assertFalse(assess_turn(turn)['passed'])
        turn['delivery']['missing']=[];turn['delivery']['items'].append({'stage_code':'b'})
        self.assertFalse(assess_turn(turn)['passed'])
    def test_unknown_and_errors_fail(self):
        self.assertFalse(assess_turn({'contract':{}, 'task':{'status':'completed'}})['passed'])
        self.assertFalse(assess_turn({'contract':{'current_deliverable':'chat'},'messages':[{'role':'assistant','text':'x'}], 'errors':['bad']})['passed'])

    def test_import_requires_completed_task_and_persisted_readback(self):
        turn={'contract':{'current_deliverable':'maa_import'}, 'task':{'status':'completed'},
              'import_verification':{'verified':True,'battle_start_submitted':False,'evidence_refs':[{'path':'readback'}]}}
        self.assertTrue(assess_turn(turn)['passed'])
        turn['import_verification']['verified']=False
        self.assertFalse(assess_turn(turn)['passed'])
        turn['import_verification']['verified']=True
        turn['import_verification']['battle_start_submitted']=True
        self.assertFalse(assess_turn(turn)['passed'])


class BattleEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the actual SQLite/PNG fixture, not its completion assertions.
        from tests.test_battle_ledger import BattleLedgerTests
        self.ledger = BattleLedgerTests('test_verified_idle_and_matching_persistent_maa_result_complete')
        self.ledger.setUp()
        self.addCleanup(self.ledger.doCleanups)

    def test_actual_idle_proof_and_dispatch_required_after_completion(self):
        from shu.conversation_acceptance import battle_verification
        case = self.ledger
        self.assertFalse(battle_verification(case.store, case.tid)['verified'])
        case.settle()
        case.finish_dispatch()
        case.store.complete_battle_verified(case.tid, [case.proof], expected_epoch=case.state['epoch'])
        result = battle_verification(case.store, case.tid)
        self.assertTrue(result['verified'])
        self.assertFalse(result['victory_verified'])
        self.assertEqual(result['start_operation_id'], case.operation['operation_id'])
        Path(case.idle1['path']).write_bytes(b'changed')
        self.assertFalse(battle_verification(case.store, case.tid)['verified'])


class ImportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.expected = {'code': 'prts://123456', 'title': '测试作业',
                         'stage_code': 'AA-2', 'operator_names': ['干员甲']}
        self.frames = {}
        self.frame_refs = []
        for index, identifier in enumerate(('before', 'entry', 'after-1', 'after-2')):
            image = self.root / (identifier + '.png')
            # Four valid, distinct one-pixel PNG captures keep the receipt and
            # screenshot hash checks real without any GUI or image runtime.
            def chunk(kind, data):
                return (struct.pack('>I', len(data)) + kind + data
                        + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
            image.write_bytes(b'\x89PNG\r\n\x1a\n'
                              + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                              + chunk(b'IDAT', zlib.compress(bytes((0, index, 0, 0))))
                              + chunk(b'IEND', b''))
            loaded = identifier.startswith('after-')
            frame = {'screenshot_id': identifier, 'ok': True, 'page': 'copilot',
                     'idle': True, 'running': False, 'field_empty': identifier == 'before',
                     'expected_title_visible': loaded, 'expected_stage_visible': loaded,
                     'expected_operators_visible': ['干员甲'] if loaded else [],
                     'missing_operators': [] if loaded else ['干员甲'], 'errors': [],
                     'operator_count': 1 if loaded else None,
                     'codes': [self.expected['code']] if identifier == 'entry' else [],
                     'code_id_readback': None,
                     'resolved_path': r'D:\MAA\cache\123456.json' if loaded else '',
                     'preview_signature': 'loaded-preview' if loaded else 'empty-preview',
                     'preview_sha256': 'loaded-pixels' if loaded else 'empty-pixels',
                     'image_path': str(image),
                     'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}
            self.frames[identifier] = frame
            self.frame_refs.append(self.write_artifact(identifier, frame))
        self.proof = {'transport': 'screenshot_ocr', 'start_clicked': False,
                      'code': self.expected['code'], 'expected': self.expected,
                      'before': 'before', 'code_entry': 'entry', 'after': ['after-1', 'after-2'],
                      'observation_refs': self.frame_refs,
                      'actions': [{'operation_id': 'operation-1', 'action': 'replace_code'}]}
        self.receipt = {'status': 'import_verified', 'transport': 'screenshot_keyboard',
                        'mystery_code': self.expected['code'], 'battle_start_submitted': False}
        self.save_proof()
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.executescript('''
            CREATE TABLE workflow_dispatches(dispatch_id,task_id,revision,epoch,role);
            CREATE TABLE workflow_results(dispatch_id);
            CREATE TABLE workflow_operations(operation_id,dispatch_id,task_id,epoch,
                action,starts,status,submitted_at);
            INSERT INTO workflow_dispatches VALUES('dispatch-1','task-1',1,1,'maa');
            INSERT INTO workflow_results VALUES('dispatch-1');
        ''')
        database.execute('INSERT INTO workflow_operations VALUES(?,?,?,?,?,?,?,?)',
                         ('operation-1', 'dispatch-1', 'task-1', 1,
                          json.dumps({'kind': 'maa_import', 'action': 'replace_code',
                                      'code': self.expected['code']}), 0, 'verified', 'submitted'))
        state = {'revision': 1, 'epoch': 1, 'status': 'completed', 'control_kind': 'import_verified'}
        self.store = SimpleNamespace(engine=SimpleNamespace(
            _lock=threading.RLock(), _db=database, data_dir=self.root),
            current=lambda task_id: state, dispatch_result=lambda dispatch_id: self.receipt)

    def write_artifact(self, identifier, value):
        path = self.root / (identifier + '.json')
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def save_proof(self):
        ref = self.write_artifact('proof', self.proof)
        self.receipt.update(receipt_ref=ref, evidence_refs=[*self.frame_refs, ref])

    def test_cached_path_previews_use_the_independent_hashed_code_entry(self):
        self.assertTrue(import_verification(self.store, 'task-1')['verified'])

    def test_code_entry_artifact_hash_is_required(self):
        path = self.root / 'entry.json'
        path.write_bytes(path.read_bytes() + b' ')
        self.assertFalse(import_verification(self.store, 'task-1')['verified'])

    def test_code_entry_screenshot_hash_is_required(self):
        image = self.root / 'entry.png'
        image.write_bytes(image.read_bytes() + b'changed')
        self.assertFalse(import_verification(self.store, 'task-1')['verified'])

    def test_code_entry_must_be_in_both_the_proof_and_receipt_evidence(self):
        entry_ref = self.frame_refs[1]
        self.receipt['evidence_refs'].remove(entry_ref)
        self.assertFalse(import_verification(self.store, 'task-1')['verified'])
        self.save_proof()
        self.proof['observation_refs'] = [ref for ref in self.frame_refs if ref != entry_ref]
        self.save_proof()
        self.assertFalse(import_verification(self.store, 'task-1')['verified'])

    def test_wrong_code_entry_cannot_bind_cached_previews(self):
        self.frames['entry']['codes'] = ['prts://654321']
        self.frame_refs[1] = self.write_artifact('entry', self.frames['entry'])
        self.save_proof()
        self.assertFalse(import_verification(self.store, 'task-1')['verified'])

    def test_direct_code_previews_still_verify_without_separate_entry_id(self):
        self.proof.pop('code_entry')
        for index, identifier in ((2, 'after-1'), (3, 'after-2')):
            self.frames[identifier]['codes'] = [self.expected['code']]
            self.frame_refs[index] = self.write_artifact(identifier, self.frames[identifier])
        self.save_proof()
        self.assertTrue(import_verification(self.store, 'task-1')['verified'])

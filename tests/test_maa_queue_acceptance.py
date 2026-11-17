import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from shu.maa_queue_acceptance import run_queue_acceptance, read_request, snapshot_source, _inspect_queue_evidence


class QueueAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'user-data'
        self.source.mkdir()
        (self.source/'settings.json').write_text(json.dumps({'model':'deepseek-flash',
            'api_key':'fake-token-must-not-copy'}), encoding='utf-8')
        self.key = self.root/'secret.txt'
        self.key.write_text('sk-acceptance-sentinel-never-report', encoding='utf-8')
        self.request = self.root/'request.json'
        self.request.write_text(json.dumps({'request':'查找这个活动的作业并导入多作业队列，不开始战斗'}), encoding='utf-8')
        self.output = self.root/'artifacts'/'acceptance'

    def run_acceptance(self, **kwargs):
        return run_queue_acceptance(self.root, self.root, self.output, self.key,
            self.source, request_file=self.request, version='test', **kwargs)

    def report(self):
        return json.loads((self.output/'report.json').read_text(encoding='utf-8'))

    def test_natural_requires_opt_in_before_loading_key_or_model(self):
        with patch('shu.providers.load_api_key') as loader, patch(
                'shu.maa_queue_acceptance._run_conversation') as conversation, patch(
                'shu.maa_queue_acceptance._run_probe') as probe:
            self.assertNotEqual(self.run_acceptance(mode='natural'), 0)
        loader.assert_not_called(); conversation.assert_not_called(); probe.assert_not_called()
        self.assertEqual(self.report()['code'], 'import_opt_in_required')

    def test_request_only_accepts_original_text(self):
        for value in ({'request':'真实请求', 'expected_codes':['prts://1']},
                      {'request':'真实请求','copilot_id':1}, ['真实请求'], {'request':''}):
            self.request.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_request(self.request)

    def test_default_probe_does_not_create_task_profile_or_use_key(self):
        with patch('shu.providers.load_api_key') as loader, patch(
                'shu.maa_queue_acceptance._run_conversation') as conversation, patch(
                'shu.maa_queue_acceptance._run_probe', return_value={
                    'passed':False,'code':'maa_window_unavailable','gui_inputs':0,
                    'model_requests':0}) as probe:
            self.assertNotEqual(self.run_acceptance(), 0)
        loader.assert_not_called(); conversation.assert_not_called()
        self.assertEqual(probe.call_count, 1)
        self.assertFalse((self.output/'source-snapshot').exists())
        self.assertEqual(self.report()['coverage']['natural_import'], 'pending')

    def test_probe_success_is_not_claimed_as_natural_queue_success(self):
        with patch('shu.maa_queue_acceptance._run_probe', return_value={
                'passed':True,'code':'queue_snapshot_captured','gui_inputs':0,'model_requests':0}):
            self.assertEqual(self.run_acceptance(), 0)
        self.assertTrue(self.report()['passed'])
        self.assertFalse(self.report()['queue_import_verified'])

    def test_source_snapshot_uses_read_only_backup_and_copies_no_key(self):
        database = self.source/'tasks.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE facts(value)'); db.execute("INSERT INTO facts VALUES('actual-box')")
            db.commit()
        original = database.read_bytes()
        target = self.root/'snapshot'
        snapshot_source(self.source, target)
        self.assertEqual(database.read_bytes(), original)
        with closing(sqlite3.connect(target/'tasks.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM facts').fetchone()[0], 'actual-box')
        self.assertNotIn('api_key', json.loads((target/'settings.json').read_text()))
        self.assertFalse(any('secret' in path.name.lower() for path in target.rglob('*')))

    def test_report_claim_alone_cannot_pass_without_durable_queue_evidence(self):
        def conversation(*args, **kwargs):
            destination = Path(args[2]); destination.mkdir()
            (destination/'report.json').write_text(json.dumps({'passed':True,
                'turns':[{'question':'原话','assessment':{'passed':True},
                    'task':{'task_id':'invented'},'import_verification':{'verified':True}}]}))
            return 0
        with patch('shu.maa_queue_acceptance._run_conversation', side_effect=conversation):
            self.assertNotEqual(self.run_acceptance(mode='natural', allow_queue_import=True), 0)
        self.assertFalse(self.report()['queue_import_verified'])
        self.assertEqual(self.report()['code'], 'queue_evidence_missing')

    def test_component_is_honestly_unavailable_and_does_not_load_key(self):
        with patch('shu.providers.load_api_key') as loader, patch(
                'shu.maa_queue_acceptance._run_conversation') as conversation:
            self.assertNotEqual(self.run_acceptance(mode='component', allow_queue_import=True), 0)
        loader.assert_not_called(); conversation.assert_not_called()
        self.assertEqual(self.report()['code'], 'component_harness_not_implemented')

    def test_successful_chat_and_task_without_queue_cannot_pass_durable_check(self):
        from shu.core import TaskEngine
        from shu.workflow_store import WorkflowStore
        data = self.root/'private-run'
        engine = TaskEngine(data)
        try:
            workflow = WorkflowStore(engine)
            workflow.management.accept_message('actual-request','用户原话')
            state = workflow.submit('用户原话',{'goal':'用户原话','allow_start':False,
                'attempt_limit':0,'current_deliverable':'maa_import','task_spec':{'kind':'import'}})
            workflow.management.finish_message('actual-request',task_id=state['task_id'])
        finally:
            engine.close()
        evidence = _inspect_queue_evidence(data,'用户原话')
        self.assertFalse(evidence['verified'])
        self.assertEqual(evidence['requests'][0]['request_id'],'actual-request')
        self.assertFalse(evidence['tasks'][0]['verified'])

    def test_actual_submitted_start_ledger_is_not_hidden_by_report(self):
        data = self.root/'private-run'
        data.mkdir()
        with closing(sqlite3.connect(data/'tasks.sqlite3')) as db:
            db.executescript('CREATE TABLE workflow_messages(request_id,task_id,relation,status,text);'
                'CREATE TABLE workflow_operations(operation_id,task_id,dispatch_id,epoch,status,starts,stop_only,action,submitted_at);')
            db.execute('INSERT INTO workflow_operations VALUES(?,?,?,?,?,?,?,?,?)',
                ('actual-start','task','dispatch',1,'verified',1,0,
                 json.dumps({'kind':'game_input'}),'submitted'))
            db.commit()
        evidence = _inspect_queue_evidence(data,'用户原话')
        self.assertFalse(evidence['verified'])
        self.assertTrue(evidence['battle_start_submitted'])
        self.assertEqual(evidence['forbidden_operations'][0]['operation_id'],'actual-start')

    def test_natural_forwards_original_request_and_retains_durable_failure(self):
        original = read_request(self.request)
        def conversation(*args, **kwargs):
            self.assertEqual(json.loads(Path(args[3]).read_text(encoding='utf-8')),[original])
            self.assertEqual(args[4],self.key)
            self.assertEqual(kwargs['timeout'],75)
            self.assertTrue(kwargs['allow_maa_import'])
            self.assertTrue(kwargs['allow_maa_queue_import'])
            destination = Path(args[2]); destination.mkdir()
            (destination/'report.json').write_text(json.dumps({'passed':True,
                'intent_metrics':[{'usage':{'total_tokens':9}}],
                'role_metrics':{'vision':[{'usage':{'total_tokens':11}}]}}))
            return 0
        durable = {'verified':False,'code':'queue_evidence_unverified','tasks':[],
            'requests':[{'request_id':'real-request'}],'operations':[{'operation_id':'real-op'}],
            'battle_start_submitted':False,'forbidden_operations':[]}
        with patch('shu.maa_queue_acceptance._run_conversation',side_effect=conversation), patch(
                'shu.maa_queue_acceptance._inspect_queue_evidence',return_value=durable):
            self.assertNotEqual(self.run_acceptance(mode='natural',allow_queue_import=True,timeout=75),0)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertEqual(report['operations'][0]['operation_id'],'real-op')
        self.assertEqual(report['usage']['tokens']['total_tokens'],20)
        self.assertIsNone(report['usage']['cost']['amount'])

    def test_existing_output_and_outside_artifacts_are_rejected(self):
        self.output.mkdir(parents=True)
        with self.assertRaises(FileExistsError): self.run_acceptance()
        with self.assertRaises(ValueError):
            run_queue_acceptance(self.root,self.root,self.root/'elsewhere',self.key,self.source,version='test')

    def test_report_scrubs_credential_fields_and_token_sentinel(self):
        with patch('shu.maa_queue_acceptance._run_probe', return_value={'passed':False,
                'code':'probe_failed','api_key':'fake-token-must-not-copy',
                'detail':'sk-acceptance-sentinel-never-report','gui_inputs':0,'model_requests':0}):
            self.run_acceptance()
        raw = (self.output/'report.json').read_text(encoding='utf-8')
        self.assertNotIn('fake-token-must-not-copy', raw)
        self.assertNotIn('sk-acceptance-sentinel-never-report', raw)

    def test_main_entry_dispatches_same_production_function(self):
        import main
        with patch('sys.argv',['ShuCompanion.exe','--maa-queue-test',str(self.output)]), patch(
                'shu.maa_queue_acceptance.run_queue_acceptance',return_value=73) as production:
            self.assertEqual(main.main(),73)
        self.assertEqual(production.call_args.kwargs['mode'],'probe')
        self.assertFalse(production.call_args.kwargs['allow_queue_import'])

    def test_cli_modes_are_mutually_exclusive(self):
        from tools.maa_queue_acceptance import main
        with self.assertRaises(SystemExit) as caught:
            main(['--output',str(self.output),'--natural','--probe-only'])
        self.assertEqual(caught.exception.code,2)


if __name__ == '__main__':
    unittest.main()

"""Check real SQLite checkpoints without model calls or desktop access."""
import json
from pathlib import Path
import tempfile
import unittest

from shu.runtime import RuntimeStore


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.store=RuntimeStore(self.directory)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_checkpoint_survives_reopen_with_contract_context_and_metrics(self):
        contract={'goal':'进入基建并收取产物','user_request':'去基建收产物，不换班',
                  'constraints':['不换班'],'completion_criteria':['新画面确认产物已收取']}
        context={'schema_version':1,'goal':contract['goal'],'model_state':{'plan':['进入基建','收取产物']}}
        self.assertTrue(self.store.save('task-one',3,phase='paused',contract=contract,
                                        context=context,metrics={'model_calls':2}))
        contract['constraints'].append('调用方后来修改')
        self.store.close();self.store=RuntimeStore(self.directory)
        restored=self.store.load('task-one')
        self.assertEqual(restored['version'],3);self.assertEqual(restored['phase'],'paused')
        self.assertEqual(restored['contract']['constraints'],['不换班'])
        self.assertEqual(restored['context'],context)
        self.assertEqual(restored['metrics']['model_calls'],2)
        restored['context']['model_state']['plan'].clear()
        self.assertEqual(len(self.store.load('task-one')['context']['model_state']['plan']),2)

    def test_older_version_cannot_overwrite_newer_saved_progress(self):
        self.store.save('task-one',5,phase='queued',contract={'goal':'新目标'})
        self.assertFalse(self.store.save('task-one',4,phase='running',contract={'goal':'旧目标'}))
        self.assertEqual(self.store.load('task-one')['contract']['goal'],'新目标')
        self.assertEqual(self.store.load('task-one')['phase'],'queued')

    def test_missing_task_has_no_checkpoint(self):
        self.assertIsNone(self.store.load('not-found'))

    def test_invalid_identity_is_rejected_without_inserting_rows(self):
        for task_id,version in (('',1),(None,1),('task',0),('task',True),('task',-2)):
            with self.subTest(task_id=task_id,version=version),self.assertRaises(ValueError):
                self.store.save(task_id,version,phase='running')
        self.assertIsNone(self.store.load(''))

    def test_nonfinite_or_oversized_checkpoint_does_not_replace_existing_data(self):
        self.store.save('task-one',1,phase='paused',context={'kept':True})
        for context in ({'invalid':float('nan')},{'oversized':'字'*120001}):
            with self.subTest(context_type=next(iter(context))),self.assertRaises(ValueError):
                self.store.save('task-one',2,phase='running',context=context)
        self.assertEqual(self.store.load('task-one')['context'],{'kept':True})

    def test_images_and_provider_reasoning_are_never_persisted(self):
        context={'schema_version':1,'goal':'观察',
                 'model_state':{'state_summary':'已进入主页','reasoning_content':'private fixture reasoning'},
                 'recent_steps':[{'image_base64':'fixture-pixel-payload',
                                  'image_data_url':'data:image/png;base64,fixture-pixels'}]}
        try:
            self.store.save('task-one',1,phase='paused',context=context)
        except ValueError:
            return  # Explicit refusal also satisfies the storage boundary.
        encoded=json.dumps(self.store.load('task-one'),ensure_ascii=False)
        for forbidden in ('private fixture reasoning','fixture-pixel-payload','data:image/png;base64'):
            self.assertNotIn(forbidden,encoded)

    def test_key_shaped_values_are_redacted_before_database_storage(self):
        self.store.save('task-one',1,phase='paused',context={'note':'sk-fixture-not-a-real-secret'})
        self.assertNotIn('sk-fixture-not-a-real-secret',json.dumps(self.store.load('task-one')))


if __name__=='__main__':unittest.main()

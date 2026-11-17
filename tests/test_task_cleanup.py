import tempfile, unittest
from pathlib import Path
from shu.memory import ConversationMemory
from shu.runtime import RuntimeStore

class TaskProjectionTests(unittest.TestCase):
    def test_memory_explicit_ownership_context_and_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            memory=ConversationMemory(Path(folder), task_visible=lambda task: task!='A')
            legacy=memory.append('user','legacy')
            user=memory.append('user','private'); reply=memory.append('assistant','private answer')
            memory.bind_request('r','A',[user['id'],reply['id']])
            self.assertEqual(memory.model_messages(),[])
            self.assertEqual(memory.delete_task('A'),2)
            self.assertEqual([x['id'] for x in memory.load_messages()],[legacy['id']])
            with self.assertRaises(ValueError): memory.bind_request('late','A',[legacy['id']])
            memory.close()
    def test_runtime_delete_fences_late_write_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=RuntimeStore(Path(folder)); runtime.save('A',1,phase='x');runtime.save('B',1,phase='x')
            runtime.record_call('leader',{'tokens':1},'A'); runtime.delete_task('A'); runtime.close()
            runtime=RuntimeStore(Path(folder))
            self.assertFalse(runtime.save('A',2,phase='late'))
            runtime.record_call('leader',{'tokens':2},'A')
            self.assertIsNone(runtime.load('A'));self.assertEqual(runtime.calls('A'),[])
            self.assertIsNotNone(runtime.load('B'));runtime.close()
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.task_cleanup import TaskCleanup

class TaskCleanupTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.path=Path(self.folder.name)
        self.engine=TaskEngine(self.path);self.store=WorkflowStore(self.engine)
        self.a=self.store.submit('private task',{})['task_id'];self.b=self.store.submit('keep task',{})['task_id']
    def tearDown(self):
        self.engine.close();self.folder.cleanup()

    def test_duplicate_references_resolve_each_lexical_path_once(self):
        from unittest.mock import patch
        import hashlib, json
        cleanup=TaskCleanup(self.store)
        owned=cleanup.task_root/self.a/'owned.json'
        owned.parent.mkdir(parents=True);owned.write_text('{}')
        ref={'path':str(owned),'sha256':hashlib.sha256(owned.read_bytes()).hexdigest()}
        # Same production-shaped references repeated across receipts and fields.
        self.engine._db.execute('CREATE TABLE shared_documents(payload TEXT)')
        self.engine._db.execute('INSERT INTO shared_documents VALUES(?)',(json.dumps([ref]*500),))
        resolved=[]; original=Path.resolve
        def count(path,*args,**kwargs):
            if path==owned:resolved.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',count):
            cleanup._file_manifest(self.a,cleanup._selection(self.a))
        self.assertEqual(len(resolved),1,'deduplicate before filesystem work, retaining owner/hash bindings')

    def test_cleanup_job_is_persisted_and_writer_unlocked_before_manifest_io(self):
        cleanup=TaskCleanup(self.store)
        self.store.management.trash(self.a)
        original=cleanup._file_manifest
        observations=[]
        def inspect(*args,**kwargs):
            row=self.engine._db.execute('SELECT * FROM workflow_cleanup_jobs WHERE task_id=?',(self.a,)).fetchone()
            observations.append((row is not None,self.engine._db.in_transaction))
            return original(*args,**kwargs)
        cleanup._file_manifest=inspect
        result=cleanup.purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(observations)
        self.assertTrue(all(saved and not locked for saved,locked in observations),observations)
    def test_purge_is_idempotent_and_preserves_shared_and_other_task(self):
        self.engine._db.execute('CREATE TABLE shared_box(value TEXT)');self.engine._db.execute("INSERT INTO shared_box VALUES('unchanged')")
        self.store.management.trash(self.a)
        cleanup=TaskCleanup(self.store)
        result=cleanup.purge(self.a);self.assertEqual(result['status'],'completed',result)
        self.assertEqual(cleanup.purge(self.a)['status'],'completed')
        self.assertEqual(self.store.current(self.b)['goal'],'keep task')
        self.assertEqual(self.engine._db.execute('SELECT * FROM shared_box').fetchone()[0],'unchanged')
        self.assertEqual(self.engine._db.execute('SELECT goal FROM tasks WHERE id=?',(self.a,)).fetchone()[0],'')
    def test_running_and_unknown_are_blocked(self):
        dispatch=self.store.ensure_dispatch(self.a,'maa','one',{})
        self.engine._db.execute("UPDATE workflow_dispatches SET status='running' WHERE dispatch_id=?",(dispatch['dispatch_id'],))
        self.store.management.trash(self.a)
        self.assertEqual(TaskCleanup(self.store).purge(self.a)['status'],'blocked')

    def test_unregistered_queue_artifact_blocks_purge_and_peer_is_preserved(self):
        cleanup = TaskCleanup(self.store)
        path = self.path/'task-private'/self.a/'maa-queue'/'1'/'incomplete.json'
        peer = self.path/'task-private'/self.b/'maa-queue'/'1'/'keep.json'
        for file in (path,peer):
            file.parent.mkdir(parents=True)
            file.write_text('{}',encoding='utf-8')
        cleanup.register_file(self.b,peer)
        self.store.management.trash(self.a)
        result = cleanup.purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('未登记所有权',result['reason'])
        self.assertTrue(path.is_file())
        self.assertTrue(peer.is_file())
    def test_unrecognized_foreign_reference_blocks(self):
        self.engine._db.execute('CREATE TABLE external_ref(task TEXT REFERENCES tasks(id))')
        self.engine._db.execute('INSERT INTO external_ref VALUES(?)',(self.a,))
        self.store.management.trash(self.a)
        self.assertEqual(TaskCleanup(self.store).purge(self.a)['status'],'blocked')
    def test_manifest_rejects_outside_path(self):
        cleanup=TaskCleanup(self.store);outside=self.path/'key.json';outside.write_text('keep')
        with self.assertRaises(ValueError):cleanup.register_file(self.a,outside)
        self.assertEqual(outside.read_text(),'keep')
    def test_partial_cleanup_restart(self):
        class FailingMemory:
            def delete_task(self, task):raise RuntimeError('simulated crash')
        self.store.management.trash(self.a)
        self.assertEqual(TaskCleanup(self.store,memory=FailingMemory()).purge(self.a)['status'],'error')
        self.assertEqual(self.store.management.lifecycle(self.a)['state'],'purge_pending')
        self.assertEqual(TaskCleanup(self.store).purge(self.a)['status'],'completed')

    def test_graph_saver_removes_checkpoints_and_writes_only_owned_thread(self):
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);engine=TaskEngine(path);store=WorkflowStore(engine)
            try:
                a=store.submit('remove',{})['task_id'];b=store.submit('keep',{})['task_id']
                conn=sqlite3.connect(path/'maa-graph.sqlite3');saver=SqliteSaver(conn);saver.setup()
                for task in (a,b):
                    conn.execute('INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata) VALUES(?,?,?,?,?,?,?)',(task,'','id',None,'json',b'{}',b'{}'))
                    conn.execute('INSERT INTO writes(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) VALUES(?,?,?,?,?,?,?,?)',(task,'','id','worker',0,'x','json',b'{}'))
                conn.commit();conn.close();store.management.trash(a)
                result=TaskCleanup(store,graph_directory=path).purge(a);self.assertEqual(result['status'],'completed',result)
                conn=sqlite3.connect(path/'maa-graph.sqlite3')
                try:
                    for table in ('checkpoints','writes'):
                        self.assertEqual(conn.execute('SELECT DISTINCT thread_id FROM '+table).fetchall(),[(b,)])
                finally:conn.close()
            finally:engine.close()

    def test_cross_task_adoption_blocks_source_purge(self):
        source=self.store.ensure_dispatch(self.a,'research','source',{})
        target=self.store.ensure_dispatch(self.b,'research','target',{})
        self.engine._db.execute('INSERT INTO workflow_research_adoptions VALUES(?,?,?,?,?)',(target['dispatch_id'],source['dispatch_id'],'hash','input','now'))
        self.store.management.trash(self.a)
        self.assertEqual(TaskCleanup(self.store).purge(self.a)['status'],'blocked')
    def test_unknown_gui_operation_blocks_without_removing_barrier(self):
        dispatch=self.store.ensure_dispatch(self.a,'maa','one',{})
        operation=self.store.prepare_operation(self.a,dispatch['dispatch_id'],'effect',{'kind':'import'})
        self.engine._db.execute("UPDATE workflow_operations SET status='unknown' WHERE operation_id=?",(operation['operation_id'],))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(self.engine._db.execute('SELECT status FROM workflow_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()[0],'unknown')
    def test_explicit_file_manifest_deletes_single_owned_file(self):
        cleanup=TaskCleanup(self.store);owned=cleanup.task_root/self.a/'result.json';owned.parent.mkdir(parents=True);owned.write_text('private')
        shared=owned.parent/'unowned.json';shared.write_text('shared')
        cleanup.register_file(self.a,owned);self.store.management.trash(self.a)
        self.assertEqual(cleanup.purge(self.a)['status'],'completed');self.assertFalse(owned.exists());self.assertTrue(shared.exists())
    def test_import_json_ownership_is_exact(self):
        a=self._production_preparation(self.a,self.a)
        b=self._production_preparation(self.b,self.b)
        self.store.management.trash(self.a);result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual([row[0] for row in self.engine._db.execute('SELECT cache_key FROM import_preparations')],[b])

    def test_all_local_projections_and_real_box_tables_are_scoped(self):
        from shu.activity_history import ActivityHistory
        from shu.maa_facts import RosterStore
        RosterStore(self.store)
        self.engine._db.execute('INSERT INTO workflow_roster_batches VALUES(?,?,?,?,?,?,?)',('box','profile',1,'hash','input',None,'{"operators":[]}'))
        self.engine._db.execute('INSERT INTO workflow_roster_current VALUES(?,?)',('profile','box'))
        before=[tuple(row) for row in self.engine._db.execute('SELECT * FROM workflow_roster_batches')]
        memory=ConversationMemory(self.path);runtime=RuntimeStore(self.path);history=ActivityHistory(self.path/'history.json')
        try:
            for task in (self.a,self.b):
                message=memory.append('user',task);memory.bind_request(task,task,[message['id']])
                runtime.save(task,1,phase='done')
                history.begin(task,'private');history.bind_request(task,task)
            unbound=memory.append('user','legacy')
            self.store.management.trash(self.a)
            result=TaskCleanup(self.store,memory=memory,runtime=runtime,history=history).purge(self.a)
            self.assertEqual(result['status'],'completed',result)
            self.assertEqual([tuple(row) for row in self.engine._db.execute('SELECT * FROM workflow_roster_batches')],before)
            self.assertEqual([x['id'] for x in memory.load_messages()],[unbound['id']-1,unbound['id']])
            self.assertIsNotNone(runtime.load(self.b));self.assertIsNone(runtime.load(self.a))
        finally:memory.close();runtime.close()

    def test_purge_pending_restore_and_late_projection_writes_are_fenced(self):
        class Broken:
            def delete_task(self,task):raise RuntimeError('interrupted')
        self.store.management.trash(self.a)
        cleanup=TaskCleanup(self.store,memory=Broken())
        self.assertEqual(cleanup.purge(self.a)['status'],'error')
        with self.assertRaises(Exception):self.store.management.restore(self.a)
        runtime=RuntimeStore(self.path,task_writable=lambda task:self.store.management.lifecycle(task)['state']=='active')
        memory=ConversationMemory(self.path,task_visible=lambda task:self.store.management.lifecycle(task)['state']=='active')
        try:
            self.assertFalse(runtime.save(self.a,100,phase='late'))
            with self.assertRaises(ValueError):memory.append('assistant','late',request_id='late',task_id=self.a)
        finally:runtime.close();memory.close()
    def test_manifest_symlink_is_rejected(self):
        import os
        cleanup=TaskCleanup(self.store);target=self.path/'shared';target.mkdir()
        root=cleanup.task_root/self.a;root.mkdir(parents=True)
        link=root/'jump'
        try:os.symlink(target,link,target_is_directory=True)
        except OSError as exc:self.skipTest('OS does not permit creating test symlink: '+str(exc))
        try:
            with self.assertRaises(ValueError):cleanup.register_file(self.a,link/'file.json')
        finally:link.unlink()

    def test_reparse_metadata_is_rejected_before_resolve(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        cleanup=TaskCleanup(self.store)
        owned=cleanup.task_root/self.a/'result.json'
        owned.parent.mkdir(parents=True);owned.write_text('private')
        original=Path.lstat
        def metadata(path):
            info=original(path)
            if path==owned.parent:
                return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
            return info
        with patch.object(Path,'lstat',metadata):
            with self.assertRaises(ValueError):cleanup.register_file(self.a,owned)

    def _production_preparation(self, owner, source):
        import json
        from shu.workflow_store import _encode
        self.engine._db.execute('CREATE TABLE IF NOT EXISTS import_preparations(cache_key TEXT PRIMARY KEY,payload TEXT,payload_hash TEXT)')
        material={'source_binding':{'source_task_id':source,'source_revision':1,'step_id':'s','slot':'primary'},
                  'document_ref':{'path':str(self.path/'research'/owner/'d'/'copilot.json'),'sha256':'0'*64}}
        key=_encode({'task_id':owner,'revision':1,'source_task_id':source,'step_id':'s','slot':'primary'})[1]
        encoded,digest=_encode(material)
        self.engine._db.execute('INSERT INTO import_preparations(cache_key,payload,payload_hash) VALUES(?,?,?)',(key,encoded,digest))
        return key

    def test_real_preparation_shape_is_migrated_and_removed(self):
        key=self._production_preparation(self.a,self.a)
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        self.assertIsNone(self.engine._db.execute('SELECT 1 FROM import_preparations WHERE cache_key=?',(key,)).fetchone())

    def test_real_preparation_cross_task_source_blocks(self):
        self._production_preparation(self.b,self.a)
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(self.store.current(self.a)['goal'],'private task')

    def test_followup_contract_source_blocks_before_preparation_exists(self):
        self.store.submit('followup',{'source_task_id':self.a,'current_deliverable':'maa_import'})
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('来源',result['reason'])

    def test_existing_private_research_file_blocks_unregistered_cleanup(self):
        import json
        dispatch=self.store.ensure_dispatch(self.a,'research','one',{})
        private=self.path/'research'/self.a/dispatch['dispatch_id']/'copilot.json'
        private.parent.mkdir(parents=True);private.write_text('private')
        self.engine._db.execute('INSERT INTO workflow_research_artifacts VALUES(?,?,?)',
            (dispatch['dispatch_id'],json.dumps([{'path':str(private),'sha256':'hash'}]),'hash'))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertTrue(private.exists())
        self.assertEqual(self.store.management.lifecycle(self.a)['state'],'purge_pending')

    def _ref(self,path,value):
        import hashlib,json
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}

    def _register_research(self,task,refs):
        from shu.workflow_store import _encode
        dispatch=self.store.ensure_dispatch(task,'research','files',{})
        encoded,digest=_encode(refs)
        self.engine._db.execute('INSERT INTO workflow_research_artifacts VALUES(?,?,?)',
            (dispatch['dispatch_id'],encoded,digest))
        return dispatch

    def test_production_research_and_nested_visual_files_are_cleaned(self):
        import hashlib
        document=self._ref(self.path/'research'/self.a/'dispatch'/'copilots'/'42.json',{'stage_name':'unit','actions':[]})
        capture=self.path/'maa-visual-captures'/'frame.png'
        capture.parent.mkdir();capture.write_bytes(b'private screenshot')
        frame={'screenshot_id':'frame','image_path':str(capture),'image_sha256':hashlib.sha256(capture.read_bytes()).hexdigest()}
        evidence=self._ref(self.path/'evidence'/'maa-visual-operation-proof.json',
            {'task_id':self.a,'download':document,'frame':frame})
        public=self._ref(self.path/'public-cache'/'research'/'response.json',{'public':True})
        external=self._ref(self.path/'MAA'/'cache'/'user.json',{'keep':'MAA-owned'})
        other=self._ref(self.path/'research'/self.b/'dispatch'/'other.json',{'keep':'other task'})
        self._register_research(self.a,[evidence,public,external]);self._register_research(self.b,[other])
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        for path in (document['path'],evidence['path'],str(capture)):self.assertFalse(Path(path).exists(),path)
        for ref in (public,external,other):self.assertTrue(Path(ref['path']).exists())

    def test_shared_research_reference_blocks_instead_of_deleting(self):
        owned=self._ref(self.path/'research'/self.a/'d'/'private.json',{'private':'a'})
        self._register_research(self.a,[owned]);self._register_research(self.b,[owned])
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('其他任务',result['reason']);self.assertTrue(Path(owned['path']).exists())
        self.assertEqual(self.store.management.lifecycle(self.a)['state'],'purge_pending')

    def test_shared_nested_reference_blocks(self):
        owned=self._ref(self.path/'evidence'/'private.json',{'task_id':self.a})
        nested=self._ref(self.path/'evidence'/'other.json',{'task_id':self.b,'source':owned})
        self._register_research(self.a,[owned]);self._register_research(self.b,[nested])
        self.store.management.trash(self.a)
        self.assertEqual(TaskCleanup(self.store).purge(self.a)['status'],'blocked')
        self.assertTrue(Path(owned['path']).exists());self.assertTrue(Path(nested['path']).exists())

    def test_changed_registered_file_blocks_before_projection_deletion(self):
        cleanup=TaskCleanup(self.store);owned=cleanup.task_root/self.a/'result.json'
        owned.parent.mkdir(parents=True);owned.write_text('original')
        cleanup.register_file(self.a,owned);owned.write_text('changed')
        self.store.management.trash(self.a)
        result=cleanup.purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(owned.read_text(),'changed')
        self.assertEqual(self.store.management.lifecycle(self.a)['state'],'purge_pending')

    def test_unprovable_legacy_import_row_blocks_without_guessing(self):
        key=self._production_preparation(self.a,self.a)
        self.engine._db.execute('UPDATE import_preparations SET cache_key=? WHERE cache_key=?',('unknown',key))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('所有权',result['reason'])
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM import_preparations').fetchone()[0],1)

    def test_real_prepare_writer_persists_owner_and_nested_source(self):
        # Reuse the offline fixture that exercises the actual production writer,
        # real plans/code receipts/Box and a fake HTTP transport (never a model).
        from tests.test_import_preparation import ImportPreparationTests
        fixture=ImportPreparationTests('test_download_binds_complete_candidate_and_current_goal_evidence')
        try:
            fixture.setUp();prepared=fixture.prepare()
            row=fixture.engine._db.execute('SELECT * FROM import_preparations').fetchone()
            self.assertEqual((row['owner_task_id'],row['source_task_id'],row['owner_revision']),
                (fixture.context['task_id'],fixture.source,1))
            self.assertIsNone(row['ownership_error'])
            fixture.workflow.management.trash(fixture.source)
            result=TaskCleanup(fixture.workflow).purge(fixture.source)
            self.assertEqual(result['status'],'blocked',result)
            self.assertTrue(Path(prepared['candidate_record']['path']).exists())
        finally:fixture.doCleanups()

    def test_real_prepared_task_purges_download_and_candidate_evidence(self):
        from tests.test_import_preparation import ImportPreparationTests
        fixture=ImportPreparationTests('test_download_binds_complete_candidate_and_current_goal_evidence')
        try:
            fixture.setUp();prepared=fixture.prepare()
            fixture.workflow.finish_dispatch(fixture.context['dispatch_id'],
                {'status':'researched','candidate_refs':[prepared['candidate_ref']]})
            owner=fixture.context['task_id'];fixture.workflow.management.trash(owner)
            result=TaskCleanup(fixture.workflow).purge(owner)
            self.assertEqual(result['status'],'completed',result)
            for ref in (prepared['candidate_ref'],prepared['candidate_record'],prepared['goal_stage']['evidence_ref']):
                self.assertFalse(Path(ref['path']).exists())
            self.assertEqual(fixture.engine._db.execute('SELECT COUNT(*) FROM import_preparations').fetchone()[0],0)
            self.assertEqual(fixture.workflow.current(fixture.source)['goal'],'查询活动作业')
        finally:fixture.doCleanups()

    def test_gui_operation_result_hash_is_not_confused_with_action_hash(self):
        import json
        dispatch=self.store.ensure_dispatch(self.a,'maa','visual',{})
        operation=self.store.prepare_operation(self.a,dispatch['dispatch_id'],'effect',{'kind':'import'})
        proof=self._ref(self.path/'evidence'/'maa-visual-operation.json',{'operation_id':operation['operation_id']})
        self.engine._db.execute("UPDATE workflow_operations SET status='verified',result=? WHERE operation_id=?",
            (json.dumps({'evidence_ref':proof}),operation['operation_id']))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result);self.assertFalse(Path(proof['path']).exists())

    def test_json_in_user_prose_does_not_grant_file_ownership(self):
        import json
        proof=self._ref(self.path/'evidence'/'unowned.json',{'keep':'unowned'})
        self.store.management.accept_message('prose',json.dumps(proof),target_task_id=self.a)
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result);self.assertTrue(Path(proof['path']).exists())

    def test_graph_deletion_uses_coordinator_run_lock(self):
        import threading
        from types import SimpleNamespace
        held=[];deleted=[]
        class Guard:
            def __enter__(inner):held.append(True)
            def __exit__(inner,*args):held.pop()
        def delete(thread):
            self.assertTrue(held,'graph cleanup must serialize with all graph writers')
            deleted.append(thread)
        graph=SimpleNamespace(saver=SimpleNamespace(delete_thread=delete),_lock=threading.Lock())
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store,graph_directory=graph,graph_lock=Guard()).purge(self.a)
        self.assertEqual(result['status'],'completed',result);self.assertEqual(deleted,[self.a])

    def test_minimal_request_receipt_tombstone_is_retained(self):
        self.engine._db.execute('CREATE TABLE IF NOT EXISTS workflow_request_receipts('
            'request_id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),operation TEXT,payload_hash TEXT,'
            'revision INTEGER,epoch INTEGER,created_at TEXT)')
        self.engine._db.execute('INSERT INTO workflow_request_receipts '
            '(request_id,task_id,operation,payload_hash,revision,epoch,created_at) VALUES(?,?,?,?,?,?,?)',
            ('r',self.a,'submit','sha256',1,1,'now'))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.engine._db.execute('SELECT task_id FROM workflow_request_receipts WHERE request_id=?',('r',)).fetchone()[0],self.a)

    def test_resume_after_parent_evidence_deleted_keeps_pinned_child_proof(self):
        child=self._ref(self.path/'research'/self.a/'dispatch'/'child.json',{'private':True})
        parent=self._ref(self.path/'evidence'/'parent.json',{'task_id':self.a,'child':child})
        self._register_research(self.a,[parent])
        self.store.management.trash(self.a)
        class Interrupted(TaskCleanup):
            def _files(inner,task,files):
                Path(parent['path']).unlink()
                raise RuntimeError('crashed after parent unlink')
        self.assertEqual(Interrupted(self.store).purge(self.a)['status'],'error')
        self.assertTrue(Path(child['path']).exists())
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'completed',result)
        self.assertFalse(Path(child['path']).exists())

    def test_unindexed_download_left_by_interrupted_writer_blocks(self):
        orphan=self.path/'research'/self.a/'dispatch'/'copilots'/'orphan.json'
        orphan.parent.mkdir(parents=True);orphan.write_text('{"private":"uncommitted"}')
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('未登记所有权',result['reason']);self.assertTrue(orphan.exists())
        self.assertEqual(self.store.management.lifecycle(self.a)['state'],'purge_pending')

    def test_legacy_observation_capture_without_hash_blocks(self):
        capture=self.path/'captures'/'legacy-observation.png'
        capture.parent.mkdir();capture.write_bytes(b'private legacy pixels')
        self.engine._db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',
            (self.a,'legacy-observation',1,0.0,'window','unknown','now'))
        self.store.management.trash(self.a)
        result=TaskCleanup(self.store).purge(self.a)
        self.assertEqual(result['status'],'blocked',result)
        self.assertTrue(capture.exists());self.assertIn('截图',result['reason'])

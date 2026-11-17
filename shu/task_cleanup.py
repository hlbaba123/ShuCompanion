"""Resumable, task-owned cleanup. No recursive deletes and no shared-data sweep."""
from __future__ import annotations
from contextlib import nullcontext
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import stat
import threading
import uuid
from .core import _utc_now

# Closed list: discovering another task-bearing table never grants deletion rights.
_PRIVATE = frozenset('task_events observations battle_results workflow_contracts workflow_tasks workflow_commands workflow_outbox workflow_inbox workflow_dispatches workflow_results workflow_research_artifacts workflow_research_adoptions workflow_operations workflow_messages workflow_questions leader_plans leader_steps leader_leases leader_budget leader_step_attempts code_receipts import_preparations workflow_queue_runs workflow_queue_items'.split())
_TOMBSTONES = frozenset(('workflow_lifecycle','workflow_cleanup_jobs','workflow_cleanup_threads',
                         'workflow_cleanup_files','workflow_request_receipts'))
# User prose that happens to contain JSON is never a file ownership receipt.
_REFERENCE_FIELDS = {
    'task_events': {'details': None}, 'workflow_contracts': {'contract': 'payload_hash'},
    'workflow_commands': {'payload': 'payload_hash'}, 'workflow_outbox': {'payload': 'payload_hash'},
    'workflow_dispatches': {'payload': 'payload_hash'}, 'workflow_results': {'result': 'payload_hash'},
    'workflow_research_artifacts': {'references_json': 'payload_hash'},
    'workflow_operations': {'action': 'payload_hash', 'result': None},
    'leader_plans': {'payload': 'payload_hash'}, 'leader_steps': {'result': None},
    'leader_step_attempts': {'result': None}, 'leader_budget': {'received_ref': None},
    'code_receipts': {'payload': 'hash'}, 'import_preparations': {'payload': 'payload_hash'},
    'workflow_queue_runs': {'payload': 'payload_hash'}, 'workflow_queue_items': {'payload': 'payload_hash'},
}
_GRAPH_LOCKS = {}
_GRAPH_LOCKS_GUARD = threading.Lock()

class CleanupBlocked(ValueError):
    pass

class CleanupPaused(Exception):
    pass

class TaskCleanup:
    def __init__(self, store, *, runtime=None, memory=None, history=None, graph_directory=None, graph_lock=None,
                 progress=None, stop_event=None):
        self.store=store;self.engine=store.engine;self.db=store._db
        self.runtime=runtime;self.memory=memory;self.history=history
        self.graph=graph_directory if hasattr(graph_directory,'saver') else None
        self.graph_lock=graph_lock
        self.progress=progress;self.stop_event=stop_event
        self.graph_directory=Path(graph_directory) if graph_directory is not None and self.graph is None else None
        database=self.db.execute('PRAGMA database_list').fetchone()[2]
        self.profile_directory=Path(database).parent
        self.task_root=self.profile_directory/'task-private'
        self.ensure_schema(self.engine)

    @staticmethod
    def ensure_schema(engine):
        """Prepare progress readers as well as workers for persisted old tables."""
        db = engine._db
        with engine._transaction():
            db.execute('CREATE TABLE IF NOT EXISTS workflow_cleanup_jobs(task_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, completed TEXT NOT NULL, manifest TEXT NOT NULL, error TEXT NOT NULL DEFAULT "")')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_cleanup_files(task_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(task_id,path))')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_cleanup_threads(task_id TEXT NOT NULL,thread_id TEXT NOT NULL UNIQUE,PRIMARY KEY(task_id,thread_id))')
            columns={r['name'] for r in db.execute('PRAGMA table_info(workflow_cleanup_files)')}
            if 'sha256' not in columns:
                db.execute('ALTER TABLE workflow_cleanup_files ADD COLUMN sha256 TEXT')
            columns={r['name'] for r in db.execute('PRAGMA table_info(workflow_cleanup_jobs)')}
            for name in ('stage','updated_at'):
                if name not in columns:
                    db.execute('ALTER TABLE workflow_cleanup_jobs ADD COLUMN '+name+' TEXT NOT NULL DEFAULT ""')

    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise CleanupPaused('清理已暂停，重启后可继续')

    def request(self,task_id):
        """Persist revocation before any filesystem inventory or projection IO."""
        with self.engine._transaction():
            life=self.store.management._lifecycle(task_id)
            if life['state']=='purged':return {'task_id':task_id,'status':'completed'}
            if life['state'] not in ('trashed','purge_pending'):
                raise CleanupBlocked('仅回收箱任务可彻底删除')
            job=self.db.execute('SELECT * FROM workflow_cleanup_jobs WHERE task_id=?',(task_id,)).fetchone()
            if job is None:
                operation=uuid.uuid4().hex
                self.db.execute('INSERT INTO workflow_cleanup_jobs(task_id,operation_id,completed,manifest,stage,updated_at) VALUES(?,?,?,?,?,?)',
                    (task_id,operation,'[]','{}','requested',_utc_now()))
                self.db.execute("UPDATE workflow_lifecycle SET state='purge_pending',updated_at=?,operation_id=? WHERE task_id=?",(_utc_now(),operation,task_id))
                self.store._publish_event(task_id,'task.cleanup_requested',{'operation_id':operation},event_key='cleanup:'+operation)
            return {'task_id':task_id,'status':'queued'}

    def _current(self,task_id,operation_id):
        life=self.store.management._lifecycle(task_id)
        if life['state']!='purge_pending' or life['operation_id']!=operation_id:
            raise CleanupBlocked('清理生命周期版本已改变，旧结果已作废')

    def _report(self,task_id,stage,*,status='running',reason='',completed=None):
        with self.engine._transaction():
            values=[stage,reason,_utc_now()]
            sql='UPDATE workflow_cleanup_jobs SET stage=?,error=?,updated_at=?'
            if completed is not None:
                sql+=',completed=?';values.append(json.dumps(sorted(completed)))
            self.db.execute(sql+' WHERE task_id=?',(*values,task_id))
        if self.progress is not None:
            self.progress({'task_id':task_id,'stage':stage,'status':status,'reason':reason})

    def _version(self):
        return self.db.total_changes,self.db.execute('PRAGMA data_version').fetchone()[0]

    def _scan_manifest(self,task_id,operation_id,*,pinned=()):
        # Read detached rows and perform slow IO without the shared writer lock.
        # An unchanged version is required again under BEGIN IMMEDIATE before use.
        with self.engine._transaction():
            self._current(task_id,operation_id)
            selected=self._guard(task_id)
            version=self._version()
        files=self._file_manifest(task_id,selected,pinned=pinned)
        return files,version

    def register_thread(self,task_id,thread_id):
        with self.engine._transaction():
            self.store.management.require_active(task_id)
            self.db.execute('INSERT OR IGNORE INTO workflow_cleanup_threads VALUES(?,?)',(task_id,thread_id))

    def _safe_path(self, task_id, path):
        if not task_id or Path(task_id).name!=task_id or task_id in ('.','..'):
            raise ValueError('Invalid task directory identity')
        root=self.task_root/task_id
        candidate=Path(path).absolute()
        self._no_links(candidate)
        real=candidate.resolve();real_root=root.resolve()
        if real==real_root or not real.is_relative_to(real_root):
            raise ValueError('Manifest path is outside the managed private task directory')
        if candidate.exists() and not candidate.is_file():
            raise ValueError('Only individually owned files can be cleaned')
        return real

    @staticmethod
    def _no_links(candidate):
        # Check every lexical ancestor before resolve: resolve alone hides symlinks.
        for part in (candidate,*candidate.parents):
            if part.exists() or part.is_symlink():
                info=part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    raise ValueError('Task manifest contains a symlink/reparse point')

    def register_file(self,task_id,path):
        path=self._safe_path(task_id,path)
        with self.engine._transaction():
            self.store.management.require_active(task_id)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO workflow_cleanup_files(task_id,path,sha256) VALUES(?,?,?)',
                            (task_id,str(path),digest))

    def _tables(self):
        return {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _selection(self,task_id):
        tables=self._tables(); selected={}
        for table in tables & _PRIVATE:
            columns={r['name'] for r in self.db.execute('PRAGMA table_info("'+table+'")')}
            if 'task_id' in columns:
                selected[table]={r[0] for r in self.db.execute('SELECT rowid FROM "'+table+'" WHERE task_id=?',(task_id,))}
        # Explicit child ownership through existing FK links, never through text.
        for table,column,parent,key in (
            ('workflow_results','dispatch_id','workflow_dispatches','dispatch_id'),
            ('workflow_research_artifacts','dispatch_id','workflow_dispatches','dispatch_id'),
            ('workflow_research_adoptions','dispatch_id','workflow_dispatches','dispatch_id'),
            ('workflow_inbox','event_id','workflow_outbox','event_id')):
            if table in tables:
                selected.setdefault(table,set()).update(r[0] for r in self.db.execute('SELECT rowid FROM '+table+' WHERE '+column+' IN (SELECT '+key+' FROM '+parent+' WHERE task_id=?)',(task_id,)))
        if 'import_preparations' in tables:
            selected['import_preparations']={r[0] for r in self.db.execute(
                'SELECT rowid FROM import_preparations WHERE owner_task_id=? AND ownership_error IS NULL',(task_id,))}
        if 'workflow_messages' in tables:
            selected.setdefault('workflow_messages',set()).update(r[0] for r in self.db.execute('SELECT rowid FROM workflow_messages WHERE target_task_id=? AND task_id IS NULL',(task_id,)))
        return selected

    def _guard(self,task_id):
        if self.db.execute("SELECT 1 FROM workflow_dispatches WHERE task_id=? AND status='running'",(task_id,)).fetchone():
            raise CleanupBlocked('等待该任务执行者退出')
        if self.db.execute("SELECT 1 FROM workflow_dispatches WHERE task_id=? AND role='maa' AND status='unknown'",(task_id,)).fetchone():
            raise CleanupBlocked('等待未知 GUI 执行结果核对')
        if self.db.execute("SELECT 1 FROM workflow_operations WHERE task_id=? AND status NOT IN ('verified','not_issued','failed')",(task_id,)).fetchone():
            raise CleanupBlocked('等待 GUI 操作核对')
        if self.db.execute('SELECT 1 FROM workflow_slots s JOIN workflow_operations o ON o.operation_id=s.barrier_operation_id WHERE o.task_id=?',(task_id,)).fetchone():
            raise CleanupBlocked('该任务仍持有 GUI 屏障')
        tables=self._tables()
        if 'import_preparations' in tables:
            from .import_preparation import ensure_preparation_ownership
            ensure_preparation_ownership(self.engine)
        selected=self._selection(task_id)
        for table in tables-_PRIVATE-_TOMBSTONES-{'tasks'}:
            columns={r['name'] for r in self.db.execute('PRAGMA table_info("'+table+'")')}
            if 'task_id' in columns and self.db.execute('SELECT 1 FROM "'+table+'" WHERE task_id=?',(task_id,)).fetchone():
                raise CleanupBlocked('未识别的任务数据表：'+table)
        # FK references from outside the exact deletion set must survive intact.
        for table in tables:
            if table.startswith('sqlite_'):continue
            groups={}
            for fk in self.db.execute('PRAGMA foreign_key_list("'+table+'")'):
                groups.setdefault(fk['id'],[]).append(fk)
            for fields in groups.values():
                parent=fields[0]['table']
                if parent not in selected and parent!='tasks':continue
                fields.sort(key=lambda f:f['seq'])
                on=' AND '.join('c."'+f['from']+'"=p."'+f['to']+'"' for f in fields)
                query='SELECT c.rowid,p.rowid FROM "'+table+'" c JOIN "'+parent+'" p ON '+on
                for row in self.db.execute(query):
                    target=(parent=='tasks' and self.db.execute('SELECT id FROM tasks WHERE rowid=?',(row[1],)).fetchone()[0]==task_id) or row[1] in selected.get(parent,set())
                    if not target:continue
                    if table in _TOMBSTONES:continue
                    if row[0] not in selected.get(table,set()):raise CleanupBlocked('仍有其他任务或未识别表引用：'+table)
        if 'workflow_messages' in tables and self.db.execute('SELECT 1 FROM workflow_messages WHERE target_task_id=? AND task_id IS NOT NULL AND task_id<>?',(task_id,task_id)).fetchone():
            raise CleanupBlocked('其他任务消息引用该任务')
        for row in self.db.execute('SELECT contract FROM workflow_contracts WHERE task_id<>?',(task_id,)):
            if json.loads(row['contract']).get('source_task_id')==task_id:
                raise CleanupBlocked('其他任务的合同仍关联该来源任务')
        # Source IDs in import cache are explicit JSON bindings (not prose matching).
        if 'import_preparations' in tables:
            for row in self.db.execute('SELECT rowid,* FROM import_preparations'):
                if row['ownership_error']:
                    raise CleanupBlocked(row['ownership_error'])
                if row['source_task_id']==task_id and row[0] not in selected.get('import_preparations',set()):
                    raise CleanupBlocked('其他任务仍引用导入资料')
        return selected

    def _file_kind(self,task_id,path):
        """Closed app-owned roots; public/Box/MAA/configuration paths never qualify."""
        path=Path(path).absolute()
        if '..' in path.parts:return None
        root=self.profile_directory.absolute()
        try:parts=path.relative_to(root).parts
        except ValueError:return None
        if len(parts)>2 and parts[0] in ('research','task-private') and parts[1]==task_id:
            return parts[0]
        if len(parts)==2 and (parts[0]=='evidence' and path.suffix=='.json'
                            or parts[0] in ('maa-visual-captures','captures') and path.suffix=='.png'):
            return parts[0]
        return None

    @staticmethod
    def _refs(value):
        """Only structured references, including the native visual-frame shape."""
        if isinstance(value,dict):
            for key,digest in (('path','sha256'),('image_path','image_sha256')):
                if isinstance(value.get(key),str):
                    yield value[key],value.get(digest)
            for child in value.values():yield from TaskCleanup._refs(child)
        elif isinstance(value,list):
            for child in value:yield from TaskCleanup._refs(child)

    def _file_manifest(self,task_id,selected,*,pinned=()):
        """Derive exclusive file ownership from durable rows and verified bytes.

        Reference closures from other tasks and shared tables are included in the
        same inventory. A private file with any surviving reference blocks purge;
        it is never deleted merely because its pathname contains the task id.
        """
        # A separate read snapshot prevents JSON traversal and filesystem checks
        # from monopolizing the GUI's engine connection or writer transaction.
        reader=sqlite3.connect(self.engine.database_path.absolute().as_uri()+'?mode=ro',uri=True)
        reader.row_factory=sqlite3.Row
        try:
            reader.execute('BEGIN')
            return self._file_manifest_snapshot(task_id,pinned,reader)
        finally:
            reader.close()

    def _file_manifest_snapshot(self,task_id,pinned,reader):
        dispatches={r['dispatch_id']:r['task_id'] for r in reader.execute('SELECT dispatch_id,task_id FROM workflow_dispatches')}
        pending=[]
        for table in {r[0] for r in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            self._check_stop()
            if table.startswith('sqlite_') or table.startswith('workflow_cleanup_'):continue
            # Some external tables may be WITHOUT ROWID. They are shared roots.
            columns={r['name'] for r in reader.execute('PRAGMA table_info("'+table+'")')}
            for row in reader.execute('SELECT * FROM "'+table+'"'):
                self._check_stop()
                owner=None
                if table in _PRIVATE:
                    if table=='import_preparations':owner=row['owner_task_id']
                    elif 'task_id' in columns:owner=row['task_id']
                    elif 'dispatch_id' in columns:owner=dispatches.get(row['dispatch_id'])
                fields=_REFERENCE_FIELDS.get(table,{}) if table in _PRIVATE else dict.fromkeys(columns)
                for column,hash_column in fields.items():
                    if column not in columns:continue
                    value=row[column]
                    if not isinstance(value,str) or not value.lstrip().startswith(('{','[')):continue
                    try:document=json.loads(value)
                    except ValueError:continue
                    refs=list(self._refs(document))
                    if refs and owner==task_id:
                        expected=row[hash_column] if hash_column else None
                        if expected is not None:
                            from .workflow_store import _encode
                            if _encode(document)[1]!=expected:
                                raise CleanupBlocked('任务文件的数据库凭据哈希不一致：'+table)
                    pending.extend((path,digest,owner) for path,digest in refs)
        for row in reader.execute('SELECT path,sha256 FROM workflow_cleanup_files WHERE task_id=?',(task_id,)):
            pending.append((row['path'],row['sha256'],task_id))
        # A crash may have removed a parent JSON before its child files. The
        # persisted, previously verified closure retains the child's proof.
        for ref in pinned:
            if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:
                raise CleanupBlocked('旧文件清单缺少哈希所有权凭据，请核对后重试')
            if self._file_kind(task_id,ref['path']) is None:
                raise CleanupBlocked('文件清单越出任务私有范围')
            pending.append((ref['path'],ref['sha256'],task_id))
        observations=[r[0] for r in reader.execute('SELECT observation_id FROM observations WHERE task_id=?',(task_id,))]
        # Release the read transaction before touching the filesystem as well.
        reader.commit()
        owners={};hashes={};seen=set();errors={};normalized={};documents={}
        profile=self.profile_directory.absolute()
        while pending:
            self._check_stop()
            raw_path,digest,owner=pending.pop()
            candidate=Path(raw_path).absolute()
            if '..' in candidate.parts:
                if candidate.is_relative_to(self.profile_directory.absolute()):
                    raise CleanupBlocked('任务或共享文件引用包含无法证明的父目录跳转')
                continue
            lexical=os.path.normcase(str(candidate))
            if lexical not in normalized:
                if candidate.is_relative_to(profile):
                    try:self._no_links(candidate)
                    except (ValueError,OSError) as exc:
                        raise CleanupBlocked('任务或共享文件引用无法安全验证：'+str(exc)) from exc
                    # Resolve aliases only after lexical dedup and no-link checks.
                    candidate=candidate.resolve()
                normalized[lexical]=Path(os.path.normcase(str(candidate)))
            path=normalized[lexical]
            key=str(path)
            owners.setdefault(key,set()).add(owner)
            hashes.setdefault(key,set()).add(digest if isinstance(digest,str) else None)
            identity=(key,digest if isinstance(digest,str) else None,owner)
            if identity in seen:continue
            seen.add(identity)
            # Do not open or traverse external/MAA files, even for reconciliation.
            if not path.is_relative_to(self.profile_directory.absolute()):continue
            try:
                if not isinstance(digest,str) or len(digest)!=64:
                    raise ValueError('文件缺少可信 SHA-256')
                if not path.exists():continue  # A previous idempotent step may already have removed it.
                queue_root = self.task_root/task_id/'maa-queue'
                size_limit = 192 if owner == task_id and path.is_relative_to(queue_root) else 64
                if not path.is_file() or path.stat().st_size>size_limit*1024*1024:
                    raise ValueError('文件类型或大小无法核实')
                content_key=(key,digest)
                if content_key not in documents:
                    raw=path.read_bytes()
                    self._check_stop()
                    if hashlib.sha256(raw).hexdigest()!=digest:
                        raise ValueError('文件内容与已登记哈希不一致')
                    refs=[]
                    if path.suffix=='.json':
                        try:refs=list(self._refs(json.loads(raw.decode('utf-8-sig'))))
                        except (ValueError,UnicodeError):pass
                    documents[content_key]=refs
                pending.extend((child,sha,owner) for child,sha in documents[content_key])
            except (ValueError,OSError) as exc:
                errors[key]=str(exc)
        manifest=[]
        for path,bindings in owners.items():
            if task_id not in bindings or self._file_kind(task_id,path) is None:continue
            if bindings!={task_id}:raise CleanupBlocked('任务文件仍被其他任务或共享资料引用：'+Path(path).name)
            if path in errors:raise CleanupBlocked('无法安全清理任务文件：'+Path(path).name+'；'+errors[path])
            if len(hashes[path])!=1 or None in hashes[path]:
                raise CleanupBlocked('任务文件哈希或所有权凭据冲突：'+Path(path).name)
            manifest.append({'path':path,'sha256':next(iter(hashes[path]))})
        proven={item['path'] for item in manifest}
        # Legacy native captures were indexed by observation id, without bytes
        # hashes. Recognize that exact producer path, but do not invent a hash
        # proof from its current content or silently leave it after completion.
        for identifier in observations:
            self._check_stop()
            if not identifier or Path(identifier).name!=identifier:
                raise CleanupBlocked('旧截图身份无法核实')
            capture=self.profile_directory/'captures'/(identifier+'.png')
            if capture.exists() or capture.is_symlink():
                try:self._no_links(capture)
                except (ValueError,OSError) as exc:raise CleanupBlocked('旧截图路径无法安全核实：'+str(exc)) from exc
                if str(Path(os.path.normcase(str(capture.resolve())))) not in proven:
                    raise CleanupBlocked('旧截图缺少已登记哈希所有权凭据，暂保留并等待核对：'+capture.name)
        # Research writers also create files before their SQLite receipt commits.
        # Directory identity alone cannot authorize deletion of those orphans,
        # but silently reporting a complete purge would leave task data behind.
        for owned_root in (self.profile_directory/'research'/task_id, self.task_root/task_id/'maa-queue'):
            if not owned_root.exists() and not owned_root.is_symlink():
                continue
            directories=[owned_root]
            while directories:
                self._check_stop()
                directory=directories.pop()
                try:self._no_links(directory)
                except (ValueError,OSError) as exc:raise CleanupBlocked('任务产物目录无法安全核对：'+str(exc)) from exc
                for child in directory.iterdir():
                    try:self._no_links(child)
                    except (ValueError,OSError) as exc:raise CleanupBlocked('任务产物目录包含无法核实的链接：'+child.name) from exc
                    if child.is_dir():directories.append(child)
                    elif str(Path(os.path.normcase(str(child.resolve())))) not in proven:
                        raise CleanupBlocked('任务产物目录含未登记所有权文件，暂保留并等待核对：'+child.name)
        return sorted(manifest,key=lambda item:item['path'])

    def _graph_cleanup(self,task_id,threads):
        outer=self.graph_lock
        if self.graph is not None and outer is self.graph._lock:outer=None
        with outer if outer is not None else nullcontext():
            self._delete_graph_threads(threads)

    def _delete_graph_threads(self,threads):
        if self.graph is not None:
            with self.graph._lock:
                for thread in threads:self.graph.saver.delete_thread(thread)
            return
        directory=self.graph_directory or self.profile_directory
        path=directory/'maa-graph.sqlite3'
        if not path.exists():return
        from langgraph.checkpoint.sqlite import SqliteSaver
        with _GRAPH_LOCKS_GUARD:
            lock=_GRAPH_LOCKS.setdefault(str(path.resolve()),threading.Lock())
        with lock:
            connection=sqlite3.connect(path)
            try:
                saver=SqliteSaver(connection)
                for thread in threads:saver.delete_thread(thread)
            finally:connection.close()

    def _business(self,task_id):
        with self.engine._transaction():
            selected=self._guard(task_id)
            self.db.execute('PRAGMA defer_foreign_keys=ON')
            for table,ids in selected.items():
                self.db.executemany('DELETE FROM "'+table+'" WHERE rowid=?',[(i,) for i in ids])
            self.db.execute("UPDATE tasks SET goal='',status='cancelled',completed_runs=0,latest_observation_id=NULL,latest_observation_version=NULL,latest_observation_at=NULL,latest_observation_state=NULL,latest_window_id=NULL WHERE id=?",(task_id,))

    def purge(self,task_id):
        try:
            requested=self.request(task_id)
            if requested['status']=='completed':return requested
            self._check_stop()
            with self.engine._transaction():
                job=self.db.execute('SELECT * FROM workflow_cleanup_jobs WHERE task_id=?',(task_id,)).fetchone()
                operation_id=job['operation_id']
                self._current(task_id,operation_id)
                done=set(json.loads(job['completed']));manifest=json.loads(job['manifest'])
            self._report(task_id,'manifest')
            files,version=self._scan_manifest(task_id,operation_id,pinned=manifest.get('files',()))
            with self.engine._transaction():
                self._current(task_id,operation_id)
                if version!=self._version():raise CleanupBlocked('扫描期间任务引用发生变化，请重试清理')
                if not manifest:
                    threads=[r[0] for r in self.db.execute('SELECT thread_id FROM workflow_cleanup_threads WHERE task_id=?',(task_id,))]
                    manifest={'files':files,'threads':sorted(set(threads+[task_id]))}
                    self.db.execute('UPDATE workflow_cleanup_jobs SET manifest=? WHERE task_id=?',(json.dumps(manifest),task_id))
                else:
                    saved_files={ref['path']:ref['sha256'] for ref in manifest.get('files',[]) if isinstance(ref,dict)}
                    if any(saved_files.get(ref['path'])!=ref['sha256'] for ref in files):
                        raise CleanupBlocked('清理开始后任务文件清单发生变化，需要核对所有权')
            for component,filename in ((self.runtime,'task-runtime.sqlite3'),(self.memory,'conversation.sqlite3')):
                if component is None and (self.profile_directory/filename).exists():
                    raise RuntimeError('Cleanup projection is unavailable: '+filename)
            steps=[('graph',lambda:self._graph_cleanup(task_id,manifest['threads'])),
                   ('runtime',lambda:self.runtime.delete_task(task_id) if self.runtime else None),
                   ('memory',lambda:self.memory.delete_task(task_id) if self.memory else None),
                   ('history',lambda:self.history.delete_task(task_id) if self.history else None),
                   ('files',lambda:self._files(task_id,manifest['files'])),
                   ('business',lambda:self._business(task_id))]
            for name,operation in steps:
                if name in done:continue
                self._check_stop()
                with self.engine._lock:self._current(task_id,operation_id)
                self._report(task_id,name)
                operation();done.add(name)
                self._report(task_id,name,completed=done)
            with self.engine._transaction():
                self._current(task_id,operation_id)
                self.db.execute("UPDATE workflow_lifecycle SET state='purged',original_status=NULL,updated_at=? WHERE task_id=?",(_utc_now(),task_id))
                self.db.execute('UPDATE workflow_cleanup_jobs SET manifest=? WHERE task_id=?',('{}',task_id))
                self.db.execute('DELETE FROM workflow_cleanup_files WHERE task_id=?',(task_id,))
                self.db.execute('DELETE FROM workflow_cleanup_threads WHERE task_id=?',(task_id,))
            self._report(task_id,'completed',status='completed',completed=done)
            return {'task_id':task_id,'status':'completed'}
        except CleanupPaused as exc:
            self._report(task_id,'paused',status='paused',reason=str(exc))
            return {'task_id':task_id,'status':'paused','reason':str(exc)}
        except CleanupBlocked as exc:
            self._report(task_id,'blocked',status='blocked',reason=str(exc))
            return {'task_id':task_id,'status':'blocked','reason':str(exc)}
        except Exception as exc:
            self._report(task_id,'error',status='error',reason=str(exc))
            return {'task_id':task_id,'status':'error','reason':str(exc)}

    def _files(self,task_id,files):
        with self.engine._lock:
            operation_id=self.store.management._lifecycle(task_id)['operation_id']
        refs,version=self._scan_manifest(task_id,operation_id,pinned=files)
        current={os.path.normcase(item['path']):item['sha256'] for item in refs}
        saved={os.path.normcase(ref['path']):ref['sha256'] for ref in files if isinstance(ref,dict)}
        if any(saved.get(path)!=digest for path,digest in current.items()):
            raise CleanupBlocked('清理开始后任务文件清单发生变化，需要核对所有权')
        for ref in files:
            self._check_stop()
            if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:
                raise CleanupBlocked('旧文件清单缺少哈希所有权凭据，请核对后重试')
            path=Path(ref['path']).absolute()
            if self._file_kind(task_id,path) is None:
                raise CleanupBlocked('文件清单越出任务私有范围')
            self._no_links(path)
            if not path.exists():continue
            # Hashing is itself a read and Windows may update last-access time.
            # Fence identity/content changes, not our own access timestamp.
            stamp=lambda stat:(stat.st_dev,stat.st_ino,stat.st_mode,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            before=stamp(path.stat())
            if current.get(os.path.normcase(str(path)))!=ref['sha256'] or hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
                raise CleanupBlocked('任务文件所有权或内容已变化：'+path.name)
            # Only final authority/link/stat checks and one unlink hold the writer
            # transaction; hashing and the global reference closure do not.
            with self.engine._transaction():
                self._current(task_id,operation_id)
                if version!=self._version():raise CleanupBlocked('清理期间任务引用发生变化，请重试清理')
                self._no_links(path)
                if stamp(path.stat())!=before:
                    raise CleanupBlocked('任务文件所有权或内容已变化：'+path.name)
                path.unlink()

    def resume_pending(self):
        with self.engine._lock:
            tasks=[r[0] for r in self.db.execute("SELECT task_id FROM workflow_lifecycle WHERE state='purge_pending'")]
        return [self.purge(task_id) for task_id in tasks]

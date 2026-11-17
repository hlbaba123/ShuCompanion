"""Local resumable task checkpoints; never stores images or provider reasoning."""
from __future__ import annotations
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

def _clean(value):
    if isinstance(value,dict):
        blocked={'api_key','authorization','reasoning_content','reasoning','hidden_reasoning','image_url','images','image_data','image_base64','audio_url','audio_base64','base64','pixels','frame_buffer','provider_payload','raw_response'}
        return {k:_clean(v) for k,v in value.items() if str(k).lower() not in blocked}
    if isinstance(value,(list,tuple)):return [_clean(v) for v in value]
    if isinstance(value,bytes):return '[binary omitted]'
    if isinstance(value,str):
        value=re.sub(r'data:(?:image|audio)/[^\s"<>]+','[media omitted]',value)
        value=re.sub(r'(?i)Bearer\s+\S+','Bearer [redacted]',value)
        return re.sub(r'\bsk-[A-Za-z0-9_.\-]{8,}','[Key 已隐藏]',value)
    return value


class RuntimeStore:
    def __init__(self, directory: Path, *, task_writable=None):
        self._task_writable=task_writable
        self._lock=threading.RLock()
        Path(directory).mkdir(parents=True,exist_ok=True)
        self._closed=False
        self._db=sqlite3.connect(Path(directory)/'task-runtime.sqlite3',check_same_thread=False)
        self._db.row_factory=sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('CREATE TABLE IF NOT EXISTS checkpoints(task_id TEXT PRIMARY KEY, version INTEGER NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY, role TEXT, task_id TEXT, metrics TEXT, created_at REAL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS deleted_tasks(task_id TEXT PRIMARY KEY)')
        self._db.commit()

    def set_task_writable(self, callback):
        self._task_writable=callback

    def _writable(self, task_id):
        return not task_id or (not self._db.execute('SELECT 1 FROM deleted_tasks WHERE task_id=?',(task_id,)).fetchone() and (self._task_writable is None or bool(self._task_writable(task_id))))

    def delete_task(self, task_id):
        with self._lock, self._db:
            self._db.execute('INSERT OR IGNORE INTO deleted_tasks VALUES(?)',(task_id,))
            self._db.execute('DELETE FROM checkpoints WHERE task_id=?',(task_id,))
            self._db.execute('DELETE FROM model_calls WHERE task_id=?',(task_id,))

    def save(self,task_id,version,*,phase,contract=None,context=None,metrics=None):
        if not isinstance(task_id,str) or not task_id.strip() or type(version) is not int or version<1:raise ValueError('Invalid checkpoint identity')
        payload=json.dumps(_clean({'contract':contract or {},'context':context or {},'metrics':metrics or {}}),ensure_ascii=False,allow_nan=False)
        if len(payload)>120000:raise ValueError('Checkpoint exceeds local budget')
        payload=re.sub(r'\bsk-[A-Za-z0-9_.\-]{8,}','[Key 已隐藏]',payload)
        with self._lock:
            if self._closed or not self._writable(task_id):return False
            row=self._db.execute('SELECT version FROM checkpoints WHERE task_id=?',(task_id,)).fetchone()
            if row and row['version']>version:return False
            self._db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET version=excluded.version,phase=excluded.phase,payload=excluded.payload,updated_at=excluded.updated_at',
                (task_id,version,phase,payload,time.time()))
            self._db.commit()
        return True

    def load(self,task_id):
        with self._lock:row=self._db.execute('SELECT * FROM checkpoints WHERE task_id=?',(task_id,)).fetchone()
        if not row:return None
        value=dict(row);value.update(json.loads(value.pop('payload')));return value

    def record_call(self,role,metrics,task_id=None):
        if not isinstance(metrics,dict):return
        with self._lock:
            if self._closed or not self._writable(task_id):return
            self._db.execute('INSERT INTO model_calls(role,task_id,metrics,created_at) VALUES(?,?,?,?)',
                (role,task_id,json.dumps(_clean(metrics),ensure_ascii=False,allow_nan=False),time.time()))
            self._db.commit()

    def calls(self,task_id=None):
        with self._lock:
            rows=self._db.execute('SELECT * FROM model_calls'+(' WHERE task_id=?' if task_id else '')+' ORDER BY id',
                (task_id,) if task_id else ()).fetchall()
        return [{**dict(row),'metrics':json.loads(row['metrics'])} for row in rows]

    def close(self):
        with self._lock:
            if not self._closed:self._db.close();self._closed=True

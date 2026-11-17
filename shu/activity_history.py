"""Small portable JSON history for display, never execution or task authority.

The index lists the latest 100 requests. Older task records are read only by
proven request/task bindings; this view never scans archival directories.
"""
from __future__ import annotations

import copy
from functools import wraps
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_REQUESTS = 100
MAX_EVENTS = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_.-]{6,}|(?i:Bearer\s+\S+)')
_ASSIGNMENT = re.compile(r'(?i)\b(?:api[_-]?key|authorization|access[_-]?token|password)\s*[:=]\s*\S+')
_PRIVATE_QUERY = re.compile(
    r'(?i)token|secret|password|passwd|credential|session|cookie|signature|ticket|'
    r'^(?:api|access|private|client)?key$|^(?:auth|authorization|oauth|oauth2|sign|sig)$')
_FIELDS = ('role', 'stage', 'status', 'summary', 'step_title', 'tool', 'query', 'url',
           'task_id', 'epoch', 'timestamp', 'request_id', 'dispatch_id', 'tool_call_id',
           'elapsed_seconds', 'item_count', 'total', 'cached', 'step_id', 'stage_code',
           'group_label', 'primary_count', 'backup_count', 'stop_reason', 'plan_revision','attempt','target_key')
TERMINAL_TASK_STATUSES = frozenset({'needs_input', 'paused', 'cancelled',
    'completed', 'research_verified', 'import_verified', 'planned_verified', 'catalog_verified'})
_STORE_LOCKS = {}
_STORE_LOCKS_LOCK = threading.Lock()


def _locked(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return run


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def safe_text(value, limit=900):
    if not isinstance(value, str):
        return ''
    value = ''.join(c for c in value[:limit * 2] if c.isprintable() or c in '\n\t')
    return _ASSIGNMENT.sub('[凭据已隐藏]', _SECRET.sub('[凭据已隐藏]', value))[:limit]


def public_source_url(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value
            or _SECRET.search(unquote(value))):
        return ''
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('https', 'http') or not parts.hostname or parts.username
                or parts.password or parts.port is not None or '.' not in parts.hostname
                or parts.hostname.endswith(('.local', '.localhost', '.internal'))):
            return ''
        try:
            if not ipaddress.ip_address(parts.hostname).is_global:
                return ''
        except ValueError:
            pass
        for key, child in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32):
            if _PRIVATE_QUERY.search(re.sub(r'[^a-z0-9]', '', key.lower())) or _SECRET.search(child):
                return ''
    except (ValueError, TypeError):
        return ''
    return value


def event_fields(event):
    if not isinstance(event, dict):
        return {}
    clean = {}
    for field in _FIELDS:
        value = event.get(field)
        if field in ('epoch','item_count','total','primary_count','backup_count','plan_revision','attempt'):
            if type(value) is int and 0 <= value <= 1000000:
                clean[field] = value
        elif field=='cached':
            if type(value) is bool:clean[field]=value
        elif field=='elapsed_seconds':
            if type(value) in (int,float) and math.isfinite(value) and 0<=value<=86400:
                clean[field]=round(value,4)
        elif field == 'url':
            value = public_source_url(value)
            if value:
                clean[field] = value
        elif field == 'step_title' and isinstance(value, str):
            title = ' '.join(safe_text(value, 240).split())[:120]
            if title:
                clean[field] = title
        elif isinstance(value, str) and value:
            clean[field] = safe_text(value, 900 if field in ('summary', 'query') else 128)
    return clean


def _identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and all(c.isprintable() and not c.isspace() for c in value)
            and safe_text(value, 128) == value)


def _json_file(path):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('History file exceeds display budget')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                prefix='.'+path.name+'.', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ActivityHistory:
    """A replaceable local display store; no GUI calls, network or execution."""
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        with _STORE_LOCKS_LOCK:
            self._lock = _STORE_LOCKS.setdefault(str(self.path.resolve()), threading.RLock()) if self.path else threading.RLock()
        self.directory = self.path.with_name(self.path.stem+'-records') if self.path else None
        self._records = {}
        self._archives = {}
        self._order = []
        self._invalid_index = False
        self._task_bindings = {}
        self._deleted_tasks = set()
        self._deleted_requests = set()
        if self.path is None or not self.path.exists():
            return
        try:
            index = _json_file(self.path)
            ids = index.get('requests') if isinstance(index, dict) and index.get('version') == 1 else None
            if not isinstance(ids, list):
                raise ValueError('Invalid display history index')
            bindings=index.get('task_bindings',{})
            if not isinstance(bindings,dict): raise ValueError('Invalid display history bindings')
            self._task_bindings = {key:value for key,value in bindings.items()
                if _identifier(key) and _identifier(value)}
            self._deleted_tasks = {value for value in index.get('deleted_tasks',[]) if _identifier(value)}
            self._deleted_requests = {value for value in index.get('deleted_requests',[]) if _identifier(value)}
            for request_id in ids[:MAX_REQUESTS]:
                if not _identifier(request_id) or request_id in self._records or request_id in self._deleted_requests:
                    continue
                try:
                    record = _json_file(self._record_path(request_id))
                    if not isinstance(record, dict) or record.get('request_id') != request_id:
                        continue
                    record = self._clean_record(record)
                    self._records[request_id] = record
                    self._order.append(request_id)
                except (OSError, ValueError, TypeError):
                    continue
        except (OSError, ValueError, TypeError):
            self._invalid_index = True

    def _record_path(self, request_id):
        return self.directory / (hashlib.sha256(request_id.encode('utf-8')).hexdigest()+'.json')

    @staticmethod
    def _clean_record(record):
        events = record.get('events')
        return {'request_id':record['request_id'], 'text':safe_text(record.get('text'), 12000),
            'timestamp':safe_text(record.get('timestamp'),80),
            'status':safe_text(record.get('status'),64) or 'running',
            'summary':safe_text(record.get('summary'),900),
            'events':[clean for event in events[-MAX_EVENTS:] if (clean := event_fields(event))]
                      if isinstance(events,list) else []}

    @_locked
    def records(self):
        self._sync_fences()
        return [copy.deepcopy(self._records[key]) for key in self._order]

    @_locked
    def task_groups(self, bindings=(), *, task_id=None):
        """Group only explicit message bindings or unambiguous event evidence."""
        linked = {**self._task_bindings, **{row['request_id']: row['task_id'] for row in bindings
                  if isinstance(row, dict) and _identifier(row.get('request_id'))
                  and _identifier(row.get('task_id'))}}
        groups, legacy = {}, []
        records = {row['request_id']: row for row in self.records()}
        if task_id is not None:
            for request_id, owner in linked.items():
                if owner == task_id:
                    self.bind_request(request_id, owner)
                    archived = self.load_bound(request_id, owner)
                    if archived is not None:
                        records[request_id] = archived
        for row in bindings:
            if isinstance(row, dict) and _identifier(row.get('request_id')):
                records.setdefault(row['request_id'], {'request_id': row['request_id'],
                    'text': safe_text(row.get('text'),12000), 'events': []})
        for request_id, record in records.items():
            evidence = {e['task_id'] for e in record.get('events', []) if e.get('task_id')}
            task_id = linked.get(request_id) or (next(iter(evidence)) if len(evidence) == 1 else None)
            if task_id:
                groups.setdefault(task_id, []).append(record)
            else:
                legacy.append(record)
        return groups, legacy

    @_locked
    def summaries(self):
        self._sync_fences()
        return [{key:value for key,value in self._records[identifier].items() if key != 'events'}
                for identifier in self._order]

    @_locked
    def get(self, request_id):
        record = self._records.get(request_id) or self._archives.get(request_id)
        return copy.deepcopy(record) if record is not None else None

    @_locked
    def load_bound(self, request_id, task_id):
        """Read one registered task record, without admitting unknown replays."""
        self._sync_fences()
        if (not _identifier(task_id) or self._task_bindings.get(request_id) != task_id
                or request_id in self._deleted_requests or task_id in self._deleted_tasks):
            return None
        known = self.get(request_id)
        if known is not None or self.path is None:
            return known
        try:
            record = _json_file(self._record_path(request_id))
            if not isinstance(record,dict) or record.get('request_id') != request_id:
                return None
            self._archives[request_id] = self._clean_record(record)
            while len(self._archives) > MAX_REQUESTS:
                self._archives.pop(next(iter(self._archives)))
            return self.get(request_id)
        except (OSError,ValueError,TypeError):
            return None

    @_locked
    def load_request(self, request_id):
        """Read one caller-proven request identity; never scan old directories."""
        self._sync_fences()
        if not _identifier(request_id) or request_id in self._deleted_requests:
            return None
        known = self.get(request_id)
        if known is not None or self.path is None:
            return known
        try:
            record = _json_file(self._record_path(request_id))
            if not isinstance(record, dict) or record.get('request_id') != request_id:
                return None
            self._archives[request_id] = self._clean_record(record)
            while len(self._archives) > MAX_REQUESTS:
                self._archives.pop(next(iter(self._archives)))
            return self.get(request_id)
        except (OSError, ValueError, TypeError):
            return None

    @_locked
    def request_task(self, request_id):
        """Task evidence blocks chat cleanup, including archived or conflicting evidence."""
        self._sync_fences()
        record = self.load_request(request_id)
        owners = {event['task_id'] for event in (record or {}).get('events', []) if event.get('task_id')}
        if self._task_bindings.get(request_id):
            owners.add(self._task_bindings[request_id])
        if len(owners) > 1:
            raise ValueError('History request has conflicting task ownership')
        return next(iter(owners), None)

    def _save(self, request_id):
        if self.path is None:
            return
        _atomic_json(self._record_path(request_id), self._records.get(request_id) or self._archives[request_id])
        if self._invalid_index and self.path.exists():
            # Preserve a malformed original for recovery; do not scan old records.
            raw = self.path.read_bytes()
            backup = self.path.with_name(self.path.stem+'.invalid-'+hashlib.sha256(raw).hexdigest()[:12]+'.json')
            if not backup.exists():
                backup.write_bytes(raw)
            self._invalid_index = False
        self._save_index()

    def _save_index(self):
        if self.path is not None:
            self._sync_fences()
            _atomic_json(self.path, {'version':1,'requests':self._order,
                'task_bindings':self._task_bindings,'deleted_tasks':sorted(self._deleted_tasks),
                'deleted_requests':sorted(self._deleted_requests)})

    def _sync_fences(self):
        """Another cleanup owner may have fenced records since this view loaded."""
        if self.path is None or not self.path.exists(): return
        try:
            index=_json_file(self.path)
            for request_id, task_id in index.get('task_bindings', {}).items():
                if _identifier(request_id) and _identifier(task_id):
                    self._task_bindings.setdefault(request_id, task_id)
            self._deleted_tasks.update(value for value in index.get('deleted_tasks',[]) if _identifier(value))
            self._deleted_requests.update(value for value in index.get('deleted_requests',[]) if _identifier(value))
        except (OSError,ValueError,TypeError,AttributeError):
            return
        self._deleted_requests.update(key for key,value in self._task_bindings.items() if value in self._deleted_tasks)
        self._order=[key for key in self._order if key not in self._deleted_requests]
        for key in self._deleted_requests:
            self._records.pop(key,None)
            self._archives.pop(key,None)
            self._task_bindings.pop(key,None)

    @_locked
    def bind_request(self, request_id, task_id):
        self._sync_fences()
        if not _identifier(request_id) or not _identifier(task_id): return False
        if task_id in self._deleted_tasks or request_id in self._deleted_requests: return False
        existing=self._task_bindings.get(request_id)
        if existing and existing != task_id: return False
        if existing == task_id: return True
        self._task_bindings[request_id]=task_id
        self._save_index()
        return True

    @_locked
    def delete_task(self, task_id):
        """Purge proven display ownership with a durable late-callback fence."""
        if not _identifier(task_id): raise ValueError('Invalid task ID')
        groups,_=self.task_groups()
        owned={record['request_id'] for record in groups.get(task_id,[])}
        owned.update(key for key,value in self._task_bindings.items() if value==task_id)
        self._deleted_tasks.add(task_id)
        self._deleted_requests.update(owned)
        self._order=[key for key in self._order if key not in owned]
        for key in owned:
            self._records.pop(key,None)
            self._archives.pop(key,None)
            self._task_bindings.pop(key,None)
        # Fence first; interrupted unlink remains retryable using deleted IDs.
        self._save_index()
        if self.directory is not None:
            if self.directory.is_symlink() or (hasattr(self.directory,'is_junction') and self.directory.is_junction()):
                raise ValueError('History cleanup rejects linked directories')
            if self.directory.resolve().parent != self.path.parent.resolve():
                raise ValueError('History cleanup directory escaped the profile')
            for key in self._deleted_requests:
                self._record_path(key).unlink(missing_ok=True)
        return len(owned)

    @_locked
    def delete_request(self, request_id):
        """Fence and unlink exactly one unbound display record; no task cleanup."""
        if not _identifier(request_id):
            raise ValueError('Invalid request ID')
        self._sync_fences()
        if self.request_task(request_id):
            raise ValueError('Task history cannot be deleted as ordinary chat')
        existed = self.get(request_id) is not None
        self._deleted_requests.add(request_id)
        self._order = [key for key in self._order if key != request_id]
        self._records.pop(request_id, None)
        self._archives.pop(request_id, None)
        self._save_index()
        if self.directory is not None:
            if self.directory.is_symlink() or (hasattr(self.directory, 'is_junction') and self.directory.is_junction()):
                raise ValueError('History cleanup rejects linked directories')
            if self.directory.resolve().parent != self.path.parent.resolve():
                raise ValueError('History cleanup directory escaped the profile')
            self._record_path(request_id).unlink(missing_ok=True)
        return int(existed)

    @_locked
    def begin(self, request_id, text, timestamp=None):
        self._sync_fences()
        if not _identifier(request_id):
            raise ValueError('Invalid display request ID')
        if request_id in self._deleted_requests: return None
        if request_id in self._records:
            return self.get(request_id)
        if self.path is not None and self._record_path(request_id).exists():
            # A record outside the recent display index remains archival data.
            # Do not read it back implicitly or overwrite it on a stale replay.
            return None
        self._records[request_id] = {'request_id':request_id, 'text':safe_text(text,12000),
            'timestamp':safe_text(timestamp,80) or now(), 'status':'running', 'summary':'', 'events':[]}
        self._order.insert(0,request_id)
        self._order = self._order[:MAX_REQUESTS]
        # Drop the old in-memory view only. Its individual file remains intact.
        self._records = {key:self._records[key] for key in self._order}
        self._save(request_id)
        return self.get(request_id)

    @_locked
    def append(self, request_id, event):
        self._sync_fences()
        if request_id in self._deleted_requests or (isinstance(event,dict) and event.get('task_id') in self._deleted_tasks):
            return False
        clean = event_fields(event)
        if not clean:
            return False
        if self.get(request_id) is None and self.load_bound(request_id,clean.get('task_id')) is None:
            return False
        if clean.get('task_id') and self._task_bindings.get(request_id) not in (None,clean['task_id']):
            return False
        if clean.get('task_id'):
            self.bind_request(request_id,clean['task_id'])
        record = self._records.get(request_id) or self._archives[request_id]
        record['events'] = [*record['events'],clean][-MAX_EVENTS:]
        stage = clean.get('stage')
        status = None
        if stage in ('error','intent_failed') or clean.get('status') in ('failed','error'):
            status = 'failed'
        elif stage in ('intent_started','dispatch_started','tool_started','task_started'):
            status = 'running'
        elif stage in ('task_waiting','task_paused','task_stopped'):
            status = {'task_waiting':'needs_input','task_paused':'paused','task_stopped':'cancelled'}[stage]
        elif stage == 'task_finished':
            status = clean.get('status') or 'unknown'
        if (record['status'] in TERMINAL_TASK_STATUSES and stage not in
                ('task_started','task_waiting','task_paused','task_stopped','task_finished')):
            # Late callbacks remain inspectable, but only an explicit task
            # restart or terminal event can replace a finished task heading.
            status = None
        if status is not None:
            record['status'] = status
            record['summary'] = clean.get('summary','')
        self._save(request_id)
        return True

    @_locked
    def finish(self, request_id, status, summary=''):
        self._sync_fences()
        if self.get(request_id) is None and self.load_bound(request_id,self._task_bindings.get(request_id)) is None:
            return False
        record = self._records.get(request_id) or self._archives[request_id]
        record['status'] = safe_text(status,64) or 'unknown'
        record['summary'] = safe_text(summary,900)
        self._save(request_id)
        return True

"""Export immutable, verified job receipts into a local activity library."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import copy
import math
import sqlite3
import time
import uuid
from .code_presentation import stage_label
from .leader_plan import read_reference
from .workflow_store import _encode


DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_PROOF_BYTES = 32 * 1024 * 1024


def _source_signature(stage):
    """Paths may move; public evidence content and semantics must not change."""
    def portable(value):
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, dict):
            return {key: portable(item) for key, item in value.items() if key != 'path'}
        return value
    return _encode(portable({key: stage.get(key) for key in (
        'source_refs', 'opening_source_refs', 'difficulty_domain_source_refs',
        'public_level_id', 'difficulty_domain', 'difficulty_domain_complete')}))[1]


def _identity(stage, activity_id, group_id):
    return {**{key: stage.get(key, '') for key in (
        'stage_key', 'stage_id', 'display_code', 'query_key', 'difficulty', 'branch', 'edition')},
        'activity_id': activity_id, 'group_id': group_id}


def _origin_key(task_id, revision, step):
    return _encode({'task_id': task_id, 'revision': revision, 'step_id': step['step_id'],
        'primary_ref': step['result'].get('primary_ref'), 'backup_ref': step['result'].get('backup_ref')})[1]


def _safe(value):
    value = str(value or 'unknown')
    if value in ('.', '..') or any(c in value for c in '/\\:') or any(ord(c) < 32 for c in value):
        raise ValueError('活动路径包含不安全字符')
    cleaned = re.sub(r'[<>"|?*]', '_', value).strip(' .')
    if not cleaned or len(cleaned) > 120:
        raise ValueError('活动路径无效')
    return cleaned


def _write(path, content):
    raw = content.encode('utf-8')
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('已有导出内容不同，保留历史文件')
        return hashlib.sha256(raw).hexdigest()
    tmp = path.with_name('.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


class JobLibrary:
    def __init__(self, directory, deliveries):
        self.directory = Path(directory).resolve()
        self.deliveries = deliveries

    def _path(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError('公共作业证据越出作业库')
        return path

    def _index(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path(self.directory / 'index.sqlite3'), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS published_jobs ('
                'digest TEXT PRIMARY KEY,activity_id TEXT NOT NULL,stage_key TEXT NOT NULL,'
                'group_id TEXT NOT NULL,acquired_at REAL NOT NULL,path TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS published_stage '
                               'ON published_jobs(activity_id,stage_key,group_id,acquired_at)')
            connection.execute('CREATE TABLE IF NOT EXISTS published_origins ('
                               'origin_key TEXT PRIMARY KEY,entry_digest TEXT NOT NULL)')
        except sqlite3.Error as exc:
            connection.close()
            raise ValueError('公共作业索引不可用，保留原索引等待重建') from exc
        return connection

    def _save_proof(self, value, *, pool=False):
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        raw = content.encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_PROOF_BYTES:
            raise ValueError('公共作业证据超过大小上限')
        directory = self._path(self.directory / ('candidate-pools' if pool else 'proof'))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + '.json')
        _write(path, content)
        ref = {'path': str(path), 'sha256': digest}
        if pool:
            ref['pool_id'] = value['pool_id']
        return ref

    def _copy_value(self, value, memo=None, active=None):
        """Copy the verified reference closure, rather than retaining task paths."""
        memo = {} if memo is None else memo
        active = set() if active is None else active
        if isinstance(value, list):
            return [self._copy_value(item, memo, active) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        if not isinstance(value.get('path'), str) or not isinstance(value.get('sha256'), str):
            return {key: self._copy_value(item, memo, active) for key, item in value.items()}
        path = Path(value['path']).resolve(strict=True)
        source_root = Path(self.deliveries.directory).resolve()
        if not path.is_relative_to(source_root) or not path.is_file():
            raise ValueError('发布来源不在应用资料目录')
        key = (str(path), value['sha256'])
        if key in memo:
            return {**value, **memo[key]}
        if key in active:
            raise ValueError('公共作业证据存在循环引用')
        with path.open('rb') as stream:
            raw = stream.read(MAX_PROOF_BYTES + 1)
        if len(raw) > MAX_PROOF_BYTES or hashlib.sha256(raw).hexdigest() != value['sha256']:
            raise ValueError('发布来源已变化或超过大小上限')
        active.add(key)
        try:
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeError):
                directory = self._path(self.directory / 'proof')
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / (value['sha256'] + '.bin')
                if destination.exists():
                    if destination.read_bytes() != raw:
                        raise ValueError('已发布来源内容不同')
                else:
                    with destination.open('xb') as stream:
                        stream.write(raw)
                copied = {'path': str(destination), 'sha256': value['sha256']}
            else:
                document = self._copy_value(document, memo, active)
                is_pool = isinstance(document, dict) and 'candidates' in document and 'pool_id' in document
                copied = self._save_proof(document, pool=is_pool)
                if isinstance(document, dict) and document.get('kind') == 'roster-pool-screen':
                    # Relocating unique-public-mode proof changes the hashed
                    # requirements payload. Re-screen the copied pool against
                    # the current Box instead of keeping its old hash verdict.
                    from .maa_roster_tools import RosterTools
                    screened=RosterTools(self.deliveries.workflow,self.deliveries.directory).screen_pool(
                        document['profile_id'],document['pool_ref'],document.get('policy'))
                    document=read_reference(screened['screen_ref'],self.deliveries.directory,maximum=MAX_PROOF_BYTES)
                    copied=self._save_proof(document)
                    copied.update(pool_sha256=document['pool_ref']['sha256'])
            memo[key] = copied
            return {**value, **copied}
        finally:
            active.remove(key)

    def _publish_step(self, plan, step, stage, group):
        # Human-readable legacy exports carry no verified-receipt capability.
        # They remain readable but can never become searchable trusted jobs.
        if not callable(getattr(self.deliveries, '_verify', None)):
            return None
        if not self.directory.is_relative_to(Path(self.deliveries.directory).resolve()):
            raise ValueError('公共作业库必须位于应用资料目录')
        if step['result'].get('status') not in {'primary_only', 'two_verified'}:
            return None
        records, acquired, identifiers = {}, [], set()
        for slot in ('primary', 'backup'):
            ref = step['result'].get(slot + '_ref')
            if not ref:
                continue
            code = self.deliveries.read_code(ref)
            if (code['task_id'] != plan['task_id'] or code['revision'] != plan['revision']
                    or code['step_id'] != step['step_id'] or code['resolved_stage'] != stage
                    or code['id'] in identifiers):
                raise ValueError('发布代码凭据不属于此任务关卡或主备身份重复')
            identifiers.add(code['id'])
            with self.deliveries.engine._lock:
                row = self.deliveries.engine._db.execute('SELECT * FROM code_receipts WHERE id=?', (ref['receipt_id'],)).fetchone()
                if not row or row['hash'] != ref['sha256']:
                    raise ValueError('发布代码凭据不存在')
                payload = json.loads(row['payload'])
            if _encode(payload)[1] != ref['sha256']:
                raise ValueError('发布代码凭据已变化')
            screen = read_reference(payload['screen_ref'], self.deliveries.directory, maximum=MAX_PROOF_BYTES)
            from .copilot_pool import CopilotPool
            pool_path = Path(screen['pool_ref']['path']).resolve()
            pool = CopilotPool(pool_path.parent.parent).read(screen['pool_ref'])
            obtained = pool.get('updated_at')
            if not isinstance(obtained, (int, float)) or isinstance(obtained, bool) or not math.isfinite(obtained):
                # Older valid receipts still support their original task and
                # text export. Unknown acquisition time cannot gain reusable
                # freshness merely because migration runs today.
                return None
            acquired.append(obtained)
            copied = self._copy_value(payload)
            self.deliveries._verify(copied)
            records[slot] = {'payload': copied, 'original_receipt_ref': copy.deepcopy(ref),
                'original_screen_sha256': payload['screen_ref']['sha256'],
                'original_pool_sha256': screen['pool_ref']['sha256'], 'acquired_at': obtained,
                'roster_ref': copy.deepcopy(screen['roster_ref'])}
        if 'primary' not in records:
            return None
        if step['result']['status'] == 'two_verified' and 'backup' not in records:
            raise ValueError('主备交付缺少真实备选凭据')
        group_id = group.get('group_id') or stage.get('group_id') or ''
        entry = {'schema': 1, 'kind': 'published-job-pair',
            'identity': _identity(stage, plan['activity_id'], group_id),
            'source_signature': _source_signature(stage), 'acquired_at': min(acquired),
            'activity_name': plan.get('activity_name'), 'chapter_label': group.get('label'),
            'origin': {key: records['primary']['payload'][key] for key in ('task_id', 'revision', 'step_id')},
            'records': records, 'status': 'two_verified' if 'backup' in records else 'primary_only',
            'backup_reason': step['result'].get('backup_reason')}
        reference = self._save_proof(entry)
        connection = self._index()
        try:
            with connection:
                connection.execute('INSERT OR IGNORE INTO published_jobs VALUES(?,?,?,?,?,?)', (
                    reference['sha256'], str(plan['activity_id']), stage['stage_key'], str(group_id),
                    entry['acquired_at'], reference['path']))
                connection.execute('INSERT OR IGNORE INTO published_origins VALUES(?,?)', (
                    _origin_key(entry['origin']['task_id'], plan['revision'], step), reference['sha256']))
        finally:
            connection.close()
        return reference

    def index_verified_history(self, *, limit=32, activity_id=None, stage_keys=None, max_milliseconds=1000):
        """Bounded lazy migration from actual current-revision receipts only.

        No filesystem traversal, negative cache, old receipt mutation or GUI
        authority. A subsequent call retries failed origins; published origin
        markers avoid repeatedly re-reading their reference closures.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('历史作业索引批次必须为1至128')
        if type(max_milliseconds) is not int or not 0 <= max_milliseconds <= 30000:
            raise ValueError('历史作业索引时间预算无效')
        if stage_keys is not None and (not isinstance(stage_keys, (list, tuple)) or len(stage_keys) > 1000
                or any(not isinstance(key, str) or not key for key in stage_keys)):
            raise ValueError('历史作业范围无效')
        result = {'examined': 0, 'indexed': 0, 'already_indexed': 0, 'skipped': 0, 'errors': [], 'has_more': False}
        if max_milliseconds == 0 or stage_keys is not None and not stage_keys:
            return result
        query = ('SELECT s.task_id,s.revision,s.step_id,s.stage_key,s.result FROM leader_steps s '
            'JOIN workflow_tasks w ON w.task_id=s.task_id AND w.revision=s.revision '
            'JOIN leader_plans p ON p.task_id=s.task_id AND p.revision=s.revision '
            "WHERE s.status IN ('primary_only','two_verified')")
        params = []
        if activity_id is not None:
            query += " AND json_extract(p.payload,'$.activity_id')=?"
            params.append(activity_id)
        if stage_keys is not None:
            query += ' AND s.stage_key IN (' + ','.join('?' for _ in stage_keys) + ')'
            params.extend(stage_keys)
        query += ' ORDER BY s.rowid DESC LIMIT ?'
        params.append(limit + 1)
        with self.deliveries.engine._lock:
            rows = self.deliveries.engine._db.execute(query, params).fetchall()
        result['has_more'] = len(rows) > limit
        started = time.monotonic()
        connection = self._index()
        try:
            for row in rows[:limit]:
                if (time.monotonic() - started) * 1000 >= max_milliseconds:
                    result['has_more'] = True
                    break
                result['examined'] += 1
                try:
                    step = {'step_id': row['step_id'], 'result': json.loads(row['result'])}
                    key = _origin_key(row['task_id'], row['revision'], step)
                    if connection.execute('SELECT 1 FROM published_origins WHERE origin_key=?', (key,)).fetchone():
                        result['already_indexed'] += 1
                        continue
                    plan = self.deliveries.plans.current(row['task_id'])
                    if not plan or plan['revision'] != row['revision']:
                        result['skipped'] += 1
                        continue
                    stage = next(stage for stage in plan['stages'] if stage['stage_key'] == row['stage_key'])
                    group = next((group for group in plan['groups'] if stage['stage_key'] in group['stage_keys']), {})
                    if self._publish_step(plan, step, stage, group):
                        result['indexed'] += 1
                    else:
                        result['skipped'] += 1
                except (ValueError, OSError, KeyError, TypeError, StopIteration, sqlite3.Error) as exc:
                    result['skipped'] += 1
                    result['errors'].append({'task_id': row['task_id'], 'step_id': row['step_id'], 'code': type(exc).__name__})
        finally:
            connection.close()
        return result

    def _read_candidate(self, reference):
        self._path(reference['path'])
        entry = read_reference(reference, self.directory, maximum=MAX_PROOF_BYTES)
        if entry.get('schema') != 1 or entry.get('kind') != 'published-job-pair' or 'primary' not in entry.get('records', {}):
            raise ValueError('公共作业条目无效')
        for record in entry['records'].values():
            # Recheck all published bytes, including stage provenance that the
            # existing code verifier uses as typed evidence rather than reading.
            self._check_references(record['payload'])
            self.deliveries._verify(record['payload'], current=False)
        return entry

    def _check_references(self, value, checked=None):
        checked = set() if checked is None else checked
        if isinstance(value, list):
            for item in value: self._check_references(item, checked)
        elif isinstance(value, dict):
            if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
                path = self._path(value['path'])
                key = (str(path), value['sha256'])
                if key in checked: return
                checked.add(key)
                with path.open('rb') as stream: raw = stream.read(MAX_PROOF_BYTES + 1)
                if len(raw) > MAX_PROOF_BYTES or hashlib.sha256(raw).hexdigest() != value['sha256']:
                    raise ValueError('公共作业来源已变化')
                try: content = json.loads(raw)
                except (ValueError, UnicodeError): return
                self._check_references(content, checked)
            else:
                for item in value.values(): self._check_references(item, checked)

    def lookup_plan(self, task_id, *, max_age_seconds=DEFAULT_MAX_AGE_SECONDS, stage_keys=None):
        """Return exact planned-stage hits and reasons; never persist misses."""
        if not isinstance(max_age_seconds, (int, float)) or isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or max_age_seconds < 0:
            raise ValueError('本地作业有效期无效')
        plan = self.deliveries.plans.current(task_id)
        if not plan:
            raise ValueError('任务计划不存在')
        evaluated_at, rows = time.time(), []
        box = self.deliveries.roster_store.current('local-default')
        from .roster_match import roster_ref
        current_roster = roster_ref(box or {})
        try:
            connection = self._index()
        except (ValueError, OSError, sqlite3.Error):
            rows = [{'stage_key': step['stage_key'], 'status': 'stale', 'reason': 'index_unavailable'} for step in plan['steps']]
            return {'task_id': task_id, 'revision': plan['revision'], 'evaluated_at': evaluated_at,
                    'stages': rows, 'usable': [], 'missing': rows}
        try:
            for step in plan['steps']:
                if stage_keys is not None and step['stage_key'] not in stage_keys:
                    continue
                stage = next(s for s in plan['stages'] if s['stage_key'] == step['stage_key'])
                group = next((g for g in plan['groups'] if stage['stage_key'] in g['stage_keys']), {})
                group_id = group.get('group_id') or stage.get('group_id') or ''
                identity = _identity(stage, plan['activity_id'], group_id)
                item = {'stage_key': stage['stage_key'], 'stage_code': stage.get('display_code'),
                    'difficulty': stage.get('difficulty'), 'group_id': group_id, 'status': 'missing', 'reason': 'not_published'}
                from .job_library_adoption import _check_current_stage_sources
                try:
                    _check_current_stage_sources(stage, self.deliveries.directory)
                except (ValueError, OSError, KeyError, TypeError):
                    item.update(status='stale', reason='current_stage_source_unavailable')
                    rows.append(item)
                    continue
                candidates = connection.execute('SELECT * FROM published_jobs WHERE activity_id=? AND stage_key=? '
                    'AND group_id=? ORDER BY acquired_at DESC,digest', (str(plan['activity_id']), stage['stage_key'], str(group_id))).fetchall()
                for candidate in candidates:
                    reference = {'path': candidate['path'], 'sha256': candidate['digest']}
                    try:
                        entry = self._read_candidate(reference)
                        if entry['identity'] != identity:
                            continue
                        if (entry['source_signature'] != _source_signature(stage)
                                or not 0 <= evaluated_at - entry['acquired_at'] <= max_age_seconds):
                            item.update(status='stale', reason='source_changed_or_expired')
                            continue
                        if not box or any(record['roster_ref'] != current_roster for record in entry['records'].values()):
                            item.update(status='box_mismatch', reason='box_version_changed')
                            continue
                        for record in entry['records'].values(): self.deliveries._verify(record['payload'])
                        item.update(status='usable', reason='verified_local_job', candidate_ref=reference,
                            acquired_at=entry['acquired_at'], origin=entry['origin'],
                            has_backup='backup' in entry['records'], roster_ref=current_roster)
                        break
                    except (ValueError, OSError, KeyError, TypeError, RecursionError):
                        item.update(status='stale', reason='published_evidence_unavailable')
                rows.append(item)
        finally:
            connection.close()
        return {'task_id': task_id, 'revision': plan['revision'], 'evaluated_at': evaluated_at, 'stages': rows,
                'usable': [row for row in rows if row['status'] == 'usable'],
                'missing': [row for row in rows if row['status'] != 'usable']}

    def adopt_step(self, lease, *, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
        from .job_library_adoption import adopt_step
        return adopt_step(self, lease, max_age_seconds=max_age_seconds)

    def export_step(self, task_id, step_id):
        plan = self.deliveries.plans.current(task_id)
        if not plan: raise ValueError('任务计划不存在')
        step = next((s for s in plan['steps'] if s['step_id'] == step_id), None)
        if not step or not step['result']: raise ValueError('步骤尚未封口')
        stage = next(s for s in plan['stages'] if s['stage_key'] == step['stage_key'])
        group = next((g for g in plan['groups'] if step['stage_key'] in g['stage_keys']), {})
        group_name = group.get('label') or group.get('name') or group.get('group_name') or group.get('group_id') or '未分组'
        activity = _safe(plan.get('activity_name') or plan.get('activity_id'))
        root = self.directory / (activity+'-'+hashlib.sha256(str(plan.get('activity_id')).encode()).hexdigest()[:10]) / _safe(group_name) / (_safe(stage.get('display_code'))+'-'+step['stage_key'][:10]) / (_safe(task_id)+'-r'+str(plan['revision']))
        root = root.resolve()
        if not root.is_relative_to(self.directory): raise ValueError('导出路径越界')
        result = step['result']; records = {}
        for kind in ('primary','backup'):
            ref = result.get(kind+'_ref')
            if ref:
                record = self.deliveries.read_code(ref)
                if record['task_id'] != task_id or record['step_id'] != step_id or record.get('revision') != plan['revision']: raise ValueError('回执不属于此步骤或计划版本')
                records[kind] = record
        if 'backup' in records and ('primary' not in records or records['primary']['id'] == records['backup']['id']): raise ValueError('备选作业身份重复')
        root.mkdir(parents=True, exist_ok=True)
        paths, hashes = {}, {}
        # Add identity alongside existing immutable exports; older code/result
        # files remain byte-identical when this task is exported again.
        stage_path = root / 'stage.json'
        identity = {key: stage.get(key) for key in ('stage_key', 'stage_id', 'difficulty', 'branch')}
        identity.update(stage_code=stage.get('display_code'), stage_label=stage_label(stage),
                        activity_id=plan.get('activity_id'), activity_name=plan.get('activity_name'),
                        task_id=task_id, step_id=step_id, revision=plan['revision'])
        hashes['stage'] = _write(stage_path, json.dumps(identity, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
        for kind, record in records.items():
            path = root / (kind+'.txt'); hashes[kind] = _write(path, record['code']+'\n'); paths[kind] = str(path)
        text = '状态：'+result['status']+'\n'+('已有备选' if 'backup' in records else '暂无备选')+'\n'
        result_path = root/'result.txt'; hashes['result'] = _write(result_path, text)
        output = io.StringIO(newline=''); writer = csv.writer(output); writer.writerow(['kind','id','code','sha256'])
        for kind, record in records.items(): writer.writerow([kind,record['id'],record['code'],hashes[kind]])
        manifest = root/'manifest.csv'; _write(manifest, output.getvalue())
        deferred_path = None
        if plan.get('deferred_stages'):
            activity_root = self.directory / (activity+'-'+hashlib.sha256(str(plan.get('activity_id')).encode()).hexdigest()[:10])
            # Task/revision subdirectory preserves prior published plans.
            pending = activity_root / ('待复查-'+_safe(task_id)+'-r'+str(plan['revision']))
            pending.mkdir(parents=True, exist_ok=True)
            deferred_path = pending / '待复查事项.json'
            _write(deferred_path, json.dumps(plan['deferred_stages'], ensure_ascii=False, sort_keys=True, indent=2)+'\n')
        library_ref = self._publish_step(plan, step, stage, group)
        return {'deferred_path': str(deferred_path) if deferred_path else None, 'primary_path': paths.get('primary'), 'backup_path': paths.get('backup'), 'result_path': str(result_path), 'manifest_path': str(manifest), 'stage_path': str(stage_path), 'hashes': hashes, 'library_ref': library_ref}


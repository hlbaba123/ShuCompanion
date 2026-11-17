"""Reuse only freshly verified public catalog facts across explicitly linked tasks."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import unicodedata
import uuid

_SOURCE = 'https://prts.maa.plus/arknights/level'


def _name(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold()
                   if c.isalnum())


def _read(ref, root, limit):
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('invalid reference')
    path = Path(ref['path']).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('reference outside data directory')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
        raise ValueError('invalid reference hash')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid document')
    return value, raw


def linked_catalog(workflow, task_id, directory, *, now=None, max_age_seconds=900):
    """Copy an explicitly linked, fresh catalog; return None when it cannot be proven."""
    try:
        root = Path(directory).resolve()
        if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id or task_id in ('.', '..'):
            return None
        if type(max_age_seconds) not in (int, float) or not 0 <= max_age_seconds <= 900:
            return None
        instant = now or datetime.now(timezone.utc)
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            return None
        with workflow.engine._lock:
            current = workflow.current(task_id)
            source_id = current['contract'].get('source_task_id')
            if not isinstance(source_id, str) or not source_id or source_id == task_id:
                return None
            source = workflow.current(source_id)
            rows = workflow.engine._db.execute(
                "SELECT d.* FROM workflow_dispatches d JOIN workflow_results r ON r.dispatch_id=d.dispatch_id "
                "WHERE d.task_id=? AND d.revision=? AND d.role='catalog' AND d.status='completed' AND r.status='completed' "
                "ORDER BY d.rowid DESC LIMIT 20", (source_id, source['revision'])).fetchall()
            dispatches = [(dict(row), workflow.dispatch_result(row['dispatch_id'], current_only=False)) for row in rows]
        for dispatch, result in dispatches:
            created = datetime.fromisoformat(dispatch['created_at'].replace('Z', '+00:00'))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (instant.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > max_age_seconds or not isinstance(result, dict):
                continue
            for ref in result.get('evidence_refs', []):
                try:
                    obs, _ = _read(ref, root, 512000)
                    data = obs.get('result') or {}
                    if (obs.get('task_id') != source_id or obs.get('epoch') != dispatch['epoch']
                            or obs.get('dispatch_id') != dispatch['dispatch_id'] or obs.get('read_only') is not True
                            or obs.get('tool') != 'research_activity_stages' or data.get('ok') is not True
                            or data.get('source') != _SOURCE or data.get('catalog_complete') is not True
                            or data.get('has_more') is not False):
                        continue
                    catalog, raw = _read(data.get('stage_catalog_ref'), root, 16 * 1024 * 1024)
                    stages = catalog.get('stages'); event = catalog.get('event_name')
                    requested = current['contract'].get('activity_name')
                    if (catalog.get('source') != _SOURCE or catalog.get('catalog_complete') is not True
                            or catalog.get('has_more') is not False or not isinstance(stages, list) or not stages
                            or type(catalog.get('total_stages')) is not int or catalog['total_stages'] != len(stages)
                            or data.get('total_stages') != len(stages) or data.get('stages') != stages
                            or not isinstance(event, str) or not event or data.get('event_name') != event
                            or not catalog.get('raw_sha256') or catalog.get('raw_sha256') != data.get('raw_sha256')
                            or (requested and (not isinstance(requested, str) or not _name(requested)
                                or (_name(requested) != _name(event) and _name(event) != _name(requested)+'复刻')))):
                        continue
                    target_dir = (root / 'research' / task_id / 'activity-cache').resolve()
                    if not target_dir.is_relative_to(root):
                        return None
                    # Recheck authority after reading the files, before publication.
                    with workflow.engine._lock:
                        if workflow.current(task_id) != current or workflow.current(source_id)['revision'] != source['revision']:
                            return None
                        if workflow.dispatch_result(dispatch['dispatch_id'], current_only=False) != result:
                            return None
                        target_dir.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256(raw).hexdigest()
                        target = target_dir / ('activity-stages-' + digest + '.json')
                        temporary = target_dir / ('.' + uuid.uuid4().hex + '.tmp')
                        try:
                            with temporary.open('xb') as stream:
                                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                            os.replace(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    fields = ('query','code','event_name','event_candidates','total_events','has_more_events',
                              'stages','total_stages','returned_stages','has_more','source','raw_sha256',
                              'fetched_at','untrusted_content','content_is_instruction',
                              'account_unlock_verified','opening_status_verified')
                    output = {key:data[key] for key in fields if key in data}
                    output.update(ok=True, tool='activity_stages', cached=True, catalog_complete=True,
                        stage_catalog_ref={'path':str(target), 'sha256':digest},
                        reused_from={'source_task_id':source_id, 'source_dispatch_id':dispatch['dispatch_id'],
                                     'source_ref':data['stage_catalog_ref']})
                    return output
                except (ValueError, TypeError, KeyError, OSError):
                    continue
    except (ValueError, TypeError, KeyError, OSError):
        pass
    return None

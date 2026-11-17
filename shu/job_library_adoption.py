"""Lease-bound local evidence adoption, using the existing code receipt gate."""
import copy
import hashlib
import json
from pathlib import Path

from .copilot_pool import CopilotPool
from .leader_plan import read_reference
from .maa_roster_tools import RosterTools
from .workflow_store import _encode


def _check_current_stage_sources(stage, directory):
    for field in ('source_refs', 'opening_source_refs', 'difficulty_domain_source_refs'):
        for ref in stage.get(field, []):
            if not isinstance(ref, dict) or not {'path', 'sha256'}.intersection(ref):
                continue
            if not isinstance(ref.get('path'), str) or not isinstance(ref.get('sha256'), str):
                raise ValueError('当前关卡来源引用不完整')
            path = Path(ref['path']).resolve(strict=True)
            if not path.is_relative_to(Path(directory).resolve()) or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('当前关卡来源越界或过大')
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                raise ValueError('当前关卡来源已变化')


def adopt_step(library, lease, *, max_age_seconds):
    """Return find_pair's result shape or None; no network or execution grant."""
    deliveries = library.deliveries
    with deliveries.engine._lock:
        deliveries.plans._lease(lease)
    lookup = library.lookup_plan(lease['task_id'], max_age_seconds=max_age_seconds,
                                 stage_keys={lease['resolved_stage']['stage_key']})
    if lookup['revision'] != lease['revision']:
        raise ValueError('本地采纳计划版本已变化')
    hit = next((item for item in lookup['usable'] if item['stage_key'] == lease['resolved_stage']['stage_key']), None)
    if hit is None:
        return None
    entry = library._read_candidate(hit['candidate_ref'])
    stage = lease['resolved_stage']
    _check_current_stage_sources(stage, deliveries.directory)
    event_key = ':'.join(str(value) for value in ('local-adoption', lease['task_id'], lease['revision'],
        lease['epoch'], lease['step_id'], lease.get('attempt', 1), hit['candidate_ref']['sha256']))
    with deliveries.engine._lock:
        previous = deliveries.engine._db.execute('SELECT * FROM workflow_outbox WHERE event_key=?', (event_key,)).fetchone()
        if previous:
            event = json.loads(previous['payload'])
            if _encode(event)[1] != previous['payload_hash']:
                raise ValueError('本地采纳回执已变化')
            result = event['result']
            for slot in ('primary', 'backup'):
                if result.get(slot + '_ref'):
                    code = deliveries.read_code(result[slot + '_ref'])
                    if (any(code[key] != lease[key] for key in ('task_id', 'revision', 'step_id'))
                            or code['resolved_stage'] != stage):
                        raise ValueError('采纳回执不属于当前任务关卡')
            return result
    rosters = RosterTools(deliveries.workflow, deliveries.directory)
    selected = {}
    # Both slots are re-screened from immutable source candidates. Rebinding
    # exact current stage evidence does not reuse source task state/authority.
    for slot in ('primary', 'backup'):
        if slot not in entry['records']:
            continue
        payload = entry['records'][slot]['payload']
        screen = read_reference(payload['screen_ref'], deliveries.directory, maximum=32 * 1024 * 1024)
        pool_path = Path(screen['pool_ref']['path']).resolve()
        pool = CopilotPool(pool_path.parent.parent).read(screen['pool_ref'])
        matches = [candidate for candidate in pool['candidates'] if candidate.get('candidate_locator') == payload['candidate_locator']]
        if len(matches) != 1:
            raise ValueError('本地来源候选身份不唯一')
        candidate = copy.deepcopy(matches[0])
        candidate['candidate_locator']['resolved_stage'] = copy.deepcopy(stage)
        if candidate.get('difficulty_basis') == 'unique_public_stage_mode':
            candidate['difficulty_proof'] = copy.deepcopy(stage['difficulty_domain_source_refs'])
        adopted_pool = {**copy.deepcopy(pool), 'candidates': [candidate], 'unverified_candidates': [],
            'coverage': {'total': 1, 'collected': 1, 'complete': True, 'next_page': None},
            # Keep source hashes in public facts. The task's persisted result
            # carries the full delivery ref; embedding it here would make each
            # later publication recursively copy every previous task's proof.
            'local_adoption': {'published_sha256': hit['candidate_ref']['sha256'], 'slot': slot,
                               'original_pool_sha256': entry['records'][slot]['original_pool_sha256']}}
        # Content addressing already separates exact inputs. A deeply nested
        # task/step path would exceed Windows path limits in isolated workdirs.
        pool_ref = CopilotPool(library.directory / 'adopted')._save(adopted_pool)
        screened = rosters.screen_pool('local-default', pool_ref)
        selected[slot] = deliveries.register_code(lease, candidate['candidate_locator'], screened['screen_ref'])
    result = {'status': 'two_verified' if 'backup' in selected else 'primary_only',
        'primary_ref': selected['primary'], 'backup_ref': selected.get('backup'),
        'backup_reason': entry.get('backup_reason'), 'reason': 'verified_local_library',
        'search_coverage': {'source': 'local_library', 'published_ref': hit['candidate_ref']},
        'unresolved_count': 0, 'budget_used': {}, 'attempt': lease.get('attempt', 1),
        'pages_attempted': 0, 'pages_fetched': 0, 'error_classification': None, 'errors': [],
        'local_adoption': {'published_ref': hit['candidate_ref'], 'origin': entry['origin'],
                           'acquired_at': entry['acquired_at'], 'roster_ref': hit['roster_ref']}}
    with deliveries.engine._transaction():
        deliveries.plans._lease(lease)
        deliveries.workflow._publish_event(lease['task_id'], 'job_library.adopted', {
            'step_id': lease['step_id'], 'published_ref': hit['candidate_ref'],
            'result': result}, event_key=event_key)
    return result

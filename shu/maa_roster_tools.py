"""Fixed local MAA export -> shared roster facts -> conservative job match.

The local profile is not proof of the current game account. No model-supplied
operator rows, arbitrary files or game input enter this boundary.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import copy

from .maa_facts import RosterStore, parse_maa_export
from .roster_match import (assess_requirements, assess_metadata, requirements_hash, encode,
                           normalize_policy, policy_hash, roster_ref)


class RosterTools:
    def __init__(self, store, directory):
        self.store = store
        self.directory = Path(directory).resolve()
        self.facts = RosterStore(store)

    def current(self, profile_id):
        return self.facts.current(profile_id)

    def import_export(self, profile_id):
        path = (self.directory / 'roster-imports' / 'maa-roster.json').resolve()
        if not path.is_relative_to(self.directory) or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('MAA 导出文件超出本地档案范围')
        raw = path.read_bytes()
        parsed = parse_maa_export(json.loads(raw.decode('utf-8-sig')))
        return self.facts.publish(profile_id, parsed['operators'], parsed['complete'],
            {'kind': 'maa-local-export', 'observed_at': path.stat().st_mtime,
             'evidence_ref': str(path) + '#sha256=' + hashlib.sha256(raw).hexdigest(),
             'scan_id': hashlib.sha256(raw).hexdigest()})

    def assess(self, profile_id, copilot_ref, policy=None):
        policy = normalize_policy(policy)
        path = Path(copilot_ref['path']).resolve()
        if not path.is_relative_to(self.directory) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('作业文件必须位于当前工作流目录')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != copilot_ref.get('sha256'):
            raise ValueError('作业哈希已变化')
        body = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(body, dict) or not body.get('stage_name') or not body.get('actions'):
            raise ValueError('不是完整作业文件')
        box = self.current(profile_id)
        if box is None:
            return {'classification': 'unknown', 'profile_id': profile_id,
                    'missing_requirements': [{'field': 'roster', 'reason': '尚无本地 MAA 干员导出档案'}],
                    'account_binding': 'unverified', 'box_revision': None,
                    'policy': policy, 'policy_sha256': policy_hash(policy),
                    'copilot_ref': copy.deepcopy(copilot_ref), 'can_start_battle': False}
        result = assess_requirements(body, box, policy)
        result['copilot_ref'] = copy.deepcopy(copilot_ref)
        result['full_document_verified'] = True
        result['account_binding'] = 'local_profile_requires_current_account_confirmation'
        return result

    def screen_pool(self, profile_id, pool_ref, policy=None):
        """Filter all persisted requirements before any full document download.

        The public pool supplies requirements only. The account facts come
        exclusively from the versioned local RosterStore. A screen receipt is
        immutable and bound to both inputs and the declared user policy.
        """
        from .copilot_pool import CopilotPool, MAX_POOL_BYTES
        policy = normalize_policy(policy)
        if not isinstance(pool_ref, dict) or not isinstance(pool_ref.get('path'), str):
            raise ValueError('候选池引用无效')
        path = Path(pool_ref['path']).resolve()
        if not path.is_relative_to(self.directory) or path.parent.name != 'candidate-pools':
            raise ValueError('候选池必须位于当前工作流资料目录')
        pool = CopilotPool(path.parent.parent).read(pool_ref)
        box = self.current(profile_id)
        has_roster = box is not None
        box = box or {'profile_id': profile_id, 'box_revision': None, 'operators': [], 'complete': False}
        results = []
        counts = {key: 0 for key in ('satisfied', 'conditional', 'unknown', 'unsatisfied', 'needs_verified_support')}
        for candidate in [*pool['candidates'], *pool.get('unverified_candidates', [])]:
            if not isinstance(candidate, dict) or type(candidate.get('id')) is not int:
                raise ValueError('候选池条目缺少有效作业ID')
            result = (assess_metadata(candidate, box, policy) if 'schema_version' in candidate
                      else assess_requirements(candidate, box, policy, metadata=True))
            if candidate in pool.get('unverified_candidates', []):
                result['classification'] = 'unknown'
                result['missing_requirements'].append({'field': 'stage_identity', 'state': 'unknown'})
            if not has_roster:
                result['classification'] = 'unknown'
                result['missing_requirements'] = [{'field': 'roster', 'state': 'unknown'}]
            if candidate.get('available') is False and result['classification'] != 'unknown':
                result['classification'] = 'unsatisfied'
                result['missing_requirements'].append({'field': 'catalog_available',
                                                      'actual': False, 'state': 'insufficient'})
            counts[result['classification']] += 1
            result.update({key: copy.deepcopy(candidate.get(key)) for key in (
                'id', 'source', 'mystery_code', 'title', 'stage_name', 'catalog_source',
                'candidate_locator', 'schema_version', 'difficulty', 'requirements_complete')})
            result['requirements_sha256'] = requirements_hash(candidate)
            results.append(result)
        payload = {'schema': 1, 'kind': 'roster-pool-screen', 'profile_id': profile_id,
                   'box_revision': box.get('box_revision'), 'roster_ref': roster_ref(box),
                   'policy': policy, 'policy_sha256': policy_hash(policy),
                   'pool_ref': copy.deepcopy(pool_ref), 'coverage': copy.deepcopy(pool['coverage']),
                   'counts': counts, 'results': results, 'can_start_battle': False}
        raw = encode(payload)
        if len(raw) > MAX_POOL_BYTES:
            raise ValueError('阵容筛选证据超过大小上限')
        digest = hashlib.sha256(raw).hexdigest()
        directory = (self.directory / 'roster-screens').resolve()
        if not directory.is_relative_to(self.directory):
            raise ValueError('筛选证据目录超出资料目录')
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / (digest + '.json')
        if output.exists():
            if output.read_bytes() != raw:
                raise ValueError('已有筛选证据内容不一致')
        else:
            with output.open('xb') as stream:
                stream.write(raw)
        screen_ref = {'path': str(output), 'sha256': digest, 'pool_id': pool_ref['pool_id'],
                      'pool_sha256': pool_ref['sha256'], 'box_revision': box.get('box_revision'),
                      'policy_sha256': policy_hash(policy)}
        accepted = [item for item in results if item['classification'] == 'satisfied']
        projection = []
        for item in accepted[:10]:
            entry = {key: copy.deepcopy(item.get(key)) for key in (
                'id', 'source', 'mystery_code', 'title', 'stage_name', 'classification',
                'hard_elite_checks', 'ignored_requirements')}
            entry['operators'] = [{'name': row['name'], 'origin': row['origin']}
                                  for row in item['assignments']]
            projection.append(entry)
        classification = ('satisfied' if accepted else 'conditional' if counts['conditional']
                          else 'unknown' if counts['unknown'] or not results else 'unsatisfied')
        return {'ok': True, 'tool': 'screen_pool', 'classification': classification,
                'profile_id': profile_id, 'box_revision': box.get('box_revision'),
                'roster_ref': roster_ref(box), 'policy': policy, 'policy_sha256': policy_hash(policy),
                'pool_ref': copy.deepcopy(pool_ref), 'screen_ref': screen_ref, 'counts': counts,
                'coverage': copy.deepcopy(pool['coverage']), 'screened_count': len(results),
                'accepted': projection, 'accepted_count': len(accepted),
                'requires_full_document_verification': True, 'can_start_battle': False,
                'account_binding': 'local_profile_requires_current_account_confirmation',
                'hint': '先核对持有与精英化；等级、技能、模组阈值依用户策略忽略。缺人需验证实际助战。'}

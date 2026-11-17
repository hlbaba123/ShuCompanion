"""Deterministic account screening policy and auditable requirement projections.

Public candidate metadata describes the job, never the user's ownership. This
module performs no download or model call. Its satisfied result concerns roster
requirements under the declared policy; full action JSON still needs checking.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .maa_facts import _expired, _fresh_operator, _same, _valid_support, match_copilot


DEFAULT_POLICY = {'schema': 1, 'ignore_level': True, 'ignore_skill': True,
                  'ignore_module': True, 'high_rarity_elite': 2,
                  'max_support': 1, 'require_verified_support': True}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def normalize_policy(policy=None):
    if policy is None:
        return dict(DEFAULT_POLICY)
    if not isinstance(policy, dict) or set(policy) - set(DEFAULT_POLICY):
        raise ValueError('不支持的阵容筛选策略；精英化要求不能忽略')
    result = dict(DEFAULT_POLICY, **policy)
    for field in ('ignore_level', 'ignore_skill', 'ignore_module'):
        if type(result[field]) is not bool:
            raise ValueError('筛选策略必须使用布尔值')
    for field in ('schema', 'high_rarity_elite', 'max_support'):
        if type(result[field]) is not int or result[field] != DEFAULT_POLICY[field]:
            raise ValueError('不能放宽精英化或单助战约束')
    if result['require_verified_support'] is not True:
        raise ValueError('借用干员必须有实际可用的助战证据')
    return result


def policy_hash(policy):
    return hashlib.sha256(encode(normalize_policy(policy))).hexdigest()


def roster_ref(box):
    return {key: copy.deepcopy(box.get(key)) for key in (
        'profile_id', 'batch_id', 'box_revision', 'content_hash', 'source')}


def requirements_hash(candidate):
    return hashlib.sha256(encode({key: candidate.get(key) for key in (
        'operators', 'groups', 'schema_version', 'requirements_complete',
        'parse_issues', 'source_counts', 'difficulty', 'declared_difficulty',
        'inferred_difficulty', 'difficulty_basis', 'difficulty_proof')})).hexdigest()


def assess_metadata(candidate: dict, box: dict, policy=None) -> dict:
    """Typed code-delivery gate; legacy summaries cannot acquire a typed receipt."""
    result = assess_requirements(candidate, box, policy, metadata=True)
    issues = []
    if candidate.get('schema_version') != 2 or candidate.get('requirements_complete') is not True:
        issues.append({'field': 'requirements_complete', 'state': 'unknown'})
    inferred = candidate.get('difficulty_basis') == 'unique_public_stage_mode' and candidate.get('inferred_difficulty') in (1, 2, 3) and bool(candidate.get('difficulty_proof'))
    if not inferred and (type(candidate.get('difficulty')) is not int or candidate['difficulty'] not in (1, 2, 3)):
        issues.append({'field': 'difficulty', 'state': 'unknown'})
    if candidate.get('stage_match_verified') is False or candidate.get('difficulty_match') in ('unknown', 'incompatible'):
        issues.append({'field': 'stage_difficulty', 'state': 'unknown'})
    if candidate.get('parse_issues'):
        issues.extend({'field': 'metadata_parse', **item, 'state': 'unknown'}
                      for item in candidate['parse_issues'] if not (inferred and item.get('path') == 'difficulty'))
    if issues:
        result['classification'] = 'unknown'
        result['missing_requirements'].extend(issues)
    result.update({'id': candidate.get('id'), 'candidate_locator': copy.deepcopy(candidate.get('candidate_locator')),
                   'requirements_sha256': requirements_hash(candidate),
                   'requirements_complete': candidate.get('requirements_complete') is True,
                   'schema_version': candidate.get('schema_version'),
                   'difficulty': candidate.get('difficulty'), 'parse_issues': copy.deepcopy(candidate.get('parse_issues', [])),
                   'checked_at': time.time()})
    return result


def _audits(result, box, policy, now, supports):
    ownership, elite_checks, ignored, support_evidence = [], [], [], []
    for assignment in result.get('assignments', []):
        requirement = assignment.get('requirement', {})
        wanted = requirement.get('requirements') or {}
        rows = [row for row in box.get('operators', []) if _same(requirement, row)]
        raw = rows[0] if len(rows) == 1 else {}
        row = _fresh_operator(raw, box, now) if raw else {}
        sources = raw.get('field_sources', {})
        owned = row.get('owned')
        state = ('owned' if owned is True else 'explicit_absent' if owned is False
                 else 'complete_absent' if box.get('complete') is True and not raw
                 and not _expired(box.get('source'), now) else 'unknown')
        ownership.append({'slot': assignment['slot'], 'name': assignment['name'],
                          'owned': owned, 'state': state,
                          'source': copy.deepcopy(sources.get('owned', box.get('source')))})
        if assignment['origin'] == 'support':
            verified = [item for item in supports if _same(requirement, item)
                        and _valid_support(item, box, now, 300)]
            if verified:
                raw = next((item for item in verified if item.get('id') == assignment.get('operator_id')),
                           verified[0])
                row = _fresh_operator(raw, {'source': {}}, now)
                sources = {'elite': {key: copy.deepcopy(raw.get(key)) for key in (
                    'source', 'evidence_ref', 'observed_at', 'profile_id')}}
                support_evidence.append(dict(sources['elite'], slot=assignment['slot'], name=assignment['name']))
            else:
                row, sources = {}, {}
        required = wanted.get('elite', -1)
        high_rarity = row.get('rarity') in (5, 6)
        required = max(2 if high_rarity else -1, required)
        if required >= 0:
            elite_checks.append({'slot': assignment['slot'], 'name': assignment['name'],
                                 'required': required, 'actual': row.get('elite'),
                                 'rarity': row.get('rarity'), 'high_rarity_floor': high_rarity,
                                 'origin': assignment['origin'],
                                 'source': copy.deepcopy(sources.get('elite', box.get('source')))})
        for field, flag in (('level', 'ignore_level'), ('skill_level', 'ignore_skill'),
                            ('module', 'ignore_module'), ('module_level', 'ignore_module')):
            desired = wanted.get(field, -1)
            if policy[flag] and type(desired) is int and desired >= 0:
                ignored.append({'slot': assignment['slot'], 'name': assignment['name'],
                                'field': field, 'required': desired, 'reason': 'user_policy'})
    return ownership, elite_checks, ignored, support_evidence


def assess_requirements(document, box, policy=None, *, metadata=False, supports=(), now=None):
    """Screen requirements; unavailable support remains conditional, never accepted."""
    policy = normalize_policy(policy)
    now = time.time() if now is None else now
    if metadata:
        document = {'opers': document.get('operators'), 'groups': document.get('groups')}
    try:
        if not (document.get('opers') or document.get('groups')):
            raise ValueError('No explicit operator requirements')
        result = match_copilot(document, box, supports, policy=policy, now=now)
    except (TypeError, ValueError):
        result = {'classification': 'unknown', 'profile_id': box.get('profile_id'),
                  'box_revision': box.get('box_revision'), 'assignments': [],
                  'missing_requirements': [{'field': 'requirement_schema', 'state': 'unknown'}]}
    missing = result.get('missing_requirements', [])
    if (result['classification'] == 'unknown' and missing
            and all(fact.get('field') == 'support' for fact in missing)):
        result['classification'] = 'conditional'
    ownership, elite_checks, ignored, support_evidence = _audits(result, box, policy, now, supports)
    support_status = ('verified_available' if support_evidence else
                      'verification_required' if any(item.get('field') == 'support' for item in missing)
                      else 'not_required')
    result.update({'ownership_checks': ownership, 'hard_elite_checks': elite_checks,
                   'ignored_requirements': ignored, 'policy': policy,
                   'support_status': support_status, 'support_evidence': support_evidence,
                   'policy_sha256': policy_hash(policy), 'roster_ref': roster_ref(box),
                   'assessment_scope': 'roster_requirements_under_user_policy',
                   'requires_full_document_verification': metadata,
                   'can_start_battle': False})
    return result

"""Validate public activity identities and an evidenced, activity-specific order.

This module performs no IO and grants no game access. A catalog row is evidence
of catalog membership only. In particular neither a code prefix nor an opaque
stage-id suffix establishes difficulty, opening, or a navigation region.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


class _Invalid(ValueError):
    def __init__(self, code):
        self.code = code


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _refs(value):
    refs = value.get('source_refs', [])
    if not isinstance(refs, list) or any(not isinstance(r, (dict, str)) or not r for r in refs):
        raise _Invalid('missing_evidence')
    return copy.deepcopy(refs)


def _evidence(value):
    refs = _refs(value)
    if not refs:
        raise _Invalid('missing_evidence')
    return refs


def _natural(value):
    return tuple((1, int(p)) if p.isdigit() else (0, p.casefold())
                 for p in re.split(r'(\d+)', str(value)))


def normalize_structure(catalog: dict, proposal: dict) -> dict:
    """Return a JSON structure or ``{ok: False, code: ...}`` diagnostic.

    ``catalog.stages`` (or raw ``rows``/``data``) must be complete. Groups may
    cite catalog group IDs/labels, actual event/category labels, or an explicit
    researched membership with source_refs. No member silently disappears:
    excluded_stages needs a source and reason. Stage selectors accept stage_key,
    or stage_id plus difficulty/branch when the ID is ambiguous. Query mappings
    require per-row source_refs; absent mappings preserve the opaque ID and mark
    query_key_verified false if difficulty is unknown. ``0`` always means unknown.
    """
    try:
        return _normalize(catalog, proposal)
    except _Invalid as exc:
        return {'ok': False, 'code': exc.code, 'schema_version': 1}
    except (TypeError, ValueError, KeyError):
        return {'ok': False, 'code': 'invalid_structure', 'schema_version': 1}


def _normalize(catalog, proposal):
    if not isinstance(catalog, dict) or not isinstance(proposal, dict):
        raise _Invalid('missing_catalog')
    rows = catalog.get('stages', catalog.get('rows', catalog.get('data')))
    if not isinstance(rows, list) or not rows:
        raise _Invalid('missing_catalog')
    if (catalog.get('has_more') or catalog.get('catalog_complete') is False
            or catalog.get('total_stages', len(rows)) != len(rows)):
        raise _Invalid('catalog_truncated')
    activity_name = catalog.get('activity_name') or catalog.get('event_name')
    if not activity_name:
        names = {r.get('event', r.get('cat_two')) for r in rows if isinstance(r, dict)}
        if len(names) == 1:
            activity_name = names.pop()
    if not isinstance(activity_name, str) or not activity_name:
        raise _Invalid('missing_catalog')
    activity_id = catalog.get('activity_id') or activity_name
    edition = catalog.get('edition', '')
    refs = _refs(catalog) or [{'source': catalog.get('source', 'public_catalog'),
                              'raw_sha256': catalog.get('raw_sha256', '')}]
    stages, index = [], {}
    mappings = proposal.get('query_mappings', [])
    if not isinstance(mappings, list):
        raise _Invalid('invalid_structure')
    used_mappings = set()
    for raw in rows:
        if not isinstance(raw, dict) or not raw.get('stage_id'):
            raise _Invalid('unknown_stage_ids')
        sid = raw['stage_id']
        difficulty = raw.get('difficulty', 0)
        if type(difficulty) is not int or difficulty not in (0, 1, 2, 3):
            raise _Invalid('difficulty_ambiguous')
        branch = raw.get('branch', '')
        query_key = raw.get('query_key') or sid
        row_refs = _refs(raw) or copy.deepcopy(refs)
        mapping_matches = [(i, m) for i, m in enumerate(mappings)
                           if isinstance(m, dict) and m.get('stage_id') == sid
                           and ('branch' not in m or m['branch'] == branch)
                           and ('catalog_difficulty' not in m or m['catalog_difficulty'] == difficulty)]
        if len(mapping_matches) > 1:
            raise _Invalid('difficulty_ambiguous')
        if mapping_matches:
            i, mapping = mapping_matches[0]
            used_mappings.add(i)
            row_refs += _evidence(mapping)
            mapped = mapping.get('difficulty')
            if type(mapped) is not int or mapped not in (1, 2, 3) or not mapping.get('query_key'):
                raise _Invalid('difficulty_ambiguous')
            if difficulty and mapped != difficulty:
                raise _Invalid('difficulty_ambiguous')
            query_key, difficulty = mapping['query_key'], mapped
        identity = {'activity_id': activity_id, 'edition': raw.get('edition', edition),
                    'stage_id': sid, 'difficulty': difficulty, 'branch': branch}
        stage = dict(identity, stage_key=_hash(identity), level_id=raw.get('level_id'),
                     display_code=raw.get('display_code', raw.get('code', raw.get('cat_three', ''))),
                     query_key=query_key, query_key_verified=bool(difficulty and row_refs),
                     source_refs=row_refs, group_id=None,
                     phase_id=raw.get('phase_id'), navigation_region_id=raw.get('navigation_region_id'),
                     opening_status='unknown', account_unlock_verified=False,
                     difficulty_verified=bool(difficulty), branch_verified=bool(branch),
                     category=raw.get('category', raw.get('cat_one', '')), event=activity_name)
        stage['code'] = stage['display_code']
        if mapping_matches:
            stage['public_level_id'] = mapping.get('public_level_id')
            stage['difficulty_domain'] = copy.deepcopy(mapping.get('difficulty_domain', []))
            stage['difficulty_domain_complete'] = mapping.get('difficulty_domain_complete') is True
            stage['difficulty_domain_source_refs'] = copy.deepcopy(mapping['source_refs'])
        # Claims of availability need separate evidence; membership does not suffice.
        if raw.get('opening_source_refs'):
            stage['opening_status'] = raw.get('opening_status', 'unknown')
            stage['opening_source_refs'] = _evidence({'source_refs': raw['opening_source_refs']})
        key = stage['stage_key']
        if key in index:
            if index[key] != stage:
                raise _Invalid('difficulty_ambiguous')
            continue
        stages.append(stage)
        index[key] = stage
    if len(used_mappings) != len(mappings):
        raise _Invalid('unknown_stage_ids')

    def select(selector):
        if isinstance(selector, str):
            if selector in index:
                return selector
            matches = [s for s in stages if s['stage_id'] == selector]
        elif isinstance(selector, dict):
            if selector.get('stage_key') in index:
                candidate = index[selector['stage_key']]
                if any(k in selector and selector[k] != candidate[k]
                       for k in ('stage_id', 'difficulty', 'branch')):
                    raise _Invalid('unknown_stage_ids')
                return candidate['stage_key']
            matches = [s for s in stages if s['stage_id'] == selector.get('stage_id')
                       and all(k not in selector or s[k] == selector[k] for k in ('difficulty', 'branch'))]
        else:
            raise _Invalid('unknown_stage_ids')
        if not matches:
            raise _Invalid('unknown_stage_ids')
        if len(matches) != 1:
            raise _Invalid('difficulty_ambiguous')
        return matches[0]['stage_key']

    # Phase and navigation membership are independent researched dimensions.
    # A phase's opening date without members stays an unknown link; it never
    # attaches itself to stages by guessing from codes or group labels.
    unknown_links = copy.deepcopy(proposal.get('unknown_phase_links', catalog.get('unknown_phase_links', [])))
    for field, destination in (('phase_links', 'phase_id'),
                               ('navigation_regions', 'navigation_region_id')):
        for link in catalog.get(field, []) + proposal.get(field, []):
            link_refs = _evidence(link)
            public_zone = (field == 'navigation_regions' and link.get('basis') == 'public_zone_membership')
            if public_zone and any(not isinstance(ref, dict) or not isinstance(ref.get('path'), str)
                    or not ref['path'] or not isinstance(ref.get('sha256'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', ref['sha256']) is None for ref in link_refs):
                raise _Invalid('missing_evidence')
            identity = link.get(destination)
            if not identity:
                raise _Invalid('invalid_structure')
            members = link.get('stage_keys', link.get('stage_ids', link.get('members', [])))
            if not members:
                if field == 'phase_links':
                    unknown_links.append(copy.deepcopy(link))
                    continue
                raise _Invalid('missing_members')
            for member in members:
                stage = index[select(member)]
                if stage[destination] is not None and stage[destination] != identity:
                    raise _Invalid('membership_conflict')
                stage[destination] = identity
                stage['source_refs'] += copy.deepcopy(link_refs)
                if field == 'navigation_regions':
                    stage['navigation_verified'] = False
                    if public_zone:
                        stage['navigation_basis'] = 'public_zone_membership'
                if destination == 'phase_id' and link.get('opening_status') in ('open', 'closed', 'ended', 'permanent', 'unknown'):
                    stage['opening_status'] = link['opening_status']
                    stage['opening_source_refs'] = copy.deepcopy(link_refs)

    excluded = []
    for exclusion in proposal.get('excluded_stages', []):
        _evidence(exclusion)
        if not exclusion.get('reason'):
            raise _Invalid('missing_evidence')
        key = select(exclusion)
        if key in excluded:
            raise _Invalid('duplicate_members')
        excluded.append(key)
    actual_groups = catalog.get('groups', [])
    groups_proposed = proposal.get('groups', actual_groups)
    if not groups_proposed:
        groups_proposed = [{'group_id': 'catalog-' + _hash(activity_name)[:16],
                            'label': activity_name, 'stage_keys': [s['stage_key'] for s in stages
                                                                   if s['stage_key'] not in excluded]}]
    groups, assigned, group_ids = [], set(), set()
    labels = {activity_name} | {s['category'] for s in stages if s['category']}
    for proposed in groups_proposed:
        gid, label = proposed.get('group_id'), proposed.get('label')
        if not gid or not label or gid in group_ids:
            raise _Invalid('invalid_group')
        actual = next((g for g in actual_groups if g.get('group_id') == gid and g.get('label') == label), None)
        group_refs = _refs(proposed)
        if label not in labels and actual is None and not group_refs:
            raise _Invalid('unverified_group')
        selectors = proposed.get('stage_keys', proposed.get('stage_ids', proposed.get('members', [])))
        if not isinstance(selectors, list) or not selectors:
            raise _Invalid('missing_members')
        keys = [select(member) for member in selectors]
        if len(keys) != len(set(keys)) or any(k in assigned or k in excluded for k in keys):
            raise _Invalid('duplicate_members')
        if actual:
            actual_selectors = actual.get('stage_keys', actual.get('stage_ids', actual.get('members', [])))
            if set(keys) != {select(s) for s in actual_selectors}:
                raise _Invalid('invalid_group_members')
        for key in keys:
            index[key]['group_id'] = gid
        assigned.update(keys)
        group_ids.add(gid)
        groups.append({'group_id': gid, 'label': label, 'stage_keys': keys,
                       'catalog_ordinal': actual_groups.index(actual)+1 if actual else None,
                       'source_refs': group_refs or (_refs(actual) if actual else []) or copy.deepcopy(refs)})
    if assigned | set(excluded) != set(index):
        raise _Invalid('missing_members')
    # Topological sorting honors real prerequisite edges; natural code order
    # resolves unconstrained nodes. Group order remains the reviewed proposal.
    constraints = catalog.get('order_constraints', []) + proposal.get('order_constraints', [])
    edges = []
    group_position = {g['group_id']: n for n, g in enumerate(groups)}
    for constraint in constraints:
        _evidence(constraint)
        before, after = select(constraint['before']), select(constraint['after'])
        if before in excluded or after in excluded:
            continue
        if group_position[index[before]['group_id']] > group_position[index[after]['group_id']]:
            raise _Invalid('order_conflict')
        edges.append((before, after))
    for group in groups:
        remaining, ordered = set(group['stage_keys']), []
        while remaining:
            ready = [k for k in remaining if not any(a in remaining and b == k for a, b in edges)]
            if not ready:
                raise _Invalid('order_cycle')
            key = min(ready, key=lambda k: (_natural(index[k]['display_code']),
                                           index[k]['difficulty'], _natural(index[k]['branch']), k))
            remaining.remove(key)
            ordered.append(key)
        group['stage_keys'] = ordered
    return {'ok': True, 'schema_version': 1, 'activity_id': activity_id,
            'activity_name': activity_name, 'edition': edition, 'stages': stages, 'groups': groups,
            'excluded_stage_keys': excluded, 'exclusions': copy.deepcopy(proposal.get('excluded_stages', [])),
            'unknown_phase_links': unknown_links,
            'source_refs': refs, 'catalog_complete': True, 'account_unlock_verified': False,
            'opening_status_verified': False}


def ordered_stage_keys(structure: dict) -> list[str]:
    """Flatten validated groups, rejecting duplicate or omitted members."""
    if not isinstance(structure, dict) or not structure.get('ok'):
        raise ValueError('invalid_structure')
    keys = [k for group in structure['groups'] for k in group['stage_keys']]
    expected = {s['stage_key'] for s in structure['stages']} - set(structure.get('excluded_stage_keys', []))
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError('invalid_structure_members')
    return keys

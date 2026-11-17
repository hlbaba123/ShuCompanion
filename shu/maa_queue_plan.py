"""Pure, evidence-bound multi-job target selection and partition planning.

This module performs no IO and grants no input authority. Callers must rehash
the structure, prepared records and complete documents, and revalidate current
task/source/Box at the submission boundary. A well-formed dictionary is not
proof that any of those external facts are current.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .leader_policy import research_stage_codes
from .maa_queue_types import QueuePlan


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _ref(value):
    if (not isinstance(value, dict) or not _text(value.get('path'))
            or not isinstance(value.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])):
        raise ValueError('queue_reference_invalid')
    return {'path': value['path'], 'sha256': value['sha256']}


def _hash_evidence(references):
    """Metadata URLs may accompany pinned bytes, but cannot replace them."""
    if not isinstance(references, list) or not references:
        raise ValueError('queue_stage_evidence_invalid')
    pinned = []
    for reference in references:
        if isinstance(reference, dict) and {'path', 'sha256'}.intersection(reference):
            pinned.append(_ref(reference))
    if not pinned:
        raise ValueError('queue_stage_evidence_unpinned')
    return pinned


def make_target_key(stage_key: str, difficulty: int) -> str:
    if not _text(stage_key) or type(difficulty) is not int or difficulty not in (1, 2):
        raise ValueError('queue_target_identity_invalid')
    return hashlib.sha256(f'{stage_key}\0{difficulty}'.encode('utf-8')).hexdigest()


class ImportScopeAmbiguous(ValueError):
    """The durable catalog provides choices the user can resolve."""


def _stage_code(row):
    return str(row.get('stage_code') or row.get('display_code') or '').upper()


def _stage_label(row):
    return _stage_code(row) + {1:'（普通）',2:'（突袭）'}.get(row.get('difficulty'),'')


def _typed_import_stages(rows, scope):
    from .task_directive import normalize_import_scope

    scope = normalize_import_scope(scope)
    kind, mode = scope['kind'], scope['difficulty']
    eligible = [row for row in rows if mode in (None,3) or row.get('difficulty') == mode]
    if kind == 'all':
        if not eligible:
            raise ImportScopeAmbiguous('已核对活动目录，没有符合当前难度的导入关卡。请确认要导入的关卡和难度。')
        return eligible
    if kind == 'stages':
        requested = set(scope['stage_codes'])
        selected = [row for row in eligible if _stage_code(row) in requested]
        if requested - {_stage_code(row) for row in selected}:
            raise ImportScopeAmbiguous('所选关卡或难度不在已核实的目录内。请给出要导入的关卡代码和难度。')
        return selected
    if kind == 'ordinal':
        groups = {}
        for row in eligible:
            key = (row.get('group_id'),row.get('difficulty'))
            groups.setdefault(key,[]).append(row)
        hint = scope['group_hint']
        ordinal = scope['ordinal']
        matching = [group for group in groups.values() if not hint or any(
            hint == row.get('group_label') or hint == row.get('group_id') for row in group)]
        choices = [group[ordinal-1] for group in matching if len(group)>=ordinal]
        if choices and len({_stage_code(row) for row in choices}) == 1:
            return choices
        labels = '、'.join(dict.fromkeys(map(_stage_label,choices)))
        suffix = '。' if not labels else f'，目录中的候选是 {labels}。'
        raise ImportScopeAmbiguous(f'你说的第 {ordinal} 关属于哪个分区{suffix}请回复要导入的关卡代码和难度。')
    raise ImportScopeAmbiguous('请说明要导入哪一关，或是否将活动作业全部加入多作业队列。')


def select_research_stages(rows, contract):
    """Query scope is independent of the exact GUI import selection."""
    from .chapter_scope import select_chapter
    if contract.get('import_scope') is None:
        return select_import_stages(rows, contract)
    rows = select_chapter(rows, contract.get('chapter_scope'))
    requested = set(contract.get('target_stage_codes') or ())
    selected = [row for row in rows if not requested or _stage_code(row) in requested]
    if not selected or requested - {_stage_code(row) for row in selected}:
        raise ImportScopeAmbiguous('部分查询关卡不在已核实的目录中。请确认查询范围。')
    scope = contract['import_scope']
    if requested and (scope.get('kind')=='all'
                      or scope.get('kind')=='stages' and set(scope.get('stage_codes',[]))==requested):
        mode=scope.get('difficulty')
        if mode in (1,2):
            selected=[row for row in selected if row.get('difficulty')==mode]
    if not selected:
        raise ImportScopeAmbiguous('目录没有符合所选难度的查询关卡。')
    return copy.deepcopy(selected)


def select_import_stages(rows: list[dict], contract: dict) -> list[dict]:
    """Select a contract-exact ordered subset, without inferring page regions.

    Rows can be normalized catalog stages or ImportPreparation.options steps.
    This only filters scope: opening/navigation/prepared evidence is checked by
    build_queue_plan and the production persistence/submission boundaries.
    """
    if not isinstance(rows, list) or not isinstance(contract, dict):
        raise ValueError('queue_selection_invalid')
    if any(not isinstance(row, dict) or not _text(row.get('stage_key')) for row in rows):
        raise ValueError('queue_stage_identity_invalid')
    if len({row['stage_key'] for row in rows}) != len(rows):
        raise ValueError('queue_stage_identity_duplicate')
    from .chapter_scope import select_chapter
    rows = select_chapter(rows, contract.get('chapter_scope'))
    scope = contract.get('import_scope')
    if scope is not None:
        if scope.get('kind') == 'all' and contract.get('target_stage_codes'):
            requested = {code.upper() for code in contract['target_stage_codes']}
            rows = [row for row in rows if _stage_code(row) in requested]
            if requested - {_stage_code(row) for row in rows}:
                raise ImportScopeAmbiguous('部分查询关卡不在可导入目录中，请确认查询范围。')
        return copy.deepcopy(_typed_import_stages(rows,scope))
    from .task_directive import ordinal_import_request
    ordinal = ordinal_import_request(contract.get('user_request') or '')
    if ordinal is not None:
        return copy.deepcopy(_typed_import_stages(rows,{'kind':'ordinal','stage_codes':[],
            'ordinal':ordinal,'difficulty':None,'group_hint':''}))
    declared = contract.get('target_stage_codes') or []
    if not isinstance(declared, list) or any(not _text(code) for code in declared):
        raise ValueError('queue_scope_invalid')
    declared = {code.upper() for code in declared}
    literal_contract = {'user_request':contract.get('user_request', ''),
                        'goal':'', 'task_spec':{}}
    literal = {code.upper() for code in research_stage_codes(literal_contract)}
    scope = {code.upper() for code in research_stage_codes(contract)}
    if declared and literal and declared != literal:
        raise ValueError('queue_scope_conflict')
    if 'target_stage_codes' in contract:
        wanted = scope
    else:
        wanted = literal or scope
    code = lambda row: str(row.get('stage_code') or row.get('display_code') or '').upper()
    if wanted - {code(row) for row in rows}:
        raise ValueError('queue_scope_missing_stage')
    selected = [row for row in rows if not wanted or code(row) in wanted]
    request = contract.get('user_request') or contract.get('goal') or ''
    if not isinstance(request, str):
        raise ValueError('queue_scope_invalid')
    # Only an explicit single mode narrows the catalog. An unspecified mode
    # retains both real variants instead of selecting the first match.
    normal, raid = '普通' in request, '突袭' in request
    if re.search(r'(?:不要|不用|不打|不选|不查|不需要|不是|无需|排除|不含|非|不)\s*'
                 r'(?:导入|加载|选择|查询|查找)?\s*(?:普通|突袭)'
                 r'|(?:普通|突袭)\s*(?:都)?(?:不要|不用|不导入)', request):
        # A negative mention is not a positive request for both modes. The
        # current contract has no typed mode field; ask rather than invent NLU.
        raise ValueError('queue_mode_selection_ambiguous')
    if normal != raid:
        selected = [row for row in selected if row.get('difficulty') == (1 if normal else 2)]
    if not selected or wanted - {code(row) for row in selected}:
        raise ValueError('queue_scope_missing_mode')
    return copy.deepcopy(selected)


def _ordered(structure):
    if (not isinstance(structure, dict) or structure.get('ok') is not True
            or structure.get('catalog_complete') is not True):
        raise ValueError('queue_catalog_incomplete')
    stages, groups = structure.get('stages'), structure.get('groups')
    if (not isinstance(stages, list) or not stages or not isinstance(groups, list)
            or any(not isinstance(stage, dict) or not _text(stage.get('stage_key')) for stage in stages)):
        raise ValueError('queue_structure_invalid')
    index = {stage['stage_key']: stage for stage in stages}
    excluded = structure.get('excluded_stage_keys', [])
    if (not isinstance(excluded, list) or any(not _text(key) for key in excluded)
            or len(set(excluded)) != len(excluded)
            or len(index) != len(stages) or set(excluded) - set(index)):
        raise ValueError('queue_structure_members_invalid')
    ordered, group_index = [], {}
    for group in groups:
        if (not isinstance(group, dict) or not _text(group.get('group_id'))
                or group['group_id'] in group_index or not _text(group.get('label'))
                or not isinstance(group.get('stage_keys'), list) or not group['stage_keys']):
            raise ValueError('queue_group_invalid')
        group_index[group['group_id']] = group
        for key in group['stage_keys']:
            if not _text(key) or key not in index or index[key].get('group_id') != group['group_id']:
                raise ValueError('queue_group_members_invalid')
            ordered.append(key)
    if (len(ordered) != len(set(ordered)) or set(ordered) != set(index) - set(excluded)):
        raise ValueError('queue_structure_members_invalid')
    exclusions = structure.get('exclusions', [])
    if not isinstance(exclusions, list) or any(not isinstance(item, dict) for item in exclusions):
        raise ValueError('queue_exclusions_invalid')
    for key in excluded:
        stage = index[key]
        matches = [item for item in exclusions if item.get('stage_key') == key
            or item.get('stage_id') == stage.get('stage_id') and not item.get('stage_key')
            and ('difficulty' not in item or item['difficulty'] == stage.get('difficulty'))]
        if len(matches) != 1 or not _text(matches[0].get('reason')):
            raise ValueError('queue_exclusion_unverified')
        _hash_evidence(matches[0].get('source_refs'))
    return [index[key] for key in ordered], group_index


def ordered_import_candidates(structure):
    stages, groups = _ordered(structure)
    return [{**stage,'group_label':groups[stage['group_id']]['label'],
             'group_ordinal':groups[stage['group_id']].get('catalog_ordinal')} for stage in stages]


def _stage_ready(stage):
    mode = stage.get('difficulty')
    if (type(mode) is not int or mode not in (1, 2)
            or stage.get('difficulty_verified') is not True
            or stage.get('query_key_verified') is not True
            or not _text(stage.get('query_key')) or not _text(stage.get('display_code'))):
        raise ValueError('queue_stage_mode_unverified')
    if stage.get('opening_status') not in {'open', 'permanent', 'ended'}:
        raise ValueError('queue_opening_unverified')
    if not stage.get('opening_source_refs'):
        raise ValueError('queue_opening_unverified')
    if not _text(stage.get('navigation_region_id')) or not stage.get('source_refs'):
        raise ValueError('queue_navigation_scope_unverified')
    # Hashable public refs, not labels or a model's guessed code prefix.
    for field in ('source_refs', 'opening_source_refs'):
        _hash_evidence(stage[field])


def _prepared_identity(result, context):
    if not isinstance(result, dict) or not isinstance(result.get('candidate_record'), dict):
        raise ValueError('queue_preparation_invalid')
    record = result['candidate_record']
    if any(record.get(field) != context[field] for field in ('task_id', 'revision', 'epoch')):
        raise ValueError('queue_preparation_stale')
    goal, binding = record.get('goal_stage'), record.get('source_binding')
    if (not isinstance(goal, dict) or not isinstance(binding, dict)
            or goal != result.get('goal_stage') or goal.get('source_binding') != binding
            or any(goal.get(field) != context[field] for field in ('task_id', 'revision', 'epoch'))):
        raise ValueError('queue_preparation_binding_invalid')
    resolved = goal.get('resolved_stage')
    if not isinstance(resolved, dict) or not _text(resolved.get('stage_key')):
        raise ValueError('queue_preparation_stage_invalid')
    if (binding.get('slot') not in ('primary', 'backup') or not _text(binding.get('source_task_id'))
            or type(binding.get('source_revision')) is not int or binding['source_revision'] < 1
            or not _text(binding.get('step_id'))):
        raise ValueError('queue_preparation_source_invalid')
    contract = context['contract']
    source_id = contract.get('source_task_id') or context['task_id']
    expected_revision = context['revision'] if source_id == context['task_id'] else contract.get('source_revision')
    if (binding['source_task_id'] != source_id
            or expected_revision is not None and binding['source_revision'] != expected_revision):
        raise ValueError('queue_preparation_source_mismatch')
    _ref(binding.get('evidence_ref'))
    _ref(goal.get('evidence_ref'))
    _ref(result.get('candidate_ref'))
    return resolved['stage_key'], binding['slot']


def _target(result, stage, context, batch_key):
    record, goal = result['candidate_record'], result['goal_stage']
    binding, resolved = record['source_binding'], goal['resolved_stage']
    for field in ('stage_key', 'stage_id', 'query_key', 'display_code', 'difficulty'):
        if resolved.get(field) != stage.get(field):
            raise ValueError('queue_preparation_stage_mismatch')
    mode, source_mode = stage['difficulty'], result.get('source_difficulty')
    if type(source_mode) is not int or source_mode not in (1, 2, 3) or not source_mode & mode:
        raise ValueError('queue_source_difficulty_unverified')
    document = _ref(record)
    verified, assessment, roster = result.get('verified_copilot'), result.get('assessment'), binding.get('roster_ref')
    cid = record.get('copilot_id')
    if (not isinstance(verified, dict) or not isinstance(cid, str)
            or not re.fullmatch(r'[1-9][0-9]*', cid)
            or any(verified.get(key) != record.get(key) for key in
                ('copilot_id', 'path', 'sha256', 'stage_name', 'source_url'))
            or verified.get('mystery_code') != 'prts://' + cid
            or verified.get('code') != stage['display_code'] or verified.get('difficulty') != mode
            or record.get('difficulty') != mode or goal.get('difficulty') != mode
            or goal.get('code') != stage['display_code'] or goal.get('query') != stage['query_key']
            or goal.get('stage_id') != record.get('stage_name')
            or record.get('source_url') != 'https://prts.maa.plus/copilot/get/' + cid):
        raise ValueError('queue_preparation_code_mismatch')
    if (not isinstance(roster, dict) or not roster or record.get('roster_ref') != roster
            or not isinstance(assessment, dict) or assessment.get('classification') != 'satisfied'
            or assessment.get('full_document_verified') is not True
            or assessment.get('roster_ref') != roster or assessment.get('copilot_ref') != document):
        raise ValueError('queue_preparation_box_unverified')
    target = {'target_key': make_target_key(stage['stage_key'], mode), 'stage_key': stage['stage_key'],
        'stage_id': record['stage_name'], 'stage_code': stage['display_code'],
        'difficulty': mode, 'source_difficulty': source_mode, 'batch_key': batch_key,
        'slot': binding['slot'], 'candidate_ref': _ref(result['candidate_ref']),
        'source_binding_ref': _ref(binding['evidence_ref']), 'document_ref': document,
        'roster_ref': copy.deepcopy(roster), 'mystery_code': verified['mystery_code']}
    if 'source_semantic_sha256' in result:
        semantic = result['source_semantic_sha256']
        if not isinstance(semantic, str) or not re.fullmatch(r'[0-9a-f]{64}', semantic):
            raise ValueError('queue_source_semantic_hash_invalid')
        target['source_semantic_sha256'] = semantic
    if 'expected_copilot_id' in result:
        expected_id = result['expected_copilot_id']
        if type(expected_id) is not int or expected_id not in (0, int(cid)):
            raise ValueError('queue_expected_copilot_id_invalid')
        target['expected_copilot_id'] = expected_id
    return target


def build_queue_plan(context: dict, structure_ref: dict, structure: dict,
                     prepared: list[dict], *, source_stage_keys=None) -> QueuePlan:
    """Bind every selected target to one current prepared main/explicit backup.

    Missing candidates or unknown pages block the plan, never silently reduce
    its coverage. Closed stages belong in evidenced structure exclusions. A
    historic ended window does not preclude preparation of a permanent stage's
    code, but its current execution availability remains explicitly unknown.
    The caller validates explicit slot choice with the existing import policy;
    this pure plan neither grants GUI authority nor proves page navigability.
    """
    if (not isinstance(context, dict) or not _text(context.get('task_id'))
            or any(type(context.get(k)) is not int or context[k] < 1 for k in ('revision', 'epoch'))):
        raise ValueError('queue_context_invalid')
    contract = context.get('contract') or {}
    if (not isinstance(contract, dict) or contract.get('current_deliverable') != 'maa_import'
            or (contract.get('task_spec') or {}).get('kind') != 'import'
            or contract.get('allow_start') is not False or type(contract.get('attempt_limit')) is not int
            or contract['attempt_limit'] != 0):
        raise ValueError('queue_import_scope_invalid')
    reference = _ref(structure_ref)
    ordered, groups = _ordered(structure)
    if contract.get('source_task_id') and (contract.get('import_scope') or {}).get('kind') == 'all':
        if (not isinstance(source_stage_keys,list) or not source_stage_keys
                or len(source_stage_keys) != len(set(source_stage_keys))
                or set(source_stage_keys) - {stage['stage_key'] for stage in ordered}):
            raise ValueError('queue_source_scope_unverified')
        ordered = [stage for stage in ordered if stage['stage_key'] in source_stage_keys]
    selected = select_import_stages([{**stage,'group_label':groups[stage['group_id']]['label'],
                                     'group_ordinal':groups[stage['group_id']].get('catalog_ordinal')}
                                     for stage in ordered], contract)
    allowed = {stage['stage_key'] for stage in selected}
    slot = contract.get('selected_slot', 'primary')
    if slot not in ('primary', 'backup') or not isinstance(prepared, list):
        raise ValueError('queue_preparation_selection_invalid')
    preparations = {}
    for result in prepared:
        identity = _prepared_identity(result, context)
        if identity[0] not in allowed or identity in preparations:
            raise ValueError('queue_preparation_duplicate_or_out_of_scope')
        preparations[identity] = result
    targets, batches, previous_partition = [], [], None
    for stage in selected:
        _stage_ready(stage)
        selection = preparations.get((stage['stage_key'], slot))
        if selection is None:
            raise ValueError('queue_' + slot + '_preparation_missing:' + stage['stage_key'])
        partition = (stage['navigation_region_id'], stage.get('phase_id'), stage['difficulty'])
        if partition != previous_partition:
            batch_key = _hash({'activity_id': structure.get('activity_id'), 'edition': structure.get('edition'),
                              'partition': partition, 'ordinal': len(batches)})
            scope = _ref(stage.get('navigation_scope_ref', reference))
            batches.append({'batch_key': batch_key,
                'label': groups[stage['group_id']]['label'] + (' · 普通' if stage['difficulty'] == 1 else ' · 突袭'),
                'stage_keys': [], 'target_keys': [], 'navigation_scope_ref': scope,
                'navigation_verified': False})
            previous_partition = partition
        batch = batches[-1]
        target = _target(selection, stage, context, batch['batch_key'])
        if targets and target['roster_ref'] != targets[0]['roster_ref']:
            raise ValueError('queue_box_changed_between_preparations')
        targets.append(target)
        batch['stage_keys'].append(stage['stage_key'])
        batch['target_keys'].append(target['target_key'])
    exclusions = copy.deepcopy(structure.get('exclusions', []))
    if not isinstance(exclusions, list) or any(not isinstance(item, dict) for item in exclusions):
        raise ValueError('queue_exclusions_invalid')
    deferred = [copy.deepcopy(item) for item in exclusions if item.get('opens_at')]
    for stage in selected:
        if stage.get('opening_status') == 'ended':
            deferred.append({'stage_key': stage['stage_key'], 'stage_code': stage['display_code'],
                'opening_status': 'ended', 'status': 'execution_opening_unknown',
                'blocks_import': False, 'source_refs': copy.deepcopy(stage['opening_source_refs']),
                'reason': '历史开放时间已结束；仅准备作业导入，当前常驻/复刻与账号解锁须在执行前复核。'})
    plan = {'schema': 'maa_queue_plan/v1', 'task_id': context['task_id'],
        'revision': context['revision'], 'epoch': context['epoch'], 'structure_ref': reference,
        'targets': targets, 'batches': batches, 'exclusions': exclusions, 'deferred': deferred}
    plan['plan_sha256'] = _hash(plan)
    return plan

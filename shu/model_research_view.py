"""Small model-facing projections of complete, separately persisted research.

These functions perform no IO, verification or mutation. They preserve actual
identities and unresolved facts; they never invent a successful classification.
The returned objects are views, never receipts for completion/admission. Full
operator audits and observation envelopes stay in the original local results.
"""
from __future__ import annotations

import copy
import json


_FAILURE_FIELDS = (
    'ok', 'tool', 'status', 'code', 'summary', 'error', 'error_code', 'http_status',
    'reason', 'errors', 'tool_errors', 'unknowns', 'unresolved', 'proposals',
    'pending_objectives', 'unmet_requirements', 'blocked_actions', 'cancelled',
)


def _pick(value, fields):
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def _roster_reference(value):
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _pick(value, ('profile_id', 'batch_id', 'box_revision', 'content_hash'))
    source = value.get('source')
    if isinstance(source, dict):
        result['source'] = _pick(source, ('kind', 'observed_at', 'expires_at', 'evidence_ref', 'scan_id'))
    elif source is not None:
        result['source'] = copy.deepcopy(source)
    return result


def _assessment(value):
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _pick(value, _FAILURE_FIELDS + (
        'classification', 'profile_id', 'box_revision', 'policy', 'policy_sha256',
        'evidence_ref', 'support_status', 'support_evidence', 'account_binding',
        'assessment_scope', 'requires_full_document_verification', 'full_document_verified',
        'can_start_battle', 'missing_requirements',
    ))
    if 'roster_ref' in value:
        result['roster_ref'] = _roster_reference(value['roster_ref'])
    ignored = value.get('ignored_requirements')
    if isinstance(ignored, list) and ignored:
        counts = {}
        for row in ignored:
            field = row.get('field', 'unknown') if isinstance(row, dict) else 'unknown'
            counts[field] = counts.get(field, 0) + 1
        result['ignored_requirement_counts'] = counts
    # The separately persisted assessment references its full candidate and
    # every per-operator source. Replaying that recursive tree adds no decision.
    return result


def _stage_resolution(value):
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _pick(value, ('query', 'source', 'raw_sha256', 'has_more', 'total_matches'))
    if isinstance(value.get('levels'), list):
        result['levels'] = [_pick(row, ('code', 'stage_id', 'level_id'))
                            if isinstance(row, dict) else copy.deepcopy(row) for row in value['levels']]
    return result


def _pool(value):
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _pick(value, _FAILURE_FIELDS + ('pool_ref', 'coverage', 'cached', 'hint'))
    if 'stage_resolution' in value:
        result['stage_resolution'] = _stage_resolution(value['stage_resolution'])
    return result


def _catalog(value):
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _pick(value, _FAILURE_FIELDS + (
        'event_name', 'source', 'raw_sha256', 'stage_catalog_ref', 'total_stages',
        'returned_stages', 'has_more', 'catalog_complete', 'total_events',
        'event_candidates', 'has_more_events', 'opening_status_verified',
        'query', 'cached', 'reused_from', 'observation_ref',
    ))
    if isinstance(value.get('stages'), list):
        result['stages'] = [_pick(row, (
            'code', 'stage_id', 'level_id', 'name', 'stage_key', 'display_code',
            'difficulty', 'branch', 'phase_id', 'group_id', 'category', 'stage_kind',
            'opening_status', 'opens_at', 'ends_at', 'account_unlock_status',
        ))
                            if isinstance(row, dict) else copy.deepcopy(row) for row in value['stages']]
    return result


def compact_catalog_receipt(result):
    """Keep the catalog's decisions, identities and exact durable references.

    Public raw metadata is already retained in ``stage_catalog_ref``; replaying
    it for every stage and then for every role is not additional evidence.
    Structured mode/phase/opening facts and every gap remain model-visible.
    """
    if not isinstance(result, dict):
        return copy.deepcopy(result)
    view = copy.deepcopy(result)
    if 'activity_catalog' in view:
        view['activity_catalog'] = _catalog(view['activity_catalog'])
    view.update(model_view_schema=1, model_view_only=True, full_local_evidence_retained=True)
    return view


def compact_planned_receipt(result):
    """Project plan receipts without erasing stage outcomes or deferred scope.

    The planned research receipt is already a small status table, so unknown
    fields are preserved. Only a nested authoritative research/catalog result
    receives its existing typed projection; paths/hashes are never shortened.
    """
    if not isinstance(result, dict):
        return copy.deepcopy(result)
    view = copy.deepcopy(result)
    if 'activity_catalog' in view:
        view['activity_catalog'] = _catalog(view['activity_catalog'])
    if 'assessments' in view or 'verified_copilots' in view:
        evidence = compact_research_result(view)
        for field in ('assessments', 'assessment_contexts', 'verified_copilots', 'candidate_pools'):
            if field in evidence:
                view[field] = evidence[field]
    view.update(model_view_schema=1, model_view_only=True, full_local_evidence_retained=True)
    return view


def compact_dialogue_context(context):
    """Remove duplicate display prose from typed context, never slice JSON.

    Callers retain the original facts locally. Opaque caller prose cannot be
    safely reinterpreted, so it is returned whole. This function does not route
    intent, truncate constraints or turn an old receipt into current evidence.
    """
    if isinstance(context, str):
        try:
            parsed = json.loads(context)
        except (ValueError, TypeError, RecursionError):
            return context
        if not isinstance(parsed, dict):
            return context
        context = parsed
    if not isinstance(context, dict):
        return copy.deepcopy(context)
    view = copy.deepcopy(context)
    for field in ('task_contract', 'contract'):
        if isinstance(view.get(field), dict):
            view[field].pop('reply', None)
    if isinstance(view.get('registered_job_output'), dict):
        view['registered_job_output'] = compact_planned_receipt(view['registered_job_output'])
    return view


def _delivery_summary(summary, verified, assessments):
    """Deduplicate only exact program delivery lines already represented below."""
    if not isinstance(summary,str):return copy.deepcopy(summary)
    lines=summary.splitlines()
    if not lines or lines[0] not in {'已核验的作业结果：','已取得以下真实资料；整项任务尚未完成：'}:
        return summary
    repeated=set()
    for item in verified or []:
        if not isinstance(item,dict):continue
        if isinstance(item.get('path'),str):repeated.add('  JSON：'+item['path'])
        if isinstance(item.get('detail_url'),str):repeated.add('  来源：'+item['detail_url'])
        assessment=(assessments or {}).get(item.get('copilot_id')) or {}
        label='符合本地干员硬性条件' if assessment.get('classification')=='satisfied' else '阵容尚未核对'
        if item.get('code') and item.get('mystery_code'):
            repeated.add(f"- {item['code']}：{item['mystery_code']}（{label}）")
    return '\n'.join([lines[0]]+[line for line in lines[1:] if line not in repeated])


def compact_research_result(result):
    """Project a role receipt while preserving every output and known gap.

    Observation-envelope references are counted, not replayed. The complete
    candidate, assessment, candidate-pool and activity-catalog references remain
    available with exact paths/hashes. Common roster/policy inputs are shared
    once in ``assessment_contexts`` rather than copied for every stage.
    """
    if not isinstance(result, dict):
        return copy.deepcopy(result)
    if result.get('model_view_only') is True and result.get('model_view_schema') == 1:
        return copy.deepcopy(result)
    view = _pick(result, _FAILURE_FIELDS + (
        'adoption', 'source_dispatch_id', 'dispatch_id', 'research_dispatch_id',
        'task_id', 'epoch', 'revision', 'input_task', 'task', 'created_at',
        'candidate_refs', 'activity_scope', 'missing_stage_codes', 'unmatched_stage_codes',
    ))
    verified = result.get('verified_copilots')
    if isinstance(verified, list):
        view['verified_copilots'] = [
            _pick(row, ('code', 'copilot_id', 'mystery_code', 'path', 'sha256', 'stage_name', 'source_url', 'detail_url'))
            if isinstance(row, dict) else copy.deepcopy(row) for row in verified]
    assessments = result.get('assessments')
    if 'summary' in view:
        view['summary']=_delivery_summary(view['summary'],verified,assessments)
    if isinstance(assessments, dict):
        view['assessments'] = {}
        contexts = []; by_context = {}
        for identifier, original in assessments.items():
            item = _assessment(original)
            if isinstance(item, dict):
                shared_fields=('policy','roster_ref','assessment_scope','requires_full_document_verification',
                               'full_document_verified','can_start_battle','account_binding','support_status')
                context = _pick(item, ('profile_id', 'box_revision', 'policy_sha256') + shared_fields)
                if context:
                    key = json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    if key not in by_context:
                        by_context[key] = 'assessment-context-' + str(len(contexts) + 1)
                        contexts.append({'context_id': by_context[key], **context})
                    item['context_id'] = by_context[key]
                    for field in shared_fields:item.pop(field,None)
                # The policy already states which thresholds were ignored;
                # per-slot audit counts do not change this stage decision.
                item.pop('ignored_requirement_counts',None)
                if item.get('missing_requirements')==[]:item.pop('missing_requirements')
            view['assessments'][identifier] = item
        if contexts:
            view['assessment_contexts'] = contexts
    pools = result.get('candidate_pools')
    if isinstance(pools, list):
        view['candidate_pools'] = [_pool(pool) for pool in pools]
    if 'activity_catalog' in result:
        view['activity_catalog'] = _catalog(result['activity_catalog'])
    required = (result.get('activity_scope') or {}).get('required_stage_codes') or []
    if required:
        codes = {row.get('code') for row in verified or [] if isinstance(row, dict)}
        good = {row.get('code') for row in verified or [] if isinstance(row, dict)
                and isinstance((assessments or {}).get(row.get('copilot_id')), dict)
                and assessments[row['copilot_id']].get('classification') == 'satisfied'}
        view['missing_stage_codes'] = list(dict.fromkeys(
            list(view.get('missing_stage_codes') or []) + [code for code in required if code not in codes]))
        view['unmatched_stage_codes'] = list(dict.fromkeys(
            list(view.get('unmatched_stage_codes') or []) + [code for code in required if code not in good]))
    if 'research_refs' in result or 'evidence_refs' in result:
        view['local_observation_reference_count'] = len(result.get('research_refs') or [])
    view['model_view_schema'] = 1
    view['model_view_only'] = True
    view['full_local_evidence_retained'] = True
    return view


def _is_maa_snapshot(value):
    return (isinstance(value, dict) and isinstance(value.get('window'), dict)
            and isinstance(value.get('elements'), list) and isinstance(value.get('snapshot_id'), str))


def _maa_snapshot(value, budget):
    """Keep diagnostic semantics with a visible budget, never an admission receipt."""
    view = _pick(value, ('snapshot_id', 'snapshot_status', 'timestamp', 'examined',
                         'possibly_truncated', 'limits', 'reason'))
    view['window'] = _pick(value['window'], ('hwnd', 'pid', 'creation_time', 'executable', 'title', 'owner'))
    view.update(controls=[], static_texts=[], static_text_ids=[], original_element_count=len(value['elements']),
                displayed_control_count=0, omitted_element_count=0, omitted_help_text_count=0,
                model_view_truncated=False)
    used = len(json.dumps(view, ensure_ascii=False).encode('utf-8')) + 256
    seen_texts = {}
    controls, texts = [], []
    for element in value['elements']:
        if not isinstance(element, dict) or element.get('password') or element.get('offscreen') is True:
            continue
        if element.get('patterns'):
            controls.append(element)
        else:
            texts.append(element)
    # Inputs, mode selection and options precede other controls if the view fills.
    controls.sort(key=lambda item: not any(pattern in item.get('patterns', [])
                                          for pattern in ('set_value', 'select', 'toggle')))
    for element in controls:
        item = _pick(element, ('id', 'name', 'automation_id', 'control_type', 'patterns',
                              'enabled', 'offscreen', 'read_only', 'value', 'toggle', 'selected'))
        item = {key: entry for key, entry in item.items() if entry is not None and entry != ''}
        for key, entry in item.items():
            limit = 4096 if key == 'value' else 1000
            if isinstance(entry, str) and len(entry) > limit:
                item[key] = entry[:limit]
                view['model_view_truncated'] = True
        size = len(json.dumps(item, ensure_ascii=False).encode('utf-8')) + 2
        if used + size > budget:
            view['model_view_truncated'] = True
            continue
        view['controls'].append(item)
        used += size
    for element in texts:
        for key in ('name', 'value'):
            text = element.get(key)
            if not isinstance(text, str) or not text.strip():
                continue
            if text in seen_texts:
                index = seen_texts[text]
                identifier = element.get('id')
                size = len(json.dumps(identifier, ensure_ascii=False).encode('utf-8')) + 2
                if identifier and identifier not in view['static_text_ids'][index] and used + size <= budget:
                    view['static_text_ids'][index].append(identifier)
                    used += size
                elif identifier and identifier not in view['static_text_ids'][index]:
                    view['model_view_truncated'] = True
                continue
            if text.lstrip().startswith(('小提示:', '小提示：')):
                view['omitted_help_text_count'] += 1
                continue
            clipped = text[:1000]
            if clipped != text:
                view['model_view_truncated'] = True
            size = len(json.dumps(clipped, ensure_ascii=False).encode('utf-8')) + 2
            identifiers = [element['id']] if element.get('id') else []
            size += len(json.dumps(identifiers, ensure_ascii=False).encode('utf-8')) + 2
            if used + size > budget:
                view['model_view_truncated'] = True
                continue
            view['static_texts'].append(clipped)
            view['static_text_ids'].append(identifiers)
            seen_texts[text] = len(view['static_texts']) - 1
            used += size
    view['displayed_control_count'] = len(view['controls'])
    view['displayed_static_text_count'] = len(view['static_texts'])
    represented_ids = {item['id'] for item in view['controls'] if item.get('id')}
    represented_ids.update(identifier for identifiers in view['static_text_ids'] for identifier in identifiers)
    view['represented_element_count'] = len(represented_ids)
    view['omitted_element_count'] = max(0, view['original_element_count'] - len(represented_ids))
    return view


def _maa_evidence(result):
    if _is_maa_snapshot(result.get('snapshot')):
        view = _pick(result, ('task_id', 'epoch', 'revision', 'dispatch_id', 'operation_id'))
        view['snapshot'] = _maa_snapshot(result['snapshot'], 10000)
    else:
        response = result.get('response')
        operation = response.get('result') if isinstance(response, dict) else None
        snapshots = [key for key in ('before', 'after')
                     if isinstance(operation, dict) and _is_maa_snapshot(operation.get(key))]
        if not snapshots:
            return copy.deepcopy(result)
        view = _pick(result, ('task_id', 'epoch', 'revision', 'dispatch_id', 'operation_id'))
        view['response'] = _pick(response, ('status', 'submission_entered', 'error', 'helper_exit_confirmed'))
        view['response']['result'] = _pick(operation, ('action', 'element_id', 'ok', 'result', 'snapshot_id',
                                                       'timestamp', 'verification_required'))
        for key in snapshots:
            view['response']['result'][key] = _maa_snapshot(operation[key], 10000 // len(snapshots))
    view.update(model_view_schema=1, model_view_only=True, full_local_evidence_retained=True,
                omitted_fields=['rect', 'empty_layout_nodes', 'duplicate_static_text', 'fixed_help_text'])
    return view


def compact_tool_result(name, result):
    """Reduce deterministic job audits; leave real web text available to quote."""
    if name in ('research_activity_stages', 'reuse_catalog') and isinstance(result, dict):
        return _catalog(result)
    if name in ('normalize_structure', 'register_plan') and isinstance(result, dict) and result.get('ok') is True:
        view = _pick(result, _FAILURE_FIELDS + ('activity_name', 'structure_ref', 'stage_count',
                                               'plan_revision', 'revision'))
        view['groups'] = [dict(_pick(group, ('group_id', 'label')),
                               stage_count=len(group.get('stage_keys', [])))
                          for group in result.get('groups', [])]
        return view
    if not isinstance(result, dict):
        return copy.deepcopy(result)
    if name == 'read_evidence':
        return _maa_evidence(result)
    if name in {'research_search', 'research_fetch', 'web_search', 'web_fetch', 'search', 'fetch'}:
        return copy.deepcopy(result)
    if name == 'assess_candidate':
        return _assessment(result)
    if name == 'research_stage_jobs' or result.get('tool') == 'stage_jobs':
        view = copy.deepcopy(result)
        if 'assessment' in result:
            view['assessment'] = _assessment(result['assessment'])
        return view
    if name == 'research_screen' or result.get('tool') == 'screen_pool':
        view = _assessment(result)
        view.update(_pick(result, ('pool_ref', 'screen_ref', 'coverage', 'counts', 'screened_count',
                                   'accepted_count', 'hint')))
        if isinstance(result.get('accepted'), list):
            view['accepted'] = [_pick(row, ('id', 'source', 'mystery_code', 'title', 'stage_name',
                                           'classification', 'operators', 'missing_requirements'))
                                if isinstance(row, dict) else copy.deepcopy(row) for row in result['accepted']]
        return view
    if name == 'research_download' or result.get('tool') == 'download_copilot':
        return {key: copy.deepcopy(value) for key, value in result.items()
                if key not in {'copilot', 'actions', 'data', 'raw_response'}}
    return copy.deepcopy(result)

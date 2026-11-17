"""Read-only execution of a Leader-registered plan; no model-selected stages."""
import re

from .leader_plan import PlanStore
from .research_tools import ResearchTools


class ImportSelectionRequired(ValueError):
    """A user-selectable ambiguity, distinct from a missing tool/evidence error."""


def _import_slot(request, *, contract=None, selected_slot=None):
    """Resolve explicit choices while retaining the typed-import primary default.

    A typed import contract can connect a unique choice in a preceding clause
    to the import action. Explicit action-bound choices take precedence over
    research mentions. This selects a receipt slot, not GUI permission.
    """
    if not isinstance(request, str) or selected_slot not in (None, 'primary', 'backup'):
        raise ValueError('本次导入需要明确选择主选或备选。')
    typed_import = (isinstance(contract, dict) and contract.get('current_deliverable') == 'maa_import'
                    and (contract.get('task_spec') or {}).get('kind') == 'import')
    action = (r'(?:(?:导入|载入|加载)(?!\s*(?:结果|情况|状态|信息|记录|日志))|'
              r'(?:输入|填入|填写)(?=[^，,。；;！？!?\n]{0,80}(?<![A-Za-z0-9_])MAA(?![A-Za-z0-9_])))')
    choice = r'(?:主选|首选|备用|备选)'
    target = r'\s*(?:[A-Za-z0-9-]+\s*的?\s*)?(?:所选的?|刚才的?|找到的?)?\s*'
    object_suffix = r'\s*(?:(?:的|作业|方案|神秘代码|作业码|代码|JSON|路径)\s*){0,5}'
    pair = choice + object_suffix + r'(?:和|与|及|、|或)\s*' + choice
    negative = r'(?:不要|不用|不需要|无需|禁止|暂不|先不|别|不)'
    choices, mentioned, excluded = set(), set(), set()
    negative_import, positive_import = False, False
    for clause in re.split(r'[，,。；;！？!?\n]', request):
        if re.search(negative + r'\s*(?:把|将)?\s*(?:' + choice + object_suffix + r')?\s*' + action,
                     clause, re.I):
            negative_import = True
            excluded.update('backup' if word in ('备用', '备选') else 'primary'
                            for word in re.findall(choice, clause))
            continue
        excluded.update('backup' if match.group(1) in ('备用', '备选') else 'primary'
                        for match in re.finditer(negative + r'\s*(?:采用|使用|选择|考虑|选|用)?\s*(' + choice + ')', clause))
        clause = re.sub(negative + r'\s*(?:采用|使用|选择|考虑|选|用)?\s*' + choice, '', clause)
        positive_import = positive_import or bool(re.search(action, clause, re.I))
        clause_choices = set(re.findall(choice, clause))
        # A pasted inventory is data, not an instruction to select both slots.
        if not re.search(r'prts\s*://|主选\s*[:：]|备选\s*[:：]', clause, re.I):
            mentioned.update('backup' if word in ('备用', '备选') else 'primary' for word in clause_choices)
        if re.search(r'(?:失败|不行|不可用|过不了).*(?:再|才).*(?:备选|备用)', clause):
            continue
        after = re.search(action + target + '(' + choice + ')', clause, re.I)
        before = re.search('(' + choice + ')' + object_suffix + action, clause, re.I)
        if (re.search(action + target + pair, clause, re.I)
                or re.search(pair + object_suffix + action, clause, re.I)
                or (not after and not before and re.search(action, clause, re.I)
                    and clause_choices and (not typed_import or len(clause_choices) != 1))):
            raise ValueError('本次导入需要明确选择主选或备选。')
        for match in (after, before):
            if match:
                choices.add('backup' if match.group(1) in ('备用', '备选') else 'primary')
    if not choices and typed_import and (not negative_import or positive_import) and len(mentioned) == 1:
        selected=re.search(r'(?:^\s*|(?:选择|采用|选用|选|用)\s*)'+choice+r'(?=\s*(?:$|[，,。；;\n]|作业))',request)
        if selected and not re.search(r'(?:留作|作为|替代|都有|保留)',request):
            choices = mentioned
    if len(choices) > 1 or choices & excluded:
        raise ValueError('本次导入需要明确选择主选或备选。')
    if choices:
        return next(iter(choices))
    if (selected_slot is not None and selected_slot not in excluded
            and (positive_import or typed_import) and (not negative_import or positive_import)):
        return selected_slot
    if (typed_import and (positive_import or (contract.get('import_scope') or {}).get('kind') in {'all','stages'})
            and (not negative_import or positive_import) and 'primary' not in excluded):
        return 'primary'
    raise ValueError('本次导入需要明确选择主选或备选。')


def register_import_plan(store, state, contract):
    from .leader_plan import read_reference
    from .maa_queue_plan import (ImportScopeAmbiguous, ordered_import_candidates,
                                 select_import_stages, select_research_stages)

    verified = verify_planned_completion(store, state, 'stage_catalog')
    if not verified.get('complete'):
        raise ValueError(verified.get('summary') or '导入缺少本任务的真实目录证据。')
    reference = state['role_results']['catalog']['structure_ref']
    structure = read_reference(reference, store.engine.data_dir)
    from .chapter_scope import resolved_contract
    contract = resolved_contract(contract, structure)
    candidates = ordered_import_candidates(structure)
    try:
        selected = select_import_stages(candidates, contract)
        research = select_research_stages(candidates, contract)
    except ImportScopeAmbiguous as exc:
        raise ImportSelectionRequired(str(exc)) from None
    except ValueError as exc:
        if str(exc).startswith(('queue_scope_', 'queue_mode_selection_')):
            raise ImportSelectionRequired('请确认要导入的关卡范围，以及普通、突袭或两者；当前没有改变范围。') from None
        raise
    if any(stage.get('query_key_verified') is not True or stage.get('difficulty_verified') is not True
           for stage in selected):
        raise ValueError('目标关卡目录尚无可核验的作业查询标识或难度。')
    import_keys = [stage['stage_key'] for stage in selected]
    research_keys = [stage['stage_key'] for stage in research]
    if set(import_keys) - set(research_keys):
        raise ImportSelectionRequired('导入关卡超出本次查询范围。请确认需要查询和导入的关卡。')
    return PlanStore(store).register(state['task_id'], expected_revision=state['revision'],
        expected_epoch=state['epoch'], structure_ref=reference, ordered_steps=research_keys,
        search_policy={}, import_stage_keys=import_keys)


def run_import_research(agents, context, cancel):
    from .import_preparation import import_boundary
    with import_boundary(agents.store,agents.directory,context,'import_research'):
        return _run_import_research(agents,context,cancel)


def _run_import_research(agents, context, cancel):
    from .import_preparation import ImportPreparation
    from .maa_queue_plan import ImportScopeAmbiguous, select_import_stages

    agents._guard(context, cancel)
    contract = context['contract']
    slot = _import_slot(contract.get('user_request') or contract.get('goal', ''),
                        contract=contract, selected_slot=contract.get('selected_slot'))
    if not contract.get('source_task_id'):
        result = run_planned_research(agents, context, cancel)
        if result.get('status') != 'researched' and contract.get('import_scope') is None:
            from .code_delivery import CodeDeliveryStore
            from .code_presentation import render_code_summary
            delivery = CodeDeliveryStore(agents.store, agents.directory).summary(context['task_id'])
            if delivery['items']:
                gaps = [item for item in delivery['items'] if not item.get('primary_code')]
                gap_summary = render_code_summary({'summary':'待补齐关卡', 'items':gaps})
                result = {**result, 'code':'import_research_partial',
                          'summary':'已完成本轮作业查询，全部导入准备仍有缺口。\n'+
                                    gap_summary+'\n\n'+render_code_summary(delivery)}
            return result
    preparation = ImportPreparation(agents.store, agents.directory)
    try:
        options = preparation.options(context)
        selected = options['steps'] if options.get('scope_resolved') else select_import_stages(options['steps'],contract)
    except ImportScopeAmbiguous as error:
        return {'status':'needs_input','code':'import_selection_required','waiting_kind':'business',
                'summary':str(error)}
    except ValueError as error:
        if str(error).startswith(('queue_scope_', 'queue_mode_selection_')):
            return {'status':'needs_input','code':'import_selection_required','waiting_kind':'business',
                    'summary':'请确认本次导入的关卡范围，以及普通、突袭或两者。'}
        raise
    missing = [row['stage_code']+('（突袭）' if row.get('difficulty')==2 else '（普通）')
               for row in selected if not row[slot + '_code']]
    if missing:
        return {'status': 'needs_input', 'code': 'verified_job_unavailable',
                'summary': '以下导入目标仍缺符合本地 Box 的作业：'+'、'.join(missing)+'。'}
    if len(selected) > 1:
        selections = [{'source_task_id':options['source_task_id'], 'step_id':row['step_id'], 'slot':slot}
                      for row in selected]
        plan, plan_ref = preparation.prepare_queue_plan(context, selections, cancel_event=cancel)
        agents._guard(context, cancel)
        candidates = [target['candidate_ref'] for target in plan['targets']]
        from .import_mode import single_document_plan
        if single_document_plan(plan):
            return {'status':'researched','summary':'同一份核验作业兼容所选难度，使用单作业导入。',
                'single_document_plan_ref':plan_ref,'target_count':len(candidates),
                'candidate_refs':candidates,'evidence_refs':[plan_ref,*candidates]}
        return {'status':'researched', 'summary':f"已核验 {len(candidates)} 个导入目标的作业及 Box 条件。",
                'queue_plan_ref':plan_ref, 'target_count':len(candidates),
                'candidate_refs':candidates, 'evidence_refs':[plan_ref,*candidates]}
    result = preparation.prepare(context, source_task_id=options['source_task_id'],
        step_id=selected[0]['step_id'], slot=slot, cancel_event=cancel)
    agents._guard(context, cancel)
    candidate = result['candidate_ref']
    copilot = result['verified_copilot']
    return {'status': 'researched', 'summary': '完整作业已通过关卡、难度和本地 Box 核验。',
            'candidate_refs': [candidate], 'evidence_refs': [candidate],
            'verified_copilots': [copilot], 'assessments': {copilot['copilot_id']: result['assessment']}}


def run_planned_research(agents, context, cancel):
    from .import_preparation import import_boundary
    with import_boundary(agents.store,agents.directory,context,'planned_research'):
        return _run_planned_research(agents,context,cancel)


def _run_planned_research(agents, context, cancel):
    from .code_delivery import CodeDeliveryStore, CodeTools
    from .job_library import JobLibrary
    agents._guard(context, cancel)
    plans = PlanStore(agents.store)
    deliveries = CodeDeliveryStore(agents.store, agents.directory)
    research = ResearchTools(agents.clients['research'].config,
        agents.directory / 'research' / context['task_id'] / context['dispatch_id'],
        public_cache_root=agents.store.engine.data_dir / 'public-cache')
    research.set_pool_directory(agents.directory / 'research' / context['task_id'] / 'candidate-cache')
    tools = CodeTools(deliveries, plans, research, agents.rosters)
    library = JobLibrary(agents.directory / 'job-library', deliveries)
    current_plan=plans.current(context['task_id'])
    library.index_verified_history(activity_id=current_plan['activity_id'],
        stage_keys=current_plan['ordered_stage_keys'],max_milliseconds=1000)
    results, export_errors = [], []
    retried=False
    while True:
        agents._guard(context, cancel)
        lease = plans.claim_next(context['task_id'], expected_revision=context['revision'],
            expected_epoch=context['epoch'], dispatch_id=context['dispatch_id'])
        if lease is None:
            if not retried:
                retried=True
                if plans.retry_failed(context['task_id'],expected_revision=context['revision'],
                    expected_epoch=context['epoch'],dispatch_id=context['dispatch_id']):
                    continue
            break
        plan = plans.current(context['task_id'])
        group = next((g for g in plan['groups'] if lease['resolved_stage']['stage_key'] in g['stage_keys']), {})
        arguments = {'step_id':lease['step_id'], 'stage_code':lease['resolved_stage'].get('display_code', ''),
                     'group_label':group.get('label',''), 'plan_revision':context['revision'],
                     'attempt':lease.get('attempt',1)}
        agents._progress('tool_started', 'research', context, cancel, 'research_find_pair', arguments=arguments)
        result = library.adopt_step(lease)
        if result is None:
            result = tools.find_pair(lease, cancel_event=cancel)
        else:
            agents._progress('tool_finished', 'research', context, cancel, 'local_job_library',
                {'status':'reused','summary':'本地作业及当前 Box 已复核，本关无需重新联网。'}, arguments=arguments)
        agents._guard(context, cancel)
        plans.close_step(lease, result)
        export_error = None
        try:
            library.export_step(context['task_id'], lease['step_id'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            export_error = type(exc).__name__
            export_errors.append({'step_id':lease['step_id'], 'code':export_error})
            with agents.store.engine._transaction():
                agents.store._publish_event(context['task_id'],'job_library.publish_retry',
                    {'revision':context['revision'],'step_id':lease['step_id'],'code':export_error},
                    event_key=f"library-retry:{context['task_id']}:{context['revision']}:{lease['step_id']}")
        results.append({'step_id':lease['step_id'], 'stage':lease['resolved_stage'].get('display_code'),
                        'status':result['status'],'attempt':lease.get('attempt',1)})
        agents._progress('tool_finished', 'research', context, cancel, 'research_find_pair',
                         {**result, 'export_error':export_error}, arguments=arguments)
    plan = plans.current(context['task_id'])
    gaps = [{'step_id':s['step_id'], 'status':s['status'],
             'reason':(s.get('result') or {}).get('backup_reason') or (s.get('result') or {}).get('reason') or s['status']}
            for s in plan['steps'] if s['status'] not in ('two_verified','primary_only')]
    closed = bool(plan['steps']) and all(s['status'] != 'pending' for s in plan['steps'])
    return {'status':'unknown' if gaps else 'researched',
            'summary':('计划已封口，仍有 %d 项缺口；同计划不能重新检索，需诊断或修订。' % len(gaps)
                       if gaps and closed else '已按计划处理 %d 关，结果以持久回执为准。' % len(results)),
            'steps':results, 'structural_gaps':gaps, 'closed_not_retryable':closed,
            'plan_revision':context['revision'], 'export_errors':export_errors}


def verify_planned_completion(store, state, typed):
    """Re-read durable evidence; role summaries never authorize completion."""
    from .leader_plan import PlanStore, read_reference
    try:
        if typed == 'copilot_codes':
            from .code_delivery import CodeDeliveryStore
            if PlanStore(store).current(state['task_id']) is None:
                return {'complete':False, 'summary':'尚无已登记计划。'}
            summary = CodeDeliveryStore(store, store.engine.data_dir).summary(state['task_id'], verify_current=True)
            from .code_presentation import render_code_summary
            summary['summary'] = render_code_summary(summary)
            return summary
        catalog = state.get('role_results', {}).get('catalog') or {}
        structure = read_reference(catalog['structure_ref'], store.engine.data_dir)
        if structure.get('ok') is not True or structure.get('catalog_complete') is not True:
            raise ValueError('invalid structure')
        found = False
        for ref in catalog.get('evidence_refs', []):
            obs = read_reference(ref, store.engine.data_dir)
            result = obs.get('result') or {}
            if (obs.get('task_id') == state['task_id'] and obs.get('epoch') == state['epoch']
                    and obs.get('tool') == 'research_activity_stages' and result.get('ok') is True
                    and result.get('stage_catalog_ref') == structure.get('catalog_ref')):
                raw_catalog = read_reference(result['stage_catalog_ref'], store.engine.data_dir)
                if (raw_catalog.get('catalog_complete') is True and raw_catalog.get('source')
                        and raw_catalog.get('raw_sha256') and raw_catalog.get('event_name') == structure.get('activity_name')):
                    found = True
                    break
        if not found:
            return {'complete':False, 'summary':'缺少本任务真实目录来源。'}
        stages = {s['stage_key']:s for s in structure['stages']}
        from .chapter_scope import resolved_contract,select_chapter
        from .maa_queue_plan import ordered_import_candidates
        contract=resolved_contract(state.get('contract') or store.current(state['task_id'])['contract'],structure)
        selected={s['stage_key'] for s in select_chapter(ordered_import_candidates(structure),contract.get('chapter_scope'))}
        lines = [structure['activity_name']+'：已核对完整目录（账号解锁未核实）。']
        for group in structure['groups']:
            members = [stages[k] for k in group['stage_keys'] if k in selected]
            if not members:continue
            lines.append(group['label']+'：'+ '、'.join(str(s.get('display_code') or s['stage_id'])+
                ('（突袭）' if s.get('difficulty') == 2 else '（难度未知）' if not s.get('difficulty') else '') for s in members))
        return {'complete':True, 'summary':'\n'.join(lines)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'complete':False, 'summary':'计划或目录凭据缺失、变化或不完整。'}

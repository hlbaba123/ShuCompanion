"""Validate model-selected delivery, task references and import scope."""
import re
from .leader_policy import _number

DELIVERABLES = ('stage_catalog', 'copilot_codes', 'copilot_json', 'maa_import', 'gameplay', 'chat')
RELATIONS = ('new', 'continue', 'modify', 'followup')
IMPORT_SCOPE_KINDS = ('all', 'stages', 'ordinal', 'unspecified')
_STAGE_CODE = re.compile(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,5}')
_ORDINAL = re.compile(r'第\s*([0-9]{1,3}|[零〇一二两三四五六七八九十百]{1,6})\s*(?:个)?\s*(?:关卡|关)|首关')
_IMPORT_ACTION = re.compile(r'导入|载入|加入\s*MAA|添加到\s*MAA|输入到\s*MAA', re.I)
_IMPORT_NEGATION = re.compile(r'(?:不(?:要|用)?|别|无需|禁止|暂不|先不)\s*(?:导入|载入|加入|添加|输入)')


class TaskDirectiveError(ValueError):
    pass


def ordinal_import_request(request: str) -> int | None:
    """Protect a literal ordinal attached to a positive import clause.

    This is a guard against widening an import. It does not map ordinal
    phrases to a stage or choose a difficulty.
    """
    if not isinstance(request, str):
        return None
    ordinals = set()
    for clause in re.split(r'[，,。；;！？!?\n]', request):
        if not _IMPORT_ACTION.search(clause) or _IMPORT_NEGATION.search(clause):
            continue
        for match in _ORDINAL.finditer(clause):
            if re.match(r'\s*(?:之外|以外|以后|之后)', clause[match.end():]):
                continue
            ordinal = _number(match.group(1)) if match.group(1) else 1
            if 1 <= ordinal <= 300:
                ordinals.add(ordinal)
    return next(iter(ordinals)) if len(ordinals) == 1 else None


def normalize_import_scope(value: dict) -> dict:
    """Validate a scope description. Catalog evidence is checked separately."""
    fields = {'kind', 'stage_codes', 'difficulty', 'ordinal', 'group_hint'}
    if not isinstance(value, dict) or set(value) - fields:
        raise TaskDirectiveError('导入范围格式无效。')
    kind = value.get('kind')
    stage_codes = value.get('stage_codes', [])
    difficulty = value.get('difficulty')
    ordinal = value.get('ordinal')
    group_hint = value.get('group_hint', '')
    if (kind not in IMPORT_SCOPE_KINDS
            or not isinstance(stage_codes, (list, tuple)) or len(stage_codes) > 200
            or any(not isinstance(code, str) or len(code) > 80 or not _STAGE_CODE.fullmatch(code)
                   for code in stage_codes)
            or (difficulty is not None and (type(difficulty) is not int or difficulty not in (1, 2, 3)))
            or (ordinal is not None and (type(ordinal) is not int or not 1 <= ordinal <= 300))
            or not isinstance(group_hint, str) or len(group_hint) > 300 or '\x00' in group_hint):
        raise TaskDirectiveError('导入范围中的关卡、难度、顺序或分组格式无效。')
    if (bool(stage_codes) != (kind == 'stages') or (ordinal is not None) != (kind == 'ordinal')):
        raise TaskDirectiveError('导入关卡代码只用于 stages，顺序只用于 ordinal。')
    return {'kind': kind, 'stage_codes': list(dict.fromkeys(code.upper() for code in stage_codes)),
            'difficulty': difficulty, 'ordinal': ordinal, 'group_hint': group_hint.strip()}


def validate_directive(proposed: dict, *, user_request: str, known_tasks: list[dict]) -> dict:
    if not isinstance(proposed, dict) or not isinstance(user_request, str) or not user_request.strip():
        raise TaskDirectiveError('任务指令和用户原话格式无效。')
    relation = proposed.get('relation')
    delivery = proposed.get('current_deliverable')
    if relation not in RELATIONS or delivery not in DELIVERABLES:
        raise TaskDirectiveError('需要明确当前交付类型与任务关联。')
    if not isinstance(known_tasks, list) or any(not isinstance(t, dict) for t in known_tasks):
        raise TaskDirectiveError('已知任务清单格式无效。')
    ids = {t.get('task_id') or t.get('id') for t in known_tasks
           if isinstance(t.get('task_id') or t.get('id'), str)}
    selected = proposed.get('selected_task_id')
    source = proposed.get('source_task_id')
    for ref in (selected, source):
        if ref is not None and (not isinstance(ref, str) or ref not in ids):
            raise TaskDirectiveError('引用的任务编号不在真实任务清单中。')
    if relation in ('continue', 'modify'):
        if not selected or source:
            raise TaskDirectiveError('续接或修改必须选择现有任务，不能同时建立来源关联。')
        previous=next(t for t in known_tasks if (t.get('task_id') or t.get('id'))==selected)
        old_delivery=(previous.get('contract') or previous).get('current_deliverable')
        if relation == 'continue' and old_delivery and old_delivery != delivery:
            raise TaskDirectiveError('当前交付类型已经改变，不能continue旧合同；请用followup和source_task_id建立关联新任务。')
        old=previous.get('contract') or previous
        old_codes=old.get('target_stage_codes') or [x for x in [(old.get('task_spec') or {}).get('stage_code')] if x]
        new_codes=proposed.get('target_stage_codes') or [x for x in [(proposed.get('task_spec') or {}).get('stage_code')] if x]
        question=previous.get('question') or {}
        answering_business=(question.get('kind')=='business' and question.get('status')=='open')
        if (delivery=='copilot_codes' and new_codes and not answering_business
                and {str(c).upper() for c in new_codes}!={str(c).upper() for c in old_codes}):
            raise TaskDirectiveError('关卡范围已变化；请用followup及source_task_id建立关联查询。')
    elif selected:
        raise TaskDirectiveError('新任务或关联查询不能复用旧任务编号。')
    if relation == 'followup' and not source:
        raise TaskDirectiveError('关联查询需要真实来源任务编号。')
    if relation == 'new' and source:
        raise TaskDirectiveError('复用来源资料应明确标记followup。')
    if delivery == 'chat' and (relation != 'new' or selected or source):
        raise TaskDirectiveError('普通聊天不能选择或改变任务。')
    deferred = proposed.get('deferred_intents', [])
    if (not isinstance(deferred, (list, tuple)) or len(deferred) > 16
            or any(not isinstance(x, str) or not x.strip() or len(x) > 1000 for x in deferred)):
        raise TaskDirectiveError('后续意图需要有界文本列表。')
    name = proposed.get('activity_name', '')
    if not isinstance(name, str) or len(name) > 300 or '\x00' in name:
        raise TaskDirectiveError('活动名称格式无效。')
    codes = proposed.get('target_stage_codes', [])
    if (not isinstance(codes, (list, tuple)) or len(codes) > 300
            or any(not isinstance(code, str) or len(code) > 80 or not _STAGE_CODE.fullmatch(code)
                   for code in codes)):
        raise TaskDirectiveError('明确目标关卡需为有界代码列表；活动整体范围留给真实目录决定。')
    result = {'relation': relation, 'current_deliverable': delivery,
              'selected_task_id': selected, 'source_task_id': source,
              'deferred_intents': list(dict.fromkeys(deferred)), 'activity_name': name.strip(),
              'target_stage_codes': list(dict.fromkeys(code.upper() for code in codes))}
    if proposed.get('chapter_scope') is not None:
        from .chapter_scope import label, normalize_chapter_scope
        try:
            result['chapter_scope'] = normalize_chapter_scope(proposed['chapter_scope'])
        except ValueError as exc:
            raise TaskDirectiveError(str(exc)) from None
        scope = result['chapter_scope']
        if (relation == 'new' and name.strip() and scope['ordinal'] is None
                and label(scope['name']) == label(name)
                and _STAGE_CODE.search(user_request)
                and not re.search(r'章|篇|分区|区域|部分', user_request)):
            raise TaskDirectiveError('用户仅指定活动和关卡，chapter_scope不能重复父活动名；请修复为null，保留活动与目标关卡。')
    if proposed.get('import_scope') is not None:
        scope = normalize_import_scope(proposed['import_scope'])
        if delivery != 'maa_import':
            raise TaskDirectiveError('仅 MAA 导入交付可填写 import_scope。')
        result['import_scope'] = scope
    if delivery == 'maa_import':
        ordinal = ordinal_import_request(user_request)
        if ordinal is not None:
            scope = result.get('import_scope')
            if scope is None:
                result['import_scope'] = normalize_import_scope({'kind': 'ordinal', 'ordinal': ordinal})
            elif scope['kind'] != 'ordinal' or scope['ordinal'] != ordinal:
                raise TaskDirectiveError('原话限定按顺序导入一关，须用 ordinal 并保留原顺序，待真实目录核定。')
    return result

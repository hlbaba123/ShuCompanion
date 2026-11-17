"""Resolve semantic chapter selectors against the actual ordered directory.

No chapter label is a substitute for an event identity. No EX/S template is
invented here; selectors filter only groups present in verified catalog rows.
"""
import unicodedata


def label(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def normalize_chapter_scope(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'name', 'ordinal'}:
        raise ValueError('章节选择格式无效')
    name, ordinal = value.get('name', ''), value.get('ordinal')
    if (not isinstance(name, str) or len(name) > 300 or '\x00' in name
            or ordinal is not None and (type(ordinal) is not int or not 1 <= ordinal <= 300)
            or not name.strip() and ordinal is None):
        raise ValueError('章节需要名称或顺序，由真实目录核实')
    return {'name': name.strip(), 'ordinal': ordinal}


def select_chapter(rows, scope):
    scope = normalize_chapter_scope(scope)
    if scope is None:
        return rows
    groups = list(dict.fromkeys((r.get('group_id'), r.get('group_label')) for r in rows))
    chosen = groups
    if scope['name']:
        chosen = [g for g in chosen if label(scope['name']) in {label(g[0] or ''), label(g[1] or '')}]
    if scope['ordinal'] is not None:
        ordinal = scope['ordinal']
        known={r.get('group_id'):r.get('group_ordinal') for r in rows if r.get('group_ordinal') is not None}
        if known:
            chosen=[g for g in chosen if known.get(g[0])==ordinal]
        else:
            chosen = [groups[ordinal - 1]] if ordinal <= len(groups) and groups[ordinal-1] in chosen else []
    if len(chosen) != 1:
        raise ValueError('章节未能对应唯一的真实目录分区；不能扩展为整个活动')
    return [r for r in rows if (r.get('group_id'), r.get('group_label')) == chosen[0]]


def resolved_contract(contract, structure):
    """Repair legacy chapter-as-event only when the directory proves membership."""
    from .leader_plan import activity_query_matches
    result = dict(contract)
    activity = contract.get('activity_name') or ''
    if activity and not activity_query_matches(activity, structure.get('activity_name', '')):
        matches = [g for g in structure.get('groups', []) if label(activity) == label(g.get('label', ''))]
        if len(matches) != 1:
            raise ValueError('计划活动与本次任务指定活动不一致，且不是真实目录中的章节')
        scope = result.get('chapter_scope') or {'name': matches[0]['label']}
        if scope.get('name') and label(scope['name']) != label(matches[0]['label']):
            raise ValueError('章节选择与原任务指定名称不一致')
        result['chapter_scope'] = scope
        result['activity_name'] = structure['activity_name']
    return result

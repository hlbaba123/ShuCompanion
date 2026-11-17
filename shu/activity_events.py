"""Small public activity projections, independent of model and GUI internals."""
from datetime import datetime, timedelta, timezone
import math
import re
from .activity_history import public_source_url


ROLE_NAMES = {'leader': 'Leader', 'research': '研究 Agent', 'catalog': '目录 Agent',
              'maa': 'MAA Agent', 'vision': '视觉 Agent', 'diagnosis': '诊断 Agent'}
TOOL_NAMES = {
    'queue_snapshot':'核对当前作业队列','queue_mode':'开启多作业模式',
    'queue_options':'核对编队选项','queue_mode_enabled':'开启多作业模式',
    'queue_option_verified':'核对编队选项','queue_code_submitted':'提交当前作业码',
    'queue_item_verified':'核对已入队作业','queue_target_reused':'复用队列已有作业',
    'queue_verified':'核对全部导入目标','queue_partial':'保存已完成与待处理目标',
    'vision_observe': '识别 MAA 截图',
    'catalog_structure': '核实活动结构', 'leader_plan': '规划活动步骤',
    'research_find_pair': '查找主备作业',
    'research_import_options': '读取已核验作业选项',
    'research_prepare_import': '准备完整执行作业',
    'catalog_stage_facts': '核对关卡公开资料', 'normalize_structure': '整理活动结构',
    'register_plan': '登记活动计划', 'find_pair': '查找主备作业',
    'web_search': '联网搜索', 'web_fetch': '读取网页', 'knowledge': '查询本地资料',
    'research_search': '搜索作业资料', 'research_catalog': '查询作业目录',
    'research_levels': '核对关卡目录', 'research_fetch': '读取攻略网页',
    'research_download': '下载完整作业', 'research_candidates':'收集作业候选池',
    'research_pool_page':'读取本地候选', 'get_roster': '读取干员档案',
    'research_activity_stages':'核实活动关卡范围','research_scope':'登记本次关卡范围',
    'research_screen':'筛选符合本地干员的候选','research_stage_jobs':'查找并核验该关可用作业',
    'assess_candidate': '核对作业与干员', 'import_roster_export': '读取干员导出',
    'inspect_maa': '读取 MAA 状态', 'maa_act': '操作 MAA 控件',
    'verify_import': '核实作业加载结果', 'read_evidence': '读取任务证据',
    'delegate': '分配下一项工作', 'finish_role': '提交角色结果',
}
STAGES = frozenset({'intent_started', 'intent_finished', 'intent_failed',
    'dispatch_started', 'dispatch_completed', 'dispatch_reused',
    'tool_started', 'tool_finished', 'task_started', 'task_finished',
    'task_paused', 'task_stopped', 'task_waiting'})


def _text(value, limit):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'(?i)\bsk-[a-z0-9_.-]{6,}|\bbearer\s+\S+|data:[^\s]+',
                   '[已隐藏]', value)
    value = re.sub(r'(?i)\b(?:api[_-]?key|authorization|access[_-]?token|password)\s*[:=]\s*\S+',
                   '[已隐藏]', value)
    return ' '.join(value.split())[:limit]


def make_activity_event(stage, details=None, **facts):
    """Drop unknown fields rather than recursively copying an upstream payload."""
    if not isinstance(stage, str) or stage not in STAGES:
        return None
    values = {**(details if isinstance(details, dict) else {}), **facts}
    role = values.get('role', '')
    if not isinstance(role, str) or role not in (*ROLE_NAMES, ''):
        return None
    event = {'stage': stage, 'role': role,
             'status': _text(values.get('status', ''), 64),
             'summary': _text(values.get('summary', ''), 300),
             'timestamp': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')}
    for field, limit in (('request_id',128), ('task_id', 128), ('dispatch_id', 128), ('tool_call_id',128), ('tool', 80), ('step_id',128), ('target_key',128), ('stage_code',80), ('group_label',120), ('stop_reason',120)):
        value = _text(values.get(field), limit)
        if value:
            event[field] = value
    query=_text(values.get('query'),128)
    if query:event['query']=query
    source=public_source_url(values.get('url'))
    if source:event['url']=source
    title=_text(values.get('step_title'),120)
    if title:event['step_title']=title
    for field in ('item_count','total','primary_count','backup_count','plan_revision','attempt'):
        value=values.get(field)
        if type(value) is int and 0<=value<=1000000:event[field]=value
    if type(values.get('cached')) is bool:event['cached']=values['cached']
    epoch = values.get('epoch', values.get('execution_epoch', values.get('goal_version')))
    if type(epoch) is int and epoch >= 0:
        event['epoch'] = epoch
    elapsed = values.get('elapsed_seconds')
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 86400:
        event['elapsed_seconds'] = round(elapsed, 4)
    return event

"""Present verified targets without merging distinct modes or branches."""

_GAP_REASONS = {
    'budget_exhausted': '本轮查询预算已用完',
    'network_error': '作业站连接或返回异常',
    'local_io_error': '本地候选资料读写失败',
    'evidence_unavailable': '候选凭据暂不可用',
    'catalog_unverified': '已查至目录末尾，部分记录未核实',
    'coverage_incomplete': '候选目录未查完',
    'unresolved_requirements': '仍有候选条件待核实',
    'unverified_stage': '关卡或难度尚未核实',
}


def stage_label(stage):
    code = str(stage.get('display_code') or stage.get('stage_code') or stage.get('stage_id') or '未知关卡')
    details = []
    if 'difficulty' in stage:
        difficulty = stage['difficulty']
        details.append({1: '普通', 2: '突袭', 3: '普通/突袭'}.get(difficulty, '难度未知')
                       if type(difficulty) is int else '难度未知')
    if stage.get('branch'):
        details.append('分支 ' + str(stage['branch']))
    return code + ('（' + '；'.join(details) + '）' if details else '')


def render_code_summary(summary):
    lines = [summary['summary']]
    for item in summary.get('items', []):
        label = item.get('stage_label') or stage_label(item)
        line = (label + '：主选 ' + (item.get('primary_code') or '尚无合格主选')
                + '；备选 ' + (item.get('backup_code') or '暂无'))
        if not item.get('primary_code'):
            reason = {'no_box_match': '已核对的候选不符合当前 Box',
                      'no_jobs_yet': '本次目录暂未返回作业'}.get(item.get('status'))
            reason = reason or _GAP_REASONS.get(item.get('reason'))
            if reason:
                if item.get('unresolved_count') and item.get('reason') != 'unresolved_requirements':
                    reason += '，仍有候选条件待核实'
                line += '（' + ('已补查，' if item.get('attempt') == 2 else '') + reason + '）'
        lines.append(line)
    return '\n'.join(lines)

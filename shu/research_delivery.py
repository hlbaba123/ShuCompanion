"""Render tangible research outputs from local receipts, never model claims."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re


def _read(root, ref):
    path=Path(ref['path']).resolve(strict=True)
    if not path.is_relative_to(root) or path.stat().st_size>4*1024*1024:raise ValueError('Invalid receipt file')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('Receipt changed')
    return json.loads(raw)


def deliverables(root, receipt):
    root=Path(root).resolve();items=[];seen=set()
    for ref in (receipt or {}).get('candidate_refs',[])[:100]:
        try:
            record=_read(root,ref);identifier=record['copilot_id'];stage=record['goal_stage']
            if not re.fullmatch(r'[1-9][0-9]{0,11}',identifier):continue
            source=_read(root,stage['evidence_ref'])
            body=_read(root,{'path':record['path'],'sha256':record['sha256']})
            if (source!={k:v for k,v in stage.items() if k!='evidence_ref'}
                    or body['stage_name']!=record['stage_name'] or not body.get('actions')
                    or body['stage_name'].casefold() not in {stage['stage_id'].casefold(),str(stage['level_id']).casefold()}
                    or record['source_url']!='https://prts.maa.plus/copilot/get/'+identifier):continue
            key=(stage['code'],identifier)
            if key in seen:continue
            seen.add(key)
            assessment=(receipt.get('assessments') or {}).get(identifier) or {}
            items.append({'code':stage['code'],'mystery_code':'prts://'+identifier,
                'copilot_id':identifier,'path':record['path'],'sha256':record['sha256'],
                'title':record.get('title',''),'detail_url':'https://prts.plus/?op='+identifier,
                'source_url':record['source_url'],'classification':assessment.get('classification','not_checked'),
                'box_revision':assessment.get('box_revision'),'ignored_requirements':assessment.get('ignored_requirements',[])})
        except (ValueError,KeyError,TypeError,OSError):continue
    return items


def render_delivery(items, *, completed=False, require_roster=False, missing=None, pending=None):
    if not items:
        return '尚未取得经过工具核验的作业码或完整 JSON；当前查询未完成。没有把网页链接或角色回复计作交付。'
    usable=[i for i in items if not require_roster or i['classification']=='satisfied']
    if not usable:
        return '已取得作业资料，但尚无通过本地干员条件核对的作业；当前查询未完成。不能将未匹配的作业当作可用结果。'
    lines=['已核验的作业结果：' if completed else '已取得以下真实资料；整项任务尚未完成：']
    for item in usable:
        label=('符合本地干员硬性条件' if item['classification']=='satisfied' else '阵容尚未核对')
        lines.extend([f"- {item['code']}：{item['mystery_code']}（{label}）",
                      '  JSON：'+item['path'],'  来源：'+item['detail_url']])
    if require_roster:lines.append('按已设策略忽略等级、技能和模组门槛；有无与精英化仍为硬条件。未验证好友助战不计作满足。')
    if missing:lines.append('仍缺少有效结果：'+'、'.join(missing))
    if pending:lines.append('尚未完成的核对：'+'；'.join(pending))
    lines.append('本轮未导入 MAA，未进行游戏操作。')
    return '\n'.join(lines)

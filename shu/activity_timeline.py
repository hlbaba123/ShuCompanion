"""Project public events into actual role episodes, without execution authority.

This module accepts only the history display allowlist. It never interprets
model reasoning, tool results, future plans, or summaries as executed actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .activity_history import event_fields


SUCCESS = frozenset({'completed', 'reused', 'understood', 'research_verified',
                     'import_verified', 'planned_verified', 'catalog_verified'})
FAILURE = frozenset({'failed', 'error', 'rejected'})
_TITLES = {'leader':'Leader · 黍', 'research':'研究 Agent', 'catalog':'目录 Agent', 'maa':'MAA Agent',
           'diagnosis':'诊断 Agent', 'dialogue':'黍 · 对话', 'persona':'黍 · 对话'}


@dataclass
class TimelineItem:
    stage: str = ''
    tool: str = ''
    call_id: str = ''
    status: str = ''
    summary: str = ''
    query: str = ''
    url: str = ''
    timestamp: str = ''
    elapsed_seconds: float | None = None
    item_count: int | None = None
    total: int | None = None
    cached: bool | None = None
    task_id: str = ''
    step_id: str = ''
    stage_code: str = ''
    group_label: str = ''
    primary_count: int | None = None
    backup_count: int | None = None
    stop_reason: str = ''
    plan_revision: int | None = None


@dataclass
class TimelineStep:
    key: str
    title: str
    role: str
    kind: str
    status: str = ''
    summary: str = ''
    timestamp: str = ''
    returned: bool = False
    items: list[TimelineItem] = field(default_factory=list)
    step_title: str = ''

    @property
    def completed(self):
        # A research dispatch can return successfully without completing the task.
        return self.status in SUCCESS or (self.kind == 'dispatch' and self.status == 'researched' and self.returned)

    @property
    def failures(self):
        values=[]
        for item in self.items:
            if item.status in FAILURE:
                text=item.summary or (item.tool + ' 调用失败').strip()
                if text not in values:
                    values.append(text)
        return values


def _new_step(event, index, kind):
    role=event.get('role','')
    identity=event.get('dispatch_id') if kind == 'dispatch' else None
    status=event.get('status','')
    if kind == 'episode' and event.get('stage') == 'tool_finished' and status in SUCCESS:
        status='returned'
    step_title=event.get('step_title','') if kind != 'intent' else ''
    return TimelineStep('dispatch:'+identity if identity else 'event:'+str(index),
        step_title or ('理解要求' if kind == 'intent' else _TITLES.get(role,'状态记录')),
        role, kind, status, event.get('summary',''),
        event.get('timestamp',''), step_title=step_title)


def _append_item(step, event):
    stage=event.get('stage','')
    item=None
    if stage == 'tool_finished':
        call_id=event.get('tool_call_id','')
        for previous in reversed(step.items):
            if previous.stage != 'tool_started' or previous.tool != event.get('tool',''):
                continue
            if call_id and previous.call_id != call_id:
                continue
            # Without IDs, pair only an anonymous unfinished invocation.
            if not call_id and previous.call_id:
                continue
            item=previous
            break
    if item is None:
        item=TimelineItem()
        step.items.append(item)
    item.stage=stage
    for name in ('tool','status','summary','query','url','timestamp',
                 'elapsed_seconds','item_count','total','cached', 'step_id', 'stage_code',
                 'group_label', 'primary_count', 'backup_count', 'stop_reason', 'plan_revision', 'task_id'):
        if name in event:
            setattr(item,name,event[name])
    if 'tool_call_id' in event:
        item.call_id=event['tool_call_id']
    if stage == 'tool_started' and not item.status:
        item.status='running'
    if stage == 'tool_finished' and not event.get('status'):
        item.status='returned'
    if stage in ('error','intent_failed'):
        item.status='failed'


def project_timeline(events, terminal_status=None):
    """Return only phases evidenced by events, preserving chronological order.

    dispatch_id is authoritative for grouping. Old display histories without
    IDs fall back to contiguous role episodes and explicit boundary events.
    A tool return closes its own row, never its enclosing role or task.
    """
    steps=[]
    dispatches={}
    current=None
    intent=None
    for index, raw in enumerate(events):
        event=event_fields(raw)
        if not event:
            continue
        stage=event.get('stage','')
        role=event.get('role','')
        dispatch_id=event.get('dispatch_id')
        step=None
        if stage.startswith('intent_'):
            if stage == 'intent_started' or intent is None:
                intent=_new_step(event,index,'intent')
                steps.append(intent)
            step=intent
        elif dispatch_id:
            step=dispatches.get(dispatch_id)
            if step is None:
                step=_new_step(event,index,'dispatch')
                steps.append(step)
                dispatches[dispatch_id]=step
        elif stage in ('dispatch_started','dispatch_reused'):
            step=_new_step(event,index,'dispatch')
            steps.append(step)
        elif stage in ('task_finished','task_waiting','task_paused','task_stopped','error','notice'):
            step=current
        elif current is not None and (not role or role == current.role):
            step=current
        if step is None:
            step=_new_step(event,index,'episode')
            steps.append(step)
        # A delayed tool result belongs to its original dispatch. Do not change
        # which newer episode receives untagged status or stop events.
        if step is steps[-1]:
            current=step
        if not step.timestamp:
            step.timestamp=event.get('timestamp','')
        if event.get('step_title') and step.kind != 'intent':
            step.step_title=event['step_title']
            step.title=step.step_title
        request_start = stage == 'task_started' and step.kind == 'intent' and step.completed
        if stage in ('intent_started','dispatch_started','task_started') and not request_start:
            step.status='running'
        elif stage == 'intent_finished':
            step.status='understood'
        elif stage == 'dispatch_reused':
            step.returned=True
            step.status=event.get('status') or 'reused'
        elif stage == 'dispatch_completed':
            step.returned=True
            step.status=event.get('status') or 'returned'
        elif stage in ('task_finished','task_waiting','task_paused','task_stopped'):
            step.status=event.get('status') or {
                'task_waiting':'needs_input','task_paused':'paused',
                'task_stopped':'cancelled','task_finished':'returned'}[stage]
        elif stage in ('intent_failed','error'):
            step.status='failed'
        elif not step.status:
            step.status='running' if stage == 'tool_started' else event.get('status','')
        if not request_start and (stage.startswith(('intent_','dispatch_','task_')) or stage == 'error'
                or step.kind == 'episode'):
            step.summary=event.get('summary') or step.summary
        # The phase header already carries boundary facts. Keep only distinct
        # public tool/notice/observation rows in its expandable details.
        has_progress = any(name in event for name in ('step_id','stage_code','group_label',
            'primary_count','backup_count','stop_reason','plan_revision'))
        if has_progress or stage.startswith('tool_') or stage not in (
                'intent_started','intent_finished','dispatch_started','dispatch_completed','dispatch_reused'):
            _append_item(step,event)
    if steps and terminal_status and terminal_status != 'running':
        # The request record supplies final UI state, including local stop or
        # chat completion. No tool success is promoted into task completion.
        if steps[-1].status in ('','running','returned'):
            steps[-1].status=terminal_status
    research_returned=False
    for step in steps:
        tools={item.tool for item in step.items if item.tool and item.tool != 'finish_role'}
        if step.kind != 'intent' and not step.step_title:
            if step.role == 'research':
                if 'research_candidates' in tools:
                    step.title='收集并核对作业' if 'research_download' in tools else '收集作业候选'
                elif tools and tools <= {'web_search','web_fetch','research_search','research_fetch'}:
                    step.title='查询公开资料'
            elif step.role == 'leader' and 'delegate' in tools:
                step.title='审核结果' if research_returned else '分配任务'
            elif step.role == 'diagnosis' and tools:
                step.title='排查问题'
        if step.role == 'research' and step.returned:
            research_returned=True
    return steps

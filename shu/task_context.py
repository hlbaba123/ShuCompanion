"""Bounded task-owned visible memory; structural receipts remain authoritative.

An optional CPU-only TOMC preparer may shorten redacted history. Its output is
never interpreted as a tool result or permission; failure retains the raw view.
"""
import hashlib


def task_context(memory,workflow,task_id,*,prepare=None,char_budget=7000):
    workflow.management.require_active(task_id)
    state=workflow.current(task_id)
    turns=memory.model_messages(last_turns=12,task_id=task_id)
    raw='\n'.join(m['role']+': '+m['content'] for m in turns)
    view=raw[-char_budget:]
    source='visible_task_transcript'
    if prepare is not None and len(raw)>char_budget:
        try:
            prepared=prepare(history=raw,task=state['goal'],task_id=task_id)
            if isinstance(prepared,str) and prepared.strip() and len(prepared)<=char_budget:
                view,source=prepared,'tomc_cpu_prepared'
        except (OSError,ValueError,RuntimeError,TimeoutError):
            pass
    result={'task_id':task_id,'revision':state['revision'],'epoch':state['epoch'],
            'history':view,'history_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'history_source':source,'authority':'historical_data_only'}
    # Codes in the bounded structural list are pointers, not a prose success.
    # Actual import always rechecks the receipt, document, source and current Box.
    try:
        from .code_delivery import CodeDeliveryStore
        delivery=CodeDeliveryStore(workflow,workflow.engine.data_dir).summary(task_id,verify_current=True)
        result['job_outputs']=[{k:item.get(k) for k in ('stage_code','difficulty','primary_code','backup_code','status')}
                               for item in delivery.get('items',[])[:100]]
    except (OSError,ValueError,KeyError,TypeError):
        result['job_outputs']=[]
    return result

"""Verify a completed batch against the durable queue ledger and final readback."""
from collections import Counter

from .core import ActionRejected
from .import_preparation import ImportPreparation
from .leader_plan import read_reference
from .leader_policy import verify_queue_completion
from .maa_queue_receipt import diff_queue
from .maa_queue_store import QueueStore


def verify_queue_result(workflow, state, contract, research, result):
    """Only the same task's registered plan and item results can complete it."""
    failure = {'goal_complete':False, 'status':'needs_input', 'code':'queue_receipt_unverified',
               'summary':'队列结果尚缺完整的本地回读凭据。'}
    try:
        if (result.get('status') != 'import_verified' or result.get('battle_start_submitted') is not False
                or not research.get('queue_plan_ref') or not result.get('queue_receipt_ref')):
            return failure
        journal = QueueStore(workflow)
        plan = read_reference(research['queue_plan_ref'],workflow.engine.data_dir)
        run = journal.current(state['task_id'])
        if not run or run['plan'] != plan or not run['baseline'] or not run['snapshot']:
            return failure
        if any(plan.get(field) != state[field] for field in ('task_id','revision','epoch')):
            return failure
        if read_reference(run['plan_ref'],workflow.engine.data_dir) != plan:
            return failure
        receipt = journal._private_ref(state,result['queue_receipt_ref'],decode=True)
        baseline_ref, final_ref = run['baseline']['ref'], run['snapshot']['ref']
        if receipt.get('baseline_ref') != baseline_ref or receipt.get('final_snapshot_ref') != final_ref:
            return failure
        fields = ('target_key','status','row_identity','owned','operation_ids','evidence_refs','code')
        items = {item['target_key']:{key:item[key] for key in fields} for item in run['items']}
        item_match_basis = {item['target_key']:item.get('match_basis') for item in run['items']}
        receipt_match_basis = {item['target_key']:item.get('match_basis') for item in receipt['items']}
        if any(items.get(item['target_key']) != {key:item[key] for key in fields} for item in receipt['items']):
            return failure
        decision = verify_queue_completion(contract,plan,receipt)
        if not decision['goal_complete']:
            return decision
        baseline = journal._read_snapshot(state,baseline_ref)
        final = journal._read_snapshot(state,final_ref)
        if receipt['binding'] != baseline['binding'] or receipt['binding'] != final['binding']:
            return failure
        owned = [item for item in items.values() if item['owned']]
        difference = diff_queue(baseline,final,plan['targets'],owned)
        old_rows = Counter(row['identity'] for row in baseline['rows'])
        final_rows = Counter(row['identity'] for row in final['rows'])
        if (difference['conflicts'] or difference['external_changes'] or difference['extra_owned']
                or set(difference['reusable']) != set(items)
                or final_rows - old_rows != Counter(item['row_identity'] for item in owned)
                or old_rows - final_rows):
            return failure
        for item in items.values():
            if item['status'] == 'reused' and old_rows[item['row_identity']] != 1:
                return failure
            for reference in item['evidence_refs']:
                journal._private_ref(state,reference)
        preparation = ImportPreparation(workflow,workflow.engine.data_dir)
        for target in plan['targets']:
            binding = read_reference(target['source_binding_ref'],workflow.engine.data_dir)
            source, code, _ = preparation._source({**state,'contract':contract},
                binding['source_task_id'],binding['step_id'],binding['slot'])
            document = preparation._document(target['document_ref'])
            from .maa_queue_receipt import expected_loaded_copilot_document, expected_queue_copilot_id
            from .workflow_store import _encode
            expected_id=expected_queue_copilot_id(document,target['mystery_code'])
            if (source != binding or code['code'] != target['mystery_code']
                    or binding['roster_ref'] != target['roster_ref']
                    or target.get('expected_copilot_id',int(target['mystery_code'].split('://')[1]))
                        != expected_id
                    or expected_id==0 and (item_match_basis.get(target['target_key'])!='normalized_content'
                        or receipt_match_basis.get(target['target_key'])!='normalized_content')
                    or _encode(expected_loaded_copilot_document(document))[1] != target.get('source_semantic_sha256')):
                return failure
        return decision
    except (ActionRejected,AttributeError,KeyError,OSError,RuntimeError,TypeError,ValueError):
        return failure

"""Recheck an ordinal-import clarification using read-only persisted evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for component in ('.agent-runtime', '.capture-runtime', '.gui-runtime', '.companion-runtime'):
    sys.path.insert(0, str(ROOT.parent / component))


def audit(directory: Path) -> dict:
    from shu.core import TaskEngine
    from shu.import_preparation import _readonly_preparation, verify_import_preparation_result
    from shu.leader_plan import read_reference
    from shu.task_management import TaskManagement
    from shu.workflow_store import WorkflowStore, _encode

    directory = directory.resolve(strict=True)
    report_path = directory / 'report.json'
    original = report_path.read_bytes()
    report = json.loads(original)
    first, answer = report['turns']
    initial = first.get('contract') or {}
    selected = answer.get('contract') or {}
    task_id = (first.get('task') or {}).get('id')
    checks = {
        'both_turns_finished_without_errors': all(not turn.get('errors') and not turn.get('timed_out')
                                                   for turn in (first, answer)),
        'initial_ordinal_is_unresolved': (initial.get('import_scope') or {}).get('kind') == 'ordinal',
        'first_turn_requested_user_selection': any(result.get('code') == 'import_selection_required'
                                                  and result.get('waiting_kind') == 'business'
                                                  for result in first.get('results', [])),
        'same_task_continued': bool(task_id and (answer.get('task') or {}).get('id') == task_id
                                     and selected.get('selected_task_id') == task_id
                                     and selected.get('relation') in {'continue', 'modify'}),
        'query_and_activity_preserved': (selected.get('target_stage_codes') == initial.get('target_stage_codes')
                                          and selected.get('activity_name') == initial.get('activity_name')),
        'answer_selects_one_stage': ((selected.get('import_scope') or {}).get('kind') == 'stages'
                                      and len((selected.get('import_scope') or {}).get('stage_codes', [])) == 1),
        'first_turn_did_not_claim_prepared_or_imported': (
            not first.get('preparation_verification', {}).get('verified')
            and not first.get('import_verification', {}).get('verified')),
    }
    data = directory / 'data'
    with sqlite3.connect((data / 'tasks.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        # Read views deliberately bypass constructors that run schema migrations.
        engine = object.__new__(TaskEngine)
        engine._db, engine._lock, engine._closed, engine.data_dir = db, threading.RLock(), False, data
        workflow = object.__new__(WorkflowStore)
        workflow.engine, workflow._db = engine, db
        management = object.__new__(TaskManagement)
        management.store, management.engine, management._db = workflow, engine, db
        workflow.management = management
        current = workflow.current(task_id)
        live_preparation = verify_import_preparation_result(workflow, task_id)
        bridge = _readonly_preparation(workflow)
        plan = bridge.plans.current(task_id)
        delivery = bridge.codes.summary(task_id)
        question = db.execute('SELECT * FROM workflow_questions WHERE task_id=? ORDER BY rowid LIMIT 1',
                              (task_id,)).fetchone()
        first_scope = initial.get('import_scope') or {}
        selected_scope = selected.get('import_scope') or {}
        research_row = db.execute("SELECT * FROM workflow_dispatches WHERE task_id=? AND revision=? "
            "AND role='research' ORDER BY rowid DESC LIMIT 1", (task_id, current['revision'])).fetchone()
        dispatch = workflow.get_dispatch(research_row['dispatch_id'])
        research = workflow.dispatch_result(dispatch['dispatch_id'], current_only=False)
        context = {**current, 'epoch': dispatch['epoch'], 'dispatch_id': dispatch['dispatch_id']}
        items = []
        for reference in research.get('candidate_refs', []):
            record = read_reference(reference, data)
            bridge._verify_material(context, record)
            stage = record['goal_stage']['resolved_stage']
            items.append({'stage_code': stage['display_code'], 'difficulty': stage['difficulty'],
                          'mystery_code': 'prts://' + record['copilot_id'],
                          'stage_key': stage['stage_key']})
        event_rows = db.execute("SELECT * FROM workflow_outbox WHERE task_id=? AND revision=? "
            "AND epoch=? AND type='import.preparation_verified'",
            (task_id, current['revision'], dispatch['epoch'] + 1)).fetchall()
        preparation_event_verified = False
        for event in event_rows:
            payload = json.loads(event['payload'])
            preparation_event_verified |= (_encode(payload)[1] == event['payload_hash']
                and payload.get('research_dispatch_id') == dispatch['dispatch_id']
                and payload.get('research_epoch') == dispatch['epoch']
                and payload.get('candidate_refs') == research.get('candidate_refs'))
        submitted_operations = db.execute('SELECT COUNT(*) FROM workflow_operations WHERE task_id=? '
            'AND submitted_at IS NOT NULL', (task_id,)).fetchone()[0]
        maa_dispatches = db.execute("SELECT COUNT(*) FROM workflow_dispatches WHERE task_id=? AND role='maa'",
                                   (task_id,)).fetchone()[0]
        checks.update(
            durable_question_answered=bool(question and question['kind'] == 'business'
                and question['status'] == 'answered'
                and question['answer_request_id'] == selected.get('request_id')),
            durable_contract_matches_answer=(current['contract'].get('import_scope') == selected_scope),
            historical_preparation_material_and_event_verified=(bool(items) and preparation_event_verified
                and dispatch['status'] == 'completed' and research.get('status') == 'researched'
                and dispatch['payload'].get('contract') == current['contract']),
            only_selected_stage_prepared=(len(items) == 1
                and items[0]['stage_code'] == selected_scope.get('stage_codes', [None])[0]
                and items[0]['difficulty'] == selected_scope.get('difficulty')),
            plan_query_and_import_scopes_separate=bool(plan
                and len(plan.get('import_stage_keys', [])) == 1
                and set(plan['import_stage_keys']) <= set(plan['ordered_stage_keys'])
                and items and items[0]['stage_key'] == plan['import_stage_keys'][0]),
            current_query_has_real_codes=(delivery.get('complete') is True
                and len(delivery.get('items', [])) == len(plan['ordered_stage_keys'])
                and not delivery.get('missing')
                and all(item.get('primary_code') for item in delivery['items'])),
            no_maa_dispatch_or_native_input=(maa_dispatches == 0 and submitted_operations == 0),
            recorded_task_waited_for_execution=((answer.get('task') or {}).get('status') == 'needs_input'
                and answer.get('preparation_verification', {}).get('goal_complete') is False),
            shutdown_revoked_execution_authority=(current['status'] == 'paused'
                and current['control_kind'] == 'pause' and current['writes_enabled'] is False
                and live_preparation.get('verified') is False),
        )
        result = {
            'schema': 'import_clarification_audit/v1',
            'passed': all(checks.values()), 'checks': checks,
            'scope': 'ordinal clarification, same-task continuation, real query and selected import preparation',
            'gui_import_verified': False, 'native_input': False,
            'original_report_sha256': hashlib.sha256(original).hexdigest(),
            'original_report_passed': report.get('passed'),
            'task_id': task_id, 'initial_ordinal': first_scope.get('ordinal'),
            'question': question['prompt'] if question else None,
            'query_stage_count': len(delivery['items']),
            'prepared_items': [{'stage_code': item['stage_code'], 'difficulty': item['difficulty'],
                                'mystery_code': item['mystery_code']} for item in items],
            'submitted_operations': submitted_operations,
            'maa_dispatches': maa_dispatches,
            'current_status': current['status'],
            'current_execution_preparation_verified': live_preparation['verified'],
            'historical_only': True,
            'original_report_reason': 'The general runner requires completed preparation on every turn; the first turn intentionally waits for a business answer.',
        }
    if report_path.read_bytes() != original:
        raise ValueError('Original report changed during audit')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = audit(args.directory)
    destination = args.directory / 'clarification-audit.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'query_stage_count': result['query_stage_count'],
                      'prepared_items': result['prepared_items'], 'output': str(destination)}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

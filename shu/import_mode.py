"""Select the GUI mode from verified retained documents, never request wording."""


def single_document_plan(plan):
    targets=plan.get('targets') or []
    if not targets or len(targets)>2:
        return False
    first=targets[0]
    fields=('mystery_code','stage_id','stage_code','document_ref')
    modes=[t.get('difficulty') for t in targets]
    return (len(set(modes))==len(modes) and all(type(m) is int and m in (1,2) for m in modes)
            and all(first.get(f) and all(t.get(f)==first[f] for t in targets) for f in fields)
            and all(type(t.get('source_difficulty')) is int and t['source_difficulty'] in (1,2,3)
                    and t['source_difficulty'] & t['difficulty'] for t in targets))


def verify_single_document_result(workflow,state,research,receipt):
    """Cover both catalog targets by one actually loaded compatible document."""
    from .leader_plan import read_reference
    from .import_preparation import ImportPreparation
    from .maa_visual_readback import verify_import
    from .core import ActionRejected
    try:
        root=workflow.engine.data_dir
        current=workflow.current(state['task_id'])
        if any(current[k]!=state[k] for k in ('task_id','revision','epoch')):
            return False
        row=workflow.engine._db.execute("SELECT dispatch_id FROM workflow_dispatches WHERE task_id=? "
            "AND revision=? AND epoch=? AND role='maa' AND status='completed' ORDER BY rowid DESC LIMIT 1",
            (state['task_id'],state['revision'],state['epoch'])).fetchone()
        if not row or workflow.dispatch_result(row['dispatch_id'])!=receipt:
            return False
        context={**state,'contract':current['contract'],'dispatch_id':row['dispatch_id']}
        reference=research['single_document_plan_ref']
        plan=read_reference(reference,root)
        if not single_document_plan(plan) or receipt.get('single_document_plan_ref')!=reference:
            return False
        if receipt.get('status')!='import_verified' or receipt.get('battle_start_submitted') is not False:
            return False
        if [t['candidate_ref'] for t in plan['targets']]!=research.get('candidate_refs'):
            return False
        preparation=ImportPreparation(workflow,root)
        for target in plan['targets']:
            # This is completion/readback, after the MAA dispatch has ended.
            # The active-dispatch input guard must NOT be invoked here.
            preparation._verify_material(context,read_reference(target['candidate_ref'],root))
        source,source_plan,wanted=preparation._linked_plan(context)
        if ({t['stage_key'] for t in plan['targets']}!=wanted
                or any(plan.get(k)!=state[k] for k in ('task_id','revision','epoch'))):
            return False
        proof=read_reference(receipt['receipt_ref'],root)
        if proof.get('battle_start_submitted') is not False:
            return False
        expected=proof['expected']
        target=plan['targets'][0]
        if (expected['code']!=target['mystery_code'] or expected['stage_code']!=target['stage_code']
                or expected['source_document']!=read_reference(target['document_ref'],root)):
            return False
        observations=[read_reference(ref,root,maximum=32*1024*1024) for ref in proof['observation_refs']]
        frames={o['screenshot_id']:o for o in observations if o.get('screenshot_id')}
        import hashlib
        from pathlib import Path
        for frame in frames.values():
            path=Path(frame['image_path']).resolve(strict=True)
            if not path.is_relative_to(Path(root).resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=frame['image_sha256']:
                return False
        loaded=(read_reference(proof['load_receipt_ref'],root,maximum=32*1024*1024)
                if proof.get('load_receipt_ref') else None)
        return verify_import(frames[proof['before']],[frames[k] for k in proof['after']],expected=expected,
                             code_entry=frames.get(proof.get('code_entry')),load_receipt=loaded)['verified']
    except (ActionRejected,OSError,ValueError,KeyError,TypeError):
        return False

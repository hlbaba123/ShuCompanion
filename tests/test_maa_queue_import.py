"""The queue recipe must stop before native input when its proof is incomplete."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import unittest
from unittest.mock import Mock, patch
import uuid
import zlib

from shu import maa_queue_receipt
from shu.maa_queue_store import QueueStore
from shu.workflow_store import _encode


class QueueCodeImporterTests(unittest.TestCase):
    def prepare_batch(self, preparation, *, combined, single_mode=False):
        from shu.code_delivery import CodeDeliveryStore
        from shu.copilot_catalog import parse_candidate
        from shu.copilot_pool import CopilotPool
        from shu.maa_queue_plan import build_queue_plan
        from shu.research_tools import ResearchTools

        source = preparation.workflow.submit('查询两种模式',{'current_deliverable':'copilot_codes'})
        preparation.source = source['task_id']
        normal = {**preparation.stage,'opening_status':'permanent','navigation_region_id':'public-zone:fixture'}
        raid = {**normal,'stage_key':'stage-raid','stage_id':'unit_stage_raid','difficulty':2}
        stages = [normal,raid]
        structure = {'ok':True,'catalog_complete':True,'activity_name':'活动','activity_id':'event',
            'stages':stages,'excluded_stage_keys':[],
            'groups':[{'group_id':'g','label':'区域','stage_keys':[stage['stage_key'] for stage in stages]}]}
        path = preparation.root/'batch-structure.json'
        path.write_text(json.dumps(structure,ensure_ascii=False),encoding='utf-8')
        reference = {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        preparation.plans.register(preparation.source,expected_revision=1,expected_epoch=1,
            structure_ref=reference,ordered_steps=[stage['stage_key'] for stage in stages],search_policy={})
        candidates,documents = [],{}
        for number,stage in enumerate(stages):
            copilot_id = 42 if combined else 42+number
            body = {**preparation.body,'difficulty':3 if combined else stage['difficulty']}
            documents[copilot_id] = body
            candidate = parse_candidate({'id':copilot_id,'available':True,'content':json.dumps(body)})
            candidate.update(stage_match_verified=True,difficulty_match='compatible')
            candidate['candidate_locator']['resolved_stage'] = stage
            candidates.append(candidate)
        pool = CopilotPool(preparation.root/'batch-cache')
        pool_ref = pool._save({'schema':1,'pool_id':pool.identity('unit_stage',None),
            'stage_id':'unit_stage','document':None,'candidates':candidates,'unverified_candidates':[],
            'sources':['https://prts.maa.plus/copilot/query'],
            'coverage':{'collected':2,'total':2,'complete':True,'exhausted':True,'next_page':None,
                        'pages_received':1,'raw_fetched':2,'unverified':0,'total_changed':False}})
        screened = preparation.rosters.screen_pool('local-default',pool_ref)['screen_ref']
        codes = CodeDeliveryStore(preparation.workflow,preparation.root)
        dispatch = preparation.workflow.ensure_dispatch(preparation.source,'research','batch-source',{})
        for candidate in candidates:
            lease = preparation.plans.claim_next(preparation.source,expected_revision=1,expected_epoch=1,
                                                dispatch_id=dispatch['dispatch_id'])
            code_ref = codes.register_code(lease,candidate['candidate_locator'],screened)
            preparation.plans.close_step(lease,{'status':'primary_only','primary_ref':code_ref,'backup_ref':None})
        request = '把 A-1 普通主选导入 MAA' if single_mode else '把 A-1 普通和突袭主选导入 MAA'
        preparation.context = preparation.new_context(user_request=request,
            allow_start=False,attempt_limit=0,task_spec={'kind':'import'})
        def transport(request, **kwargs):
            copilot_id = int(request.full_url.rsplit('/',1)[-1])
            return json.dumps({'status_code':200,'data':{'id':copilot_id,'content':documents[copilot_id]}}).encode()
        with patch('shu.import_preparation.ResearchTools',side_effect=lambda config,cache,**kwargs:
                   ResearchTools(config,cache,transport=transport,**kwargs)):
            prepared = preparation.bridge.prepare_many(preparation.context,[{
                'source_task_id':preparation.source,'step_id':step['step_id'],'slot':'primary'}
                for step in preparation.bridge.options(preparation.context)['steps']
                if not single_mode or step['difficulty']==1])
        preparation.workflow.finish_dispatch(preparation.context['dispatch_id'],{
            'status':'researched','candidate_refs':[item['candidate_ref'] for item in prepared]})
        dispatch = preparation.workflow.ensure_dispatch(preparation.context['task_id'],'maa','batch-use',
            {'contract':preparation.context['contract']})
        preparation.workflow.claim_dispatch(dispatch['dispatch_id'],'fixture')
        context = {**preparation.context,'dispatch_id':dispatch['dispatch_id']}
        plan = build_queue_plan(context,reference,structure,prepared)
        return context,plan,[item['candidate_record'] for item in prepared]

    def importer(self):
        from shu.maa_queue_import import QueueCodeImporter
        self.supervisor = Mock()
        self.submit = Mock()
        self.store = Mock()
        self.store.current.return_value = None
        self.importer = QueueCodeImporter(self.supervisor, guard=Mock(),
            submit=self.submit, publish=Mock(), progress=Mock(), readback=Mock(),
            queue_monitor=Mock(), queue_store=self.store, preparation=Mock())
        return self.importer

    def test_cancelled_recipe_does_not_observe_or_submit(self):
        importer = self.importer()
        cancelled = threading.Event()
        cancelled.set()
        result = importer.run({'task_id':'unit'}, {'targets':[]}, [], cancel_event=cancelled)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['code'],'queue_cancelled')
        self.assertIs(result['battle_start_submitted'],False)
        self.supervisor.request.assert_not_called()
        self.submit.assert_not_called()

    def test_reconcile_cannot_retry_with_no_registered_baseline(self):
        result = self.importer().reconcile({'task_id':'unit'}, {'targets':[]})
        self.assertEqual(result['code'],'queue_reconciliation_required')
        self.assertIs(result['battle_start_submitted'],False)
        self.submit.assert_not_called()

    def exercise_recipe(self, *, mode=True, original_options=None, requested_options=None,
                        foreground=True, partial_focus=False, focus_controls=None,
                        two_targets=False, combined=False, reject_second=False,
                        long_queue=False, unknown_endpoints=False, transient_observation=None,
                        existing_code=''):
        from shu.maa_queue_import import QueueCodeImporter
        from tests.test_import_preparation import ImportPreparationTests
        from tests.test_maa_queue_receipt import QueueReceiptTests, raw_config

        preparation = ImportPreparationTests()
        self.addCleanup(preparation.doCleanups)
        preparation.setUp()
        preparation.context = preparation.new_context(
            allow_start=False,attempt_limit=0,task_spec={'kind':'import'})
        preparation.body['doc']['details'] = '合成日志说明'
        prepared = preparation.prepare()
        record = prepared['candidate_record']
        context = preparation.use_context(prepared)
        workflow = preparation.workflow
        store = QueueStore(workflow)
        directory = preparation.root/'task-private'/context['task_id']/'maa-queue'/str(context['epoch'])
        directory.mkdir(parents=True)
        target = {'target_key':'unit','stage_key':preparation.stage['stage_key'],
            'stage_id':record['stage_name'],'stage_code':record['goal_stage']['code'],
            'difficulty':1,'source_difficulty':1,'slot':'primary','batch_key':'unit',
            'mystery_code':prepared['verified_copilot']['mystery_code'],
            'candidate_ref':prepared['candidate_ref'],'source_binding_ref':record['source_binding']['evidence_ref'],
            'document_ref':{key:record[key] for key in ('path','sha256')},
            'roster_ref':record['roster_ref'],'source_semantic_sha256':prepared['source_semantic_sha256']}
        plan = {'schema':'maa_queue_plan/v1',
            **{key:context[key] for key in ('task_id','revision','epoch')},
            'targets':[target],'batches':[{'target_keys':['unit']}],
            'exclusions':[],'deferred':[]}
        plan['plan_sha256'] = _encode(plan)[1]
        records = [record]
        if two_targets or combined:
            context,plan,records = self.prepare_batch(preparation,combined=combined,single_mode=not two_targets)
            record = records[0]
            directory = preparation.root/'task-private'/context['task_id']/'maa-queue'/str(context['epoch'])
            directory.mkdir(parents=True)

        files = QueueReceiptTests()
        self.addCleanup(files.doCleanups)
        files.setUp()
        def publish(kind,payload):
            return maa_queue_receipt._publish(directory,kind,payload)
        captures = preparation.root/'maa-visual-captures'
        captures.mkdir()
        sent = []
        commands = []
        snapshots_after_input = 0
        input_state = {'code':None,'polls':0}
        queue_rows = []
        scroll = {'position':0}
        delete_targets = {}
        controls = {'multiple_jobs':mode, **(original_options or {})}
        window_state = {'foreground':foreground}
        if long_queue:
            queue_rows.extend(files.row(code=100+number,name=f'Baseline-{number}') for number in range(5))
            files.config.write_bytes(raw_config(queue_rows))

        def chunk(kind,body):
            data = kind+body
            return struct.pack('>I',len(body))+data+struct.pack('>I',zlib.crc32(data))
        png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',80,80,8,2,0,0,0))
               +chunk(b'IDAT',zlib.compress((b'\x00'+b'\xff'*240)*80))+chunk(b'IEND',b''))

        def add_queue_row(code):
            current = next(record for record in records if 'prts://'+record['copilot_id']==code)
            body = json.loads(Path(current['path']).read_text(encoding='utf-8'))
            for raid in (False,True) if body['difficulty']==3 else (body['difficulty']==2,):
                row = files.row(code=int(current['copilot_id']),raid=raid)
                Path(row['FilePath']).write_text(json.dumps(body,ensure_ascii=False),encoding='utf-8')
                queue_rows.append(row)
            files.config.write_bytes(raw_config(queue_rows))
            stamp = datetime.fromtimestamp(files.now).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            def line(logger,message):
                return f'[{stamp}][INF][{logger}] <2> {message}\n'
            text = line('HttpResponseLoggingExtension',f"HTTP: OK GET {current['source_url']} null null 12ms")
            for part in (current['title'],body['doc']['details'],'-'*20,'共 1 名干员'):
                text += line('CopilotViewModel',part)
            with files.log.open('ab') as stream:
                stream.write(text.encode())

        def snapshot(*args,**kwargs):
            nonlocal snapshots_after_input
            files.now+=.2
            if input_state['code']:
                snapshots_after_input+=1
                input_state['polls']+=1
                self.assertIsNotNone(workflow.slot_state()['barrier_operation_id'])
                if input_state['polls']==3:
                    add_queue_row(input_state['code'])
                    input_state['code'] = None
            identifier=uuid.uuid4().hex
            image=captures/(identifier+'.png')
            current_png = png
            if long_queue:
                pixels = bytearray((b'\x00'+b'\xff'*240)*80)
                pixels[1] = 200+scroll['position']
                pixels[2] = 200+len(queue_rows)
                current_png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',80,80,8,2,0,0,0))
                               +chunk(b'IDAT',zlib.compress(bytes(pixels)))+chunk(b'IEND',b''))
            image.write_bytes(current_png)
            stamp=round(files.now*1000)*1_000_000
            os.utime(image,ns=(stamp,stamp))
            files.now+=.01
            return {'status':'completed','result':{'screenshot_id':identifier,'image_path':str(image),
                'image_sha256':hashlib.sha256(current_png).hexdigest(),
                'window':{**files.process,'client_rect':{'x':0,'y':0,'width':80,'height':80},
                          'foreground':window_state['foreground'],'minimized':False,'dpi':96},
                'permission':{'input_allowed':True}}}

        def recognize(path,*,expected,cancel_event):
            names = ('multiple_jobs','auto_formation','ignore_requirements','use_support')
            targets = {'code_input':{'point':{'x':15,'y':15},'rect':{'x':10,'y':10,'width':20,'height':10}}}
            for index,name in enumerate(names):
                targets[name] = {'point':{'x':15,'y':30+index*3},
                                 'rect':{'x':10,'y':28+index*3,'width':10,'height':3}}
            targets['queue_list'] = {'point':{'x':52,'y':34},'rect':{'x':40,'y':22,'width':25,'height':25}}
            rows = expected['rows'][scroll['position']:scroll['position']+3] if long_queue else expected['rows']
            delete_targets.clear()
            for index,row in enumerate(rows):
                key = 'row_delete:'+row['identity']
                delete_targets[key] = expected['rows'].index(row)
                targets[key] = {'point':{'x':73,'y':13+index*8},
                                'rect':{'x':70,'y':10+index*8,'width':6,'height':6}}
            return {'schema':'maa_queue_vision/v1','ok':True,'queue_mode':controls['multiple_jobs'],'idle':True,'running':False,
                'page':'copilot','image_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                'width':80,'height':80,'visible_complete':controls['multiple_jobs'] is True and len(rows)==len(expected['rows']),
                **({'projection_sha256':expected['projection_sha256']} if controls['multiple_jobs'] else {}),
                'field_empty':not bool(existing_code),'code_text':existing_code,
                'options':{name:{'state':controls.get(name)} for name in names},
                'rows_measured':True,'queue_rows':[{'name':row['name'],'mode':2 if row['is_raid'] else 1,
                    'is_checked':row['is_checked']} for row in rows],
                'scroll_state':{'top':None if unknown_endpoints else scroll['position']==0,
                    'bottom':None if unknown_endpoints else scroll['position']+len(rows)>=len(expected['rows'])},
                'targets':targets,
                'regions':{name:targets[name]['rect'] for name in ('code_input','queue_list')},
                'forbidden_regions':[{'rect':{'x':50,'y':50,'width':10,'height':10}}]}

        def submit(args,frame,cancel,*,operation,record=None):
            action=args['action']
            self.assertIn(action,{'focus','click','replace_code','scroll_queue'})
            sent.append(action)
            commands.append(dict(args))
            operation_id=operation['operation_id']
            if reject_second and action=='replace_code' and args['code']=='prts://43':
                workflow.finish_operation(operation_id,'not_issued',{})
                return {'status':'rejected','submission_entered':False}
            workflow.issue_operation(operation_id)
            guard = preparation.bridge.submission_guard(context,record) if record else None
            files.now+=.001
            clock=lambda:datetime.fromtimestamp(files.now,timezone.utc).isoformat()
            with patch('shu.workflow_store._utc_now',side_effect=clock):
                workflow.enter_submission(operation_id,import_guard=guard,
                                          queue_guard=store.submission_guard(context,operation))
            if action=='focus':
                if not partial_focus:
                    window_state['foreground'] = True
                if focus_controls:
                    controls.update(focus_controls)
            if action=='click' and args['target'] in controls:
                control = args['target']
                controls[control] = not controls[control]
                if control == 'multiple_jobs' and controls[control]:
                    controls['auto_formation'] = True
            if action=='click' and args['target'].startswith('row_delete:'):
                del queue_rows[delete_targets[args['target']]]
                files.config.write_bytes(raw_config(queue_rows))
            if action=='scroll_queue':
                scroll['position'] = max(0,min(len(queue_rows)-3,scroll['position']-args['notches']))
            if action in {'focus','click','scroll_queue'} and operation['queue_guard']['phase'] != 'delete':
                workflow.finish_operation(operation_id,'verified',{})
            if action=='replace_code':
                input_state.update(code=args['code'],polls=0)
            return {'status':'completed','submission_entered':True}

        supervisor=Mock(target_identity=files.process,directory=preparation.root)
        supervisor.request.side_effect=snapshot
        vision=Mock(requires_frame_refresh=False)
        vision.observe.side_effect=recognize
        progress = Mock()
        importer=QueueCodeImporter(supervisor,guard=lambda:None,submit=submit,publish=publish,
            progress=progress,readback=vision,queue_monitor=maa_queue_receipt,queue_store=store,
            preparation=preparation.bridge)
        if transient_observation:
            from shu.maa_queue_import import QueueImportBlocked
            observe = importer._observe
            injected = []
            def settling_observe(*args, **kwargs):
                if input_state['code'] and not injected:
                    injected.append(True)
                    raise QueueImportBlocked(transient_observation)
                return observe(*args, **kwargs)
            importer._observe = settling_observe
        capture_snapshot = maa_queue_receipt.capture_queue_snapshot
        def capture_after_io(*args,**kwargs):
            files.now += .001
            return capture_snapshot(*args,**kwargs)
        with patch.object(maa_queue_receipt,'capture_queue_snapshot',side_effect=capture_after_io):
            result=importer.run(context,plan,records,options=requested_options)
        self.acceptance = (workflow,context,plan,store,preparation)
        return result, commands, controls, workflow, progress, snapshots_after_input

    def test_pending_receipt_polls_read_only_until_one_auto_add(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe()
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual([command['action'] for command in commands],['click','replace_code'])
        self.assertEqual(captures,3)
        self.assertIsNone(workflow.slot_state()['barrier_operation_id'])
        self.assertIs(result['battle_start_submitted'],False)
        self.assertEqual(result['coverage'],{'verified':1,'total':1})

    def test_disabled_queue_during_one_submitted_load_only_reobserves(self):
        for error in ('config_changed_during_read','queue_scroll_endpoints_unverified','queue_scan_overlap_unverified',
                      'queue_page_or_coverage_unverified'):
            with self.subTest(error=error):
                result, commands, *_ = self.exercise_recipe(transient_observation=error)
                self.assertEqual(result['status'],'import_verified',result)
                self.assertEqual(sum(command['action']=='replace_code' for command in commands),1)

    def test_enable_mode_restores_previous_options_and_applies_explicit_override(self):
        prior = {'auto_formation':False,'ignore_requirements':False,'use_support':True}
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            mode=False,original_options=prior,requested_options={'ignore_requirements':True})
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual(controls,{'multiple_jobs':True,'auto_formation':False,
                                  'ignore_requirements':True,'use_support':True})
        self.assertEqual([command['target'] for command in commands],
            ['multiple_jobs','auto_formation','ignore_requirements','code_input','code_input'])
        self.assertEqual(workflow.pending_operations(),[])

    def test_unknown_original_option_stops_before_mode_is_changed(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            mode=False,original_options={'auto_formation':False,'ignore_requirements':None,'use_support':False})
        self.assertEqual(result['status'],'needs_input',result)
        self.assertEqual(result['code'],'queue_options_unverified')
        self.assertEqual(commands,[])
        self.assertFalse(controls['multiple_jobs'])

    def test_background_window_is_focused_once_and_observed_before_click(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(foreground=False)
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual([command['action'] for command in commands],['focus','click','replace_code'])

    def test_focus_that_did_not_acquire_foreground_cannot_click(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            foreground=False,partial_focus=True)
        self.assertEqual(result['status'],'needs_input',result)
        self.assertEqual(result['code'],'queue_foreground_or_permission')
        self.assertEqual(result['action_outcome'],'verified')
        self.assertEqual([command['action'] for command in commands],['focus'])

    def test_user_mode_change_during_focus_is_observed_before_any_toggle(self):
        prior = {'auto_formation':False,'ignore_requirements':False,'use_support':True}
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            mode=False,original_options=prior,foreground=False,
            focus_controls={'multiple_jobs':True,'auto_formation':True})
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual([command['action'] for command in commands],['focus','click','replace_code'])
        self.assertIs(controls['auto_formation'],True)

    def test_independent_final_review_rejects_changed_box(self):
        from shu.maa_queue_completion import verify_queue_result
        result, commands, controls, workflow, progress, captures = self.exercise_recipe()
        workflow,context,plan,store,preparation = self.acceptance
        research = {'queue_plan_ref':store.current(context['task_id'])['plan_ref']}
        self.assertTrue(verify_queue_result(workflow,context,context['contract'],research,result)['goal_complete'])
        preparation.publish_box(observed_at=2000)
        self.assertFalse(verify_queue_result(workflow,context,context['contract'],research,result)['goal_complete'])

    def test_progress_reports_observed_target_events(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe()
        self.assertEqual(result['status'],'import_verified',result)
        events = [call.args[0] for call in progress.call_args_list]
        self.assertIn('queue_snapshot',events)
        self.assertIn('queue_code_submitted',events)
        self.assertIn('queue_item_verified',events)
        self.assertIn('queue_verified',events)

    def test_two_modes_with_combined_document_enter_with_one_code(self):
        from shu.maa_queue_completion import verify_queue_result
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(two_targets=True,combined=True)
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual([command['code'] for command in commands if command['action']=='replace_code'],['prts://42'])
        self.assertEqual(result['coverage'],{'verified':2,'total':2})
        workflow,context,plan,store,preparation = self.acceptance
        research = {'queue_plan_ref':store.current(context['task_id'])['plan_ref']}
        self.assertTrue(verify_queue_result(workflow,context,context['contract'],research,result)['goal_complete'])

    def test_long_queue_uses_fresh_overlapping_views_and_preserves_existing_rows(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(long_queue=True)
        self.assertEqual(result['status'],'import_verified',result)
        self.assertTrue(any(command['action']=='scroll_queue' for command in commands))
        self.assertEqual([command['code'] for command in commands if command['action']=='replace_code'],['prts://42'])
        workflow,context,plan,store,preparation = self.acceptance
        run = store.current(context['task_id'])
        self.assertEqual(len(run['baseline']['row_ids']),5)
        self.assertEqual(len(run['snapshot']['row_ids']),6)

    def test_long_queue_without_proven_endpoints_does_not_scroll_or_input(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            long_queue=True,unknown_endpoints=True)
        self.assertEqual(result['code'],'queue_scroll_endpoints_unverified',result)
        self.assertEqual(commands,[])

    def test_two_distinct_documents_are_verified_in_order(self):
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(two_targets=True)
        self.assertEqual(result['status'],'import_verified',result)
        self.assertEqual([command['code'] for command in commands if command['action']=='replace_code'],
                         ['prts://42','prts://43'])
        self.assertEqual(result['coverage'],{'verified':2,'total':2})

    def test_existing_station_code_is_replaced_without_intermediate_clear(self):
        result, commands, *_ = self.exercise_recipe(existing_code='prts://999')
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual([command['code'] for command in commands if command['action']=='replace_code'],
                         ['prts://42'])
        self.assertFalse(any(command['action']=='clear_code' for command in commands))

    def test_unknown_user_field_is_preserved_without_code_input(self):
        result, commands, *_ = self.exercise_recipe(existing_code='D:/user/my-job.json')
        self.assertEqual(result['code'], 'queue_input_precondition_changed', result)
        self.assertFalse(any(command['action'] in {'replace_code','clear_code'} for command in commands))

    def test_combined_document_removes_only_its_extra_unplanned_mode(self):
        from shu.maa_queue_completion import verify_queue_result
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(combined=True)
        self.assertEqual(result['status'],'import_verified',result)
        deleted = [command['target'] for command in commands if command['target'].startswith('row_delete:')]
        self.assertEqual(len(deleted),1)
        workflow,context,plan,store,preparation = self.acceptance
        final = store._read_snapshot(context,store.current(context['task_id'])['snapshot']['ref'])
        self.assertEqual([row['is_raid'] for row in final['rows']],[False])
        research = {'queue_plan_ref':store.current(context['task_id'])['plan_ref']}
        self.assertTrue(verify_queue_result(workflow,context,context['contract'],research,result)['goal_complete'])

    def test_second_input_denied_preserves_first_verified_item_as_partial(self):
        from shu.maa_queue_completion import verify_queue_result
        result, commands, controls, workflow, progress, captures = self.exercise_recipe(
            two_targets=True,reject_second=True)
        self.assertEqual(result['status'],'partial',result)
        self.assertEqual(result['coverage'],{'verified':1,'total':2})
        workflow,context,plan,store,preparation = self.acceptance
        research = {'queue_plan_ref':store.current(context['task_id'])['plan_ref']}
        self.assertFalse(verify_queue_result(workflow,context,context['contract'],research,result)['goal_complete'])

    def test_purge_removes_only_proven_native_capture_origins(self):
        from shu.task_cleanup import TaskCleanup
        result, commands, controls, workflow, progress, captures = self.exercise_recipe()
        self.assertEqual(result['status'],'import_verified',result)
        workflow,context,plan,store,preparation = self.acceptance
        originals = list((preparation.root/'maa-visual-captures').glob('*.png'))
        self.assertTrue(originals)
        untouched = preparation.root/'maa-visual-captures'/'unrelated.png'
        untouched.write_bytes(b'unrelated original capture')
        workflow.finish_dispatch(context['dispatch_id'],result)
        workflow.management.trash(context['task_id'])
        cleaned = TaskCleanup(workflow).purge(context['task_id'])
        self.assertEqual(cleaned['status'],'completed',cleaned)
        self.assertFalse(any(path.exists() for path in originals))
        self.assertEqual(untouched.read_bytes(),b'unrelated original capture')

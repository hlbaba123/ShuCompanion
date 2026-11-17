"""Bounded screenshot recipe for MAA multi-job auto-add.

The native callback performs the atomic submission. This recipe keeps its
effect barrier open until an independent MAA queue receipt is verified.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import os
from pathlib import Path
import re
import threading
import time

from . import maa_queue_receipt as receipts
from .core import ActionRejected
from .leader_plan import read_reference
from .maa_visual_import import VisualCodeImporter, VisualImportError, _compare_frame_images
from .maa_queue_readback import stitch_queue_views


class QueueImportBlocked(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class QueueCodeImporter:
    def __init__(self, supervisor, *, guard, submit, publish, progress, readback,
                 queue_monitor, queue_store, preparation):
        self.supervisor = supervisor
        self.guard = guard
        self.submit = submit
        self.publish = publish
        self.progress = progress
        self.monitor = queue_monitor
        self.store = queue_store
        self.preparation = preparation
        self.vision = VisualCodeImporter(supervisor, guard=guard, publish=publish, readback=readback)

    def _guard(self, cancel):
        if cancel.is_set():
            raise QueueImportBlocked('queue_cancelled')
        self.guard()

    def _native_frame(self, cancel):
        self._guard(cancel)
        answer = self.supervisor.request({'op':'snapshot'}, timeout=12, cancel_event=cancel)
        if answer.get('status') != 'completed':
            raise QueueImportBlocked('queue_capture_failed')
        frame = answer['result']
        if any(frame['window'].get(key) != self.supervisor.target_identity[key]
               for key in ('hwnd','pid','creation_time','executable')):
            raise QueueImportBlocked('queue_window_changed')
        return frame

    def _pin_frame(self, frame, directory, context):
        identifier = frame['screenshot_id']
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise QueueImportBlocked('queue_capture_invalid')
        source = Path(frame['image_path'])
        origin = Path(self.supervisor.directory).resolve() / 'maa-visual-captures'
        if source != origin / (identifier + '.png'):
            raise QueueImportBlocked('queue_capture_invalid')
        raw, state = receipts._read_stable(source)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != frame['image_sha256'] or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise QueueImportBlocked('queue_capture_invalid')
        # Publish both copies while task lifecycle changes are excluded. The
        # immutable origin closure lets task cleanup account for native files
        # stored by the helper outside this epoch's private directory.
        with self.store.engine._lock:
            self.store.workflow.management.require_active(context['task_id'])
            path = receipts._no_links(directory / ('queue-frame-' + identifier + '.png'))
            try:
                with path.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                stamp = state['mtime_ns']
                os.utime(path, ns=(stamp, stamp))
            except FileExistsError:
                saved, facts = receipts._read_stable(path)
                if saved != raw or facts['mtime_ns'] != state['mtime_ns']:
                    raise QueueImportBlocked('queue_capture_changed') from None
            reference = {'path':str(path),'sha256':digest}
            origin_ref = self.publish('maa-queue-capture-origin',{
                'schema':'maa_queue_capture_origin/v1','screenshot_id':identifier,
                'original_ref':{'path':str(source),'sha256':digest},'private_ref':reference})
            self.store.register_evidence(context, [reference,origin_ref])
        return reference

    @staticmethod
    def _snapshot_refs(snapshot):
        return [snapshot['evidence_ref'], *snapshot['screenshot_refs'],
                *(item['ref'] for item in snapshot['body_evidence'])]

    @staticmethod
    def _remaining(deadline, maximum=45):
        if deadline is None:
            return maximum
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise QueueImportBlocked('queue_observation_timeout')
        return min(maximum, remaining)

    def _observe(self, context, binding, directory, cancel, *, recognition_timeout=45, deadline=None):
        frame,snapshot = self._observe_fresh(context,binding,directory,cancel,
            recognition_timeout=recognition_timeout,deadline=deadline)
        if (frame.get('queue_mode') is True and frame.get('window',{}).get('foreground')
                and frame.get('permission',{}).get('input_allowed') and snapshot['visible_complete'] is not True):
            return self._scan_queue(context,frame,snapshot,binding,directory,cancel,deadline=deadline)
        return frame,snapshot

    def _observe_fresh(self, context, binding, directory, cancel, *, recognition_timeout=45, deadline=None):
        try:
            return self._observe_one(context,binding,directory,cancel,
                recognition_timeout=self._remaining(deadline,recognition_timeout))
        except (QueueImportBlocked,receipts.MaaQueueReceiptError) as error:
            if error.code not in {'queue_vision_unverified','config_changed_during_read',
                                  'file_changed_during_read','body_changed_during_read'}:
                raise
            # An incomplete image transcription does not imply an application
            # effect or require a user answer. Retry observation once, sharing
            # the same input barrier/deadline and never replaying an input.
            self.progress('queue_snapshot_retry','tool_finished',{
                'summary':'本次截图识别不完整，正在重新读取当前画面；没有重复输入。'})
            return self._observe_one(context,binding,directory,cancel,
                recognition_timeout=self._remaining(deadline,recognition_timeout))

    def _observe_one(self, context, binding, directory, cancel, *, recognition_timeout=45):
        frame = self._native_frame(cancel)
        ref = self._pin_frame(frame, directory, context)
        source = self.monitor.capture_queue_snapshot(binding, directory, screenshot_refs=[ref],
                                                     visible_complete=False)
        self.store.register_evidence(context, self._snapshot_refs(source))
        view = self.vision._interpret(frame, {'projection_sha256':source['projection_sha256'],
            'rows':source['rows']}, cancel, recognition_timeout)
        observation = self.publish('maa-queue-vision', {'frame_ref':ref, 'snapshot_ref':source['evidence_ref'],
                                                       'view':view})
        self.store.register_evidence(context, [observation])
        geometry = frame['window']['client_rect']
        if (view.get('schema') != 'maa_queue_vision/v1' or view.get('ok') is not True
                or view.get('image_sha256') != frame['image_sha256']
                or (view.get('width'),view.get('height')) != (geometry['width'],geometry['height'])
                or view.get('queue_mode') is not False
                   and view.get('projection_sha256') != source['projection_sha256']):
            raise QueueImportBlocked('queue_vision_unverified')
        self._guard(cancel)
        complete = view.get('visible_complete') is True and view.get('queue_mode') is True
        if complete:
            observed = self.monitor.capture_queue_snapshot(binding, directory, screenshot_refs=[ref],
                                                           visible_complete=True)
            self.store.register_evidence(context, self._snapshot_refs(observed))
            if observed['projection_sha256'] != source['projection_sha256'] or observed['rows'] != source['rows']:
                raise QueueImportBlocked('queue_projection_changed')
        else:
            observed = source
        self.store.record_snapshot(context, observed)
        self.progress('queue_snapshot','tool_finished',{
            'row_count':len(observed['rows']), 'visible_complete':observed['visible_complete']})
        return {**view, **frame}, observed

    @staticmethod
    def _require_page(frame):
        if (frame.get('page') != 'copilot' or frame.get('idle') is not True
                or frame.get('running') is not False or frame.get('window',{}).get('minimized')):
            raise QueueImportBlocked('queue_page_or_coverage_unverified')
        if frame.get('permission',{}).get('input_allowed') is not True:
            raise QueueImportBlocked('queue_foreground_or_permission')

    def _require_idle(self, frame, snapshot, options):
        self._require_page(frame)
        if (frame['page'] != 'copilot' or frame.get('queue_mode') is not True
                or frame.get('idle') is not True or frame.get('running') is not False
                or snapshot['visible_complete'] is not True):
            raise QueueImportBlocked('queue_page_or_coverage_unverified')
        if not frame['window'].get('foreground') or not frame['permission'].get('input_allowed'):
            raise QueueImportBlocked('queue_foreground_or_permission')
        if any(frame['options'].get(key, {}).get('state') is not value for key,value in options.items()):
            raise QueueImportBlocked('queue_options_unverified')

    def _refresh(self, context, frame, snapshot, binding, directory, cancel, *, target, reobserved=False):
        fresh = self._native_frame(cancel)
        ref = self._pin_frame(fresh, directory, context)
        comparison = _compare_frame_images(frame, fresh, target=target,
            allow_queue_viewport=target == 'code_input' and snapshot['visible_complete'] is True)
        check = self.publish('maa-queue-refresh', {'comparison':comparison,'frame_ref':ref,
                                                  'snapshot_ref':snapshot['evidence_ref']})
        self.store.register_evidence(context, [check])
        if (comparison.get('ok') is not True
                and comparison.get('reason') in {'frame_pixels_changed','target_region_changed'}
                and target == 'code_input' and not reobserved and fresh['window'].get('foreground')):
            renewed, current = self._observe(context,binding,directory,cancel)
            fields = ('page','idle','running','queue_mode','field_empty','code_text',
                      'codes','targets','regions','options','forbidden_regions')
            rows = lambda view: [{key:value for key,value in row.items() if key != 'key'}
                                  for row in view.get('queue_rows',[])]
            if (all(renewed.get(key) == frame.get(key) for key in fields)
                    and rows(renewed) == rows(frame)
                    and current['projection_sha256'] == snapshot['projection_sha256']
                    and current['rows'] == snapshot['rows']):
                return self._refresh(context,renewed,current,binding,directory,cancel,
                                     target=target,reobserved=True)
        if comparison.get('ok') is not True or not fresh['window'].get('foreground'):
            raise QueueImportBlocked('queue_visual_refresh_changed')
        references = [ref]
        if frame.get('queue_scan_ref'):
            prior = snapshot['screenshot_refs']
            references = [*(prior if comparison.get('ignored_queue_pixels', 0) else prior[:-1]), ref]
        refreshed = self.monitor.capture_queue_snapshot(binding, directory, screenshot_refs=references,
            visible_complete=snapshot['visible_complete'] is True and frame.get('visible_complete') is True)
        self.store.register_evidence(context, self._snapshot_refs(refreshed))
        if refreshed['projection_sha256'] != snapshot['projection_sha256'] or refreshed['rows'] != snapshot['rows']:
            raise QueueImportBlocked('queue_projection_changed')
        self.store.record_snapshot(context, refreshed)
        renewed = {**frame, **fresh}
        if comparison.get('ignored_queue_pixels', 0):
            # No row-coordinate action may reuse a pre-scroll interpretation.
            renewed['targets'] = {key:value for key,value in frame['targets'].items()
                                  if not key.startswith('row_delete:')}
            renewed['queue_viewport_refreshed'] = True
        return renewed, refreshed

    @staticmethod
    def _point(frame, target='code_input'):
        point = frame.get('targets', {}).get(target, {}).get('point')
        if not point:
            raise QueueImportBlocked('queue_input_control_unverified')
        x,y = round(point['x']),round(point['y'])
        if any(rect['x']-3 <= x <= rect['x']+rect['width']+3
               and rect['y']-3 <= y <= rect['y']+rect['height']+3
               for region in frame['forbidden_regions'] for rect in (region.get('rect',region),)):
            raise QueueImportBlocked('queue_input_control_unverified')
        return x,y

    def _click_control(self, context, frame, snapshot, binding, directory, cancel, *, target, phase):
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target=target)
        x,y = self._point(frame,target)
        operation = self.store.prepare_item_action(context,None,phase=phase,snapshot=snapshot)
        response = self.submit({'action':'click','target':target,'screenshot_id':frame['screenshot_id'],
                                'x':x,'y':y},frame,cancel,operation=operation)
        self._control_response(response,'queue_control_submission_unverified')
        return self._observe(context,binding,directory,cancel)

    def _scroll(self, context, frame, snapshot, binding, directory, cancel, notches, *, deadline=None):
        self._remaining(deadline)
        self._require_page(frame)
        if frame.get('queue_mode') is not True or not frame['window'].get('foreground'):
            raise QueueImportBlocked('queue_foreground_or_permission')
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target='queue_list')
        self._remaining(deadline)
        x,y = self._point(frame,'queue_list')
        operation = self.store.prepare_item_action(context,None,phase='scroll',snapshot=snapshot)
        response = self.submit({'action':'scroll_queue','target':'queue_list','notches':notches,
            'screenshot_id':frame['screenshot_id'],'x':x,'y':y},frame,cancel,operation=operation)
        self._control_response(response,'queue_scroll_unverified')
        if getattr(getattr(getattr(self,'vision',None),'readback',None),'requires_frame_refresh',False) is True:
            self._wait_scroll_stable(context,frame,directory,cancel,deadline=deadline)
        return self._observe_fresh(context,binding,directory,cancel,deadline=deadline)

    def _wait_scroll_stable(self, context, frame, directory, cancel, *, deadline=None):
        """Bounded post-wait-freezes: compare native pixels before slow OCR."""
        until = min(time.monotonic()+4,deadline) if deadline is not None else time.monotonic()+4
        previous = self._native_frame(cancel)
        self._pin_frame(previous,directory,context)
        stable = 0
        for _ in range(16):
            self._remaining(until)
            if cancel.wait(min(.15,self._remaining(until))):
                raise QueueImportBlocked('queue_cancelled')
            current = self._native_frame(cancel)
            self._pin_frame(current,directory,context)
            comparison = _compare_frame_images({**frame,**previous},current,target='queue_list')
            ref = self.publish('maa-queue-scroll-stable',{'comparison':comparison})
            self.store.register_evidence(context,[ref])
            if comparison.get('ok'):
                stable += 1
                if stable >= 2:
                    return
            elif comparison.get('reason') in {'native_identity_geometry_or_permission_changed','invalid_frame'}:
                raise QueueImportBlocked('queue_visual_refresh_changed')
            else:
                stable = 0
            previous = current
        raise QueueImportBlocked('queue_scroll_unstable')

    def _scan_queue(self, context, frame, snapshot, binding, directory, cancel, *, deadline=None):
        """Cover one unchanged projection using measured endpoints and overlaps."""
        original = snapshot
        steps = 0
        def checked_view(view, observed):
            self._remaining(deadline)
            if (observed['projection_sha256'] != original['projection_sha256']
                    or observed['rows'] != original['rows']):
                raise QueueImportBlocked('queue_projection_changed')
            self._require_page(view)
            state = view.get('scroll_state') or {}
            if (type(state.get('top')) is not bool or type(state.get('bottom')) is not bool
                    or view.get('queue_mode') is not True):
                raise QueueImportBlocked('queue_scroll_endpoints_unverified')
            if view.get('rows_measured') is not True or len(view.get('queue_rows',[])) < 3:
                raise QueueImportBlocked('queue_scan_overlap_unverified')

        checked_view(frame,snapshot)
        while frame['scroll_state']['top'] is not True:
            if steps >= 99:
                raise QueueImportBlocked('queue_scan_limit')
            previous = frame['image_sha256']
            frame,snapshot = self._scroll(context,frame,snapshot,binding,directory,cancel,1,deadline=deadline)
            steps += 1
            checked_view(frame,snapshot)
            if frame['image_sha256'] == previous and frame['scroll_state']['top'] is not True:
                raise QueueImportBlocked('queue_scroll_no_progress')
        views, references, snapshot_refs = [], [], []
        while True:
            views.append({key:frame[key] for key in ('ok','queue_mode','projection_sha256','image_sha256',
                'rows_measured','queue_rows','scroll_state')})
            references.extend(snapshot['screenshot_refs'])
            snapshot_refs.append(snapshot['evidence_ref'])
            if frame['scroll_state']['bottom'] is True:
                break
            if steps >= 99 or len(views) >= 99:
                raise QueueImportBlocked('queue_scan_limit')
            previous = frame['image_sha256']
            frame,snapshot = self._scroll(context,frame,snapshot,binding,directory,cancel,-1,deadline=deadline)
            steps += 1
            checked_view(frame,snapshot)
            if frame['image_sha256'] == previous and frame['scroll_state']['bottom'] is not True:
                raise QueueImportBlocked('queue_scroll_no_progress')
        combined = stitch_queue_views(views,len(original['rows']),expected_rows=original['rows'])
        if combined.get('visible_complete') is not True:
            raise QueueImportBlocked('queue_scan_overlap_unverified')
        scan_ref = self.publish('maa-queue-scan',{'schema':'maa_queue_scan/v1','views':views,
            'frame_refs':references,'snapshot_refs':snapshot_refs,'projection_sha256':original['projection_sha256']})
        self.store.register_evidence(context,[scan_ref])
        complete = self.monitor.capture_queue_snapshot(binding,directory,screenshot_refs=references,visible_complete=True)
        self.store.register_evidence(context,self._snapshot_refs(complete))
        if complete['projection_sha256'] != original['projection_sha256'] or complete['rows'] != original['rows']:
            raise QueueImportBlocked('queue_projection_changed')
        self.store.record_snapshot(context,complete)
        self.progress('queue_snapshot','tool_finished',{'row_count':len(complete['rows']),'visible_complete':True,
            'summary':f"已通过 {len(views)} 张当前截图核对 {len(complete['rows'])} 行队列。"})
        return {**frame,'visible_complete':True,'queue_scan_ref':scan_ref},complete

    def _control_response(self, response, code):
        # This records native dispatch only. The caller independently rereads
        # foreground, mode and option facts before submitting any code.
        entered = response.get('submission_entered') is True
        if entered:
            self._control_outcome = 'verified' if response.get('status') == 'completed' else 'unknown'
        if response.get('status') != 'completed' or not entered:
            raise QueueImportBlocked(code)

    def _prepare_controls(self, context, frame, snapshot, binding, directory, cancel, options):
        self._require_page(frame)
        if not frame['window'].get('foreground'):
            operation = self.store.prepare_item_action(context,None,phase='focus',snapshot=snapshot)
            response = self.submit({'action':'focus','target':'window',
                'screenshot_id':frame['screenshot_id']},frame,cancel,operation=operation)
            self._control_response(response,'queue_focus_unverified')
            frame,snapshot = self._observe_one(context,binding,directory,cancel)
            self._require_page(frame)
            if not frame['window'].get('foreground'):
                raise QueueImportBlocked('queue_foreground_or_permission')
        mode = frame.get('queue_mode')
        if type(mode) is not bool:
            raise QueueImportBlocked('queue_mode_unverified')
        names = ('auto_formation','ignore_requirements','use_support')
        desired = dict(options)
        if mode is False:
            original = {name:frame.get('options',{}).get(name,{}).get('state') for name in names}
            if any(type(value) is not bool for value in original.values()):
                raise QueueImportBlocked('queue_options_unverified')
            desired = {**original,**options}
        original_projection = snapshot['projection_sha256']
        if mode is False:
            frame,snapshot = self._click_control(context,frame,snapshot,binding,directory,cancel,
                                                target='multiple_jobs',phase='mode')
            self._require_page(frame)
            if frame.get('queue_mode') is not True:
                raise QueueImportBlocked('queue_mode_unverified')
            self.progress('queue_mode','tool_finished',{'queue_mode':True,'summary':'已回读多作业模式开启。'})
        for name,value in desired.items():
            current = frame.get('options',{}).get(name,{}).get('state')
            if type(current) is not bool:
                raise QueueImportBlocked('queue_options_unverified')
            if current != value:
                frame,snapshot = self._click_control(context,frame,snapshot,binding,directory,cancel,
                                                    target=name,phase='options')
                self._require_page(frame)
                if frame.get('queue_mode') is not True or not frame['window'].get('foreground'):
                    raise QueueImportBlocked('queue_mode_unverified')
                if frame.get('options',{}).get(name,{}).get('state') is not value:
                    raise QueueImportBlocked('queue_options_unverified')
                self.progress('queue_options','tool_finished',{'option':name,'state':value,
                    'summary':'已回读本次编队选项。'})
        if snapshot['projection_sha256'] != original_projection:
            raise QueueImportBlocked('queue_projection_changed')
        if snapshot['visible_complete'] is not True:
            frame,snapshot = self._scan_queue(context,frame,snapshot,binding,directory,cancel)
        self._require_idle(frame,snapshot,desired)
        return frame,snapshot,desired

    def _focus_field(self, context, frame, snapshot, binding, directory, cancel):
        x,y = self._point(frame)
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target='code_input')
        operation = self.store.prepare_item_action(context,None,phase='options',snapshot=snapshot)
        response = self.submit({'action':'click','target':'code_input','screenshot_id':frame['screenshot_id'],
                                'x':x,'y':y},frame,cancel,operation=operation)
        self._control_response(response,'queue_input_focus_unverified')

    def _clear_field(self, context, frame, snapshot, binding, directory, cancel):
        self._point(frame)
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target='code_input')
        operation = self.store.prepare_item_action(context,None,phase='options',snapshot=snapshot)
        response = self.submit({'action':'clear_code','target':'code_input',
                                'screenshot_id':frame['screenshot_id']},
                               frame,cancel,operation=operation)
        self._control_response(response,'queue_input_clear_unverified')

    def _record_for_target(self, context, target, records):
        record = read_reference(target['candidate_ref'], self.store.engine.data_dir)
        if record not in records:
            raise QueueImportBlocked('queue_candidate_not_admitted')
        self.preparation.validate_use(context, record)
        document = read_reference(target['document_ref'], self.store.engine.data_dir,maximum=8*1024*1024)
        return record, document

    def _read_artifact(self, ref):
        return read_reference(ref, self.store.engine.data_dir, maximum=192*1024*1024)

    @staticmethod
    def _owned(run):
        return [{'identity':row,'owned':True,'operation_ids':[facts['operation_id']]}
                for row,facts in run['owned_rows'].items()]

    @staticmethod
    def _matching_row(snapshot, target):
        rows = [row for row in snapshot['rows'] if receipts.row_matches_target(row, target)]
        if len(rows) != 1:
            raise QueueImportBlocked('queue_exact_row_unverified')
        return rows[0]

    def _code(self, context, target, record, document, frame, snapshot, binding, directory, cancel):
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target='code_input')
        baseline = self.monitor.capture_queue_baseline(binding,target,document,directory,snapshot)
        self.store.register_evidence(context,[baseline['evidence_ref']])
        operation = self.store.prepare_item_action(context,target['target_key'],phase='code',
            snapshot=snapshot,input_baseline_ref=baseline['evidence_ref'])
        self._pending_effect = operation['operation_id']
        response = self.submit({'action':'replace_code','target':'code_input',
                                'screenshot_id':frame['screenshot_id'],'code':target['mystery_code']},
                               frame,cancel,operation=operation,record=record)
        if response.get('submission_entered') is not True:
            raise QueueImportBlocked('queue_code_submission_unverified')
        if response.get('status') != 'completed':
            raise QueueImportBlocked('queue_reconciliation_required')
        self.progress('queue_code_submitted','tool_finished',{
            'target_key':target['target_key'],'operation_id':operation['operation_id'],'status':'submitted'})
        return operation, baseline

    def _wait_effect(self, context, operation, baseline, binding, directory, cancel):
        deadline = time.monotonic()+180
        probes = []
        submitted_at = datetime.fromisoformat(
            self.store.workflow.get_operation(operation['operation_id'])['submitted_at']).timestamp()
        while time.monotonic() < deadline:
            if cancel.wait(.25):
                raise QueueImportBlocked('queue_cancelled')
            try:
                frame,snapshot = self._observe(context,binding,directory,cancel,
                    deadline=deadline)
            except (QueueImportBlocked,receipts.MaaQueueReceiptError) as error:
                if error.code in {'queue_projection_changed','queue_scroll_endpoints_unverified',
                                  'queue_scan_overlap_unverified','queue_page_or_coverage_unverified',
                                  'queue_visual_refresh_changed',
                                  'config_changed_during_read','file_changed_during_read',
                                  'body_changed_during_read','log_changed','screenshot_changed'}:
                    # The one submitted load may finish while its queue is
                    # disabled or being scanned. Restart observation under the same input
                    # barrier and deadline, without issuing its code again.
                    continue
                raise
            observed = self.monitor.collect_queue_receipt(baseline,binding,directory,
                screenshot_refs=snapshot['screenshot_refs'],visible_complete=snapshot['visible_complete'])
            if observed.get('receipt_ref'):
                proof = self._read_artifact(observed['receipt_ref'])
                confirmed = {**proof['current_snapshot']}
                self.store.register_evidence(context,[observed['receipt_ref'], *self._snapshot_refs(confirmed)])
            if observed['status'] == 'pending':
                # GET/preview alone is never parse-completion evidence. Keep
                # only a locally replayable busy→two-ready pixel sequence.
                try:
                    from .maa_queue_parse_ready import capture_parse_probe
                    probe = capture_parse_probe(frame,frame['regions']['queue_add'],snapshot,
                        code_text=frame['code_text'],code_input_rect=frame['regions']['code_input'])
                    state = probe['measurement']['state']
                    if state == 'busy':
                        probes = [probe]
                    elif state == 'ready' and probes and probes[0]['measurement']['state']=='busy':
                        probes = [probes[0],*probes[1:][-1:],probe]
                    else:
                        probes = []
                except (ValueError,KeyError,receipts.MaaQueueReceiptError):
                    probes = []
                if len(probes)!=3 or not observed.get('receipt_ref'):
                    continue
                absent = self.monitor.collect_queue_absence_receipt(proof,probes[1]['snapshot'],
                    probes[2]['snapshot'],probes,directory,submitted_at=submitted_at)
                if absent.get('verified') is not True:
                    continue
                self.store.register_evidence(context,[absent['receipt_ref']])
                self.store.record_reconciliation(context['task_id'],original_epoch=context['epoch'],
                    operation_id=operation['operation_id'],evidence_ref=absent['receipt_ref'],result=absent)
                self._pending_effect = None
                self.store.record_snapshot(context,snapshot)
                return frame,snapshot
            if observed['status'] != 'verified' or observed.get('verified') is not True or not observed.get('receipt_ref'):
                raise QueueImportBlocked('queue_effect_unverified')
            if confirmed['projection_sha256'] != snapshot['projection_sha256'] or confirmed['rows'] != snapshot['rows']:
                raise QueueImportBlocked('queue_projection_changed')
            self.store.record_reconciliation(context['task_id'],original_epoch=context['epoch'],
                operation_id=operation['operation_id'],evidence_ref=observed['receipt_ref'],result=observed)
            self._pending_effect = None
            self.store.record_snapshot(context,confirmed)
            return frame, confirmed
        raise QueueImportBlocked('queue_parse_completion_unverified')

    def _manual_add(self, context, plan, target, record, document, source_operation,
                    frame, snapshot, binding, directory, cancel):
        source = self.store.workflow.get_operation(source_operation['operation_id'])
        if source['status']!='verified' or (source['result'] or {}).get('queue_effect')!='parsed_no_row':
            raise QueueImportBlocked('queue_parse_completion_unverified')
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target='queue_add')
        source_ref = source['result']['evidence_ref']
        baseline = self.monitor.capture_queue_effect_baseline(binding,'add',directory,snapshot,
            target=target,source_document=document,source_input={'operation_id':source['operation_id'],
                'receipt_ref':source_ref,'receipt':self._read_artifact(source_ref)},planned_targets=plan['targets'])
        self.store.register_evidence(context,[baseline['evidence_ref']])
        operation = self.store.prepare_item_action(context,target['target_key'],phase='add',snapshot=snapshot,
            effect_baseline_ref=baseline['evidence_ref'])
        self._pending_effect = operation['operation_id']
        x,y = self._point(frame,'queue_add')
        response = self.submit({'action':'click','target':'queue_add','x':x,'y':y,
            'button':'right' if target['difficulty']==2 else 'left','screenshot_id':frame['screenshot_id']},
            frame,cancel,operation=operation,record=record)
        if response.get('submission_entered') is not True or response.get('status')!='completed':
            raise QueueImportBlocked('queue_reconciliation_required')
        return self._wait_post_effect(context,operation,baseline,binding,directory,cancel)

    def _wait_post_effect(self, context, operation, baseline, binding, directory, cancel):
        deadline = time.monotonic()+180
        previous = None
        submitted = datetime.fromisoformat(
            self.store.workflow.get_operation(operation['operation_id'])['submitted_at']).timestamp()
        while time.monotonic()<deadline:
            if cancel.wait(.11):
                raise QueueImportBlocked('queue_reconciliation_required')
            frame,current = self._observe(context,binding,directory,cancel,deadline=deadline)
            if previous is None:
                previous = current
                continue
            result = self.monitor.collect_queue_effect_receipt(baseline,binding,directory,
                previous_snapshot=previous,screenshot_refs=current['screenshot_refs'],
                visible_complete=current['visible_complete'],operation_id=operation['operation_id'],submitted_at=submitted)
            if result.get('status')=='pending':
                previous = current
                continue
            if result.get('verified') is not True or not result.get('receipt_ref'):
                raise QueueImportBlocked('queue_reconciliation_required')
            confirmed = self._read_artifact(result['receipt_ref'])['current_snapshot']
            self.store.register_evidence(context,[result['receipt_ref'],*self._snapshot_refs(confirmed)])
            if confirmed['rows']!=current['rows'] or confirmed['projection_sha256']!=current['projection_sha256']:
                raise QueueImportBlocked('queue_reconciliation_required')
            self.store.record_reconciliation(context['task_id'],original_epoch=context['epoch'],
                operation_id=operation['operation_id'],evidence_ref=result['receipt_ref'],result=result)
            self._pending_effect = None
            self.store.record_snapshot(context,confirmed)
            return frame,confirmed
        raise QueueImportBlocked('queue_reconciliation_required')

    def _aggregate(self, context, plan, baseline, final):
        run = self.store.current(context['task_id'])
        difference = self.monitor.diff_queue(baseline,final,plan['targets'],self._owned(run))
        unexpected = [item.get('identity','') for item in difference['external_changes']]
        unexpected.extend(difference['extra_owned'])
        items = run['items']
        covered = [item for item in items if item['status'] in {'added','reused'}
                   and item['target_key'] in difference['reusable']]
        complete = len(covered) == len(plan['targets']) and not unexpected and not difference['conflicts']
        refs = [baseline['evidence_ref'], final['evidence_ref']]
        for item in items:
            refs.extend(item['evidence_refs'])
        refs = list({ref['path']:ref for ref in refs}.values())
        payload = {'schema':'maa_queue_receipt/v1','plan_sha256':plan['plan_sha256'],
            'binding':baseline['binding'],'baseline_ref':baseline['evidence_ref'],
            'final_snapshot_ref':final['evidence_ref'],'items':items,
            'preserved_baseline':not difference['external_changes'],'unexpected_rows':unexpected,
            'evidence_refs':refs,'battle_start_submitted':False}
        ref = self.publish('maa-queue-receipt',payload)
        self.store.register_evidence(context,[ref])
        return complete, {'queue_receipt_ref':ref,'evidence_refs':[ref,*refs],
                          'coverage':{'verified':len(covered),'total':len(plan['targets'])},'items':items}

    def _delete_owned_extra(self, context, plan, identity, frame, snapshot, binding, directory, records, cancel):
        frame,snapshot = self._observe(context,binding,directory,cancel)
        run = self.store.current(context['task_id'])
        owner = run['owned_rows'].get(identity)
        if (not owner or identity in run['baseline']['row_ids']
                or sum(row['identity']==identity for row in snapshot['rows']) != 1):
            raise QueueImportBlocked('queue_extra_ownership_unverified')
        source_operation = self.store.workflow.get_operation(owner['operation_id'])
        source_key = source_operation['action'].get('queue_guard',{}).get('target_key')
        target = next((target for target in plan['targets'] if target['target_key']==source_key),None)
        if (not target or source_operation['status'] != 'verified'
                or source_operation['action']['queue_guard']['phase'] != 'code'):
            raise QueueImportBlocked('queue_extra_ownership_unverified')
        _,document = self._record_for_target(context,target,records)
        target_name = 'row_delete:'+identity
        if target_name not in frame['targets']:
            raise QueueImportBlocked('queue_extra_row_not_visible')
        frame,snapshot = self._refresh(context,frame,snapshot,binding,directory,cancel,target=target_name)
        source_ref = source_operation['result']['evidence_ref']
        source_input = {'operation_id':source_operation['operation_id'],'receipt_ref':source_ref,
                        'receipt':self._read_artifact(source_ref)}
        baseline = self.monitor.capture_queue_effect_baseline(binding,'delete',directory,snapshot,
            target=target,source_document=document,source_input=source_input,row_identity=identity,
            planned_targets=plan['targets'])
        self.store.register_evidence(context,[baseline['evidence_ref']])
        operation = self.store.prepare_item_action(context,None,phase='delete',snapshot=snapshot,
            row_identity=identity,effect_baseline_ref=baseline['evidence_ref'])
        self._pending_effect = operation['operation_id']
        x,y = self._point(frame,target_name)
        response = self.submit({'action':'click','target':target_name,'x':x,'y':y,
            'screenshot_id':frame['screenshot_id']},frame,cancel,operation=operation)
        if response.get('submission_entered') is not True or response.get('status') != 'completed':
            raise QueueImportBlocked('queue_reconciliation_required')
        previous = None
        deadline = time.monotonic()+180
        submitted_at = datetime.fromisoformat(
            self.store.workflow.get_operation(operation['operation_id'])['submitted_at']).timestamp()
        while time.monotonic() < deadline:
            if cancel.wait(.11):
                raise QueueImportBlocked('queue_reconciliation_required')
            frame,current = self._observe(context,binding,directory,cancel,deadline=deadline)
            if previous is None:
                previous = current
                continue
            result = self.monitor.collect_queue_effect_receipt(baseline,binding,directory,
                previous_snapshot=previous,screenshot_refs=current['screenshot_refs'],visible_complete=current['visible_complete'],
                operation_id=operation['operation_id'],submitted_at=submitted_at)
            if result.get('receipt_ref'):
                proof = self._read_artifact(result['receipt_ref'])
                confirmed = proof['current_snapshot']
                self.store.register_evidence(context,[result['receipt_ref'],*self._snapshot_refs(confirmed)])
            if result.get('status') == 'pending':
                previous = current
                continue
            if result.get('verified') is not True or result.get('status') != 'verified' or not result.get('receipt_ref'):
                raise QueueImportBlocked('queue_reconciliation_required')
            if confirmed['projection_sha256'] != current['projection_sha256'] or confirmed['rows'] != current['rows']:
                raise QueueImportBlocked('queue_reconciliation_required')
            self.store.record_reconciliation(context['task_id'],original_epoch=context['epoch'],
                operation_id=operation['operation_id'],evidence_ref=result['receipt_ref'],result=result)
            self._pending_effect = None
            self.store.record_snapshot(context,confirmed)
            self.progress('queue_extra_removed','tool_finished',{'row_identity':identity,
                'summary':'已核实移除本次导入产生的额外难度行。'})
            return frame,confirmed
        raise QueueImportBlocked('queue_reconciliation_required')

    def _remove_owned_extras(self, context, plan, baseline, frame, snapshot, binding, directory, records, cancel):
        while True:
            self._guard(cancel)
            run = self.store.current(context['task_id'])
            difference = self.monitor.diff_queue(baseline,snapshot,plan['targets'],self._owned(run))
            if difference['external_changes'] or difference['conflicts']:
                raise QueueImportBlocked('queue_target_conflict')
            extras = difference['extra_owned']
            if not extras:
                return frame,snapshot
            frame,snapshot = self._delete_owned_extra(context,plan,extras[0],frame,snapshot,binding,directory,records,cancel)

    @staticmethod
    def _result(status,code,**fields):
        return {'status':status,'code':code,'transport':'screenshot_keyboard',
                'battle_start_submitted':False,
                'action_outcome':'unknown' if status == 'unknown' else 'not_issued',**fields}

    def run(self, context, plan, records, options=None, cancel_event=None):
        cancel = cancel_event if cancel_event is not None else threading.Event()
        baseline = final = None
        active_operation = None
        any_verified_input = False
        self._control_outcome = 'not_issued'
        self._pending_effect = None
        try:
            self._guard(cancel)
            options = {} if options is None else options
            if (not isinstance(options,dict) or any(key not in {'auto_formation','ignore_requirements','use_support'}
                   or type(value) is not bool for key,value in options.items())):
                raise QueueImportBlocked('queue_options_invalid')
            if self.store.workflow.pending_operations():
                return self.reconcile(context,plan,cancel_event=cancel)
            run = self.store.current(context['task_id'])
            plan_ref = run['plan_ref'] if run is not None else self.publish('maa-queue-plan',plan)
            self.store.register_plan(context,plan,plan_ref)
            directory = Path(plan_ref['path']).parent
            binding = self.monitor.discover_queue_binding(self.supervisor.target_identity)
            frame,final = self._observe_one(context,binding,directory,cancel)
            frame,final,options = self._prepare_controls(context,frame,final,binding,directory,cancel,options)
            run = self.store.current(context['task_id'])
            baseline_ref = run['baseline']['ref']
            baseline = {**self._read_artifact(baseline_ref),'evidence_ref':baseline_ref}
            for target in plan['targets']:
                self._guard(cancel)
                record,document = self._record_for_target(context,target,records)
                key = target['target_key']
                run = self.store.current(context['task_id'])
                delta = self.monitor.diff_queue(baseline,final,[target],self._owned(run))
                if (not delta['external_changes'] and delta['conflicts']
                        and delta['conflicts'][0]['code']=='queue_absence_unverified'):
                    if cancel.wait(.11):
                        raise QueueImportBlocked('queue_cancelled')
                    frame,final = self._observe(context,binding,directory,cancel)
                    self._require_idle(frame,final,options)
                    delta = self.monitor.diff_queue(baseline,final,[target],self._owned(run))
                if delta['external_changes'] or delta['conflicts']:
                    raise QueueImportBlocked('queue_target_conflict')
                if key in delta['missing']:
                    self._focus_field(context,frame,final,binding,directory,cancel)
                    frame,final = self._observe(context,binding,directory,cancel)
                    self._require_idle(frame,final,options)
                    ready = self.monitor.diff_queue(baseline,final,[target],self._owned(run))
                    # The native operation already selects all and replaces
                    # the exact code. WPF may restore its selected ComboBox
                    # text after an independent clear, so an empty intermediate
                    # frame is neither necessary nor reliable. Only replace an
                    # empty field or a fully read-back station code; preserve
                    # arbitrary user-entered paths/text.
                    replaceable = (frame.get('field_empty') is True or
                        isinstance(frame.get('code_text'),str) and
                        re.fullmatch(r'prts://[1-9][0-9]{0,11}',frame['code_text']) is not None)
                    if key not in ready['missing'] or ready['external_changes'] or not replaceable:
                        raise QueueImportBlocked('queue_input_precondition_changed')
                    active_operation,input_baseline = self._code(context,target,record,document,frame,final,binding,directory,cancel)
                    frame,final = self._wait_effect(context,active_operation,input_baseline,binding,directory,cancel)
                    if (self.store.workflow.get_operation(active_operation['operation_id'])['result'] or {}).get('queue_effect')=='parsed_no_row':
                        frame,final = self._manual_add(context,plan,target,record,document,active_operation,
                            frame,final,binding,directory,cancel)
                    active_operation = None
                    any_verified_input = True
                    self._require_idle(frame,final,options)
                row = self._matching_row(final,target)
                run = self.store.current(context['task_id'])
                owner = run['owned_rows'].get(row['identity'])
                saved = next(item for item in run['items'] if item['target_key']==key)
                if saved['status'] not in {'added','reused'}:
                    self.store.record_item(context,{'target_key':key,'status':'added' if owner else 'reused',
                        'row_identity':row['identity'],'owned':bool(owner),
                        'operation_ids':[owner['operation_id']] if owner else [],
                        'evidence_refs':[owner['evidence_ref']] if owner else [],
                        'code':'queue_row_verified'})
                    self.progress('queue_item_verified' if owner else 'queue_target_reused','tool_finished',{
                        'target_key':key,'stage_code':target['stage_code'],'status':'added' if owner else 'reused'})
            frame,final = self._remove_owned_extras(context,plan,baseline,frame,final,binding,directory,records,cancel)
            complete, facts = self._aggregate(context,plan,baseline,final)
            self.progress('queue_verified' if complete else 'queue_partial','tool_finished',facts['coverage'])
            return self._result('import_verified' if complete else 'partial',
                'queue_verified' if complete else 'queue_final_coverage_incomplete',
                action_outcome='verified' if any_verified_input else self._control_outcome,**facts)
        except (QueueImportBlocked, VisualImportError, receipts.MaaQueueReceiptError, ActionRejected,
                OSError, ValueError, KeyError, TypeError) as error:
            code = getattr(error,'code','queue_recipe_blocked')
            pending = None
            if self._pending_effect is not None:
                pending = self.store.workflow.get_operation(self._pending_effect)
            unverified = (active_operation is not None or code=='queue_reconciliation_required'
                          or self._control_outcome == 'unknown'
                          or pending is not None and pending['submitted_at'] is not None
                          and pending['status'] not in {'verified','not_issued'})
            if baseline is not None and final is not None:
                try:
                    _, facts = self._aggregate(context,plan,baseline,final)
                    self.progress('queue_partial','tool_finished',{'code':code,**facts['coverage']})
                    status = 'partial' if facts['coverage']['verified'] else ('unknown' if unverified else 'needs_input')
                    return self._result(status,code,action_outcome='unknown' if unverified else (
                        'verified' if any_verified_input else self._control_outcome),**facts)
                except (QueueImportBlocked, receipts.MaaQueueReceiptError, ActionRejected, OSError, ValueError, KeyError):
                    pass
            return self._result('unknown' if unverified else 'needs_input',code,
                                action_outcome='unknown' if unverified else self._control_outcome)

    def reconcile(self, context, plan, *, cancel_event=None):
        """Observe a registered run without replaying a previous native action."""
        cancel = cancel_event if cancel_event is not None else threading.Event()
        try:
            self._guard(cancel)
            run = self.store.current(context['task_id'])
            if run and run['baseline'] and run['plan'] == plan:
                directory = Path(run['plan_ref']['path']).parent
                baseline_ref = run['baseline']['ref']
                baseline = {**self._read_artifact(baseline_ref),'evidence_ref':baseline_ref}
                _,current = self._observe(context,baseline['binding'],directory,cancel)
                _, facts = self._aggregate(context,plan,baseline,current)
                return self._result('unknown','queue_reconciliation_required',**facts)
        except (QueueImportBlocked, VisualImportError, receipts.MaaQueueReceiptError, ActionRejected,
                OSError, ValueError, KeyError, TypeError):
            pass
        return self._result('unknown','queue_reconciliation_required')

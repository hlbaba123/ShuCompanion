"""Isolated MAA queue file evidence: no GUI, model or account protocol."""
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu import maa_queue_receipt as receipt


SOURCE = {'version':3, 'stage_name':'unit_stage_1', 'difficulty':3,
          'doc':{'title':'合成测试关卡', 'details':'合成作业说明'},
          'opers':[{'name':'合成干员', 'skill':1}],
          'actions':[{'name':'合成干员','location':[2,3],'direction':'Right'}]}


def raw_config(rows=(), current='unit'):
    return json.dumps({'Current':current,'Configurations':{current:{
        'Copilot':{'TaskList':list(rows)}, 'RemoteControl':{'Token':'DO_NOT_PUBLISH'}},
        'UNSELECTED_SECRET':{'secret':'DO_NOT_PUBLISH'}},
        'OtherSecret':'DO_NOT_PUBLISH'},ensure_ascii=False).encode()


class QueueReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.maa=self.root/'MAA'
        (self.maa/'config/copilot').mkdir(parents=True)
        (self.maa/'debug').mkdir()
        self.exe=self.maa/'MAA.exe'; self.exe.write_bytes(b'unit executable')
        (self.maa/'MAA.dll').write_bytes(b'unit version metadata')
        self.config=self.maa/'config/gui.new.json'; self.config.write_bytes(raw_config())
        self.log=self.maa/'debug/gui.log'; self.log.write_bytes(b'older log\n')
        self.evidence=self.root/'task-private/evidence'; self.evidence.mkdir(parents=True)
        self.image=self.evidence/'frame.png'; self.image.write_bytes(b'unit frame')
        self.frame={'path':str(self.image),'sha256':hashlib.sha256(self.image.read_bytes()).hexdigest()}
        self.process={'hwnd':1,'pid':2,'creation_time':3,'executable':str(self.exe)}
        self.now=datetime(2026,11,10,12,0).timestamp()
        os.utime(self.image,ns=(int(self.now*1e9),int(self.now*1e9)))
        for name,value in (('_MAA_EXECUTABLE',self.exe),('_MAA_ROOT',self.maa)):
            item=patch.object(receipt,name,value); item.start(); self.addCleanup(item.stop)
        item=patch.object(receipt,'_product_version',return_value=receipt.PINNED_VERSION)
        item.start(); self.addCleanup(item.stop)
        item=patch.object(receipt,'_process_matches',return_value=True)
        item.start(); self.addCleanup(item.stop)
        item=patch.object(receipt.time,'time',side_effect=lambda:self.now)
        item.start(); self.addCleanup(item.stop)
        self.binding=receipt.discover_queue_binding(self.process)
        self.target={'target_key':'unit-target','stage_key':'unit:1:normal','stage_id':'unit_stage_1',
            'stage_code':'UNIT-1','difficulty':1,'source_difficulty':3,'slot':'primary',
            'mystery_code':'prts://123'}

    def row(self, code=123, *, raid=False, checked=True, name='可修改名称', path=None):
        body=self.maa/f'config/copilot/{code}.json'
        body.write_text(json.dumps(SOURCE,ensure_ascii=False),encoding='utf-8')
        return {'Name':name,'FilePath':str(body) if path is None else path,
                'CopilotId':code,'IsRaid':raid,'IsChecked':checked,'Index':0,'IsNavNameOverride':False}

    def snapshot(self, rows=None, *, visible=True, frame=None):
        if rows is not None:self.config.write_bytes(raw_config(rows))
        return receipt.capture_queue_snapshot(self.binding,self.evidence,
            screenshot_refs=[frame or self.frame],visible_complete=visible)

    def baseline(self, rows=None):
        return receipt.capture_queue_baseline(self.binding,self.target,SOURCE,
            self.evidence,self.snapshot(rows))

    def append_success(self, code=123, *, count=1):
        stamp=datetime.fromtimestamp(self.now).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        def line(logger,message):return f'[{stamp}][INF][{logger}] <2> {message}\n'
        text=line('OtherService','DO_NOT_PUBLISH')+line('HttpResponseLoggingExtension',
            f'HTTP: OK GET https://prts.maa.plus/copilot/get/{code} null null 12ms')
        text+=''.join(line('CopilotViewModel',v) for v in (
            SOURCE['doc']['title'],SOURCE['doc']['details'],'-'*20,f'共 {count} 名干员'))
        with self.log.open('ab') as stream:stream.write(text.encode())

    def fresh_frame(self, pixels=None):
        frame=self.evidence/('frame-'+str(len(list(self.evidence.glob('frame-*.png'))))+'.png')
        if pixels is None:
            pixels=b'fresh frame '+str(self.now).encode()
        frame.write_bytes(pixels)
        # Put the filesystem timestamp inside the observation window. A
        # timestamp equal to the mock clock's upper bound makes this fixture
        # depend on Windows filesystem and float timestamp rounding.
        stamp_ns=round(self.now*1000)*1_000_000
        os.utime(frame,ns=(stamp_ns,stamp_ns))
        self.now+=.01
        return {'path':str(frame),'sha256':hashlib.sha256(pixels).hexdigest()}

    def collect(self, baseline):
        reference=self.fresh_frame()
        return receipt.collect_queue_receipt(baseline,self.binding,self.evidence,
            screenshot_refs=[reference],visible_complete=True)

    def payload(self,result):
        ref=result['receipt_ref']; raw=Path(ref['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),ref['sha256'])
        return json.loads(raw)

    def expected(self,baseline):
        return {'binding':self.binding,'target':self.target,'source_document':SOURCE,
                'input_id':baseline['input_id']}

    def adjusted_source(self):
        source=copy.deepcopy(SOURCE)
        source['opers'][0].update(skill=3,requirements={'elite':0,'skill_level':10})
        return source

    def corrected_rows(self,source,*,code=0):
        row=self.row(code)
        Path(row['FilePath']).write_text(json.dumps(receipt.expected_loaded_copilot_document(source),
            ensure_ascii=False),encoding='utf-8')
        return [row,{**row,'IsRaid':True,'Index':1}]

    def append_adjusted_success(self,source,*,warning=True):
        stamp=datetime.fromtimestamp(self.now).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        def line(logger,message,level='INF'):
            return f'[{stamp}][{level}][{logger}] <2> {message}\n'
        text=line('HttpResponseLoggingExtension',
            'HTTP: OK GET https://prts.maa.plus/copilot/get/123 null null 12ms')
        if warning:
            text+=line('CopilotViewModel','合成干员 设置为要求 精0, 但是其他属性要求 精2, 已自动调整','WRN')
        text+=''.join(line('CopilotViewModel',v) for v in (
            source['doc']['title'],source['doc']['details'],'-'*20,'共 1 名干员',
            'Copilot item collection changed: "Add"','Copilot item collection changed: "Add"'))
        with self.log.open('ab') as stream:stream.write(text.encode())

    def test_saved_id_is_derived_only_from_explicit_elite_adjustment(self):
        source=self.adjusted_source()
        self.assertEqual(receipt.expected_queue_copilot_id(source,'prts://123'),0)
        source['opers'][0].pop('requirements')
        self.assertEqual(receipt.expected_queue_copilot_id(source,'prts://123'),123)
        source['opers'][0]['requirements']={'elite':2,'skill_level':10}
        self.assertEqual(receipt.expected_queue_copilot_id(source,'prts://123'),123)
        self.assertEqual(receipt.expected_queue_copilot_id(SOURCE,'prts://123'),123)
        for forged in (0,124,True):
            with self.subTest(forged=forged):
                with self.assertRaises(receipt.MaaQueueReceiptError):
                    receipt._target({**self.target,'expected_copilot_id':forged},SOURCE)
        with self.assertRaises(receipt.MaaQueueReceiptError):
            receipt._target({**self.target,'expected_copilot_id':123},self.adjusted_source())

    def test_zero_id_existing_row_is_only_exact_normalized_content_equivalence(self):
        source=self.adjusted_source()
        target=receipt._target(self.target,source)
        snapshot=self.snapshot(self.corrected_rows(source))
        diff=receipt.diff_queue(snapshot,snapshot,[target],[])
        self.assertEqual(diff['reusable'],[target['target_key']])
        self.assertEqual(diff['extra_owned'],[])
        self.assertTrue(receipt.diff_queue(snapshot,snapshot,[self.target],[])['conflicts'])
        row=snapshot['rows'][0]
        for changes in ({'copilot_id':123},{'copilot_id':124},{'is_checked':False},
                        {'is_raid':True},{'semantic_sha256':'0'*64},{'stage_id':'another'},
                        {'verification':'opaque'}):
            with self.subTest(changes=changes):
                self.assertFalse(receipt.row_matches_target({**row,**changes},target))
        duplicate=self.snapshot([*self.corrected_rows(source),self.corrected_rows(source)[0]])
        self.assertTrue(receipt.diff_queue(duplicate,duplicate,[target],[])['conflicts'])

    def test_corrected_zero_id_input_still_requires_this_get_warning_and_addition(self):
        source=self.adjusted_source()
        baseline=receipt.capture_queue_baseline(self.binding,self.target,source,self.evidence,self.snapshot([]))
        self.now+=1
        self.append_adjusted_success(source)
        self.config.write_bytes(raw_config(self.corrected_rows(source)))
        result=self.collect(baseline)
        self.assertTrue(result['verified'],result)
        self.assertEqual(len(result['added_rows']),2)
        payload=self.payload(result)
        expected={'binding':self.binding,'target':self.target,'source_document':source,
                  'input_id':baseline['input_id']}
        self.assertTrue(receipt.verify_queue_receipt(payload,expected)['verified'])
        # Reusing this baseline cannot claim a later generation without the
        # input's own new-row delta, even with the same exact source content.
        with self.assertRaises(receipt.MaaQueueReceiptError):
            receipt._target({**self.target,'expected_copilot_id':0},SOURCE)

    def test_corrected_zero_id_without_exact_adjust_warning_is_rejected(self):
        source=self.adjusted_source()
        baseline=receipt.capture_queue_baseline(self.binding,self.target,source,self.evidence,self.snapshot([]))
        self.now+=1
        self.append_adjusted_success(source,warning=False)
        self.config.write_bytes(raw_config(self.corrected_rows(source)))
        result=self.collect(baseline)
        self.assertFalse(result['verified'],result)

    def test_gui_omitted_default_skill_times_preserves_legacy_cache_identity(self):
        unchanged=receipt.canonical_copilot_document(SOURCE)
        source=copy.deepcopy(SOURCE)
        source['opers'][0]['skill_times']=1
        source['actions'][0]['skill_times']=1
        self.assertEqual(receipt.canonical_copilot_document(source),unchanged)
        for value in (0,2,True):
            for section in ('opers','actions'):
                modified=copy.deepcopy(SOURCE);modified[section][0]['skill_times']=value
                self.assertNotEqual(receipt.canonical_copilot_document(modified),unchanged)
                self.assertEqual(receipt.canonical_copilot_document(modified)[section][0]['skill_times'],value)

    def test_fresh_input_accepts_explicit_default_skill_times_only_as_dto_equivalence(self):
        source=copy.deepcopy(SOURCE);source['opers'][0]['skill_times']=1
        source['actions'][0]['skill_times']=1
        source['actions'][0].update(skill_usage=0,pre_delay=0)
        baseline=receipt.capture_queue_baseline(self.binding,self.target,source,self.evidence,self.snapshot([]))
        self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        result=self.collect(baseline)
        self.assertTrue(result['verified'],result)
        payload=self.payload(result)
        expected={'binding':self.binding,'target':self.target,'source_document':source,
                  'input_id':baseline['input_id']}
        self.assertTrue(receipt.verify_queue_receipt(payload,expected)['verified'])

    def test_only_audited_zero_action_defaults_are_omitted(self):
        original=receipt.canonical_copilot_document(SOURCE)
        for field in ('skill_usage','pre_delay'):
            for value in (0,1,2,True,False):
                source=copy.deepcopy(SOURCE);source['actions'][0][field]=value
                actual=receipt.canonical_copilot_document(source)
                if type(value) is int and value==0:
                    self.assertEqual(actual,original)
                else:
                    self.assertIn(field,actual['actions'][0])
                    self.assertIs(type(actual['actions'][0][field]),type(value))
        source=copy.deepcopy(SOURCE);source['actions'][0].update(cooling=0,unknown_unit_field=0)
        actual=receipt.canonical_copilot_document(source)
        self.assertEqual(actual['actions'][0]['cooling'],0)
        self.assertIn('unknown_unit_field',actual['actions'][0])

    def test_config_projection_never_publishes_other_service_fields(self):
        projected=receipt.project_queue_config(raw_config())
        self.assertEqual(projected,{'current':'unit','rows':[]})
        self.assertNotIn('DO_NOT_PUBLISH',json.dumps(projected))
        self.assertNotIn('UNSELECTED_SECRET',json.dumps(projected))

    def test_duplicate_keys_nonfinite_wrong_types_and_aliases_rejected_without_echo(self):
        rows=[self.row()]
        bad=[b'{"Current":"a","Current":"b"}',b'{"Current":NaN}', b'[]']
        for key,value in (('CopilotId',True),('IsRaid',0),('IsChecked',1),('Name',None)):
            row=copy.deepcopy(rows[0]);row[key]=value;bad.append(raw_config([row]))
        row=copy.deepcopy(rows[0]);row['is_raid']=False;bad.append(raw_config([row]))
        row=copy.deepcopy(rows[0]);row.pop('IsChecked');bad.append(raw_config([row]))
        for raw in bad:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(receipt.MaaQueueReceiptError) as caught:receipt.project_queue_config(raw)
                self.assertNotIn('DO_NOT_PUBLISH',str(caught.exception))

    def test_size_and_row_limits(self):
        for raw in (b' '* (8*1024*1024+1),raw_config([self.row()]*1001)):
            with self.assertRaises(receipt.MaaQueueReceiptError):receipt.project_queue_config(raw)

    def test_snapshot_body_raw_semantic_hash_and_mode_identity(self):
        rows=[self.row(),self.row(raid=True)]; snapshot=self.snapshot(rows)
        self.assertTrue(snapshot['stable']); self.assertEqual(len(snapshot['rows']),2)
        self.assertNotEqual(snapshot['rows'][0]['identity'],snapshot['rows'][1]['identity'])
        for row in snapshot['rows']:
            self.assertEqual(row['stage_id'],'unit_stage_1')
            self.assertEqual(row['verification'],'verified')
            self.assertTrue(row['body_sha256']);self.assertTrue(row['semantic_sha256'])
        contents=''.join(p.read_text(encoding='utf-8') for p in self.evidence.glob('*.json'))
        self.assertNotIn('DO_NOT_PUBLISH',contents)
        self.assertNotIn('UNSELECTED_SECRET',contents)

    def test_external_parent_traversal_and_symlinks_are_opaque_and_never_read(self):
        secret=self.root/'secret.json';secret.write_text('DO_NOT_PUBLISH')
        paths=[str(secret),str(self.maa/'config/copilot/../secret.json')]
        linked=self.maa/'config/copilot/link.json'
        try:linked.symlink_to(secret);paths.append(str(linked))
        except OSError:pass
        for path in paths:
            with self.subTest(path=path):
                snapshot=self.snapshot([self.row(path=path)])
                self.assertEqual(snapshot['rows'][0]['verification'],'opaque')
                self.assertIsNone(snapshot['rows'][0]['body_ref'])
                self.assertNotIn('DO_NOT_PUBLISH',json.dumps(snapshot))

    def test_wrong_version_binding_and_current_switch_rejected(self):
        with patch.object(receipt,'_product_version',return_value='v99.0'):
            with self.assertRaises(receipt.MaaQueueReceiptError):receipt.discover_queue_binding(self.process)
        for key in ('pid','creation_time','hwnd'):
            process={**self.process,key:True}
            with self.assertRaises(receipt.MaaQueueReceiptError):receipt.discover_queue_binding(process)
        self.config.write_bytes(raw_config(current='different'))
        with self.assertRaises(receipt.MaaQueueReceiptError):self.snapshot()

    def test_snapshot_process_change_is_rejected(self):
        with patch.object(receipt,'_process_matches',return_value=False):
            with self.assertRaises(receipt.MaaQueueReceiptError):self.snapshot()

    def test_external_row_is_not_probed_before_being_classified_opaque(self):
        external=self.root/'outside'/'external.json'
        original=receipt._no_links

        def no_links(path):
            self.assertNotEqual(Path(path),external)
            return original(path)

        with patch.object(receipt,'_no_links',side_effect=no_links):
            snapshot=self.snapshot([self.row(path=str(external))])
        self.assertEqual(snapshot['rows'][0]['verification'],'opaque')

    def test_noop_or_name_rename_reuses_only_exact_source_stage_mode_and_body(self):
        baseline=self.snapshot([self.row()]);current=self.snapshot([self.row(name='新名称')])
        target={**self.target,'semantic_sha256':current['rows'][0]['semantic_sha256']}
        diff=receipt.diff_queue(baseline,current,[target],[])
        self.assertEqual(diff['reusable'],['unit-target'])
        self.assertFalse(diff['external_changes']);self.assertFalse(diff['conflicts'])

    def test_same_stage_other_source_or_body_is_conflict(self):
        baseline=self.snapshot([]);current=self.snapshot([self.row(code=456)])
        target={**self.target,'semantic_sha256':current['rows'][0]['semantic_sha256']}
        diff=receipt.diff_queue(baseline,current,[target],[])
        self.assertFalse(diff['reusable']);self.assertEqual(len(diff['conflicts']),1)
        current=self.snapshot([self.row(code=0)])
        self.assertTrue(receipt.diff_queue(baseline,current,[target],[])['conflicts'])

    def test_new_input_does_not_inherit_previous_visual_coverage(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        result=receipt.collect_queue_receipt(baseline,self.binding,self.evidence,screenshot_refs=[self.frame])
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['code'],'queue_readback_incomplete')

    def test_old_screenshot_does_not_prove_current_queue_even_if_caller_sets_complete(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        result=receipt.collect_queue_receipt(baseline,self.binding,self.evidence,
            screenshot_refs=[self.frame],visible_complete=True)
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['code'],'queue_screenshot_not_advanced')

    def test_new_frame_same_pixels_is_fresh_file_channel_evidence(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        frame=self.fresh_frame(self.image.read_bytes())
        self.assertEqual(frame['sha256'],self.frame['sha256'])
        result=receipt.collect_queue_receipt(baseline,self.binding,self.evidence,
            screenshot_refs=[frame],visible_complete=True)
        # Pixel/queue agreement is a separate visual observer gate. This tests
        # that a genuinely newer frame file is not confused with a reused Ref.
        self.assertEqual(result['status'],'verified',result)

    def test_frame_taken_after_snapshot_but_before_input_baseline_is_stale(self):
        snapshot=self.snapshot([]);self.now+=1
        frame=self.fresh_frame()
        baseline=receipt.capture_queue_baseline(self.binding,self.target,SOURCE,self.evidence,snapshot)
        self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        result=receipt.collect_queue_receipt(baseline,self.binding,self.evidence,
            screenshot_refs=[frame],visible_complete=True)
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'queue_screenshot_not_advanced')

    def test_fresh_frame_does_not_bless_a_stale_frame_in_same_scan(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        frame=self.fresh_frame()
        result=receipt.collect_queue_receipt(baseline,self.binding,self.evidence,
            screenshot_refs=[self.frame,frame],visible_complete=True)
        self.assertEqual(result['status'],'pending',result)
        self.assertEqual(result['code'],'queue_screenshot_not_advanced')

    def test_aggregate_body_limit_is_not_silently_truncated(self):
        with patch.object(receipt,'_MAX_SNAPSHOT_BODY_BYTES',16):
            with self.assertRaises(receipt.MaaQueueReceiptError) as caught:self.snapshot([self.row()])
            self.assertEqual(caught.exception.code,'snapshot_body_limit')

    def test_baseline_order_checked_and_multiplicity_are_preserved(self):
        first=self.row();second=self.row(code=456)
        baseline=self.snapshot([first,second]);current=self.snapshot([second,first])
        self.assertTrue(receipt.diff_queue(baseline,current,[],[])['external_changes'])
        current=self.snapshot([{**first,'IsChecked':False},second])
        self.assertTrue(receipt.diff_queue(baseline,current,[],[])['external_changes'])
        baseline=self.snapshot([first,first]);current=self.snapshot([first])
        self.assertTrue(receipt.diff_queue(baseline,current,[],[])['external_changes'])

    def test_unrelated_user_append_during_input_requires_reconciliation(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        original=self.row();other=self.row(code=456)
        Path(other['FilePath']).write_text(json.dumps({**SOURCE,'stage_name':'other_unit_stage'}),encoding='utf-8')
        self.config.write_bytes(raw_config([original,other]))
        result=self.collect(baseline)
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(result['code'],'baseline_queue_changed')

    def test_navigation_override_changed_is_external_modification(self):
        row=self.row();baseline=self.snapshot([row])
        current=self.snapshot([{**row,'IsNavNameOverride':True}])
        self.assertTrue(receipt.diff_queue(baseline,current,[],[])['external_changes'])

    def test_body_rewrite_after_read_is_not_stable(self):
        row=self.row();self.config.write_bytes(raw_config([row]));original=receipt._read_stable
        count=0
        def reading(path,*args,**kwargs):
            nonlocal count
            result=original(path,*args,**kwargs)
            if Path(path)==self.config:
                count+=1
                if count==3:Path(row['FilePath']).write_text('{"changed":true}',encoding='utf-8')
            return result
        with patch.object(receipt,'_read_stable',side_effect=reading):
            with self.assertRaises(receipt.MaaQueueReceiptError) as caught:self.snapshot()
            self.assertEqual(caught.exception.code,'body_changed_during_read')

    def test_snapshot_io_failure_never_exposes_raw_path_or_exception(self):
        with patch.object(Path,'mkdir',side_effect=PermissionError('DO_NOT_PUBLISH')):
            with self.assertRaises(receipt.MaaQueueReceiptError) as caught:self.snapshot([])
            self.assertNotIn('DO_NOT_PUBLISH',str(caught.exception))

    def test_body_deleted_after_read_has_typed_failure(self):
        row=self.row();self.config.write_bytes(raw_config([row]));original=receipt._read_stable
        count=0
        def reading(path,*args,**kwargs):
            nonlocal count
            result=original(path,*args,**kwargs)
            if Path(path)==self.config:
                count+=1
                if count==3:Path(row['FilePath']).unlink()
            return result
        with patch.object(receipt,'_read_stable',side_effect=reading):
            with self.assertRaises(receipt.MaaQueueReceiptError):self.snapshot()

    def test_evidence_cannot_be_written_inside_maa(self):
        before=set(self.maa.rglob('*'))
        with self.assertRaises(receipt.MaaQueueReceiptError):
            receipt.capture_queue_snapshot(self.binding,self.maa/'unwanted-output',
                screenshot_refs=[self.frame],visible_complete=True)
        self.assertEqual(set(self.maa.rglob('*')),before)

    def test_duplicate_identity_not_reusable_or_deletable_by_occurrence(self):
        baseline=self.snapshot([]); row=self.row();current=self.snapshot([row,row])
        target={**self.target,'semantic_sha256':current['rows'][0]['semantic_sha256']}
        diff=receipt.diff_queue(baseline,current,[target],[{'identity':current['rows'][0]['identity'],'owned':True}])
        self.assertTrue(diff['conflicts']);self.assertFalse(diff['reusable']);self.assertFalse(diff['extra_owned'])

    def test_incomplete_visibility_or_opaque_row_never_proves_missing(self):
        baseline=self.snapshot([]);current=self.snapshot([],visible=False)
        diff=receipt.diff_queue(baseline,current,[self.target],[])
        self.assertFalse(diff['missing']);self.assertTrue(diff['conflicts'])
        current=self.snapshot([self.row(path=str(self.root/'elsewhere.json'))])
        self.assertFalse(receipt.diff_queue(baseline,current,[self.target],[])['missing'])

    def test_same_snapshot_can_reuse_but_cannot_prove_missing(self):
        empty=self.snapshot([])
        diff=receipt.diff_queue(empty,empty,[self.target],[])
        self.assertFalse(diff['missing'])
        self.assertEqual(diff['conflicts'][0]['code'],'queue_absence_unverified')
        present=self.snapshot([self.row()])
        target={**self.target,'source_semantic_sha256':present['rows'][0]['semantic_sha256']}
        diff=receipt.diff_queue(present,present,[target],[])
        self.assertEqual(diff['reusable'],['unit-target'])
        self.assertFalse(diff['conflicts'])

    def test_later_snapshot_with_old_frame_does_not_prove_missing(self):
        baseline=self.snapshot([]);self.now+=1
        current=self.snapshot([])
        diff=receipt.diff_queue(baseline,current,[self.target],[])
        self.assertFalse(diff['missing'])
        self.assertEqual(diff['conflicts'][0]['code'],'queue_absence_unverified')

    def test_independent_later_new_frame_can_observe_absence_with_same_pixels(self):
        baseline=self.snapshot([]);self.now+=.2
        # A stable empty queue can produce identical pixels in a new capture.
        frame=self.fresh_frame(self.image.read_bytes())
        self.assertEqual(frame['sha256'],self.frame['sha256'])
        current=self.snapshot([],frame=frame)
        diff=receipt.diff_queue(baseline,current,[self.target],[])
        self.assertEqual(diff['missing'],['unit-target'])
        self.assertFalse(diff['conflicts'])

    def test_new_frame_without_observation_interval_does_not_prove_absence(self):
        baseline=self.snapshot([]);current=self.snapshot([],frame=self.fresh_frame())
        self.assertFalse(receipt.diff_queue(baseline,current,[self.target],[])['missing'])
        self.now+=.05
        current=self.snapshot([],frame=self.fresh_frame())
        self.assertFalse(receipt.diff_queue(baseline,current,[self.target],[])['missing'])

    def test_new_path_copied_old_frame_timestamp_is_not_fresh_capture(self):
        baseline=self.snapshot([]);self.now+=1
        frame=self.fresh_frame()
        old=Path(self.frame['path']).stat().st_mtime_ns
        os.utime(frame['path'],ns=(old,old))
        current=self.snapshot([],frame=frame)
        self.assertFalse(receipt.diff_queue(baseline,current,[self.target],[])['missing'])

    def test_rewriting_observed_frame_file_does_not_prove_independent_absence(self):
        baseline=self.snapshot([]);self.now+=1
        pixels=b'changed bytes in prior capture file'
        self.image.write_bytes(pixels)
        stamp_ns=round(self.now*1000)*1_000_000
        os.utime(self.image,ns=(stamp_ns,stamp_ns))
        self.now+=.01
        replacement={'path':str(self.image),'sha256':hashlib.sha256(pixels).hexdigest()}
        current=self.snapshot([],frame=replacement)
        diff=receipt.diff_queue(baseline,current,[self.target],[])
        self.assertFalse(diff['missing'])
        self.assertEqual(diff['conflicts'][0]['code'],'queue_absence_unverified')

    def test_fresh_log_plus_difficulty_three_queue_entries_verifies_normal_and_preserves_raid(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row(),self.row(raid=True)]))
        result=self.collect(baseline)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(len(result['matched_rows']),1)
        self.assertEqual(len(result['added_rows']),2)
        payload=self.payload(result)
        self.assertNotIn('DO_NOT_PUBLISH',json.dumps(payload,ensure_ascii=False))
        self.assertEqual(receipt.verify_queue_receipt(payload,self.expected(baseline))['status'],'verified')

    def test_old_log_old_body_and_no_new_entry_cannot_prove_input(self):
        self.append_success();baseline=self.baseline([self.row()]);self.now+=1
        self.assertEqual(self.collect(baseline)['status'],'pending')

    def test_log_append_during_queue_snapshot_requires_another_readonly_poll(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        original=receipt.capture_queue_snapshot
        def capture(*args,**kwargs):
            result=original(*args,**kwargs);self.append_success(code=456);return result
        with patch.object(receipt,'capture_queue_snapshot',side_effect=capture):result=self.collect(baseline)
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['code'],'log_changed_during_queue_read')

    def test_queue_change_after_snapshot_before_input_baseline_is_rejected(self):
        snapshot=self.snapshot([])
        self.config.write_bytes(raw_config([self.row()]))
        with self.assertRaises(receipt.MaaQueueReceiptError) as caught:
            receipt.capture_queue_baseline(self.binding,self.target,SOURCE,self.evidence,snapshot)
        self.assertEqual(caught.exception.code,'baseline_snapshot_changed')

    def test_config_lag_after_valid_log_is_pending_not_retry_permission(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        result=self.collect(baseline);self.assertEqual(result['status'],'pending')
        self.assertFalse(result['verified']);self.assertNotIn('retry',result)

    def test_log_rotation_truncation_prefix_change_and_interleaved_get_rejected(self):
        for fault in ('rotate','truncate','prefix','interleave'):
            with self.subTest(fault=fault):
                self.log.write_bytes(b'original log prefix\n');baseline=self.baseline([]);self.now+=1
                if fault=='rotate':
                    self.log.rename(self.log.with_suffix('.old'));self.log.write_bytes(b'original log prefix\n')
                elif fault=='truncate':self.log.write_bytes(b'')
                elif fault=='prefix':self.log.write_bytes(b'changed! log prefix\n')
                self.append_success()
                if fault=='interleave':self.append_success(code=456)
                self.config.write_bytes(raw_config([self.row()]))
                self.assertEqual(self.collect(baseline)['status'],'rejected')

    def test_receipt_tampering_body_mode_binding_and_ref_hash_rejected(self):
        baseline=self.baseline([]);self.now+=1;self.append_success()
        self.config.write_bytes(raw_config([self.row()]))
        payload=self.payload(self.collect(baseline))
        mutations=[lambda p:p['current_binding']['process'].update(pid=88),
            lambda p:p['current_snapshot']['rows'][0].update(is_raid=True),
            lambda p:p['current_snapshot']['body_evidence'][0].update(sha256='0'*64),
            lambda p:p['current_snapshot']['rows'][0]['body_ref'].update(sha256='0'*64)]
        for mutate in mutations:
            changed=copy.deepcopy(payload);mutate(changed)
            self.assertEqual(receipt.verify_queue_receipt(changed,self.expected(baseline))['status'],'rejected')

    def test_unknown_document_field_difference_is_not_normalized_away(self):
        before=receipt.canonical_copilot_document(SOURCE)
        after=receipt.canonical_copilot_document({**SOURCE,'unknown_unit_field':1})
        self.assertNotEqual(before,after)


if __name__=='__main__':unittest.main()

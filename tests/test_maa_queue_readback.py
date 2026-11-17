"""Queue geometry and alignment from synthetic images, without GUI/model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from shu.maa_queue_readback import (LocalFirstQueueReadback, QueueVisionReadback,
                                    _measure_boxes, _mode_palette, _open_bottom_list, _projection_endpoints,
                                    _queue_frame, _single_text_mismatch,
                                    _row_observations,
                                    stitch_queue_views)
from tests.test_maa_vision_readback import Client


def fixture(path, *, scale=1, rows=('UNIT-1', 'UNIT-2')):
    image = np.full((600, 1000, 3), 255, np.uint8)
    words = []

    def rect(x, y, width, height, color=(150,150,150)):
        cv2.rectangle(image, (x,y), (x+width,y+height), color, 1)

    def word(text, x, y, width, height=14):
        words.append({'text':text, 'rect':{'x':x*scale,'y':y*scale,
                      'width':width*scale,'height':height*scale}})
        cv2.line(image, (x+2,y+5), (x+width-2,y+5), (65,65,65), 1)

    word('自动战斗',360,42,64)
    word('主线/故事集/SideStory',43,95,172)
    rect(40,128,435,31)
    word('作业路径/神秘代码',48,137,120)
    word('开始',126,199,32)
    rect(37,260,17,17)
    cv2.rectangle(image,(38,261),(53,276),(195,185,12),-1)
    word('多作业模式',62,262,65)
    rect(274,177,200,330)
    rect(310,507,98,28)
    word('关卡名',333,515,43)
    rect(408,507,30,28)
    rect(438,507,36,28)
    cv2.line(image,(418,521),(428,521),(30,30,30),2)
    cv2.line(image,(423,516),(423,526),(30,30,30),2)
    cv2.line(image,(450,516),(460,526),(10,10,195),2)
    cv2.line(image,(450,526),(460,516),(10,10,195),2)
    for index, name in enumerate(rows):
        y = 187+index*31
        if index == 1:
            cv2.rectangle(image,(278,y-3),(468,y+20),(220,224,244),-1)
        rect(284,y,17,17)
        cv2.rectangle(image,(285,y+1),(300,y+16),(195,185,12),-1)
        word(name,310,y+2,max(24, min(122,len(name)*8)))
        cv2.line(image,(451,y+4),(461,y+14),(60,60,60),2)
        cv2.line(image,(451,y+14),(461,y+4),(60,60,60),2)
    if scale!=1:
        image=cv2.resize(image,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
    path.write_bytes(cv2.imencode('.png', image)[1].tobytes())
    return {'width':image.shape[1],'height':image.shape[0],'words':words}


def complete_controls(path):
    output=fixture(path)
    image=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    for text,y,width in (('自动编队',298,60),('忽略干员属性要求',329,132),('借助战',360,42)):
        cv2.rectangle(image,(37,y),(54,y+17),(150,150,150),1)
        cv2.rectangle(image,(38,y+1),(53,y+16),(195,185,12),-1)
        cv2.line(image,(64,y+7),(60+width,y+7),(65,65,65),1)
        output['words'].append({'text':text,'rect':{'x':62,'y':y+2,'width':width,'height':14}})
    path.write_bytes(cv2.imencode('.png',image)[1].tobytes())
    return output


class StubVision:
    def __init__(self, result):
        self.result=result

    def recognize_queue(self, path, *, cancel_event=None):
        return deepcopy(self.result)


class QueueReadbackTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'frame.png'

    def test_measures_controls_and_rows_at_two_scales(self):
        for scale in (1,2):
            with self.subTest(scale=scale):
                ocr=StubVision(fixture(self.path, scale=scale))
                result=QueueVisionReadback(ocr=ocr, calibration={
                    'normal_bgr':(255,255,255), 'raid_bgr':(220,224,244),
                }).observe(self.path)
                self.assertEqual(result['queue_mode'],True)
                self.assertIn('queue_add',result['targets'])
                self.assertNotIn('queue_clear',result['targets'])
                self.assertIsNone(result['queue_add_enabled'])
                self.assertTrue(any(region['kind']=='queue_clear' for region in result['forbidden_regions']))
                self.assertEqual([(row['name'],row['mode'],row['is_checked']) for row in result['queue_rows']],
                                 [('UNIT-1',1,True),('UNIT-2',2,True)])
                self.assertTrue(all(row['delete_rect'] for row in result['queue_rows']))

    def test_input_lower_border_cannot_be_a_second_queue_top(self):
        image=np.full((1080,1000,3),255,np.uint8)
        cv2.rectangle(image,(80,144),(464,181),(224,224,224),1)
        cv2.rectangle(image,(303,188),(537,1004),(224,224,224),1)
        field={'x':80,'y':144,'width':385,'height':38}
        footer={'x':354,'y':966,'width':95,'height':38}
        clear={'x':494,'y':967,'width':43,'height':36}
        result=_open_bottom_list(image,field,footer,clear)
        self.assertIsNotNone(result)
        self.assertGreater(result['y'],field['y']+field['height'])
        self.assertLessEqual(abs(result['y']-188),2)

    def test_unfocused_input_canny_exterior_cannot_be_a_second_queue_top(self):
        image=np.full((1080,1000,3),255,np.uint8)
        for first,second in (((80,144),(464,181)),((464,145),(503,181)),((503,145),(540,181))):
            cv2.rectangle(image,first,second,(224,224,224),1)
        cv2.rectangle(image,(303,188),(537,1004),(224,224,224),1)
        field={'x':81,'y':145,'width':383,'height':36}
        footer={'x':350,'y':966,'width':100,'height':38}
        clear={'x':494,'y':967,'width':43,'height':36}
        queue=_open_bottom_list(image,field,footer,clear)
        self.assertIsNotNone(queue)
        self.assertLessEqual(abs(queue['y']-188),2)

    def test_recorded_short_queues_measure_both_rows_above_raid_separator(self):
        # Fixed-image replay of the user's failed queues. These literal OCR
        # boxes came from MaaFramework 5.15.0, without expected row labels.
        root=Path(__file__).resolve().parents[1]
        cases=(
            ('870681cd5839497cb147ad49babd6ad5/maa-queue/9',
             'queue-frame-7d6ebd9b8c8a412fb6ba572f37fa0bdc.png',
             'b9a43d4bb4ea871416bff6e40bfa92826c70eeca37840f56f6f4e387745de026','TL-EX-1'),
            ('3e26047e25c94cc9adb25c47c862b1d2/maa-queue/10',
             'queue-frame-ac2314e8dabb4cb5ad5d3570480b2384.png',
             'c6a10247935f34b680967839f8c2b288e2b9998439a5f51c33044a550be3564b','TL-EX-3'),
            ('3e26047e25c94cc9adb25c47c862b1d2/maa-queue/10',
             'queue-frame-a1d9d0359ae7407b901aa337c6386e0b.png',
             '2d0f46b801655f40cc9b985330001d334618c2c8058c59292bd8dcc0fb7064cc','TL-EX-3'))
        for folder,name,digest,stage in cases:
            with self.subTest(frame=name):
                picture=root/'user-data/task-private'/folder/name
                if not picture.is_file():
                    self.skipTest('Recorded native queue screenshot is not present')
                self.assertEqual(hashlib.sha256(picture.read_bytes()).hexdigest(),digest)
                labels=[('自动战斗',344,45,63,19),('主线/故事集/SideStory',94,108,160,20),
                        ('自动编队？',108,271,82,19),('忽略干员属性要求？',108,340,140,20),
                        ('借助战？',109,373,66,19),('多作业模式？',109,500,95,19),
                        ('开始',172,210,35,18),('关卡名',358,976,47,17),
                        (stage,341,199,63 if stage=='TL-EX-1' else 64,19),
                        (stage,343 if stage=='TL-EX-1' else 344,238,
                         58 if stage=='TL-EX-1' else 60,16)]
                words=[{'text':text,'rect':{'x':x,'y':y,'width':w,'height':h}}
                       for text,x,y,w,h in labels]
                result=QueueVisionReadback(ocr=StubVision(
                    {'width':1000,'height':1080,'words':words})).observe(picture)
                self.assertIsNotNone(result['queue_region'])
                self.assertLessEqual(abs(result['queue_region']['y']-188),2)
                self.assertTrue(result['rows_measured'])
                self.assertEqual([(row['name'],row['mode'],row['is_checked'])
                                  for row in result['queue_rows']],
                                 [(stage,1,True),(stage,2,True)])
                self.assertNotIn('start',result['targets'])
                self.assertFalse(result['input_permission'])

    def test_outer_outline_survives_empty_single_short_and_long_queue(self):
        field={'x':80,'y':144,'width':385,'height':38}
        footer={'x':350,'y':966,'width':100,'height':38}
        clear={'x':494,'y':967,'width':43,'height':36}
        for count in (0,1,2,18):
            with self.subTest(rows=count):
                image=np.full((1080,1000,3),255,np.uint8)
                cv2.rectangle(image,(303,188),(537,1004),(224,224,224),1)
                for index in range(count):
                    y=199+37*index
                    if index%2:
                        cv2.rectangle(image,(306,y-8),(533,y+26),(204,204,255),-1)
                    cv2.rectangle(image,(318,y),(337,y+19),(195,185,12),-1)
                result=_open_bottom_list(image,field,footer,clear)
                self.assertIsNotNone(result)
                self.assertLessEqual(abs(result['y']-188),2)
                self.assertLessEqual(abs(result['y']+result['height']-966),2)

    def test_disconnected_horizontal_stroke_above_queue_is_not_its_top(self):
        image=np.full((1080,1000,3),255,np.uint8)
        field={'x':80,'y':140,'width':385,'height':30}
        footer={'x':350,'y':966,'width':100,'height':38}
        clear={'x':494,'y':967,'width':43,'height':36}
        cv2.rectangle(image,(303,205),(537,1004),(224,224,224),1)
        cv2.line(image,(303,180),(537,180),(224,224,224),1)
        result=_open_bottom_list(image,field,footer,clear)
        self.assertIsNotNone(result)
        self.assertLessEqual(abs(result['y']-205),2)

    def test_missing_outer_top_cannot_promote_internal_separator(self):
        image=np.full((1080,1000,3),255,np.uint8)
        field={'x':80,'y':144,'width':385,'height':38}
        footer={'x':350,'y':966,'width':100,'height':38}
        clear={'x':494,'y':967,'width':43,'height':36}
        cv2.line(image,(303,188),(303,1004),(224,224,224),1)
        cv2.line(image,(537,188),(537,1004),(224,224,224),1)
        cv2.line(image,(303,227),(537,227),(224,224,224),1)
        # With no OCR/checkbox hints, the physical sides above the separator
        # still prove that it cannot be the missing outer top.
        self.assertIsNone(_open_bottom_list(image,field,footer,clear))

    def test_forged_lower_frame_cannot_hide_measured_checkbox_above_it(self):
        output=fixture(self.path)
        image=cv2.imdecode(np.frombuffer(self.path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        image[175:181,270:478]=255
        cv2.line(image,(274,215),(474,215),(150,150,150),1)
        boxes=_measure_boxes(image)
        queue,_,_=_queue_frame(image,boxes,output['words'],{'regions':{'code_input':
                              {'x':40,'y':128,'width':435,'height':31}}})
        self.assertIsNone(queue)

    def test_one_pixel_outline_drift_must_cover_almost_the_entire_right_border(self):
        image = np.full((1080,1000,3),255,np.uint8)
        field = {'x':81,'y':145,'width':383,'height':36}
        footer = {'x':350,'y':966,'width':100,'height':38}
        clear = {'x':494,'y':966,'width':43,'height':37}
        edges = np.zeros((1080,1000),np.uint8)
        edges[187,302:538] = 255
        edges[188:966,302] = 255
        edges[188:610,537] = 255
        edges[610:966,538] = 255
        with patch('cv2.Canny',return_value=edges):
            queue = _open_bottom_list(image,field,footer,clear)
        self.assertIsNotNone(queue)
        self.assertLessEqual(abs(queue['x']+queue['width']-538),1)
        edges[400:450,537:539] = 0
        with patch('cv2.Canny',return_value=edges):
            self.assertIsNone(_open_bottom_list(image,field,footer,clear))

    def test_footer_crop_remeasures_clear_when_full_image_contours_miss_it(self):
        ocr = fixture(self.path)
        image = cv2.imdecode(np.frombuffer(self.path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        full_boxes = [box for box in _measure_boxes(image)
                      if not (434 <= box['x'] <= 442 and 503 <= box['y'] <= 511)]
        field = {'x':40,'y':128,'width':435,'height':31}
        _, add, clear = _queue_frame(image,full_boxes,ocr['words'],{'regions':{'code_input':field}})
        self.assertIsNotNone(add)
        self.assertIsNotNone(clear)
        self.assertLessEqual(abs(clear['x']-438),2)
        # The same text/plus measurements cannot create a missing clear outline.
        image[507:538,439:477] = 255
        self.assertEqual(_queue_frame(image,full_boxes,ocr['words'],{'regions':{'code_input':field}}),
                         (None,None,None))

    def test_one_uncertain_row_needs_unique_offset_and_exact_other_facts(self):
        rows=[{'name':'UNIT-X','mode':1,'is_checked':True},
              {'name':'UNIT-2','mode':2,'is_checked':True}]
        expected=[{'name':'UNIT-1','is_raid':False,'is_checked':True},
                  {'name':'UNIT-2','is_raid':True,'is_checked':True}]
        self.assertEqual(_single_text_mismatch(rows,expected),0)
        self.assertIsNone(_single_text_mismatch(rows,expected+expected))
        changed=deepcopy(expected)
        changed[1]['is_checked']=False
        self.assertIsNone(_single_text_mismatch(rows,changed))
        changed=deepcopy(expected)
        changed[1]['name']='OTHER'
        self.assertIsNone(_single_text_mismatch(rows,changed))

    def test_single_row_closeup_requires_new_ocr_text_and_never_inserts_expected_name(self):
        output=fixture(self.path)
        wrong=deepcopy(output)
        wrong['words']=[{**word,'text':'UNIT-X' if word['text']=='UNIT-1' else word['text']}
                        for word in wrong['words']]
        actual=next(word for word in output['words'] if word['text']=='UNIT-1')
        expected={'projection_sha256':'a'*64,'rows':[
            {'name':'UNIT-1','is_raid':False,'is_checked':True},
            {'name':'UNIT-2','is_raid':True,'is_checked':True}]}
        class Observer(QueueVisionReadback):
            def _enlarge(inner,image,queue,cancel_event):
                return deepcopy(wrong['words'])
            def _reread_row(inner,image,queue,row,cancel_event):
                inner.calls.append(row['name'])
                return [deepcopy(inner.fresh)]
        for fresh,complete in ((actual,True),({**actual,'text':'UNIT-Z'},False)):
            observer=Observer(ocr=StubVision(wrong),calibration={
                'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)})
            observer.calls=[]
            observer.fresh=fresh
            result=observer.observe(self.path,expected=expected)
            self.assertEqual(result['visible_complete'],complete)
            self.assertLessEqual(len(observer.calls),2)
            self.assertNotIn('UNIT-1',observer.calls)
            self.assertEqual(result['queue_rows'][0]['name'],fresh['text'])

    def test_expected_rows_cannot_fill_unmeasured_mode(self):
        result=QueueVisionReadback(ocr=StubVision(fixture(self.path))).observe(self.path, expected={
            'projection_sha256':'a'*64, 'rows':[{'name':'UNIT-1','is_raid':False,'is_checked':True},
                                              {'name':'UNIT-2','is_raid':True,'is_checked':True}],
        })
        self.assertEqual([row['mode'] for row in result['queue_rows']],[1,None])
        self.assertFalse(result['visible_complete'])
        self.assertFalse(any(name.startswith('row_delete:') for name in result['targets']))

    def test_incomplete_full_frame_rows_are_reread_inside_measured_list(self):
        output=fixture(self.path)
        original=deepcopy(output['words'])
        for word in output['words']:
            if word['text']=='UNIT-2':
                word['rect']['x']=300
        class QueueCloseup(QueueVisionReadback):
            def _enlarge(inner,image,region,cancel_event):
                inner.closeup_region=region
                return [word for word in original if word['text'].startswith('UNIT-')]
        observer=QueueCloseup(ocr=StubVision(output),calibration={
            'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)})
        result=observer.observe(self.path,expected={'projection_sha256':'a'*64,
            'rows':[{'name':'UNIT-1','is_checked':True,'is_raid':False},
                    {'name':'UNIT-2','is_checked':True,'is_raid':True}]})
        self.assertEqual(result['row_readback'],'unhinted_measured_queue_closeup')
        self.assertTrue(result['rows_measured'])
        self.assertTrue(result['visible_complete'])
        self.assertEqual([row['name'] for row in result['queue_rows']],['UNIT-1','UNIT-2'])
        self.assertEqual(observer.closeup_region,result['queue_region'])

    def test_missing_closeup_row_cannot_be_filled_from_expected_names(self):
        output=fixture(self.path)
        original=deepcopy(output['words'])
        for word in output['words']:
            if word['text']=='UNIT-2':
                word['rect']['x']=300
        class MissingQueueRow(QueueVisionReadback):
            def _enlarge(inner,image,region,cancel_event):
                return [word for word in original if word['text']=='UNIT-1']
        observer=MissingQueueRow(ocr=StubVision(output),calibration={
            'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)})
        result=observer.observe(self.path,expected={'projection_sha256':'a'*64,
            'rows':[{'name':'UNIT-1','is_checked':True,'is_raid':False},
                    {'name':'UNIT-2','is_checked':True,'is_raid':True}]})
        self.assertFalse(result['rows_measured'])
        self.assertFalse(result['visible_complete'])
        self.assertFalse(any(name.startswith('row_delete:') for name in result['targets']))

    def test_measured_but_mistranscribed_row_is_reread_from_pixels(self):
        output=fixture(self.path)
        original=deepcopy(output['words'])
        for word in output['words']:
            if word['text']=='UNIT-2':
                word['text']='UNlT-2'
        class QueueCloseup(QueueVisionReadback):
            def _enlarge(inner,image,region,cancel_event):
                return [word for word in original if word['text'].startswith('UNIT-')]
        observer=QueueCloseup(ocr=StubVision(output),calibration={
            'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)})
        result=observer.observe(self.path,expected={'projection_sha256':'a'*64,
            'rows':[{'name':'UNIT-1','is_checked':True,'is_raid':False},
                    {'name':'UNIT-2','is_checked':True,'is_raid':True}]})
        self.assertEqual(result['row_readback'],'unhinted_measured_queue_closeup')
        self.assertTrue(result['visible_complete'])
        self.assertEqual([row['name'] for row in result['queue_rows']],['UNIT-1','UNIT-2'])

    def test_missing_last_control_label_does_not_shorten_control_closeup(self):
        output=fixture(self.path)
        original=deepcopy(output['words'])
        output['words']=[word for word in output['words'] if word['text']!='多作业模式']
        class CompleteControls(QueueVisionReadback):
            def _enlarge(inner,image,region,cancel_event):
                inner.control_region=region
                return [word for word in original if word['text']=='多作业模式']
        observer=CompleteControls(ocr=StubVision(output),calibration={
            'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)})
        result=observer.observe(self.path)
        self.assertTrue(result['queue_mode'])
        self.assertEqual(result['control_readback'],'unhinted_measured_control_closeup')
        label=next(word for word in original if word['text']=='多作业模式')['rect']
        self.assertLessEqual(observer.control_region['y'],label['y'])
        self.assertGreater(observer.control_region['y']+observer.control_region['height'],
                           label['y']+label['height'])

    def test_long_list_endpoints_require_unique_measured_ordered_slice(self):
        expected = [{'name':name, 'is_raid':False, 'is_checked':True} for name in ('A-1','A-2','A-3','A-4')]
        visible = [{'name':name, 'mode':1, 'is_checked':True} for name in ('A-2','A-3')]
        self.assertEqual(_projection_endpoints(visible,expected),
                         {'top':False,'bottom':False,'proof':'unique_visible_projection_slice'})
        self.assertTrue(_projection_endpoints(visible,expected[1:])['top'])
        self.assertTrue(_projection_endpoints(visible,expected[:-1])['bottom'])
        self.assertIsNone(_projection_endpoints(visible,expected + expected))
        self.assertIsNone(_projection_endpoints([{**visible[0],'mode':None},visible[1]],expected))
        self.assertIsNone(_projection_endpoints(list(reversed(visible)),expected))

    def test_local_first_queue_falls_back_on_unverified_geometry(self):
        from unittest.mock import Mock
        reader = LocalFirstQueueReadback(Mock(), Mock())
        reader.local.observe = Mock(return_value={'ok':False,'options':{},'targets':{}})
        reader.fallback.observe = Mock(return_value={'ok':True})
        self.assertEqual(reader.observe(self.path)['recognition_engine'], 'deepseek_vision+queue_geometry')
        reader.fallback.observe.assert_called_once()

    def test_fallback_rechecks_same_image_local_controls_while_reading_rows_independently(self):
        output=complete_controls(self.path)
        controls=QueueVisionReadback(ocr=StubVision(output)).observe(self.path)
        self.assertTrue(all(type(option.get('state')) is bool for option in controls['options'].values()))
        self.assertEqual(len(controls['options']),4)
        drifted=deepcopy(output)
        for word in drifted['words']:
            if word['text'] in {'多作业模式','自动编队','忽略干员属性要求','借助战'}:
                word['rect']['y']+=45
        result=QueueVisionReadback(ocr=StubVision(drifted),calibration={
            'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)}).observe(self.path,
            control_evidence=controls,expected={'projection_sha256':'a'*64,
                'rows':[{'name':name,'is_raid':raid,'is_checked':True}
                        for name,raid in (('UNIT-1',False),('UNIT-2',True))]})
        self.assertEqual(result['control_readback'],'same_image_local_pixel_verified_controls')
        self.assertTrue(result['queue_mode'])
        self.assertTrue(result['visible_complete'])
        self.assertEqual([row['name'] for row in result['queue_rows']],['UNIT-1','UNIT-2'])

    def test_control_evidence_from_different_image_cannot_override_transcription(self):
        output=complete_controls(self.path)
        controls=QueueVisionReadback(ocr=StubVision(output)).observe(self.path)
        controls['image_sha256']='0'*64
        drifted=deepcopy(output)
        for word in drifted['words']:
            if word['text']=='多作业模式':
                word['rect']['y']+=45
        result=QueueVisionReadback(ocr=StubVision(drifted)).observe(self.path,control_evidence=controls)
        self.assertIsNone(result['queue_mode'])
        self.assertNotEqual(result.get('control_readback'),'same_image_local_pixel_verified_controls')

    def test_same_image_footer_label_is_rechecked_against_actual_contours(self):
        output=complete_controls(self.path)
        controls=QueueVisionReadback(ocr=StubVision(output)).observe(self.path)
        self.assertIn('queue_stage_name',controls['anchors'])
        missing=deepcopy(output)
        missing['words']=[word for word in missing['words'] if word['text']!='关卡名']
        observer=QueueVisionReadback(ocr=StubVision(missing))
        self.assertEqual(observer.observe(self.path)['reason'],'queue_layout_unverified')
        result=observer.observe(self.path,control_evidence=controls)
        self.assertEqual(result['queue_mode'],True)
        self.assertIsNotNone(result['queue_region'])

    def test_local_first_queue_uses_measured_complete_queue_without_network(self):
        from unittest.mock import Mock
        reader = LocalFirstQueueReadback(Mock(), Mock())
        reader.local.observe = Mock(return_value={'ok':True,'page':'copilot','targets':{'code_input':{}},
            'options':{name:{'state':False} for name in ('auto_formation','ignore_requirements',
                                                        'use_support','multiple_jobs')},
            'queue_mode':True,'rows_measured':True,'visible_complete':True})
        reader.fallback.observe = Mock()
        self.assertEqual(reader.observe(self.path)['recognition_engine'],'maafw_ocr+queue_geometry')
        reader.fallback.observe.assert_not_called()

    def test_size_mismatch_is_rejected(self):
        output=fixture(self.path)
        output['width']-=1
        result=QueueVisionReadback(ocr=StubVision(output)).observe(self.path)
        self.assertFalse(result['ok'])
        self.assertEqual(result['targets'],{})

    def test_measured_tab_anchor_is_retained_for_strict_caption_refresh(self):
        result=QueueVisionReadback(ocr=StubVision(fixture(self.path))).observe(self.path)
        self.assertEqual(result['targets']['copilot_tab']['rect'],
                         result['anchors']['copilot_tab']['rect'])
        self.assertNotIn('start',result['targets'])

    def test_coarse_option_label_does_not_bind_adjacent_checkbox_without_closeup(self):
        output=fixture(self.path)
        for word in output['words']:
            if word['text']=='多作业模式':
                word['rect']['y']+=32
        class FailedCloseup(StubVision):
            def recognize_queue(self,path,*,cancel_event=None):
                if Path(path).name=='queue.png':
                    from shu.maa_vision_readback import VisionOcrError
                    raise VisionOcrError('vision_model_failed')
                return super().recognize_queue(path,cancel_event=cancel_event)
        result=QueueVisionReadback(ocr=FailedCloseup(output)).observe(self.path)
        self.assertIsNone(result['queue_mode'])
        self.assertNotIn('multiple_jobs',result['targets'])
        self.assertEqual(result['control_refinement_error'],'vision_model_failed')

    def test_control_closeup_receives_no_expected_queue_names_or_state(self):
        output=fixture(self.path)
        for word in output['words']:
            if word['text']=='多作业模式':
                word['rect']['y']+=32
        client=Client(output)
        result=QueueVisionReadback(client=client).observe(self.path,expected={
            'projection_sha256':'a'*64,'rows':[{'name':'PRIVATE_QUEUE_NAME',
            'identity':'PRIVATE_ROW_ID','is_checked':True,'is_raid':False}]})
        # The second model response intentionally has the original frame size,
        # so it cannot prove a valid closeup. Merely requesting it leaks no hints.
        self.assertEqual(len(client.calls),2)
        self.assertIsNone(result['queue_mode'])
        for messages,options in client.calls:
            self.assertNotIn('PRIVATE_QUEUE_NAME',str(messages))
            self.assertNotIn('PRIVATE_ROW_ID',str(messages))
            self.assertEqual(options['tool_choice'],'none')

    def test_recorded_125_percent_narrow_window_options_use_pixel_contours(self):
        # Offline regression of a recorded capture, not a real model/autonomy
        # acceptance. Full-frame anchor boxes are exactly from its saved model
        # response; closeup annotations below were manually checked on the PNG.
        report_path=Path(__file__).resolve().parents[1]/'artifacts/20261110-multi-job/probe-live-622/report.json'
        if not report_path.is_file():
            self.skipTest('Recorded 2026-11-10 MAA 6.22 capture is not present')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        picture=Path(report['evidence_refs'][0]['path'])
        if not picture.is_file():
            self.skipTest('Recorded native screenshot is not present')
        self.assertEqual(hashlib.sha256(picture.read_bytes()).hexdigest(),report['evidence_refs'][0]['sha256'])
        view_ref=next(ref for ref in report['evidence_refs'] if 'probe-vision' in ref['path'])
        view=json.loads(Path(view_ref['path']).read_text(encoding='utf-8'))
        words=list(view['anchors'].values())
        # The original model label was used to identify the measured input.
        words.append({'text':'作业路径/神秘代码','rect':{'x':43,'y':157,'width':126,'height':14}})
        transcription={'width':1000,'height':1080,'words':words}
        checked_labels=[('自动编队',184,273,60),('忽略干员属性要求',184,343,119),
                        ('借助战',184,375,45),('多作业模式',184,503,75)]
        class RecordedCloseup(QueueVisionReadback):
            def _enlarge(inner,image,region,cancel_event):
                inner.closeup_region=region
                return [{'text':text,'rect':{'x':x,'y':y,'width':width,'height':14}}
                        for text,x,y,width in checked_labels]
        observer=RecordedCloseup(ocr=StubVision(transcription))
        result=observer.observe(picture)
        self.assertEqual(result['control_readback'],'unhinted_measured_control_closeup')
        self.assertEqual({name:option['state'] for name,option in result['options'].items()},
            {'auto_formation':True,'ignore_requirements':True,'use_support':False,'multiple_jobs':False})
        self.assertFalse(result['queue_mode'])
        self.assertEqual(result['options']['multiple_jobs']['rect'],
                         {'x':157,'y':501,'width':18,'height':18})
        self.assertNotIn('start',result['targets'])
        self.assertFalse(result['input_permission'])

    def test_current_list_match_binds_only_unambiguous_delete_rows(self):
        first='UNIT-1 Longer operator composition label'
        for names, modes in (((first,'UNIT-2'),(1,2)),(('UNIT-1','UNIT-1'),(1,1))):
            with self.subTest(names=names):
                ocr=StubVision(fixture(self.path, rows=names))
                calibration={'normal_bgr':(255,255,255),'raid_bgr':(220,224,244)}
                if modes==(1,1):
                    img=cv2.imdecode(np.frombuffer(self.path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
                    img[(img==calibration['raid_bgr']).all(axis=2)]=(255,255,255)
                    self.path.write_bytes(cv2.imencode('.png',img)[1].tobytes())
                identities=['b'*64,'c'*64]
                result=QueueVisionReadback(ocr=ocr,calibration=calibration).observe(self.path, expected={
                    'projection_sha256':'a'*64,
                    'rows':[{'name':name,'is_raid':mode==2,'is_checked':True,'identity':identity}
                            for name,mode,identity in zip(names,modes,identities,strict=True)],
                })
                self.assertTrue(result['visible_complete'])
                deletes={name for name in result['targets'] if name.startswith('row_delete:')}
                self.assertEqual(deletes, set() if modes==(1,1) else {'row_delete:'+value for value in identities})

    def test_empty_ocr_cannot_hide_nonempty_list(self):
        output=fixture(self.path)
        output['words']=[word for word in output['words'] if not word['text'].startswith('UNIT-')]
        result=QueueVisionReadback(ocr=StubVision(output)).observe(self.path,
            expected={'projection_sha256':'a'*64,'rows':[]})
        self.assertFalse(result['visible_complete'])
        self.assertFalse(result['rows_measured'])

    def test_source_bound_raid_brush_uses_the_actual_light_panel_color(self):
        output=fixture(self.path)
        image=cv2.imdecode(np.frombuffer(self.path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        image[(image==(220,224,244)).all(axis=2)]=(204,204,255)
        self.path.write_bytes(cv2.imencode('.png',image)[1].tobytes())
        result=QueueVisionReadback(ocr=StubVision(output)).observe(self.path)
        self.assertEqual([row['mode'] for row in result['queue_rows']],[1,2])

    def test_dense_full_view_measures_modes_from_the_uniform_light_gutter(self):
        # The recorded 18-row queue has a one-pixel light gutter followed by
        # raid backgrounds. Averaging five columns rejects its valid palette.
        image=np.full((820,300,3),255,np.uint8)
        queue={'x':20,'y':20,'width':236,'height':777}
        words=[]
        for index in range(18):
            top=31+37*index
            if index%2:
                cv2.rectangle(image,(24,top-8),(252,top+26),(204,204,255),-1)
            cv2.rectangle(image,(35,top),(54,top+19),(195,185,12),-1)
            cv2.line(image,(65,top+8),(115,top+8),(65,65,65),1)
            words.append({'text':f'UNIT-{index//2+1}','rect':
                          {'x':64,'y':top+2,'width':61,'height':19}})
        rows,measured=_row_observations(image,queue,words,_measure_boxes(image),{},'a'*64)
        self.assertTrue(measured)
        self.assertEqual(len(rows),18)
        self.assertEqual([row['mode'] for row in rows],[1,2]*9)
        expected=[{'name':word['text'],'is_raid':bool(index%2),'is_checked':True}
                  for index,word in enumerate(words)]
        self.assertEqual(_projection_endpoints(rows,expected),
                         {'top':True,'bottom':True,'proof':'unique_visible_projection_slice'})

    def test_recorded_18_row_queue_replays_original_native_ocr_and_pixels(self):
        # Original local OCR is read from its immutable diagnostic log. The
        # names/states below were independently checked on the saved PNG;
        # neither the current reader nor a configured queue supplies them.
        root=Path(__file__).resolve().parents[2]/'q44/artifacts/qa-queue-two/conversation/data'
        picture=root/'maa-visual-captures/e473bd6847564576a0702d4e1a3ec5a6.png'
        log=root/'framework-observation-logs/debug/maafw.log'
        if not picture.is_file() or not log.is_file():
            self.skipTest('Recorded 2026-11-15 queue pixels/native OCR are not present')
        self.assertEqual(hashlib.sha256(picture.read_bytes()).hexdigest(),
                         '334e257a01aa7109b1466ded23050d334dc41196b9aece448a396bb841fb297e')
        queue={'x':302,'y':189,'width':236,'height':777}
        decoder=json.JSONDecoder()
        words=None
        for line in log.read_text(encoding='utf-8').splitlines():
            if '"all":' not in line:
                continue
            try:
                detail,_=decoder.raw_decode(line[line.index('{'):])
            except ValueError:
                continue
            pending=[detail]
            while pending:
                item=pending.pop()
                if isinstance(item,dict):
                    rows=item.get('all')
                    if isinstance(rows,list):
                        native=[row for row in rows if row.get('score',0)>=.85 and row.get('text')]
                        # Locate the full-window response by its native boxes,
                        # without selecting or correcting any expected name.
                        inside=[row for row in native if 340<=row['box'][0]<=350
                                and 189<=row['box'][1]<=850]
                        if len(inside)==18:
                            words=[{'text':row['text'],'rect':dict(zip(
                                ('x','y','width','height'),row['box']))} for row in native]
                            break
                    pending.extend(item.values())
                elif isinstance(item,list):
                    pending.extend(item)
            if words is not None:
                break
        self.assertIsNotNone(words,'Original full-window native OCR response missing')
        expected=[{'name':name,'is_checked':True,'is_raid':raid}
                  for name in ('IW-EX-1','IW-EX-2','TL-EX-1','TL-EX-2','TL-EX-3',
                               'TL-EX-4','TL-EX-5','TL-EX-6','TL-EX-7') for raid in (False,True)]
        result=QueueVisionReadback(ocr=StubVision({'width':1000,'height':1080,'words':words})).observe(
            picture,expected={'projection_sha256':'a'*64,'rows':expected})
        self.assertEqual(result['queue_region'],queue)
        self.assertTrue(result['rows_measured'])
        self.assertEqual([(row['name'],row['mode'],row['is_checked']) for row in result['queue_rows']],
                         [(row['name'],2 if row['is_raid'] else 1,True) for row in expected])
        self.assertTrue(result['visible_complete'])
        self.assertEqual(result['scroll_state'],
                         {'top':True,'bottom':True,'proof':'unique_visible_projection_slice'})
        self.assertFalse(result['input_permission'])

    def test_nonuniform_or_nonlight_gutter_does_not_calibrate_a_theme(self):
        queue={'x':0,'y':0,'width':100,'height':100}
        for color in ((230,230,230),(204,204,255)):
            with self.subTest(color=color):
                image=np.full((100,100,3),color,np.uint8)
                self.assertEqual(_mode_palette(image,queue,{}),(None,None))
        image=np.full((100,100,3),255,np.uint8)
        image[40:70,3:8]=(204,204,255)
        self.assertEqual(_mode_palette(image,queue,{}),(None,None))

    def test_recorded_125_percent_footer_can_supply_native_parse_probe(self):
        from shu.maa_queue_parse_ready import capture_parse_probe
        from tests.test_maa_queue_receipt import QueueReceiptTests
        picture=Path(__file__).resolve().parents[2]/(
            'q44/artifacts/qa-queue-two/conversation/data/maa-visual-captures/'
            'e473bd6847564576a0702d4e1a3ec5a6.png')
        if not picture.is_file():
            self.skipTest('Recorded 2026-11-15 native queue capture is not present')
        self.assertEqual(hashlib.sha256(picture.read_bytes()).hexdigest(),
                         '334e257a01aa7109b1466ded23050d334dc41196b9aece448a396bb841fb297e')
        files=QueueReceiptTests()
        files.setUp()
        self.addCleanup(files.doCleanups)
        ref=files.fresh_frame(picture.read_bytes())
        snapshot=files.snapshot([],frame=ref)
        frame={'screenshot_id':'a'*32,'image_path':ref['path'],'image_sha256':ref['sha256'],
               'method':'win32_printwindow','window':{**files.process,
               'client_rect':{'x':0,'y':0,'width':1000,'height':1080},'dpi':120,
               'foreground':True,'minimized':False}}
        probe=capture_parse_probe(frame,{'x':449,'y':966,'width':45,'height':38},snapshot,
            code_text='prts://109538',code_input_rect={'x':81,'y':147,'width':383,'height':36})
        self.assertEqual(probe['measurement']['state'],'ready')
        # One frame classifies the local icon only; it never proves that a
        # specific load finished or grants input/start permission.
        from shu.maa_queue_parse_ready import verify_parse_ready
        self.assertFalse(verify_parse_ready([probe],{})['verified'])

    def test_antialiased_parse_glyph_uses_darkest_supported_ink(self):
        from shu.maa_queue_parse_ready import _measure
        gray=np.asarray([[255,255,255,82,185,255,255,255],
                         [255,255,255,61,171,255,255,255],
                         [255,255,255,47,171,255,255,255],
                         [78,61,61,36,50,61,61,164],
                         [182,171,171,50,120,171,171,220],
                         [255,255,255,47,171,255,255,255],
                         [255,255,255,47,171,255,255,255],
                         [255,255,255,161,220,255,255,255]],dtype=np.uint8)
        # Exact native 125% + pixels, then the pinned .4 disabled opacity.
        for opacity,state in ((1,'ready'),(.4,'busy')):
            with self.subTest(opacity=opacity):
                image=np.full((40,45,3),255,dtype=np.uint8)
                pixels=np.rint(255-(255-gray.astype(float))*opacity).astype(np.uint8)
                image[14:22,17:25]=pixels[:,:,None]
                self.assertEqual(_measure(image,{'x':0,'y':0,'width':45,'height':40})['state'],state)

    def test_pixel_measured_empty_queue_proves_both_viewport_endpoints(self):
        output=fixture(self.path,rows=())
        result=QueueVisionReadback(ocr=StubVision(output)).observe(self.path,
            expected={'projection_sha256':'a'*64,'rows':[]})
        self.assertTrue(result['rows_measured'])
        self.assertTrue(result['visible_complete'])
        self.assertEqual(result['scroll_state'],
                         {'top':True,'bottom':True,'proof':'unique_visible_projection_slice'})

    def test_config_names_and_identifiers_never_reach_the_model(self):
        client=Client(fixture(self.path))
        result=QueueVisionReadback(client=client).observe(self.path, expected={
            'projection_sha256':'a'*64, 'rows':[{'name':'PrivateExpectedName','is_raid':False,
                'is_checked':True,'identity':'PrivateExpectedID'}],
        })
        self.assertFalse(result['visible_complete'])
        # The incomplete controls and mismatched row each permit one pixel-only
        # closeup. Every call must remain free of the configuration's names.
        self.assertEqual(len(client.calls),3)
        for messages, options in client.calls:
            self.assertNotIn('PrivateExpectedName',str(messages))
            self.assertNotIn('PrivateExpectedID',str(messages))
            self.assertEqual(options['tool_choice'],'none')

    def test_enlarged_crop_maps_pixels_back_to_its_measured_origin(self):
        class CropVision:
            def recognize_queue(self,path,*,cancel_event=None):
                width,height=120,160
                return {'width':width,'height':height,'words':[{'text':'UNIT-1','rect':{
                    'x':20,'y':30,'width':60,'height':20}}]}

        observer=QueueVisionReadback(ocr=CropVision())
        image=np.zeros((100,110,3),dtype=np.uint8)
        region={'x':10,'y':11,'width':52,'height':68}
        words=observer._enlarge(image,region,None)
        self.assertEqual(words[0]['rect'],{'x':20,'y':25,'width':24,'height':8})

    def test_scroll_stitch_needs_unique_two_row_overlap_and_one_projection(self):
        def view(names, top, bottom):
            return {'ok':True,'queue_mode':True,'projection_sha256':'a'*64,
                    'image_sha256':('1' if top else '2')*64,'rows_measured':True,
                    'queue_rows':[{'name':name,'mode':1,'is_checked':True} for name in names],
                    'scroll_state':{'top':top,'bottom':bottom}}
        views=[view(['A','B','C'],True,False),view(['B','C','D'],False,True)]
        expected=[{'name':name,'is_raid':False,'is_checked':True} for name in 'ABCD']
        self.assertTrue(stitch_queue_views(views,4,expected_rows=expected)['visible_complete'])
        conflicting=deepcopy(views)
        conflicting[1]['projection_sha256']='b'*64
        self.assertFalse(stitch_queue_views(conflicting,4,expected_rows=expected)['visible_complete'])
        duplicates=[view(['A','A','A'],True,False),view(['A','A','A'],False,True)]
        self.assertFalse(stitch_queue_views(duplicates,4)['visible_complete'])


if __name__=='__main__':
    unittest.main()

"""Replay native parse-readiness pixels without GUI input or a model boolean."""
import copy
import hashlib
from pathlib import Path
import unittest

from shu import maa_queue_receipt as receipt


class QueueParseReadyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_maa_queue_receipt import QueueReceiptTests
        self.files = QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)
        self.control = {'x':180,'y':160,'width':28,'height':28}
        self.field = {'x':20,'y':20,'width':180,'height':30}
        self.submitted = self.files.now
        self.probes = [self.probe(166),self.probe(33),self.probe(55)]

    def probe(self, color, *, code='prts://123', shape='+', backdrop=255, caret=False):
        import cv2
        import numpy as np
        from shu.maa_queue_parse_ready import capture_parse_probe
        self.files.now += .25
        image = np.full((220,300,3),backdrop,dtype=np.uint8)
        cv2.putText(image,code,(25,41),cv2.FONT_HERSHEY_SIMPLEX,.5,(33,33,33),1,cv2.LINE_8)
        if caret:
            cv2.line(image,(110,24),(110,44),(33,33,33),1,cv2.LINE_8)
        x,y = self.control['x']+14,self.control['y']+14
        if shape=='+':
            cv2.line(image,(x-7,y),(x+7,y),(color,)*3,2,cv2.LINE_8)
            cv2.line(image,(x,y-7),(x,y+7),(color,)*3,2,cv2.LINE_8)
        else:
            cv2.circle(image,(x,y),7,(color,)*3,2,cv2.LINE_8)
        ok,png = cv2.imencode('.png',image)
        self.assertTrue(ok)
        ref = self.files.fresh_frame(png.tobytes())
        snapshot = self.files.snapshot([],frame=ref)
        frame = {'screenshot_id':hashlib.sha256(ref['path'].encode()).hexdigest()[:32],
            'image_path':ref['path'],'image_sha256':ref['sha256'],'method':'win32_printwindow',
            'window':{**self.files.process,'client_rect':{'x':0,'y':0,'width':300,'height':220},
                      'dpi':96,'foreground':True,'minimized':False}}
        return capture_parse_probe(frame,self.control,snapshot,code_text=code,code_input_rect=self.field)

    def expected(self):
        return {'binding':self.files.binding,'mystery_code':'prts://123','submitted_at':self.submitted,
                'previous_snapshot':self.probes[1]['snapshot'],'current_snapshot':self.probes[2]['snapshot']}

    def verify(self,probes=None,expected=None):
        from shu.maa_queue_parse_ready import verify_parse_ready
        return verify_parse_ready(self.probes if probes is None else probes,
                                  self.expected() if expected is None else expected)

    def test_busy_then_two_ready_native_frames_verify_and_remain_read_only(self):
        result = self.verify()
        self.assertTrue(result['verified'],result)
        self.assertEqual(result['code'],'queue_parse_completion_verified')
        self.assertEqual(result['gui_inputs'],0)
        self.assertEqual([probe['measurement']['state'] for probe in self.probes],['busy','ready','ready'])

    def test_verification_replays_embedded_png_after_original_files_removed(self):
        for probe in self.probes:
            Path(probe['image_ref']['path']).unlink()
        self.assertTrue(self.verify()['verified'])

    def test_ready_without_native_busy_never_proves_parse_completion(self):
        probes = [self.probes[1],self.probes[1],self.probes[2]]
        self.assertFalse(self.verify(probes)['verified'])

    def test_model_enabled_boolean_cannot_replace_disabled_pixels(self):
        probes = copy.deepcopy(self.probes)
        probes[1] = self.probe(166)
        probes[1].update(queue_add_enabled=True,parse_complete=True)
        probes[1]['measurement']['state'] = 'ready'
        expected = self.expected()
        expected['previous_snapshot'] = probes[1]['snapshot']
        result = self.verify(probes,expected)
        self.assertFalse(result['verified'],result)

    def test_unknown_gray_or_nonwhite_backdrop_remains_pending(self):
        for changes in ({'color':90},{'color':33,'backdrop':235}):
            with self.subTest(changes=changes):
                probes = [self.probes[0],self.probe(**changes),self.probe(33)]
                expected = {**self.expected(),'previous_snapshot':probes[1]['snapshot'],
                            'current_snapshot':probes[2]['snapshot']}
                result = self.verify(probes,expected)
                self.assertFalse(result['verified'],result)

    def test_a_round_icon_cannot_be_used_as_plus_readiness(self):
        probes = [self.probes[0],self.probe(33,shape='round'),self.probe(33)]
        result = self.verify(probes,{**self.expected(),'previous_snapshot':probes[1]['snapshot'],
                                    'current_snapshot':probes[2]['snapshot']})
        self.assertFalse(result['verified'],result)

    def test_pressed_enabled_opacity_is_supported_from_audited_style(self):
        self.probes[2] = self.probe(122)
        self.assertTrue(self.verify()['verified'])

    def test_blinking_caret_is_conservatively_pending_instead_of_guessing_same_code(self):
        probes = [self.probes[0],self.probes[1],self.probe(33,caret=True)]
        result = self.verify(probes,{**self.expected(),'current_snapshot':probes[2]['snapshot']})
        self.assertFalse(result['verified'],result)
        self.assertEqual(result['code'],'queue_parse_request_changed')

    def test_missing_native_foreground_or_oversize_control_cannot_prove_ready(self):
        for mutate in (
            lambda p:p['native_frame']['window'].__setitem__('foreground',False),
            lambda p:p['glyph_rect'].__setitem__('width',100),
            lambda p:p.__setitem__('png_b64','A'*(12*1024*1024)),
        ):
            probes = copy.deepcopy(self.probes)
            mutate(probes[1])
            self.assertFalse(self.verify(probes)['verified'])

    def test_forged_hash_geometry_process_version_or_code_is_rejected(self):
        for key,value in (('image_sha256','0'*64),('code_text','prts://456')):
            with self.subTest(key=key):
                probes = copy.deepcopy(self.probes)
                probes[1][key] = value
                self.assertFalse(self.verify(probes)['verified'])
        for mutate in (
            lambda p:p['native_frame']['window'].__setitem__('pid',999),
            lambda p:p['native_frame']['window']['client_rect'].__setitem__('width',299),
            lambda p:p['snapshot']['binding'].__setitem__('maa_version','unreviewed-version'),
        ):
            probes = copy.deepcopy(self.probes)
            mutate(probes[1])
            self.assertFalse(self.verify(probes)['verified'])

    def test_changed_code_field_pixels_or_control_position_are_not_same_parse(self):
        probes = [self.probes[0],self.probe(33,code='prts://456'),self.probe(33)]
        probes[1]['code_text'] = 'prts://123'
        result = self.verify(probes,{**self.expected(),'previous_snapshot':probes[1]['snapshot'],
                                    'current_snapshot':probes[2]['snapshot']})
        self.assertFalse(result['verified'],result)
        probes = copy.deepcopy(self.probes)
        probes[2]['glyph_rect']['x'] += 1
        self.assertFalse(self.verify(probes)['verified'])

    def test_stale_reused_or_pre_submit_frame_cannot_complete_parse(self):
        expected = self.expected()
        expected['submitted_at'] = self.files.now+10
        self.assertFalse(self.verify(expected=expected)['verified'])
        self.assertFalse(self.verify([self.probes[0],self.probes[1],self.probes[1]])['verified'])

    def test_previous_and_current_absence_snapshots_must_be_exact_same_observations(self):
        expected = self.expected()
        expected['current_snapshot'] = self.probes[1]['snapshot']
        self.assertFalse(self.verify(expected=expected)['verified'])


if __name__=='__main__':
    unittest.main()

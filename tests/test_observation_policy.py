"""Local screenshot gating uses synthetic pixels, never a desktop or model."""
from pathlib import Path
import tempfile
import unittest

from PySide6.QtGui import QImage, QColor
from shu.observation_policy import ObservationPolicy
from shu.windows import WindowError


class ObservationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=0.;self.frames=[];self.checks=0
        self.policy=ObservationPolicy(clock=lambda:self.now,poll_seconds=.1,
            settle_seconds=.3,unchanged_seconds=.4,loading_seconds=.5)
    def frame(self,name,color):
        image=QImage(128,72,QImage.Format.Format_RGB32);image.fill(QColor(color))
        path=self.root/name;self.assertTrue(image.save(str(path)))
        return {'id':name,'path':str(path),'window_id':1,'window_pid':2,'width':128,'height':72,
            'client_rect':{'left':0,'top':0,'width':128,'height':72}}
    def check(self):self.checks+=1
    def wait(self,seconds):self.now+=seconds
    def capture(self):return self.frames.pop(0) if len(self.frames)>1 else self.frames[0]
    def settle(self,initial,**kwargs):
        return self.policy.settle(initial,self.capture,self.check,self.wait,**kwargs)
    def test_identical_pixels_have_no_semantic_success_and_original_coordinates_survive(self):
        a=self.frame('a.png','white');b=self.frame('b.png','white');self.frames=[b]
        latest,meta=self.settle(a)
        self.assertEqual(latest,b)
        self.assertTrue(meta['stable_pixels']);self.assertFalse(meta['semantic_success'])
        self.assertEqual(meta['local_polls'],1)
        self.assertEqual(latest['width'],128)
    def test_loading_label_waits_for_change_and_stability_without_calling_model(self):
        loading=self.frame('loading.png','black');ready=self.frame('ready.png','white')
        stable=self.frame('stable.png','white');self.frames=[loading,ready,stable]
        self.policy.mark_loading(loading)
        latest,meta=self.settle(loading,mode='loading')
        self.assertEqual(latest['id'],'stable.png');self.assertEqual(meta['local_polls'],3)
        self.assertTrue(meta['changed']);self.assertTrue(meta['stable_pixels'])
        self.assertFalse(meta['semantic_success'])
    def test_unchanged_wait_is_bounded_and_cannot_report_semantic_completion(self):
        a=self.frame('a.png','white');self.frames=[a]
        _,meta=self.settle(a,mode='wait')
        self.assertAlmostEqual(self.now,.4);self.assertFalse(meta['changed'])
        self.assertFalse(meta['semantic_success'])
    def test_repeated_wait_skips_old_pixels_until_new_pixels_stabilize(self):
        a=self.frame('a.png','white');b=self.frame('b.png','black');c=self.frame('c.png','black')
        self.frames=[a,b,c]
        latest,meta=self.settle(a,mode='wait')
        self.assertEqual(latest['id'],'c.png');self.assertEqual(meta['local_polls'],3)
    def test_small_animated_detail_does_not_delay_stable_interface(self):
        a=self.frame('a.png','white');b=self.frame('b.png','white')
        image=QImage(b['path'])
        for x in range(4):
            for y in range(4):image.setPixelColor(x,y,QColor('black'))
        image.save(b['path']);self.frames=[b]
        _,meta=self.settle(a)
        self.assertTrue(meta['stable_pixels']);self.assertEqual(meta['local_polls'],1)
    def test_geometry_or_pid_change_aborts_local_wait(self):
        a=self.frame('a.png','white');b=self.frame('b.png','white');b['window_pid']=3;self.frames=[b]
        with self.assertRaises(WindowError):self.settle(a)
    def test_invalid_pixels_do_not_create_a_cache_or_wait_indefinitely(self):
        a={'id':'missing','path':str(self.root/'missing.png')}
        latest,meta=self.settle(a)
        self.assertIs(latest,a);self.assertEqual(meta['local_polls'],0);self.assertEqual(self.now,0)
    def test_cancellation_guard_runs_before_each_local_capture(self):
        a=self.frame('a.png','white');self.frames=[a]
        def stop():raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.policy.settle(a,self.capture,stop,self.wait)
        self.assertEqual(self.now,0)
    def test_sample_only_loading_polls_export_exactly_one_final_evidence_frame(self):
        initial=self.frame('loading.png','black');final=self.frame('ready.png','white')
        samples=[]
        for sequence,color in [(1,0),(2,255),(3,255)]:
            samples.append({**initial,'sample_rgb':bytes([color])*6912,'sequence':sequence,
                            'generation':'stream','actionable':False})
        exports=[];requests=[]
        def capture():exports.append(1);return final
        def peek(**kwargs):requests.append(kwargs);return samples.pop(0)
        result,meta=self.policy.settle(initial,capture,self.check,self.wait,mode='loading',peek=peek)
        self.assertEqual(len(exports),1);self.assertEqual(meta['sample_only_polls'],3)
        self.assertEqual(result,final);self.assertNotIn('sample_rgb',result)
        self.assertFalse(meta['semantic_success'])
        self.assertEqual([request['after_sequence'] for request in requests],[0,1,2])
        self.assertTrue(all(request['not_before']==0. for request in requests))
    def test_missing_live_stream_falls_back_without_double_final_export(self):
        initial=self.frame('a.png','white');final=self.frame('b.png','white');exports=[]
        result,meta=self.policy.settle(initial,lambda:exports.append(1) or final,
            self.check,self.wait,peek=lambda **kwargs:None)
        self.assertEqual(exports,[1]);self.assertEqual(result,final)
        self.assertEqual(meta['sample_only_polls'],0)
    def test_sample_timeout_still_requires_new_evidence_instead_of_reusing_old_image(self):
        initial=self.frame('old.png','white');attempts=[];exports=[]
        def peek(**kwargs):
            attempts.append(1);raise WindowError('No new frame','capture_stream_timeout')
        def capture():
            exports.append(1);raise WindowError('Still no new frame','capture_stream_timeout')
        with self.assertRaises(WindowError):
            self.policy.settle(initial,capture,self.check,self.wait,peek=peek)
        self.assertGreater(len(attempts),0);self.assertEqual(exports,[1])
    def test_sample_must_be_non_actionable_and_same_generation(self):
        initial=self.frame('a.png','white');initial['stream_generation']='original'
        for change in [{'actionable':True},{'generation':'new'}]:
            sample={**initial,'sample_rgb':bytes([255])*6912,'sequence':1,
                    'generation':'original','actionable':False,**change}
            with self.subTest(change=change),self.assertRaises(WindowError):
                self.policy.settle(initial,self.capture,self.check,self.wait,peek=lambda **kwargs:sample)


if __name__=='__main__':unittest.main()

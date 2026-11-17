import importlib.util
from pathlib import Path
import unittest
import sys

class WorkerTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'assets/tts_worker.py'
        self.assertTrue(path.exists(),'GPU worker feature missing')
        spec=importlib.util.spec_from_file_location('worker',path); self.w=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.w)
    def test_request_rejects_empty_long_invalid_voice_and_speed(self):
        for request in ({'text':''},{'text':'黍'*221},{'text':'你好','voice':'../../x'},{'text':'你好','speed':float('nan')}):
            with self.subTest(request=request),self.assertRaises(ValueError): self.w.validate_request(request)
        self.assertEqual(self.w.validate_request({'text':'你好'})['voice'],'zf_001')
    def test_no_cpu_success_fallback(self):
        with self.assertRaisesRegex(RuntimeError,'CUDA'): self.w.require_cuda(['CPUExecutionProvider'])
        self.w.require_cuda(['CUDAExecutionProvider','CPUExecutionProvider'])
    def test_style_uses_last_phoneme_position_and_clamps(self):
        self.assertTrue(hasattr(self.w,'style_for'),'voice style indexing helper missing')
        self.assertEqual(self.w.style_for(['first','second','third'],1),'first')
        self.assertEqual(self.w.style_for(['first','second','third'],2),'second')
        self.assertEqual(self.w.style_for(['first','second','third'],500),'third')

    def test_cuda_profile_counts_actual_node_events(self):
        events=[{'cat':'Node','args':{'provider':'CUDAExecutionProvider'}},{'cat':'Node','args':{'provider':'CPUExecutionProvider'}},{'cat':'Session','args':{'provider':'CUDAExecutionProvider'}}]
        self.assertEqual(self.w.cuda_node_count(events),1)

    def test_multiple_voice_ids_and_sentence_segments(self):
        self.assertEqual(self.w.validate_request({'text':'你好','voice':'zf_017'})['voice'],'zf_017')
        self.assertTrue(hasattr(self.w,'split_sentences'),'Sentence timing segmentation missing')
        text='你好，我是黍。今天也一起加油吧！'+'慢'*110
        segments=self.w.split_sentences(text)
        self.assertEqual(''.join(segments),text)
        self.assertTrue(all(0<len(part)<=50 for part in segments))
        self.assertEqual(segments[0],'你好，我是黍。')

    @unittest.skipUnless(sys.platform=='win32','Windows priority handle')
    def test_worker_can_set_own_low_priority_on_windows_64bit(self):
        self.assertTrue(hasattr(self.w,'set_low_priority'),'64-bit-safe priority helper missing')
        self.assertTrue(self.w.set_low_priority())

if __name__=='__main__': unittest.main()

"""Pixel decisions and a fixed-image SDK controller require no native SDK."""
import unittest

from tools.maafw_recognition_probe import checked_roi, classify_results, fixed_controller


class RecognitionProbeTests(unittest.TestCase):
    def test_unique_box_uses_image_pixels_and_validates_roi(self):
        raw={'filtered':[{'box':[12,14,8,6],'score':.96,'text':'test'}]}
        result=classify_results(raw,(10,10,50,30),(100,200,3),confidence=.9)
        self.assertEqual(result['status'],'recognized')
        self.assertEqual(result['box'],[12,14,8,6])
        self.assertEqual(result['normalized_box'],[.06,.14,.04,.06])

    def test_missing_and_duplicate_matches_remain_unknown(self):
        hit={'box':[12,14,8,6],'score':.96}
        for matches in ([],[hit,hit]):
            self.assertEqual(classify_results({'filtered':matches},(10,10,50,30),
                                             (100,200,3),confidence=.9)['status'],'unknown')

    def test_bad_score_and_boxes_remain_unknown(self):
        for candidate in ({'box':[12,14,8,6],'score':.5},
                          {'box':[1,14,8,6],'score':.96},
                          {'box':[12,14,-8,6],'score':.96},
                          {'box':[12,14,800,6],'score':.96}):
            self.assertEqual(classify_results({'filtered':[candidate]},(10,10,50,30),
                                             (100,200,3),confidence=.9)['status'],'unknown')
        for roi in ([0,0,201,10],[-1,0,10,10],[0,0,False,10]):
            with self.assertRaises(ValueError):checked_roi(roi,(100,200,3))

    def test_controller_rejects_every_input_and_does_not_touch_os(self):
        class Image:
            def copy(self):return 'copied-pixels'
        controller=fixed_controller(object,Image())
        self.assertEqual(controller.screencap(),'copied-pixels')
        for method in ('click','swipe','start_app','stop_app','touch_down','touch_up',
                       'touch_move','click_key','input_text','key_down','key_up','scroll',
                       'relative_move','shell'):
            self.assertIs(getattr(controller,method)(),False)
        self.assertEqual(controller.input_attempts,14)


if __name__=='__main__':unittest.main()

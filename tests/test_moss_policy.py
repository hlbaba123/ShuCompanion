import importlib.util
from pathlib import Path
import struct
import unittest


class ClonePolicyTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'assets/moss_policy.py'
        self.assertTrue(path.is_file(),'Clone speech policy is missing')
        spec=importlib.util.spec_from_file_location('moss_policy',path)
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def test_known_language_conditions_cross_language_clone(self):
        self.assertEqual(self.m.language_for('博士，今天也辛苦了。'),'zh')
        self.assertEqual(self.m.language_for('ドクター、今日もお疲れ様です。'),'ja')
        self.assertEqual(self.m.language_for('Good morning, Doctor.'),'en')
        prompt=self.m.condition_prompt('- Instruction: None - Language: None - Text:','zh')
        self.assertIn('Language: zh',prompt)
        self.assertIn('clear pronunciation',prompt)
        self.assertIn('gently',prompt)

    def test_restores_official_prompt_start_exactly_once(self):
        self.assertEqual(self.m.canonical_prefix([600,289],4),[4,600,289])
        self.assertEqual(self.m.canonical_prefix([4,600,289],4),[4,600,289])

    def test_short_text_uses_complete_short_reference_to_avoid_repetition(self):
        self.assertEqual(self.m.reference_kind('今天也辛苦了。'),'short')
        self.assertEqual(self.m.reference_kind('博士，慢慢来。我会陪着你，把眼前的事情一件件做好。'),'standard')

    def test_japanese_keeps_user_selected_original_prompt_and_reference(self):
        original='- Instruction: None - Language: None - Text:'
        self.assertEqual(self.m.condition_prompt(original,'ja'),original)
        self.assertEqual(self.m.reference_kind('ドクター、今日もお疲れ様です。'),'original-ja')
        self.assertEqual(self.m.reference_kind('ありがとう。'),'original-ja')

    def test_japanese_paragraph_retains_original_prosody_without_changing_chinese(self):
        text='ドクター、焦らなくても大丈夫ですよ。私がそばにいますから。'
        self.assertEqual(self.m.segment_text(text),[{'text':text,'language':'ja'}])

    def test_original_japanese_pcm_has_no_new_fades_or_gain_changes(self):
        data=struct.pack('<hhhh',1200,-1200,3000,-3000)
        self.assertEqual(self.m.finish_pcm(data,48000,2,'ja',first=True,last=True),data)

    def test_preserves_punctuation_and_avoids_isolated_short_greeting(self):
        text='博士，慢慢来。我会陪着你，把眼前的事情一件件做好。'
        segments=self.m.segment_text(text)
        self.assertEqual(''.join(s['text'] for s in segments),text)
        self.assertGreater(len(segments[0]['text']),len('博士，慢慢来。'))
        long='请记得喝水，慢慢把事情做好。'*15
        parts=self.m.segment_text(long)
        self.assertEqual(''.join(s['text'] for s in parts),long)
        self.assertTrue(all(len(s['text'])<=55 for s in parts))

    def test_mixed_language_keeps_sentences_separate(self):
        segments=self.m.segment_text('今天也辛苦了。ドクター、おやすみなさい。')
        self.assertEqual([s['language'] for s in segments],['zh','ja'])

    def test_fade_only_edges_no_dropped_samples_or_blunted_middle(self):
        source=struct.pack('<'+'h'*2400,*([12000]*2400))
        result=self.m.fade_pcm(source,48000,2,first=True,last=True)
        self.assertEqual(len(result),len(source))
        values=struct.unpack('<'+'h'*2400,result)
        self.assertEqual(values[0],0);self.assertEqual(values[-1],0)
        self.assertEqual(values[1000:1400],(12000,)*400)
        self.assertEqual(self.m.fade_pcm(source,48000,2),source)

if __name__=='__main__':unittest.main()

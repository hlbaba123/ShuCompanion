import importlib.util
from pathlib import Path
import unittest
import types
from unittest.mock import patch


class CloneWorkerTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'assets/moss_worker.py'
        self.assertTrue(path.is_file(),'Persistent clone worker missing')
        spec=importlib.util.spec_from_file_location('moss_worker',path)
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def test_protocol_rejects_bad_ids_operations_and_unbounded_text(self):
        for item in ({'op':'shell','id':1,'text':'你好'}, {'op':'speak','id':True,'text':'你好'},
                     {'op':'speak','id':1,'text':''}, {'op':'speak','id':1,'text':'声'*221},
                     {'op':'speak','id':1,'text':'你好\x00'}):
            with self.subTest(item=item),self.assertRaises(ValueError):self.m.validate_request(item)
        self.assertEqual(self.m.validate_request({'op':'speak','id':3,'text':'你好。'})['id'],3)

    def test_explicit_language_and_speed_are_bounded(self):
        valid=self.m.validate_request({'op':'speak','id':1,'text':'你好。','language':'ja','speed':1.08})
        self.assertEqual(valid['language'],'ja');self.assertEqual(valid['speed'],1.08)
        for extra in ({'language':'auto'},{'speed':True},{'speed':float('nan')},{'speed':1.5}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                self.m.validate_request(dict(op='speak',id=1,text='你好',**extra))

    def test_runtime_paths_cannot_escape_or_load_missing_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValueError):self.m.runtime_file(root,'../outside')
            with self.assertRaises(ValueError):self.m.runtime_file(root,'missing')

    def test_japanese_profile_is_applied_and_restored_on_generation_failure(self):
        path=Path(__file__).resolve().parents[1]/'assets/moss_policy.py'
        spec=importlib.util.spec_from_file_location('moss_policy',path)
        policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
        class Engine:
            def __init__(self):
                self.manifest={'generation_defaults':{},'prompt_templates':{'user_prompt_prefix_token_ids':[4,600],
                    'user_prompt_after_reference_token_ids':[10,20]}}
                self.original_ja_prefix=[600]
                self.original_ja_reference_codes=['original-reference']
                self.original_ja_rng_state={'original':True}
                self.short_reference_codes=['short-reference']
                self.codec_streaming_session=types.SimpleNamespace(reset=lambda:None,run_frames=lambda frames:None)
            def build_voice_clone_request_rows(self,reference,tokens):
                self.observed=(reference,self.manifest['prompt_templates']['user_prompt_prefix_token_ids'],
                    self.manifest['prompt_templates']['user_prompt_after_reference_token_ids'],self.rng.bit_generator.state)
                self.text_tokens=tokens
                return {}
            def generate_audio_frames(self,*args,**kwargs):raise RuntimeError('test interruption')
        tokenizer=types.SimpleNamespace(encode=lambda text,**kwargs:list(text),decode=lambda value:'- Instruction: None - Language: None - Text:')
        fake_numpy=types.SimpleNamespace(random=types.SimpleNamespace(default_rng=lambda seed:types.SimpleNamespace(bit_generator=types.SimpleNamespace(state={}))))
        for text,japanese in [('ありがとう。',True),('博士，慢慢来。我会陪着你把事情做好。',False)]:
            engine=Engine()
            with self.subTest(text=text),patch.dict('sys.modules',{'moss_policy':policy,'voice_tempo':types.SimpleNamespace(TempoStream=lambda **kwargs:object())}),self.assertRaisesRegex(RuntimeError,'test interruption'):
                self.m.speak(engine,tokenizer,['standard-reference'],fake_numpy,{'id':1,'text':text,'language':'ja' if japanese else 'zh'})
            self.assertEqual(''.join(engine.text_tokens),text,'Punctuation must not split inference or reset the speaker')
            self.assertEqual(engine.manifest['prompt_templates']['user_prompt_prefix_token_ids'],[4,600])
            self.assertEqual(engine.manifest['prompt_templates']['user_prompt_after_reference_token_ids'],[10,20])
            if japanese:self.assertEqual(engine.observed,(['original-reference'],[600],[10,20],{'original':True}))
            else:
                self.assertEqual(engine.observed[0],['standard-reference'])
                self.assertEqual(engine.observed[1],[4,600])
                self.assertEqual(engine.observed[2],[10,20],'Prosody comes from one native recording, not per-sentence style prompts')

    def test_japanese_random_state_does_not_contaminate_chinese_after_it(self):
        fake_numpy=types.SimpleNamespace(random=types.SimpleNamespace(default_rng=lambda seed:types.SimpleNamespace(bit_generator=types.SimpleNamespace(state={}))))
        chinese=fake_numpy.random.default_rng(1234);chinese.bit_generator.state={'value':4}
        original={'value':99}
        japanese=self.m.segment_rng('ja',fake_numpy,chinese,original)
        japanese.bit_generator.state['value']=100
        resumed=self.m.segment_rng('zh',fake_numpy,chinese,original)
        self.assertIs(resumed,chinese)
        self.assertEqual(resumed.bit_generator.state,{'value':4})
        self.assertEqual(original,{'value':99})

if __name__=='__main__':unittest.main()

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


class CacheTests(unittest.TestCase):
    def test_large_cache_stays_on_gpu_only_hidden_returns_to_cpu(self):
        path=Path(__file__).resolve().parents[1]/'assets/voice_gpu_cache.py'
        self.assertTrue(path.is_file(),'GPU cache binding missing')
        spec=importlib.util.spec_from_file_location('voice_gpu_cache',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        class Value:
            def __init__(self,name):self.name=name;self.reads=0
            def numpy(self):self.reads+=1;return self.name
        class Binding:
            def __init__(self):self.inputs=[];self.outputs=[];self.values=[Value('hidden'),Value('key'),Value('value')]
            def bind_cpu_input(self,name,value):self.inputs.append((name,'cpu',value))
            def bind_ortvalue_input(self,name,value):self.inputs.append((name,'ort',value))
            def bind_output(self,name,device='cpu'):self.outputs.append((name,device))
            def get_outputs(self):return self.values
        class Session:
            def __init__(self):self.bindings=[]
            def io_binding(self):b=Binding();self.bindings.append(b);return b
            def get_outputs(self):return [SimpleNamespace(name=name) for name in ('global_hidden','present_key_0','present_value_0')]
            def run_with_iobinding(self,binding):pass
        session=Session();proxy=module.DeviceCacheSession(session,Value)
        first=proxy.run(None,{'input_ids':[1],'past_key_0':[0]})
        self.assertEqual(first[0],'hidden')
        self.assertEqual(session.bindings[0].outputs,[('global_hidden','cpu'),('present_key_0','cuda'),('present_value_0','cuda')])
        proxy.run(None,{'input_ids':[2],'past_key_0':first[1]})
        self.assertEqual(session.bindings[1].inputs[1][1],'ort')
        self.assertEqual(first[1].reads,0)

if __name__=='__main__':unittest.main()

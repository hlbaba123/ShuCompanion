"""One-shot CUDA-only Kokoro synthesis; no Qt or torch."""
import argparse
import contextlib
import json
import math
import os
import re
from pathlib import Path
import sys
import time
import wave


def validate_request(r):
    text=r.get('text'); voice=r.get('voice','zf_001'); speed=r.get('speed',.95)
    if not isinstance(text,str) or not text.strip() or len(text)>220 or any(ord(c)<32 and c not in '\n\t' for c in text): raise ValueError('invalid_text')
    allowed={v['id'] for v in json.loads(Path(__file__).with_name('tts_voices.json').read_text(encoding='utf-8'))}
    if voice not in allowed or isinstance(speed,bool) or not isinstance(speed,(int,float)) or not math.isfinite(speed) or not .5<=speed<=2: raise ValueError('invalid_voice_or_speed')
    return dict(text=text,voice=voice,speed=float(speed))


def split_sentences(text):
    result=[]
    for sentence in re.findall(r'.+?(?:[。！？!?；;]+|$)',text,flags=re.S):
        result.extend(sentence[i:i+50] for i in range(0,len(sentence),50))
    return result


def require_cuda(providers):
    if 'CUDAExecutionProvider' not in providers: raise RuntimeError('CUDA_unavailable')


def cuda_node_count(events):
    return sum(e.get('cat')=='Node' and e.get('args',{}).get('provider')=='CUDAExecutionProvider' for e in events)


def runtime_file(root,value):
    path=(root/value).resolve()
    if not path.is_relative_to(root) or not path.is_file(): raise ValueError('invalid_runtime_file')
    return path


def style_for(voice,length):
    return voice[min(length,len(voice))-1]


def set_low_priority():
    if sys.platform!='win32': return True
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.argtypes=[]
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.SetPriorityClass.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.SetPriorityClass.restype=wintypes.BOOL
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x40):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def synthesize(root,output,request,profile=False):
    start=time.perf_counter(); output.parent.mkdir(parents=True,exist_ok=True)
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'): os.environ[key]='2'
    set_low_priority()
    import numpy as np
    import onnxruntime as ort
    from misaki.zh import ZHG2P
    m=json.loads((root/'install.json').read_text(encoding='utf-8-sig'))
    if m.get('ready') is not True or m.get('provider')!='CUDAExecutionProvider': raise RuntimeError('CUDA_runtime_not_ready')
    ort.preload_dlls(directory=''); require_cuda(ort.get_available_providers())
    options=ort.SessionOptions(); options.intra_op_num_threads=2; options.inter_op_num_threads=1; options.enable_profiling=profile
    if profile: options.profile_file_prefix=str(output.with_suffix('.ort'))
    session=ort.InferenceSession(str(runtime_file(root,m['model'])),sess_options=options,providers=[('CUDAExecutionProvider',{'gpu_mem_limit':805306368,'arena_extend_strategy':'kSameAsRequested','cudnn_conv_use_max_workspace':'0'})])
    session.disable_fallback(); require_cuda(session.get_providers())
    config=json.loads(runtime_file(root,m['config']).read_text(encoding='utf-8')); voices=np.load(str(runtime_file(root,m['voices'])),allow_pickle=False)
    g2p=ZHG2P(version='1.1'); load_seconds=time.perf_counter()-start; synthesis_start=time.perf_counter(); parts=[]; cues=[]; sample_count=0
    for sentence in split_sentences(request['text']):
        phonemes=g2p(sentence)[0]; vocab=config['vocab']; tokens=[vocab[c] for c in phonemes if c in vocab]
        sentence_start=sample_count
        for index in range(0,len(tokens),500):
            chunk=tokens[index:index+500]; style=style_for(voices[request['voice']],len(chunk)); feeds={}
            for entry in session.get_inputs():
                dtype=np.float16 if 'float16' in entry.type else np.float32
                if entry.name in ('input_ids','tokens'): feeds[entry.name]=np.asarray([[0,*chunk,0]],dtype=np.int64)
                elif entry.name=='style': feeds[entry.name]=np.asarray(style,dtype=dtype).reshape(1,-1)
                elif entry.name=='speed': feeds[entry.name]=np.asarray([request['speed']],dtype=dtype)
                else: raise ValueError('unsupported_model_input')
            part=np.asarray(session.run(None,feeds)[0],dtype=np.float32).reshape(-1)
            parts.append(part);sample_count+=len(part)
        if sample_count>sentence_start:
            cues.append(dict(text=sentence,start=sentence_start/24000,end=sample_count/24000))
    if not parts: raise ValueError('no_readable_phonemes')
    samples=np.concatenate(parts)
    if not np.isfinite(samples).all() or not samples.size or np.max(np.abs(samples))<1e-5: raise RuntimeError('empty_audio')
    output.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(output),'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
    result=dict(ok=True,path=str(output),provider='CUDAExecutionProvider',voice=request['voice'],cues=cues,duration_seconds=len(samples)/24000,load_seconds=load_seconds,synthesis_seconds=time.perf_counter()-synthesis_start)
    if profile:
        profile_path=Path(session.end_profiling()); count=cuda_node_count(json.loads(profile_path.read_text(encoding='utf-8'))); result.update(cuda_node_count=count,profile=str(profile_path))
        if not count: output.unlink(missing_ok=True); raise RuntimeError('CUDA_no_profile_nodes')
    return result


def main():
    p=argparse.ArgumentParser(); p.add_argument('--runtime',required=True,type=Path); p.add_argument('--output',required=True,type=Path); p.add_argument('--profile',action='store_true'); args=p.parse_args()
    try:
        raw=sys.stdin.buffer.read(16385)
        if len(raw)>16384: raise ValueError('request_too_large')
        request=validate_request(json.loads(raw.decode('utf-8')))
        with contextlib.redirect_stdout(sys.stderr): result=synthesize(args.runtime.resolve(),args.output.resolve(),request,args.profile)
        print(json.dumps(result,ensure_ascii=True),flush=True); return 0
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr); print(json.dumps(dict(ok=False,error='GPU_SYNTHESIS_FAILED')),flush=True); return 1

if __name__=='__main__': raise SystemExit(main())

"""Persistent bounded JSONL -> streamed PCM. No Qt, Torch or network."""
import argparse
import base64
import copy
import json
import math
import os
from pathlib import Path
import sys
import time


def validate_request(request):
    if not isinstance(request,dict) or request.get('op')!='speak':raise ValueError('Invalid operation')
    ident=request.get('id');text=request.get('text')
    if type(ident) is not int or ident<0:raise ValueError('Invalid request ID')
    if not isinstance(text,str) or not text.strip() or len(text)>220 or any(ord(c)<32 and c not in '\n\t' for c in text):raise ValueError('Invalid text')
    language=request.get('language','zh');speed=request.get('speed',1.08)
    if language not in ('zh','ja'):raise ValueError('Invalid language')
    if type(speed) not in (int,float) or not math.isfinite(speed) or not 1.0<=speed<=1.2:raise ValueError('Invalid speed')
    return {'id':ident,'text':text.strip(),'language':language,'speed':float(speed)}


def runtime_file(root,value):
    root=root.resolve();path=(root/value).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise ValueError('Invalid runtime file')
    return path


def emit(event):
    sys.stdout.write(json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n');sys.stdout.flush()


def segment_rng(language,np,non_japanese_rng,original_ja_state):
    if language!='ja':return non_japanese_rng
    generator=np.random.default_rng(1234)
    generator.bit_generator.state=copy.deepcopy(original_ja_state)
    return generator


def speak(engine,tokenizer,codes,np,request):
    # A reply is one inference request. PCM chunks never restart the voice,
    # reference, random stream, text context or decoder state at punctuation.
    started=time.perf_counter();ident=request['id'];sample_offset=0
    text=request['text'];language=request.get('language','zh');speed=request.get('speed',1.08)
    templates=engine.manifest['prompt_templates']
    original=list(templates['user_prompt_after_reference_token_ids'])
    original_prefix=list(templates['user_prompt_prefix_token_ids'])
    japanese=language=='ja'
    templates['user_prompt_prefix_token_ids']=engine.original_ja_prefix if japanese else original_prefix
    templates['user_prompt_after_reference_token_ids']=original
    engine.rng=segment_rng(language,np,np.random.default_rng(1234),engine.original_ja_rng_state)
    profiles=getattr(engine,'voice_reference_codes',{'zh':codes,'ja':engine.original_ja_reference_codes})
    reference=profiles[language]
    rows=engine.build_voice_clone_request_rows(reference,tokenizer.encode(text,out_type=int))
    engine.manifest['generation_defaults']['max_new_frames']=1024
    try:
        from voice_tempo import TempoStream
        from moss_policy import fade_pcm
        tempo=TempoStream(rate=48000,channels=2,speed=speed)
        estimated_frames=round(max(1.,len(text)*.24)/speed*48000)
        pending=[];first=True;tail=b''
        engine.codec_streaming_session.reset()
        # Causal codec has real speaker context before its first audible frame.
        # Priming is discarded, not played or added to caption/sample offsets.
        engine.codec_streaming_session.run_frames(reference[-12:])
        def output(pcm):
            nonlocal sample_offset,first,tail
            if not pcm:return
            pcm=tail+pcm
            if len(pcm)<=1536:tail=pcm;return
            tail=pcm[-1536:];pcm=fade_pcm(pcm[:-1536],48000,2,first=first);first=False
            emit({'event':'pcm','id':ident,'rate':48000,'channels':2,'data':base64.b64encode(pcm).decode('ascii'),
                  'text':text,'start':0,'progressive':True,'estimated_frames':estimated_frames})
            sample_offset+=len(pcm)//4
        def decode():
            if not pending:return
            audio,length=engine.codec_streaming_session.run_frames(pending[:]);pending.clear()
            if length<=0:return
            data=audio[0,:,:length].T
            if not np.isfinite(data).all():raise ValueError('Invalid generated PCM')
            output(tempo.process(engine.original_ja_encode_pcm(data)))
        def on_frame(_frames,index,frame):
            pending.append(frame)
            if len(pending)>=3:decode()
            if time.perf_counter()-started>110:raise TimeoutError('Speech timeout')
        frames=engine.generate_audio_frames(rows,on_frame=on_frame)
        decode();output(tempo.finish())
        if not frames or len(frames)>=1024:raise ValueError('Incomplete speech generation')
        if tail:
            last=fade_pcm(tail,48000,2,first=first,last=True)
            emit({'event':'pcm','id':ident,'rate':48000,'channels':2,'data':base64.b64encode(last).decode('ascii'),
                  'text':text,'start':0,'progressive':True,'estimated_frames':estimated_frames})
            sample_offset+=len(last)//4
        if not sample_offset:raise ValueError('Empty speech')
    finally:
        templates['user_prompt_after_reference_token_ids']=original
        templates['user_prompt_prefix_token_ids']=original_prefix
        engine.codec_streaming_session.reset()
    emit({'event':'done','id':ident,'frames':sample_offset,'generation_seconds':time.perf_counter()-started,'synthesis_calls':1,'language':language,'speed':speed})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True,type=Path);args=parser.parse_args()
    root=args.runtime.resolve()
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):os.environ[name]='2'
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from tts_worker import set_low_priority
    set_low_priority()
    manifest=json.loads(runtime_file(root,'install.json').read_text(encoding='utf8'))
    if manifest.get('ready') is not True or manifest.get('backend')!='moss-nano' or manifest.get('provider')!='CUDAExecutionProvider':raise ValueError('Invalid clone runtime')
    for value in ('gentle-reference-codes.json','short-fishing-codes.json','reference-codes.json','original-ja-rng-state.json','models/MOSS-TTS-Nano-100M-ONNX/tokenizer.model','moss-source/ort_cpu_runtime.py'):runtime_file(root,value)
    from moss_engine import load_engine
    engine,tokenizer,codes,np=load_engine(root)
    emit({'event':'ready','provider':'CUDAExecutionProvider'})
    while True:
        line=sys.stdin.buffer.readline(16385)
        if not line:break
        if len(line)>16384:raise ValueError('Oversized protocol line')
        ident=None
        try:
            raw=json.loads(line);ident=raw.get('id') if isinstance(raw,dict) else None
            request=validate_request(raw);speak(engine,tokenizer,codes,np,request)
        except (ValueError,RuntimeError,TimeoutError) as exc:
            emit({'event':'error','id':ident,'message':'本地语音生成未完成，请重试。'})
            print(type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
            return 1
    return 0


if __name__=='__main__':raise SystemExit(main())

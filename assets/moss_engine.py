"""Small CUDA autoregressive engine; imported only by the speech process."""
import json
import io
from pathlib import Path
import sys
import time
import wave


def load_engine(root):
    sys.path[:0]=[str(root/'deps'),str(root/'moss-source')]
    import numpy as np
    import onnxruntime as ort
    import sentencepiece as spm
    from ort_cpu_runtime import OrtCpuRuntime
    ort.preload_dlls(directory='')
    if 'CUDAExecutionProvider' not in ort.get_available_providers():raise RuntimeError('CUDA unavailable')

    class Engine(OrtCpuRuntime):
        def _create_sessions(self):
            specs=(('prefill',self.tts_meta_path.parent,self.tts_meta['files']['prefill'],False),
                   ('decode',self.tts_meta_path.parent,self.tts_meta['files']['decode_step'],True),
                   ('local_fixed_sampled_frame',self.tts_meta_path.parent,self.tts_meta['files']['local_fixed_sampled_frame'],True),
                   ('codec_decode_step',self.codec_meta_path.parent,self.codec_meta['files']['decode_step'],False))
            sessions={}
            for name,directory,filename,gpu in specs:
                options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
                options.enable_mem_pattern=False;options.log_severity_level=3
                options.graph_optimization_level=ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                providers=[('CUDAExecutionProvider',{'gpu_mem_limit':(1536 if name=='decode' else 512)*1024*1024,
                    'arena_extend_strategy':'kNextPowerOfTwo','cudnn_conv_use_max_workspace':'0'}),'CPUExecutionProvider'] if gpu else ['CPUExecutionProvider']
                session=ort.InferenceSession(str(directory/filename),sess_options=options,providers=providers)
                session.disable_fallback()
                if gpu and 'CUDAExecutionProvider' not in session.get_providers():raise RuntimeError('CUDA session unavailable')
                if name=='decode':
                    from voice_gpu_cache import DeviceCacheSession
                    session=DeviceCacheSession(session,ort.OrtValue)
                sessions[name]=session
            return sessions

    engine=Engine(root/'models',thread_count=2,execution_provider='cuda',max_new_frames=300)
    from moss_policy import canonical_prefix
    templates=engine.manifest['prompt_templates']
    engine.original_ja_prefix=list(templates['user_prompt_prefix_token_ids'])
    templates['user_prompt_prefix_token_ids']=canonical_prefix(
        templates['user_prompt_prefix_token_ids'],engine.manifest['tts_config']['im_start_token_id'])
    tokenizer=spm.SentencePieceProcessor(model_proto=(root/'models/MOSS-TTS-Nano-100M-ONNX/tokenizer.model').read_bytes())
    codes=json.loads((root/'gentle-reference-codes.json').read_text(encoding='utf8'))
    engine.short_reference_codes=json.loads((root/'short-fishing-codes.json').read_text(encoding='utf8'))
    engine.original_ja_reference_codes=json.loads((root/'reference-codes.json').read_text(encoding='utf8'))
    engine.original_ja_rng_state=json.loads((root/'original-ja-rng-state.json').read_text(encoding='utf8'))
    profiles=json.loads((root/'continuous-voice/profiles.json').read_text(encoding='utf8'))
    engine.voice_reference_codes={}
    for language in ('zh','ja'):
        path=(root/profiles[language]['codes_file']).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('Invalid reference cache')
        engine.voice_reference_codes[language]=json.loads(path.read_text(encoding='utf8'))
    def original_ja_encode_pcm(data):
        # Use the same PCM quantizer as the accepted saved WAV. A numpy cast
        # differs by one integer unit for values close to quantization edges.
        import soundfile as sf
        buffer=io.BytesIO();sf.write(buffer,data,48000,format='WAV',subtype='PCM_16');buffer.seek(0)
        with wave.open(buffer,'rb') as wav:return wav.readframes(wav.getnframes())
    engine.original_ja_encode_pcm=original_ja_encode_pcm
    # Actual model warmup completes before readiness; no warmup samples played.
    engine.manifest['generation_defaults']['max_new_frames']=2
    engine.generate_audio_frames(engine.build_voice_clone_request_rows(codes,tokenizer.encode('博士，早上好。',out_type=int)))
    engine.manifest['generation_defaults']['max_new_frames']=300
    return engine,tokenizer,codes,np

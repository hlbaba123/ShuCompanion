"""Text and PCM boundary policy; deliberately no inference/Qt dependencies."""
from array import array
import re
import sys

STYLE='Speak gently and warmly, with clear pronunciation and natural pauses.'


def canonical_prefix(prefix,start_token):
    return list(prefix) if prefix and prefix[0]==start_token else [start_token,*prefix]


def reference_kind(text):
    if language_for(text)=='ja':return 'original-ja'
    return 'short' if len(text)<14 else 'standard'


def language_for(text):
    if re.search(r'[\u3040-\u30ff]',text):return 'ja'
    if re.search(r'[\u3400-\u9fff]',text):return 'zh'
    return 'en'


def condition_prompt(prompt,language):
    if language not in ('zh','ja','en'):raise ValueError('Unsupported language')
    if language=='ja':return prompt
    return prompt.replace('Language: None','Language: '+language).replace('Instruction: None','Instruction: '+STYLE)


def segment_text(text):
    pieces=re.findall(r'.+?(?:[。！？!?；;]+[”」』]?|$)',text,flags=re.S)
    segments=[]
    for piece in pieces:
        while len(piece)>55:
            boundaries=[m.end() for m in re.finditer(r'[，、,:：\s]',piece[:55]) if m.end()>=18]
            end=boundaries[-1] if boundaries else 55
            segments.append({'text':piece[:end],'language':language_for(piece[:end])});piece=piece[end:]
        if not piece:continue
        lang=language_for(piece)
        if segments and segments[-1]['language']==lang and (lang=='ja' or len(segments[-1]['text'])<14) and len(segments[-1]['text'])+len(piece)<=55:
            segments[-1]['text']+=piece
        else:segments.append({'text':piece,'language':lang})
    return segments


def fade_pcm(data,rate,channels,*,first=False,last=False):
    if not first and not last:return data
    if len(data)%(2*channels):raise ValueError('Incomplete PCM frame')
    values=array('h');values.frombytes(data)
    if sys.byteorder!='little':values.byteswap()
    frames=len(values)//channels
    count=min(round(rate*.008),frames//2)
    if count<2:return data
    for i in range(count):
        gain=i/(count-1)
        for channel in range(channels):
            if first:values[i*channels+channel]=round(values[i*channels+channel]*gain)
            if last:
                index=(frames-1-i)*channels+channel
                values[index]=round(values[index]*gain)
    if sys.byteorder!='little':values.byteswap()
    return values.tobytes()


def finish_pcm(data,rate,channels,language,*,first=False,last=False):
    if language=='ja':return data
    return fade_pcm(data,rate,channels,first=first,last=last)

"""Replayable native pixels for the audited MAA queue + parse-ready gate.

This module only reads a supplied capture and verifies local evidence. It never
captures the desktop, invokes a model, changes MAA, or authorizes GUI input.
The caller separately binds the same code's GET/metadata and submission facts.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import math
from pathlib import Path
import re
import struct

from . import maa_queue_receipt as receipts


_MAX_PNG = 8*1024*1024
_CODE = re.compile(r'prts://[1-9][0-9]{0,11}\Z')
_METHODS = {'win32_printwindow','windows_graphics_capture'}
_STYLE = 'maa-light-212121-handycontrols-3.6.0-opacity/v1'


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _result(verified, code):
    return {'status':'verified' if verified else 'pending','verified':verified,
            'code':code,'parse_ready':verified,'gui_inputs':0}


def _rect(value, width, height, *, control=False, dpi=96):
    if (not isinstance(value,dict) or set(value)!={'x','y','width','height'}
            or any(type(value[key]) is not int for key in value)
            or min(value['x'],value['y'])<0 or min(value['width'],value['height'])<=0
            or value['x']+value['width']>width or value['y']+value['height']>height
            or (control and (type(dpi) is not int or not 72<=dpi<=384
                or not all(20*dpi/96<=value[key]<=40*dpi/96 for key in ('width','height'))))):
        raise ValueError('invalid parse control geometry')
    return dict(value)


def _png(raw):
    import cv2
    import numpy as np
    if (type(raw) is not bytes or not 24<=len(raw)<=_MAX_PNG
            or raw[:8]!=b'\x89PNG\r\n\x1a\n' or raw[12:16]!=b'IHDR'):
        raise ValueError('invalid parse capture')
    width,height = struct.unpack('>II',raw[16:24])
    if not (40<=width<=8192 and 40<=height<=8192 and width*height<=32*1024*1024):
        raise ValueError('invalid parse capture dimensions')
    image = cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape!=(height,width,3):
        raise ValueError('invalid parse capture pixels')
    return image,width,height


def _crop(image, rect):
    return image[rect['y']:rect['y']+rect['height'],rect['x']:rect['x']+rect['width']]


def _measure(image, control):
    import numpy as np
    from .maa_queue_readback import _glyphs
    result = {'state':'unknown','style':_STYLE,'glyph_rect':None,'core_bgr':None}
    patch = _crop(image,control)
    # Pinned styles only establish the theoretical colors on a white panel.
    # A dark/custom backdrop is not silently treated as a calibrated theme.
    if float((patch.min(axis=2)>=250).mean())<.60:
        return result
    glyphs = _glyphs(image,control,'+')
    if len(glyphs)!=1:
        return result
    glyph = glyphs[0]
    pixels = _crop(image,glyph).reshape(-1,3).astype('int16')
    ink = pixels[(pixels.min(axis=1)<230) & (pixels.max(axis=1)-pixels.min(axis=1)<=3)]
    if len(ink)<12:
        return result
    # The native 125% glyph is only eight pixels wide. Its crossing has the
    # greatest foreground coverage; averaging eight samples also includes
    # translucent edges and can reject both enabled and disabled icons. The
    # independently measured + shape and exact pinned color bands still apply.
    core = ink[np.argmin(ink.mean(axis=1))]
    color = tuple(int(value) for value in core)
    matches = [value for value in (33,55,122,166) if max(abs(c-value) for c in color)<=4]
    if len(matches)!=1:
        return result
    result.update(state='busy' if matches[0]==166 else 'ready',glyph_rect=glyph,core_bgr=list(color))
    return result


def _frame_facts(frame):
    window = frame['window']
    return {'screenshot_id':frame['screenshot_id'],'image_path':frame['image_path'],
        'image_sha256':frame['image_sha256'],'method':frame['method'],
        'window':{key:copy.deepcopy(window[key]) for key in
                  ('hwnd','pid','creation_time','executable','client_rect','dpi','foreground','minimized')}}


def _validate_probe(probe):
    if probe.get('schema')!='maa_queue_parse_probe/v1' or probe.get('style')!=_STYLE:
        raise ValueError('invalid parse probe')
    snapshot = probe['snapshot']
    receipts._check_snapshot(snapshot)
    binding = receipts._binding(snapshot['binding'])
    frame = probe['native_frame']
    window = frame['window']
    encoded = probe['png_b64']
    if not isinstance(encoded,str) or len(encoded)>4*((_MAX_PNG+2)//3):
        raise ValueError('parse capture too large')
    raw = base64.b64decode(encoded,validate=True)
    digest = _hash(raw)
    image,width,height = _png(raw)
    image_ref = probe['image_ref']
    geometry = window['client_rect']
    if (not isinstance(image_ref,dict) or set(image_ref)!={'path','sha256'}
            or not isinstance(image_ref['path'],str) or not 0<len(image_ref['path'])<=4096
            or not Path(image_ref['path']).is_absolute()
            or image_ref['path']!=frame['image_path'] or image_ref['sha256']!=digest
            or probe['image_sha256']!=digest or frame['image_sha256']!=digest
            or not isinstance(frame['screenshot_id'],str)
            or not re.fullmatch(r'[0-9a-f]{32}',frame['screenshot_id'])
            or frame['method'] not in _METHODS
            or any(window[key]!=binding['process'][key] for key in binding['process'])
            or set(geometry)!={'x','y','width','height'}
            or any(type(value) is not int for value in geometry.values())
            or geometry['width']!=width or geometry['height']!=height
            or type(window['dpi']) is not int or not 72<=window['dpi']<=384
            or window['foreground'] is not True or window['minimized'] is not False
            or not isinstance(probe['code_text'],str) or not _CODE.fullmatch(probe['code_text'])):
        raise ValueError('parse capture binding changed')
    matches = [observation for observation in snapshot.get('screenshot_observations',[])
               if observation['ref']['sha256']==digest]
    if len(matches)!=1 or not any(ref['sha256']==digest for ref in snapshot['screenshot_refs']):
        raise ValueError('parse snapshot does not contain frame')
    # The screenshot may be pinned to a private copy, but its bytes, file
    # identity, and observation window must belong to this exact snapshot.
    observation = matches[0]
    if observation['file_state']['size']!=len(raw):
        raise ValueError('parse frame size changed')
    stamp = observation['file_state']['mtime_ns']/1e9
    if not snapshot['captured_at']-15<=stamp<=snapshot['captured_at']+.002:
        raise ValueError('parse frame stale')
    control = _rect(probe['glyph_rect'],width,height,control=True,dpi=window['dpi'])
    field = _rect(probe['code_input_rect'],width,height)
    if (field['width']<40 or field['height']<12 or field['y']+field['height']>=control['y']
            or control['x']<field['x'] or control['x']>field['x']+field['width']+40):
        raise ValueError('parse controls are not in the same queue panel')
    measurement = _measure(image,control)
    if probe.get('measurement')!=measurement:
        raise ValueError('parse state must match native pixels')
    return {'snapshot':snapshot,'binding':binding,'native_frame':frame,'stamp':stamp,
            'measurement':measurement,'geometry':(width,height,window['dpi']),
            'code_pixels':_crop(image,field).tobytes()}


def capture_parse_probe(frame, glyph_rect, snapshot, *, code_text, code_input_rect):
    """Pin one measured footer + capture; unknown colors remain unknown.

    ``glyph_rect`` is a 20..40 DIP control patch measured from the queue footer,
    not an approximate model point or the smaller icon-only bounding box.
    ``code_text`` is the caller's image-bound field transcription. Pixel
    equality across all probes additionally requires that field to stay put.
    """
    raw,_ = receipts._read_stable(frame['image_path'])
    image,width,height = _png(raw)
    control = _rect(glyph_rect,width,height,control=True,dpi=frame['window']['dpi'])
    probe = {'schema':'maa_queue_parse_probe/v1','style':_STYLE,
        'native_frame':_frame_facts(frame),
        'image_ref':{'path':frame['image_path'],'sha256':frame['image_sha256']},
        'image_sha256':_hash(raw),'png_b64':base64.b64encode(raw).decode('ascii'),
        'snapshot':copy.deepcopy(snapshot),'glyph_rect':control,
        'code_input_rect':copy.deepcopy(code_input_rect),'code_text':code_text,
        'measurement':_measure(image,control)}
    _validate_probe(probe)
    return probe


def verify_parse_ready(probes, expected):
    """Require native busy→ready→ready for one code and exact absence frames.

    Expected keys: binding, mystery_code, submitted_at (UTC timestamp float),
    previous_snapshot and current_snapshot. No model-provided enabled/idle
    booleans are accepted as a replacement for the embedded pixel sequence.
    The caller must still verify GET/metadata, absence, ownership and permission.
    """
    try:
        if not isinstance(probes,list) or len(probes)!=3:
            return _result(False,'queue_parse_sequence_missing')
        binding = receipts._binding(expected['binding'])
        code,submitted = expected['mystery_code'],expected['submitted_at']
        if (not isinstance(code,str) or not _CODE.fullmatch(code)
                or type(submitted) not in (int,float) or not math.isfinite(submitted)):
            raise ValueError('invalid parse request')
        facts = [_validate_probe(probe) for probe in probes]
        if (any(item['binding']!=binding for item in facts)
                or any(probe['code_text']!=code for probe in probes)
                or any(probe['glyph_rect']!=probes[0]['glyph_rect']
                    or probe['code_input_rect']!=probes[0]['code_input_rect'] for probe in probes)
                or any(item['geometry']!=facts[0]['geometry'] or item['code_pixels']!=facts[0]['code_pixels']
                       for item in facts)):
            return _result(False,'queue_parse_request_changed')
        if [item['measurement']['state'] for item in facts]!=['busy','ready','ready']:
            return _result(False,'queue_parse_native_transition_unverified')
        if any(item['measurement']['glyph_rect']!=facts[0]['measurement']['glyph_rect'] for item in facts):
            return _result(False,'queue_parse_control_changed')
        busy,previous,current = (item['snapshot'] for item in facts)
        if (previous!=expected['previous_snapshot'] or current!=expected['current_snapshot']
                or not submitted<=busy['captured_at']<previous['captured_at']<current['captured_at']<=submitted+180
                or min(item['stamp'] for item in facts)<submitted-.002
                or not receipts._frame_advanced(busy,previous,not_before=submitted)
                or not receipts._absence_observed(previous,current)
                or previous['projection_sha256']!=current['projection_sha256']
                or previous['rows']!=current['rows']
                or previous['visible_complete'] is not True or current['visible_complete'] is not True
                or len({item['native_frame']['screenshot_id'] for item in facts})!=3):
            return _result(False,'queue_parse_observation_unverified')
        result = _result(True,'queue_parse_completion_verified')
        result['proof_sha256'] = _hash(receipts._bytes(probes))
        return result
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,OSError,
            receipts.MaaQueueReceiptError):
        return _result(False,'queue_parse_proof_invalid')

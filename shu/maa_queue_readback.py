"""Read-only observation of the MAA queue in client-image coordinates."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .maa_visual_readback import _OPTIONS, _center, _letters, detect_rectangles, parse_visual
from .maa_vision_readback import VisionOcr, VisionOcrError, _check_cancel, _project, _read_png


_OPTION_NAMES = ('auto_formation', 'ignore_requirements', 'use_support', 'multiple_jobs')
_PALETTE_NAMES = ('normal_bgr', 'raid_bgr', 'add_enabled_bgr', 'add_disabled_bgr',
                  'scrollbar_track_bgr', 'scrollbar_thumb_bgr')


def _right(box):
    return box['x'] + box['width']


def _bottom(box):
    return box['y'] + box['height']


def _inside(box, region):
    return (region['x'] <= box['x'] and region['y'] <= box['y']
            and _right(box) <= _right(region) and _bottom(box) <= _bottom(region))


def _crop(image, box, inset=0):
    return image[math.ceil(box['y']) + inset:math.floor(_bottom(box)) - inset,
                 math.ceil(box['x']) + inset:math.floor(_right(box)) - inset]


def _target(box, kind, source):
    return {'rect': box, 'point': _center(box), 'kind': kind, 'source': source}


def _collapse(boxes, *, slop=4):
    result = []
    for box in sorted(boxes, key=lambda item: -item['width'] * item['height']):
        if not any(max(abs(box[key] - existing[key]) for key in box) <= slop for existing in result):
            result.append(box)
    return result


def _unique(boxes, *, slop=4):
    result = _collapse(boxes, slop=slop)
    return result[0] if len(result) == 1 else None


def _measure_boxes(image):
    import cv2
    boxes = detect_rectangles(image)
    gray = cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    # Thick or nearly horizontal WPF outlines can be split by Canny. A
    # second pass measures the continuous, light-theme border itself.
    mask = cv2.inRange(gray,85,232)
    contours,_ = cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours[:4096]:
        x,y,w,h = cv2.boundingRect(contour)
        if min(w,h) < 12 or w*h < 120 or abs(cv2.contourArea(contour)) < .78*w*h:
            continue
        polygon = cv2.approxPolyDP(contour,cv2.arcLength(contour,True)*.018,True)
        if 4 <= len(polygon) <= 8:
            boxes.append({'x':x,'y':y,'width':w,'height':h})
    return _collapse(boxes)


def _color_match(pixels, sample, tolerance=5):
    import numpy as np
    return (np.abs(pixels.astype(np.int16) - sample) <= tolerance).all(axis=-1)


def _palette_state(pixels, first, second, *, fraction):
    if not pixels.size or first is None or second is None or max(abs(a-b) for a,b in zip(first,second)) <= 10:
        return None
    matches = [float(_color_match(pixels, color).mean()) >= fraction for color in (first, second)]
    return matches.index(True) if matches.count(True) == 1 else None


def _mode_palette(image, queue, calibration):
    import numpy as np
    normal, raid = calibration.get('normal_bgr'), calibration.get('raid_bgr')
    if normal is not None or raid is not None:
        return normal, raid
    # Pinned v6.21.4 CopilotView.xaml sets ordinary rows Transparent and raid
    # rows Copilot.RaidBrush. Res/Themes/Light.xaml defines it as #33FF0000.
    # Obtain the backdrop from a uniform column INSIDE this frame's gutter.
    # Dense raid rows can fill four of the first five columns; their mixed
    # strip must not hide the remaining full-height light backdrop column.
    panel = _crop(image,queue,3)
    if not panel.size:
        return None,None
    strip = panel[:, :min(5,panel.shape[1])]
    samples = []
    for index in range(strip.shape[1]):
        column = strip[:,index]
        color = tuple(round(float(channel)) for channel in np.median(column,axis=0))
        if (min(color)>=238 and max(color)-min(color)<=3
                and float(_color_match(column,color,2).mean())>=.95):
            samples.append(color)
    if not samples or any(max(abs(a-b) for a,b in zip(samples[0],color))>2 for color in samples):
        return None,None
    normal = samples[0]
    raid = tuple(round(.8*backdrop+.2*foreground) for backdrop,foreground in zip(normal,(0,0,255)))
    return normal,raid


def _checked(image, box):
    pixels = _crop(image, box, 2).astype('int16')
    if not pixels.size:
        return None
    blue, green, red = (pixels[:, :, i] for i in range(3))
    filled = (blue > 90) & (green > 90) & (green-red > 30) & (blue-red > 30)
    if float(filled.mean()) > .55:
        return True
    if float((pixels > 235).all(axis=2).mean()) > .95:
        return False
    return None


def _queue_frame(image, boxes, words, base):
    """Bind the list to the measured footer and its actual name-field label."""
    field = base['regions'].get('code_input')
    if not field:
        return None, None, None
    labels = [word['rect'] for word in words if _letters(word['text']) == '关卡名'
              and word['rect']['y'] > _bottom(field) and _right(word['rect']) <= _right(field)+4]
    if len(labels) != 1:
        return None, None, None
    footer = _unique([box for box in boxes if _inside(labels[0], box)
                      and 1.65 <= box['width']/box['height'] <= 9
                      and box['height'] <= 1.5 * field['height']])
    if not footer:
        return None, None, None
    def footer_buttons(candidates):
        return _collapse([box for box in candidates if abs(box['y']-footer['y']) <= 4
                          and abs(box['height']-footer['height']) <= 5
                          and .65 <= box['width']/box['height'] <= 1.55
                          and box['x'] >= _right(footer)-4])

    foot_boxes = footer_buttons(boxes)
    add = _unique([box for box in foot_boxes if abs(box['x']-_right(footer)) <= 4])
    if not add:
        return None, None, None
    clear_boxes = [box for box in foot_boxes if abs(box['x']-_right(add)) <= 4]
    if not clear_boxes:
        # A scrolled list can join the clear button contour at the footer
        # seam. Remeasure only the current footer, below its known name
        # field, and require the same square and adjacency checks.
        left, top = math.floor(add['x']), math.floor(footer['y'])
        right = min(image.shape[1], math.ceil(_right(add)+2*add['width']))
        bottom = min(image.shape[0], math.ceil(_bottom(footer)+footer['height']/2))
        local_boxes = [{**box, 'x':box['x']+left, 'y':box['y']+top}
                       for box in _measure_boxes(image[top:bottom,left:right])]
        clear_boxes = [box for box in footer_buttons(local_boxes) if abs(box['x']-_right(add)) <= 4]
    clear = _unique(clear_boxes)
    if not clear:
        return None, None, None
    # A closed contour can also belong to an internal raid row. Both closed
    # and footer-open lists must prove the same physical outer three sides.
    queue = _open_bottom_list(image, field, footer, clear, boxes=boxes)
    return queue, add, clear


def _open_bottom_list(image, field, footer, clear, *, boxes=None):
    """Find the native list's three-sided outline above the footer seam."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    right, bottom = round(_right(clear)), round(footer['y'])
    # The list begins below the code input. Its lower border can span this
    # same horizontal range, so including that border creates a second false
    # outline and makes the otherwise unique list ambiguous.
    # Contour boxes can measure the border itself; its Canny exterior is one
    # pixel below it. Both focused and unfocused code boxes must be excluded.
    first_y = max(0, math.ceil(_bottom(field))+2)
    last_y = min(bottom, round(_bottom(field)+2*field['height']))
    first_x = max(0, round(footer['x']-1.8*footer['height']))
    last_x = round(footer['x'])
    if bottom-last_y < 2*field['height'] or last_x-first_x < 3 or right >= gray.shape[1]-3:
        return None
    edge = cv2.Canny(gray, 12, 35) > 0
    # Long, vertical physical edges followed by a connecting horizontal edge.
    lefts = np.flatnonzero(edge[last_y:bottom, first_x:last_x].mean(axis=0) > .85)+first_x
    right_edges = edge[last_y:bottom, right-3:right+4]
    rights = np.flatnonzero(right_edges.mean(axis=0) > .85)+right-3
    if not len(rights):
        # The expanded translucent thumb can move Canny's outer border one
        # pixel sideways along the same physical list outline. Require an
        # almost continuous edge across two adjacent columns at the measured
        # footer's right bound, instead of requiring one straight column.
        joined = right_edges[:,:-1] | right_edges[:,1:]
        rights = np.flatnonzero(joined.mean(axis=0) > .98)+right-2
    candidates = []
    for left in lefts:
        for right in rights:
            for top in np.flatnonzero(edge[first_y:last_y, left:right].mean(axis=1)>.85)+first_y:
                candidates.append({'x':int(left),'y':int(top),'width':int(right-left),'height':int(bottom-top)})
    # A horizontal row background can span almost the entire list. Its sides
    # continue ABOVE that line; a genuine outer top instead joins the start
    # of both continuous side edges. Check the complete height, including the
    # part above last_y that was only a search window for candidate tops.
    measured = _measure_boxes(image) if boxes is None else boxes
    row_squares = [box for box in measured
                   if first_y <= box['y'] and _bottom(box) < bottom
                   and first_x <= box['x'] < last_x
                   and .7 <= box['width']/box['height'] <= 1.35
                   and .3*field['height'] <= box['height'] <= .9*field['height']]
    genuine = []
    for box in _collapse(candidates, slop=5):
        if not all(_inside(square,box) for square in row_squares):
            continue
        top = box['y']-first_y
        for side_x in (box['x'], _right(box)):
            side = edge[first_y:bottom, side_x-1:side_x+2].any(axis=1)
            above = side[:max(0,top-5)]
            run = max(6,round(field['height']*.2))
            if (not side[top:top+5].any() or side[top+3:].mean() < .98
                    or len(above)>=run and np.convolve(above,np.ones(run),mode='valid').max()>=run):
                break
        else:
            genuine.append(box)
    return _unique(genuine, slop=5)


def _glyphs(image, box, glyph):
    """Measure local + or × stroke intersections, independent of model boxes."""
    import cv2
    import numpy as np
    pixels = _crop(image, box)
    if pixels.size == 0:
        return []
    # Compare luminance to nearby background so muted icons remain detectable.
    gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    background = float(np.median(gray))
    ink = (gray < background-22).astype('uint8')
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    found = []
    for label, (x,y,width,height,area) in enumerate(stats[1:],1):
        if min(width,height) < 6 or max(width,height) > max(box['height']*.77,9):
            continue
        if not .7 <= width/height <= 1.4 or area > .75*width*height:
            continue
        component = (labels[y:y+height,x:x+width] == label).astype('uint8')
        normalized = cv2.resize(component,(15,15),interpolation=cv2.INTER_NEAREST).astype(bool)
        if glyph == '+':
            axes = (normalized[5:10,:].any(axis=0), normalized[:,5:10].any(axis=1))
            blank = [normalized[a:a+4,b:b+4].mean() for a in (0,11) for b in (0,11)]
        else:
            axes = tuple(np.asarray([np.any(sample[row,max(0,row-2):min(15,row+3)])
                                    for row in range(15)]) for sample in (normalized,normalized[:,::-1]))
            blank = [normalized[0:3,6:9].mean(),normalized[12:15,6:9].mean(),
                     normalized[6:9,0:3].mean(),normalized[6:9,12:15].mean()]
        if min(float(axis.mean()) for axis in axes) >= .8 and max(blank) <= .25:
            found.append({'x':math.ceil(box['x'])+int(x), 'y':math.ceil(box['y'])+int(y),
                          'width':int(width), 'height':int(height)})
    return found


def _row_observations(image, queue, words, boxes, calibration, image_sha):
    import cv2
    import numpy as np
    inset = 3
    normal_bgr, raid_bgr = _mode_palette(image,queue,calibration)
    inside = [word for word in words if _inside(word['rect'], queue)
              and _letters(word['text']) not in ('', '×', 'x', '+')]
    # Prefer exact text anchors supported by visible ink. One row must have
    # exactly one measured checkbox. A wrapped or clipped name is not filled in.
    rows = []
    for word in sorted(inside,key=lambda word:word['rect']['y']):
        label = word['rect']
        label_pixels = _crop(image,label)
        if not label_pixels.size or cv2.cvtColor(label_pixels,cv2.COLOR_BGR2GRAY).min() > 175:
            continue
        check = _unique([box for box in boxes if _inside(box,queue) and .7 <= box['width']/box['height'] <= 1.35
                         and .7*label['height'] <= box['height'] <= 1.9*label['height']
                         and 0 <= label['x']-_right(box) <= label['height']*1.25
                         and abs(_center(label)['y']-_center(box)['y']) <= label['height']*.4])
        if not check or any(existing['checkbox_rect'] == check for existing in rows):
            return [], False
        right_start = math.ceil(_right(label)) + inset
        row_right = math.floor(_right(queue)) - inset
        top, bottom = round(check['y']), round(_bottom(check))
        row_box = {'x':queue['x']+inset,'y':top,'width':queue['width']-2*inset,'height':bottom-top}
        icon_search = {'x':right_start,'y':top,'width':row_right-right_start,'height':bottom-top}
        delete = _unique(_glyphs(image, icon_search,'×')) if icon_search['width'] > 0 else None
        mode_sample = image[top:bottom,right_start:row_right] if row_right > right_start else np.empty((0,0,3),dtype='uint8')
        mode = _palette_state(mode_sample,normal_bgr,raid_bgr,fraction=.55)
        key = hashlib.sha256(f'{image_sha}:{len(rows)}:{top}'.encode('ascii')).hexdigest()[:24]
        rows.append({'key':key,'name':word['text'],'mode':mode+1 if mode is not None else None,
                     'is_checked':_checked(image,check),'rect':row_box,'checkbox_rect':check,
                     'label_rect':label,'delete_rect':delete,'visible_ordinal':len(rows)})
    # Detect visible checkbox-like squares that the model did not transcribe.
    # This also prevents blank OCR from proving an empty, visibly occupied list.
    if not rows:
        body = _crop(image,queue,3)
        return rows, bool(body.size and np.min(body) >= 200)
    # All row checkboxes share a measured vertical column. Extra boxes in that
    # column indicate omitted/clipped rows, even if the transcription is short.
    reference = rows[0]['checkbox_rect']
    same_column = _collapse([box for box in boxes if _inside(box,queue)
                             and abs(box['x']-reference['x'])<=4
                             and abs(box['width']-reference['width'])<=4
                             and abs(box['height']-reference['height'])<=4])
    return rows, len(same_column)==len(rows)


def _scroll_state(image, queue, calibration):
    import numpy as np
    track, thumb = calibration.get('scrollbar_track_bgr'), calibration.get('scrollbar_thumb_bgr')
    unknown = {'top':None,'bottom':None,'proof':'unmeasured'}
    if track is None or thumb is None or max(abs(a-b) for a,b in zip(track,thumb))<=10:
        return unknown
    interior = _crop(image,queue,2)
    if not interior.size:
        return unknown
    # A scrollbar is a narrow vertical stripe against the measured right edge.
    strip = interior[:,-min(16,max(2,interior.shape[1]//8)):]
    track_mask, thumb_mask = _color_match(strip,track), _color_match(strip,thumb)
    columns = ((track_mask|thumb_mask).mean(axis=0)>.96) & (thumb_mask.mean(axis=0)>.04)
    if not columns.any():
        return unknown
    occupied = np.flatnonzero(thumb_mask[:,columns].mean(axis=1)>.6)
    if len(occupied)<4 or np.any(np.diff(occupied)>1):
        return unknown
    return {'top':bool(occupied[0]<=2),'bottom':bool(occupied[-1]>=len(strip)-3),
            'proof':'calibrated_scrollbar_pixels'}


def _queue_wheel_target(image, queue):
    """Prefer the measured scroll thumb to avoid hovering over a row label."""
    import numpy as np
    result = _target(queue,'list','measured_queue_outline')
    # The narrow right interior belongs to this measured list even when WPF
    # hides its thumb until hover. This is a wheel-only target, never a click;
    # keeping it off the row body avoids its difficulty-obscuring hover style.
    if queue['width'] >= 20 and queue['height'] >= 20:
        result['point'] = {'x':math.floor(_right(queue))-4,
                           'y':queue['y']+queue['height']/2}
        result['source'] = 'measured_queue_right_interior'
    top,bottom = math.ceil(queue['y'])+3,math.floor(_bottom(queue))-3
    right = math.floor(_right(queue))-1
    left = max(math.ceil(queue['x'])+3,right-7)
    strip = image[top:bottom,left:right]
    if strip.size and strip.shape[0] >= 20:
        achromatic = np.ptp(strip.astype(np.int16),axis=2) <= 8
        dark = achromatic & (strip.max(axis=2) <= 225) & (strip.min(axis=2) >= 40)
        candidates = []
        for column in range(dark.shape[1]):
            occupied = np.flatnonzero(dark[:,column])
            if (len(occupied) >= max(12,len(strip)//4)
                    and np.all(np.diff(occupied)==1)):
                candidates.append((column,occupied))
        if 1 <= len(candidates) <= 3 and all(b[0]-a[0]==1 for a,b in zip(candidates,candidates[1:])):
            column, occupied = candidates[len(candidates)//2]
            result['point'] = {'x':left+column,'y':top+int(occupied[len(occupied)//2])}
            result['source'] = 'measured_scrollbar_thumb'
    return result


def _rows_equal(rows, expected_rows):
    return all((row['name'],row['mode'],row['is_checked']) ==
               (expected.get('name'),2 if expected.get('is_raid') is True else 1,
                expected.get('is_checked')) and type(expected.get('is_raid')) is bool
               and type(expected.get('is_checked')) is bool
               for row, expected in zip(rows, expected_rows, strict=True))


def _projection_endpoints(rows, expected_rows):
    """Find visible endpoints only if all measured rows align unambiguously."""
    if len(rows) > len(expected_rows):
        return None
    positions = [index for index in range(len(expected_rows) - len(rows) + 1)
                 if _rows_equal(rows, expected_rows[index:index + len(rows)])]
    if len(positions) != 1:
        return None
    index = positions[0]
    return {'top': index == 0, 'bottom': index + len(rows) == len(expected_rows),
            'proof': 'unique_visible_projection_slice'}


def _single_text_mismatch(rows, expected_rows):
    """Locate one uncertain physical row, without supplying its expected text.

    The independent projection is used only to decide which image region to
    reread. Every visible mode/check state and every other name must match at
    one unique offset. Neither the expected name nor the offset is OCR input.
    """
    if not rows or not isinstance(expected_rows,list) or len(rows)>len(expected_rows):
        return None
    candidates=[]
    for offset in range(len(expected_rows)-len(rows)+1):
        actual=expected_rows[offset:offset+len(rows)]
        if any(type(item.get('is_raid')) is not bool or type(item.get('is_checked')) is not bool
               or row.get('mode')!=(2 if item['is_raid'] else 1)
               or row.get('is_checked') is not item['is_checked']
               for row,item in zip(rows,actual,strict=True)):
            continue
        mismatches=[index for index,(row,item) in enumerate(zip(rows,actual,strict=True))
                    if row.get('name')!=item.get('name')]
        if len(mismatches)==1:
            candidates.append(mismatches[0])
    return candidates[0] if len(candidates)==1 else None


def stitch_queue_views(views: list[dict], expected_count: int, *, expected_rows=None) -> dict:
    """Stitch only ordered, fully transcribed views from one config projection."""
    pending = {'visible_complete':False,'reason':'queue_visibility_unverified','rows':[]}
    if (not isinstance(views,list) or not 1<=len(views)<=100
            or type(expected_count) is not int or not 0<=expected_count<=1000):
        return pending
    if expected_rows is not None and (not isinstance(expected_rows,list) or len(expected_rows)!=expected_count
                                     or not all(isinstance(row,dict) for row in expected_rows)):
        return pending
    if not all(isinstance(view,dict) for view in views):
        return pending
    projection = views[0].get('projection_sha256')
    if not isinstance(projection,str) or not re.fullmatch(r'[a-f0-9]{64}',projection):
        return pending
    combined, previous_rows, hashes = [], [], set()

    def fingerprint(row):
        return (row['name'],row['mode'],row['is_checked'])

    for index, view in enumerate(views):
        rows, digest = view.get('queue_rows'), view.get('image_sha256')
        if (view.get('projection_sha256')!=projection or view.get('ok') is not True
                or view.get('queue_mode') is not True or view.get('rows_measured') is not True
                or not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest)
                or digest in hashes or not isinstance(rows,list)
                or any(not isinstance(row,dict) or not isinstance(row.get('name'),str) or not row['name']
                       or type(row.get('mode')) is not int or row['mode'] not in (1,2)
                       or type(row.get('is_checked')) is not bool for row in rows)):
            return pending
        hashes.add(digest)
        if index:
            before, after = list(map(fingerprint,previous_rows)), list(map(fingerprint,rows))
            overlaps = [size for size in range(2,min(len(before),len(after))+1)
                        if before[-size:]==after[:size]]
            if len(overlaps)!=1 or overlaps[0] == len(after):
                return pending
            combined.extend(rows[overlaps[0]:])
        else:
            combined.extend(rows)
        previous_rows = rows
    first_scroll, last_scroll = views[0].get('scroll_state') or {}, views[-1].get('scroll_state') or {}
    if not isinstance(first_scroll,dict) or not isinstance(last_scroll,dict):
        return pending
    # Matching an entire config projection in one view proves both boundaries,
    # even on a short WPF list that does not render a scrollbar.
    one_full = len(views)==1 and expected_rows is not None
    if (len(combined)!=expected_count or (not one_full and
        (first_scroll.get('top') is not True or last_scroll.get('bottom') is not True))
            or first_scroll.get('top') is False or last_scroll.get('bottom') is False
            or (expected_rows is not None and not _rows_equal(combined,expected_rows))):
        return pending
    return {'visible_complete':True,'reason':'queue_visibility_verified','projection_sha256':projection,
            'rows':[dict(row,ordinal=index) for index,row in enumerate(combined)]}


class QueueVisionReadback:
    """Measure controls locally and send only screenshot pixels to VisionOcr.

    Palette values are local probe data. For the pinned light theme a uniform
    background from this frame and the WPF raid brush also suffice for modes.
    Scrollbar endpoints and plus enable state require verified samples.
    ``expected`` is used solely for local list-to-config reconciliation.
    """
    requires_frame_refresh = True
    max_observe_seconds = 45

    def __init__(self, config=None, *, client=None, ocr=None, calibration=None,
                 progress_callback=None, metrics_callback=None):
        self.ocr = ocr if ocr is not None else VisionOcr(config,client=client,
            progress_callback=progress_callback,metrics_callback=metrics_callback)
        calibration = {} if calibration is None else calibration
        if (not isinstance(calibration,dict) or set(calibration)-set(_PALETTE_NAMES)
                or any(not isinstance(color,(tuple,list)) or len(color)!=3
                       or any(type(value) is not int or not 0<=value<=255 for value in color)
                       for color in calibration.values())):
            raise ValueError('invalid queue pixel calibration')
        self.calibration = {name:tuple(color) for name,color in calibration.items()}

    def _read_words(self, path, width, height, cancel_event):
        recognize = getattr(self.ocr, 'recognize_queue', None) or self.ocr.recognize
        return _project(recognize(path,cancel_event=cancel_event),width,height)['words']

    def _enlarge(self, image, queue, cancel_event):
        return self._cropped_words(image,queue,cancel_event,maximum_scale=2.5)

    def _cropped_words(self, image, queue, cancel_event, *, maximum_scale):
        import cv2
        region = _crop(image,queue,2)
        scale = min(maximum_scale,2200/max(region.shape[:2]))
        enlarged = cv2.resize(region,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        height,width = enlarged.shape[:2]
        with TemporaryDirectory(prefix='shu-queue-vision-') as directory:
            path = Path(directory)/'queue.png'
            success,encoded = cv2.imencode('.png',enlarged)
            if not success:
                raise VisionOcrError('vision_image_unreadable')
            path.write_bytes(encoded.tobytes())
            words = self._read_words(path,width,height,cancel_event)
        x_scale,y_scale = region.shape[1]/width,region.shape[0]/height
        for word in words:
            rect = word['rect']
            rect.update(x=math.ceil(queue['x'])+2+rect['x']*x_scale,
                        y=math.ceil(queue['y'])+2+rect['y']*y_scale,
                        width=rect['width']*x_scale,height=rect['height']*y_scale)
        return words

    def _reread_row(self,image,queue,row,cancel_event):
        check=row.get('checkbox_rect')
        if not isinstance(check,dict):
            return []
        # Derive a short strip from this visible row's physical square and
        # measured right-side icon. No fixed screen coordinates or names.
        right=(row.get('delete_rect') or {}).get('x',_right(queue))
        if isinstance(row.get('label_rect'),dict):
            right=min(right,_right(row['label_rect'])+check['height']*.5)
        left=_right(check)
        top=max(queue['y'],check['y']-check['height']*.25)
        bottom=min(_bottom(queue),_bottom(check)+check['height']*.25)
        region={'x':left,'y':top,'width':right-left,'height':bottom-top}
        if min(region['width'],region['height'])<8:
            return []
        return self._cropped_words(image,region,cancel_event,maximum_scale=4)

    def _refine_options(self, image, words, boxes, base, digest, cancel_event):
        """Re-read only the measured controls area if full-frame labels drift.

        Full-frame vision often gets the label right while its vertical box
        drifts by more than a checkbox height on tall/narrow windows. Widening
        the checkbox join could then select the neighbouring control. Instead
        request one unhinted closeup and retain the same strict contour join.
        Neither configuration states nor expected queue rows enter the model.
        """
        missing = [name for name in _OPTION_NAMES if name not in base.get('options',{})]
        field = base.get('regions',{}).get('code_input')
        if not missing or not field:
            return base, words
        height,width = image.shape[:2]
        # Read every control below this measured field. Using the last known
        # label as the crop's bottom omits a missing final option indefinitely.
        top,bottom = math.ceil(_bottom(field)),height
        left,right = max(0,math.floor(field['x'])), min(width,math.ceil(_right(field)))
        queue,_,_ = _queue_frame(image,boxes,words,base)
        if queue is not None and queue['x'] > left:
            right = min(right,math.floor(queue['x']))
        if min(right-left,bottom-top) < 16:
            return base, words
        region = {'x':left,'y':top,'width':right-left,'height':bottom-top}
        closeup = self._enlarge(image,region,cancel_event)
        _check_cancel(cancel_event)
        names = {_letters(label) for label in _OPTIONS.values()}
        labels = [word for word in closeup if _letters(word['text']) in names and _inside(word['rect'],region)]
        # Remove coarse label boxes only when an independently transcribed exact
        # counterpart is present. Missing or duplicated labels remain unbound.
        reread = {_letters(word['text']) for word in labels}
        combined = [word for word in words if _letters(word['text']) not in reread] + labels
        refined = parse_visual({'width':width,'height':height,'words':combined},
                               rectangles=boxes,image_sha256=digest)
        refined['control_readback'] = 'unhinted_measured_control_closeup'
        return refined, combined

    def observe(self, image_path, *, expected=None, cancel_event=None, control_evidence=None):
        import cv2
        import numpy as np
        result = {'schema':'maa_queue_vision/v1','ok':False,'page':'unknown','width':None,'height':None,
                  'image_sha256':'','targets':{},'forbidden_regions':[],'options':{},'idle':False,
                  'running':False,'queue_mode':None,'queue_rows':[],'queue_region':None,
                  'scroll_state':{'top':None,'bottom':None,'proof':'unmeasured'},'rows_measured':False,
                  'visible_complete':False,'queue_add_enabled':None,'parse_busy_seen':False,
                  'read_only':True,'input_permission':False}
        try:
            _check_cancel(cancel_event)
            raw,width,height = _read_png(image_path)
            image = cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None or image.shape[:2]!=(height,width):
                raise VisionOcrError('vision_image_unreadable')
            words = self._read_words(image_path,width,height,cancel_event)
            _check_cancel(cancel_event)
            if _read_png(image_path)[0]!=raw:
                raise VisionOcrError('vision_image_unreadable')
            digest = hashlib.sha256(raw).hexdigest()
            boxes = _measure_boxes(image)
            shared_controls = False
            if (isinstance(control_evidence,dict)
                    and control_evidence.get('image_sha256') == digest
                    and control_evidence.get('width') == width and control_evidence.get('height') == height
                    and control_evidence.get('ok') is True and control_evidence.get('page') == 'copilot'
                    and all(type(control_evidence.get('options',{}).get(name,{}).get('state')) is bool
                            and name in control_evidence.get('anchors',{}) for name in _OPTION_NAMES)):
                # The local pass can prove the controls while failing to read
                # one queue row. Reuse only its exact label observations from
                # this identical image, then re-run the same contour/pixel
                # checks. No expected/configuration labels enter OCR, and no
                # control state or coordinates are copied into the result.
                labels = [control_evidence['anchors'][name] for name in _OPTION_NAMES]
                names = {_letters(_OPTIONS[name]) for name in _OPTION_NAMES}
                footer=control_evidence.get('anchors',{}).get('queue_stage_name')
                if (isinstance(footer,dict) and _letters(footer.get('text',''))=='关卡名'
                        and control_evidence.get('queue_region') is not None):
                    labels.append(footer)
                    names.add('关卡名')
                words = [word for word in words if _letters(word['text']) not in names] + labels
                shared_controls = True
            base = parse_visual({'width':width,'height':height,'words':words},
                                rectangles=boxes,image_sha256=digest)
            if base['page'] == 'copilot':
                try:
                    base,words = self._refine_options(image,words,boxes,base,digest,cancel_event)
                except VisionOcrError as error:
                    if error.code == 'vision_cancelled':
                        raise
                    base['control_refinement_error'] = error.code
            result.update(base)
            if shared_controls:
                result['control_readback'] = 'same_image_local_pixel_verified_controls'
            result['schema'] = 'maa_queue_vision/v1'
            result['targets'] = {name:target for name,target in base['targets'].items()
                                 if name in ('code_input','copilot_tab')}
            if base['page']!='copilot':
                result['targets'] = {}
                return result
            for name, option in result['options'].items():
                option['state'] = _checked(image,option['rect'])
                option['measurement'] = 'local_checkbox_pixels'
                if name in _OPTION_NAMES and type(option['state']) is bool:
                    result['targets'][name] = _target(option['rect'],'checkbox','measured_square_and_exact_label')
            result['queue_mode'] = result['options'].get('multiple_jobs',{}).get('state')
            if result['queue_mode'] is not True:
                return result
            queue, add, clear = _queue_frame(image,boxes,words,base)
            if queue is None:
                return {**result,'reason':'queue_layout_unverified'}
            footer_labels=[word for word in words if _letters(word['text'])=='关卡名'
                           and _bottom(queue)-6<=word['rect']['y']<=_bottom(clear)]
            if len(footer_labels)==1:
                result['anchors']['queue_stage_name']=footer_labels[0]
            result['queue_region'] = queue
            result['regions']['queue_list'] = queue
            result['regions']['queue_add'] = add
            result['targets']['queue_list'] = _queue_wheel_target(image,queue)
            result['forbidden_regions'].append({'kind':'queue_clear','rect':clear})
            add_inside = dict(x=add['x']+4,y=add['y']+4,width=add['width']-8,height=add['height']-8)
            glyph = _unique(_glyphs(image,add_inside,'+'))
            if glyph:
                result['targets']['queue_add'] = _target(glyph,'button','measured_plus_and_footer')
                glyph_pixels = _crop(image,glyph)
                # Exclude the surrounding light panel from a foreground sample.
                ink = glyph_pixels.min(axis=2)<245
                value = _palette_state(glyph_pixels[ink],self.calibration.get('add_disabled_bgr'),
                                       self.calibration.get('add_enabled_bgr'),fraction=.4)
                if value is not None:
                    result['queue_add_enabled'] = bool(value)
                    result['parse_busy_seen'] = bool(not value and result['idle'] and not result['running'])
            queue_words = words
            if queue['width']<150 and any(_inside(word['rect'],queue) for word in words):
                queue_words = self._enlarge(image,queue,cancel_event)
            _check_cancel(cancel_event)
            result['queue_rows'],result['rows_measured'] = _row_observations(
                image,queue,queue_words,boxes,self.calibration,digest)
            expected_rows = expected.get('rows') if isinstance(expected,dict) else None
            unmatched_rows = (isinstance(expected_rows,list) and result['rows_measured'] is True
                              and _projection_endpoints(result['queue_rows'],expected_rows) is None
                              and bool(result['queue_rows']))
            if (result['rows_measured'] is not True or unmatched_rows) and queue_words is words:
                # Full-window OCR can place one label across its checkbox by a
                # pixel, invalidating the strict row join. Re-transcribe only
                # this measured list before requesting a different backend.
                # Expected/configured names never enter the OCR request.
                queue_words = self._enlarge(image,queue,cancel_event)
                _check_cancel(cancel_event)
                result['queue_rows'],result['rows_measured'] = _row_observations(
                    image,queue,queue_words,boxes,self.calibration,digest)
                result['row_readback'] = 'unhinted_measured_queue_closeup'
            if isinstance(expected_rows,list) and result['rows_measured'] is True:
                # A long-list closeup can still confuse a single glyph (e.g.
                # IW/IVW). Up to two bounded short pixel rereads replace only
                # independently transcribed words; never insert the wanted
                # projection name or loosen final exact row matching.
                for _ in range(2):
                    index=_single_text_mismatch(result['queue_rows'],expected_rows)
                    if index is None:
                        break
                    row=result['queue_rows'][index]
                    try:
                        corrected=self._reread_row(image,queue,row,cancel_event)
                    except VisionOcrError as error:
                        if error.code=='vision_cancelled':
                            raise
                        result['row_refinement_error']=error.code
                        break
                    _check_cancel(cancel_event)
                    outside=[word for word in queue_words if not
                             (word['rect']['y']<_bottom(row['rect'])
                              and _bottom(word['rect'])>row['rect']['y'])]
                    reread=outside+corrected
                    new_rows,measured=_row_observations(image,queue,reread,boxes,self.calibration,digest)
                    if (not measured or len(new_rows)!=len(result['queue_rows'])
                            or [(item['name'],item['mode'],item['is_checked']) for item in new_rows]
                            ==[(item['name'],item['mode'],item['is_checked']) for item in result['queue_rows']]):
                        break
                    queue_words=reread
                    result['queue_rows'],result['rows_measured']=new_rows,measured
                    result['row_readback']='unhinted_measured_single_row_closeup'
            result['scroll_state'] = _scroll_state(image,queue,self.calibration)
            if isinstance(expected,dict) and isinstance(expected.get('rows'),list):
                result['projection_sha256'] = expected.get('projection_sha256')
                if result['rows_measured'] and result['scroll_state']['proof'] == 'unmeasured':
                    measured = _projection_endpoints(result['queue_rows'], expected['rows'])
                    if measured is not None:
                        result['scroll_state'] = measured
                aligned = stitch_queue_views([result],len(expected['rows']),expected_rows=expected['rows'])
                result['visible_complete'] = aligned['visible_complete']
                if aligned['visible_complete']:
                    result['queue_rows'] = aligned['rows']
                    identities = [row.get('identity') for row in expected['rows']]
                    signatures = [(row['name'],row['mode'],row['is_checked']) for row in aligned['rows']]
                    for index,row in enumerate(result['queue_rows']):
                        identity = identities[index]
                        if (isinstance(identity,str) and re.fullmatch(r'[a-f0-9]{64}',identity)
                                and identities.count(identity)==1 and signatures.count(signatures[index])==1
                                and row['delete_rect']):
                            result['targets']['row_delete:'+identity] = _target(
                                row['delete_rect'],'row_delete','matched_current_queue_row')
            return result
        except VisionOcrError as exc:
            return {**result,'ok':False,'targets':{},'reason':exc.code}


class LocalFirstQueueReadback:
    """Prefer MaaFramework OCR with the same strict queue pixel interpretation."""

    requires_frame_refresh = True
    max_observe_seconds = 45

    def __init__(self, local_ocr, fallback_ocr):
        self.local = QueueVisionReadback(ocr=local_ocr)
        self.fallback = QueueVisionReadback(ocr=fallback_ocr)

    def observe(self, image_path, *, expected=None, cancel_event=None):
        from .maa_framework_observer import MaaFrameworkOcrError

        reason = 'queue_local_evidence_incomplete'
        local = None
        try:
            local = self.local.observe(image_path, expected=expected, cancel_event=cancel_event)
            controls_known = all(type(local['options'].get(name, {}).get('state')) is bool
                                 for name in _OPTION_NAMES)
            queue_mode = local.get('queue_mode')
            queue_known = (queue_mode is False or queue_mode is True and local.get('rows_measured') is True
                           and (local.get('visible_complete') is True
                                or local['scroll_state'].get('proof') != 'unmeasured'))
            if (local.get('ok') is True and local.get('page') == 'copilot' and controls_known
                    and 'code_input' in local['targets'] and queue_known):
                return {**local, 'recognition_engine': 'maafw_ocr+queue_geometry'}
        except MaaFrameworkOcrError as error:
            if error.code == 'framework_cancelled':
                raise
            reason = error.code
        except (ImportError, OSError):
            reason = 'queue_local_ocr_unavailable'
        _check_cancel(cancel_event)
        fallback = self.fallback.observe(image_path, expected=expected, cancel_event=cancel_event,
                                         control_evidence=local)
        return {**fallback, 'recognition_engine': 'deepseek_vision+queue_geometry',
                'local_fallback_reason': reason}

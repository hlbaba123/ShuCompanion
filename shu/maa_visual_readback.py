"""Read-only MAA image/OCR interpretation, independent of UI Automation.

Every rectangle is measured in the captured client image.  This module neither
captures a window nor grants input permission.  In particular, the Start button
is a *forbidden* region, never an actionable target.  Conservative failures are
intentional: an input echo alone cannot prove that MAA loaded a copilot.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unicodedata

from .local_ocr import LocalOcr, MAX_DIMENSION, MAX_WORDS


_FATAL = ('下载失败', '解析失败', '导入失败', '作业不存在', '文件不存在',
          '连接失败', '无效的神秘代码', '作业格式错误', '格式不正确')
_OPTIONS = {'auto_formation': '自动编队', 'ignore_requirements': '忽略干员属性要求',
            'use_support': '借助战', 'multiple_jobs': '多作业模式'}
_CODE = re.compile(r'(?<![a-z0-9])prts://([0-9]{1,12})(?![0-9])', re.I)
_CODE_FIELD_OCR = re.compile(r'(?:prts|pfts|p戊s)(?::?/{0,2})([1-9][0-9]{0,11})\Z')


class _OcrGeometryError(ValueError):
    pass


def _normal(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or ''))).casefold()


def _letters(text):
    """Punctuation-insensitive label comparison; never used to repair codes."""
    return ''.join(char for char in _normal(text) if char.isalnum())


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _rect(raw, width, height):
    if not isinstance(raw, dict):
        return None
    values = [raw.get(key) for key in ('x', 'y', 'width', 'height')]
    if not all(_number(value) for value in values):
        return None
    x, y, w, h = values
    if w <= 0 or h <= 0 or x + w > width or y + h > height:
        return None
    return dict(zip(('x', 'y', 'width', 'height'), values))


def _union(boxes):
    x = min(box['x'] for box in boxes)
    y = min(box['y'] for box in boxes)
    return {'x': x, 'y': y,
            'width': max(box['x'] + box['width'] for box in boxes) - x,
            'height': max(box['y'] + box['height'] for box in boxes) - y}


def _center(box):
    return {'x': box['x'] + box['width'] / 2, 'y': box['y'] + box['height'] / 2}


def _inside(inner, outer):
    point = _center(inner)
    return (outer['x'] <= point['x'] <= outer['x'] + outer['width']
            and outer['y'] <= point['y'] <= outer['y'] + outer['height'])


def _rows(words):
    groups = []
    for word in sorted(words, key=lambda word: (word['rect']['y'], word['rect']['x'])):
        box = word['rect']
        cy = box['y'] + box['height'] / 2
        row = next((row for row in groups if abs(row['cy'] - cy)
                    <= min(row['height'], box['height']) * .42), None)
        if row is None:
            row = {'cy': cy, 'height': box['height'], 'words': []}
            groups.append(row)
        row['words'].append(word)
    result = []
    for row in groups:
        values = sorted(row['words'], key=lambda word: word['rect']['x'])
        result.append({'text': ' '.join(word['text'] for word in values),
                       'rect': _union([word['rect'] for word in values]), 'words': values})
    return result


def _find(rows, label, *, exact=False):
    needle = _letters(label)
    matches = []
    for row in rows:
        stream, positions = '', []
        for i, word in enumerate(row['words']):
            text = _letters(word['text'])
            stream += text
            positions.extend([i] * len(text))
        at = stream.find(needle)
        while at >= 0 and needle:
            indices = sorted(set(positions[at:at + len(needle)]))
            actual = ''.join(row['words'][i]['text'] for i in indices)
            if not exact or _letters(actual) == needle:
                matches.append({'text': actual,
                                'rect': _union([row['words'][i]['rect'] for i in indices])})
            at = stream.find(needle, at + 1)
    return matches


def _expected(expected):
    expected = expected if isinstance(expected, dict) else {}
    code = expected.get('code', expected.get('mystery_code', ''))
    title, stage = expected.get('title', ''), expected.get('stage_code', '')
    names = expected.get('operator_names', [])
    match = _CODE.fullmatch(code) if isinstance(code, str) else None
    return {'code': 'prts://' + match.group(1) if match else '',
            'title': title if isinstance(title, str) and len(title) <= 1000 else '',
            'stage_code': stage if isinstance(stage, str) and len(stage) <= 40 else '',
            'operator_names': [name for name in names if isinstance(name, str) and 0 < len(name) <= 80]
            if isinstance(names, list) and len(names) <= 24 else []}


def parse_visual(ocr, *, rectangles=(), expected=None, image_sha256='', preview_sha256='') -> dict:
    """Interpret LocalOcr output and measured image rectangle contours.

    ``rectangles`` is a list of ``{x,y,width,height}`` image contours.  A text
    anchor alone is insufficient to invent the position of the job field or an
    icon.  ``expected`` binds source-verified code/title/stage/operators, not a
    model's claim about what is visible.
    """
    result = {'schema': 'maa_visual/v1', 'ok': False, 'page': 'unknown',
              'reason': 'invalid_ocr', 'coordinate_space': 'client_image',
              'width': None, 'height': None, 'anchors': {}, 'regions': {}, 'options': {},
              'targets': {}, 'forbidden_regions': [], 'idle': False, 'running': False,
              'codes': [], 'code_text': '', 'code_id_readback': None, 'resolved_path': '', 'preview_lines': [],
              'field_empty': False, 'single_job': False, 'operator_count': None,
              'expected_title_visible': False, 'expected_stage_visible': False,
              'expected_operators_visible': [], 'missing_operators': [], 'errors': [],
              'image_sha256': image_sha256, 'preview_sha256': preview_sha256,
              'preview_signature': '', 'read_only': True, 'input_permission': False}
    if not isinstance(ocr, dict):
        return result
    width, height = ocr.get('width'), ocr.get('height')
    words = ocr.get('words')
    if (type(width) is not int or type(height) is not int or not 0 < width <= MAX_DIMENSION
            or not 0 < height <= MAX_DIMENSION or not isinstance(words, list) or len(words) > MAX_WORDS):
        return result
    valid = []
    for word in words:
        if (not isinstance(word, dict) or not isinstance(word.get('text'), str)
                or not word['text'].strip() or len(word['text']) > 512):
            return result
        rect = _rect(word.get('rect'), width, height)
        if rect is None:
            return result
        valid.append({'text': word['text'], 'rect': rect})
    result.update(ok=True, reason='insufficient_evidence', width=width, height=height)
    rows = _rows(valid)
    exp = _expected(expected)
    boxes = [box for raw in rectangles if (box := _rect(raw, width, height)) is not None]
    tabs = [item for item in _find(rows, '自动战斗', exact=True)
            if item['rect']['y'] < height * .22]
    stage_tabs = [item for item in _find(rows, '主线故事集SideStory')
                  if item['rect']['x'] < width * .48 and item['rect']['y'] < height * .24]
    if not stage_tabs:
        stage_tabs = [item for row in rows if 'sidesto' in _letters(row['text'])
                      for item in _find([row], '主线', exact=True)
                      if item['rect']['x'] < width * .48 and item['rect']['y'] < height * .24]
    if len(tabs) == 1:
        result['anchors']['copilot_tab'] = tabs[0]
        result['targets']['copilot_tab'] = {**tabs[0], 'point': _center(tabs[0]['rect']), 'kind': 'tab'}
    if len(stage_tabs) == 1:
        result['anchors']['stage_tab'] = stage_tabs[0]
    # The left-side panel must have both a real stage-mode label and a measured
    # field below it.  A guessed screen percentage is not an input target.
    fields = []
    if len(stage_tabs) == 1:
        anchor = stage_tabs[0]['rect']
        for box in boxes:
            if (box['width'] >= width * .18 and 4 <= box['width'] / box['height'] <= 35
                    and height * .018 <= box['height'] <= height * .09
                    and box['y'] >= anchor['y'] + anchor['height'] * .65
                    and box['y'] <= anchor['y'] + anchor['height'] + height * .105
                    and abs(box['x'] - anchor['x']) <= max(width * .04, anchor['height'])
                    and box['x'] + box['width'] <= width * .78):
                fields.append(box)
    # Canny finds nested outlines of one border.  Collapse only nearly identical
    # boxes, and reject a genuinely ambiguous choice instead of picking a guess.
    fields.sort(key=lambda box: box['width'] * box['height'], reverse=True)
    unique = []
    for box in fields:
        if not any(all(abs(box[key] - other[key]) <= 4 for key in box) for other in unique):
            unique.append(box)
    if len(unique) == 2:
        outer, inner = unique
        # The composite frame also surrounds the folder and clipboard buttons.
        # Its left edge follows the editable box, but its right edge does not.
        if (abs(outer['x'] - inner['x']) <= 4 and abs(outer['y'] - inner['y']) <= 4
                and abs(outer['height'] - inner['height']) <= 5
                and 1.5 <= (outer['width'] - inner['width']) / inner['height'] <= 2.6):
            unique = [inner]
    field = unique[0] if len(unique) == 1 else None
    if field:
        result['regions']['code_input'] = field
        result['targets']['code_input'] = {'rect': field,
            'point': {'x': field['x'] + min(field['width'] * .35, field['width'] - field['height']),
                      'y': field['y'] + field['height'] / 2},
            'kind': 'edit', 'source': 'measured_border_and_stage_label'}
        field_words = [word for word in valid if _inside(word['rect'], field)]
        field_text = ' '.join(row['text'] for row in _rows(field_words))
        result['code_text'] = field_text
        result['field_empty'] = _letters(field_text) == '作业路径神秘代码'
        result['codes'] = list(dict.fromkeys('prts://' + match.group(1)
                                             for match in _CODE.finditer(_normal(field_text))))
        code_match = _CODE_FIELD_OCR.fullmatch(_normal(field_text))
        if code_match:
            result['code_id_readback'] = code_match.group(1)
        if re.match(r'^(?:[A-Za-z]:[\\/]|\\\\)', field_text.strip()) and '.json' in field_text.casefold():
            result['resolved_path'] = field_text.strip()
        # MAA has a folder icon followed by a separate clipboard icon.  Both
        # adjacent square borders must actually be visible before the second is
        # identified; an unlabelled arbitrary rectangle cannot become a target.
        neighbors = []
        for box in boxes:
            ratio = box['width'] / box['height']
            if (.65 <= ratio <= 1.45 and .65 * field['height'] <= box['height'] <= 1.5 * field['height']
                    and abs(_center(box)['y'] - _center(field)['y']) <= field['height'] * .3
                    and field['x'] + field['width'] - 3 <= box['x']
                    <= field['x'] + field['width'] + field['height'] * 3):
                neighbors.append(box)
        neighbors.sort(key=lambda box: (box['x'], -box['width'] * box['height']))
        squares = []
        for box in neighbors:
            if not any(abs(_center(box)['x'] - _center(other)['x']) <= 5 for other in squares):
                squares.append(box)
        if (len(squares) == 2 and squares[0]['x'] - field['x'] - field['width'] <= field['height'] * .35
                and 0 <= squares[1]['x'] - squares[0]['x'] - squares[0]['width'] <= field['height'] * .55):
            result['regions']['paste_button'] = squares[1]
            result['targets']['paste_button'] = {'rect': squares[1], 'point': _center(squares[1]),
                                                  'kind': 'clipboard', 'source': 'two_measured_adjacent_buttons'}
    if field:
        for key, label in (('start', '开始'), ('stop', '停止')):
            candidates = [item for item in _find(rows, label, exact=True)
                          if field['y'] + field['height'] <= item['rect']['y'] <= field['y'] + height * .22
                          and field['x'] <= _center(item['rect'])['x'] <= field['x'] + field['width']]
            if len(candidates) == 1:
                result['anchors'][key] = candidates[0]
                # Pad the text into a deny region.  No target uses this box.
                box = candidates[0]['rect']
                result['forbidden_regions'].append({'kind': key, 'rect': {
                    'x': max(0, box['x'] - field['height'] * 1.8),
                    'y': max(0, box['y'] - field['height'] * .7),
                    'width': min(width - max(0, box['x'] - field['height'] * 1.8),
                                 box['width'] + field['height'] * 3.6),
                    'height': min(height - max(0, box['y'] - field['height'] * .7),
                                  box['height'] + field['height'] * 1.4)}})
        result['idle'] = 'start' in result['anchors'] and 'stop' not in result['anchors']
        result['running'] = 'stop' in result['anchors']
        if tabs and stage_tabs:
            result.update(page='copilot', reason='measured_copilot_layout')
        for kind, label in _OPTIONS.items():
            candidates = [item for item in _find(rows, label, exact=True)
                          if field['x'] <= item['rect']['x'] <= field['x'] + field['width']
                          and item['rect']['y'] > field['y'] + field['height']]
            if len(candidates) == 1:
                result['anchors'][kind] = candidates[0]
                label = candidates[0]['rect']
                checks = [box for box in boxes
                          if .65 <= box['width'] / box['height'] <= 1.4
                          and .65 * label['height'] <= box['height'] <= 1.7 * label['height']
                          and abs(_center(box)['y'] - _center(label)['y']) <= label['height'] * .4
                          and 0 <= label['x'] - box['x'] - box['width'] <= label['height'] * 1.2]
                checks.sort(key=lambda box: box['width'] * box['height'], reverse=True)
                unique_checks = []
                for box in checks:
                    if not any(abs(_center(box)['x'] - _center(other)['x']) <= 4 for other in unique_checks):
                        unique_checks.append(box)
                if len(unique_checks) == 1:
                    result['options'][kind] = {'rect': unique_checks[0], 'state': 'unknown'}
                    result['targets'][kind] = {'kind': 'checkbox', 'rect': unique_checks[0],
                                               'point': _center(unique_checks[0]),
                                               'source': 'measured_square_and_exact_label'}
    # Readback text is restricted to the right-hand preview panel.  Instructions
    # and the code field cannot satisfy an expected title or operator name.
    right = field['x'] + field['width'] + field['height'] * 1.5 if field else width * .5
    right = min(right, width * .62)
    preview_words = [word for word in valid if word['rect']['x'] >= right
                     and word['rect']['y'] >= (field['y'] + field['height'] if field else height * .25)]
    preview_rows = _rows(preview_words)
    title_hits = _find([{'words': [word for row in preview_rows for word in row['words']]}],
                       exp['title']) if exp['title'] else []
    title = title_hits[0] if title_hits else None
    if title:
        top = max(field['y'] + field['height'] if field else 0, title['rect']['y'] - height * .012)
        preview_rows = [row for row in preview_rows if row['rect']['y'] >= top]
        # Keep the actual title/requirements area out of subsequent timestamped
        # progress logs, so a log append cannot masquerade as a new preview.
        operator_hits = [hit for name in exp['operator_names'] for hit in _find(preview_rows, name)]
        requirements_bottom = max([title['rect']['y'] + title['rect']['height']]
                                  + [hit['rect']['y'] + hit['rect']['height'] for hit in operator_hits])
        # Long descriptions and alternative-operator groups can push MAA's
        # real count well below a fixed fraction of the window. Use the count
        # label actually measured after the known requirements, before the
        # feedback footer. Ambiguous counts must remain unknown.
        footer_top = min([height + 1] + [row['rect']['y'] for row in preview_rows
                         if row['rect']['y'] > requirements_bottom
                         and any(label in _letters(row['text']) for label in ('作业怎么样','给个评价'))])
        count_rows = [row for row in preview_rows
                      if requirements_bottom <= row['rect']['y'] < footer_top
                      and re.fullmatch(r'共[1-9][0-9]?名干员', _letters(row['text']))]
        if len(count_rows) != 1:
            count_rows = []
        bottom = min(height, footer_top - 1, max([title['rect']['y'] + title['rect']['height']]
                                 + [hit['rect']['y'] + hit['rect']['height'] for hit in operator_hits + count_rows])
                     + height * .035)
        result['regions']['preview'] = {'x': right, 'y': top, 'width': width - right, 'height': bottom - top}
        preview_rows = [row for row in preview_rows if row['rect']['y'] <= bottom]
    combined = _letters(' '.join(row['text'] for row in preview_rows))
    result['preview_lines'] = [row['text'][:1000] for row in preview_rows][:48]
    result['preview_signature'] = hashlib.sha256(combined.encode('utf-8')).hexdigest()
    result['expected_title_visible'] = bool(exp['title'] and _letters(exp['title']) in combined)
    stage_text = _normal(' '.join(row['text'] for row in preview_rows))
    result['expected_stage_visible'] = bool(exp['stage_code'] and re.search(
        r'(?<![a-z0-9])' + re.escape(_normal(exp['stage_code'])) + r'(?![a-z0-9]|-[a-z0-9])', stage_text))
    result['expected_operators_visible'] = [name for name in exp['operator_names'] if _letters(name) in combined]
    result['missing_operators'] = [name for name in exp['operator_names'] if _letters(name) not in combined]
    counts = [int(match.group(1)) for row in preview_rows
              if (match := re.fullmatch(r'共([1-9][0-9]?)名干员', _letters(row['text'])))]
    if len(counts) == 1:
        result['operator_count'] = counts[0]
    all_preview = _normal(' '.join(row['text'] for row in _rows(preview_words)))
    result['errors'] = [message for message in _FATAL if _normal(message) in all_preview]
    return result


def detect_rectangles(image):
    """Measure near-axis-aligned border contours in an already decoded image."""
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    # WPF's light grey borders are low contrast; Canny still measures their
    # location without treating OCR-estimated text geometry as a control box.
    edges = cv2.Canny(gray, 18, 60)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    result = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 12 or h < 12 or w * h < 120:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, perimeter * .025, True)
        if 4 <= len(polygon) <= 8 and abs(cv2.contourArea(contour)) / (w * h) >= .72:
            result.append({'x': x, 'y': y, 'width': w, 'height': h})
    return result[:1024]


class MaaVisualReadback:
    def __init__(self, *, ocr=None):
        self.ocr = ocr or LocalOcr()

    def observe(self, image_path, *, expected=None, cancel_event=None):
        import cv2
        import numpy as np
        if cancel_event is not None and cancel_event.is_set():
            return {'ok': False, 'reason': 'cancelled'}
        path = Path(image_path)
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            return {'ok': False, 'reason': 'image_unreadable'}
        raw = path.read_bytes()
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or max(image.shape[:2]) > MAX_DIMENSION:
            return {'ok': False, 'reason': 'image_unreadable'}
        try:
            ocr = self._recognize_columns(image, cancel_event, expected=expected)
        except _OcrGeometryError:
            return {'ok': False, 'reason': 'image_ocr_geometry_mismatch'}
        result = parse_visual(ocr, rectangles=detect_rectangles(image), expected=expected,
                              image_sha256=hashlib.sha256(raw).hexdigest())
        region = result['regions'].get('preview')
        if region:
            x, y = int(region['x']), int(region['y'])
            right, bottom = math.ceil(region['x'] + region['width']), math.ceil(region['y'] + region['height'])
            result['preview_sha256'] = hashlib.sha256(image[y:bottom, x:right].tobytes()).hexdigest()
        for option in result['options'].values():
            box = option['rect']
            x, y = math.ceil(box['x']) + 2, math.ceil(box['y']) + 2
            right, bottom = int(box['x'] + box['width']) - 2, int(box['y'] + box['height']) - 2
            crop = image[y:bottom, x:right].astype(np.int16)
            if crop.size:
                blue, green, red = (crop[:, :, i] for i in range(3))
                turquoise = (blue > 90) & (green > 90) & (green - red > 30) & (blue - red > 30)
                white = (crop > 235).all(axis=2)
                if float(turquoise.mean()) > .2:
                    option['state'] = True
                elif float(white.mean()) > .95:
                    option['state'] = False
                option['measurement'] = 'local_checkbox_pixels'
        result['single_job'] = result['options'].get('multiple_jobs', {}).get('state') is False
        field = result['regions'].get('code_input')
        if field:
            x, y = math.ceil(field['x']) + 5, math.ceil(field['y']) + 4
            right = math.floor(field['x'] + field['width'] * .75)
            bottom = math.floor(field['y'] + field['height']) - 4
            crop = image[y:bottom, x:right]
            if crop.size:
                ink = crop.min(axis=2)
                # MAA v6.21.4 renders its empty-field hint in gray. Require
                # both its recognizable ends and the absence of dark text.
                hint = _letters(result['code_text'])
                hint_visible = hint.startswith('作业路') and hint.endswith('代码') and 6 <= len(hint) <= 12
                dark = ink < 140
                ys, xs = np.nonzero(dark)
                caret = False
                if xs.size:
                    left, top = int(xs.min()), int(ys.min())
                    stroke_width, stroke_height = int(xs.max()) - left + 1, int(ys.max()) - top + 1
                    stroke = crop[ys, xs].astype(np.int16)
                    # A focused empty WPF field has a solid 1-2 px neutral
                    # caret at its left padding. No extra dark mark, gap,
                    # colored selection or text-like wide stroke is ignored.
                    caret = bool(
                        1 <= stroke_width <= 2 and 8 <= stroke_height <= min(24, field['height'] * .7)
                        and left <= 5 and abs(top + stroke_height / 2 - crop.shape[0] / 2)
                        <= max(2, crop.shape[0] * .15)
                        and xs.size == stroke_width * stroke_height
                        and (stroke.max(axis=1) - stroke.min(axis=1) <= 3).all())
                result['field_empty'] = bool(
                    (hint_visible and (not xs.size or caret))
                    or (not hint and (ink > 243).mean() > .995 and not xs.size))
        result['image_path'] = str(path.resolve())
        return result

    def _recognize_columns(self, image, cancel_event, *, expected=None):
        import cv2

        height, width = image.shape[:2]
        split = width // 2
        with TemporaryDirectory(prefix='shu-maa-ocr-') as directory:
            def read_crop(name, left, top, right, bottom, *, sharpen=False):
                crop = image[top:bottom, left:right]
                scale = min(5.0 if sharpen else 3.6, 2500 / max(crop.shape[:2]))
                resized = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                if sharpen:
                    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
                    resized = cv2.threshold(gray, 165, 255, cv2.THRESH_BINARY)[1]
                path = Path(directory) / f'{name}.png'
                encoded, buffer = cv2.imencode('.png', resized)
                if not encoded:
                    raise ValueError('无法编码 MAA 局部截图')
                path.write_bytes(buffer.tobytes())
                result = self.ocr.recognize(path, cancel_event=cancel_event)
                if (result['width'], result['height']) != (resized.shape[1], resized.shape[0]):
                    raise _OcrGeometryError('MAA 局部 OCR 尺寸与截图不一致')
                x_scale, y_scale = crop.shape[1] / result['width'], crop.shape[0] / result['height']
                return [{'text': word['text'], 'rect': {
                    'x': left + word['rect']['x'] * x_scale, 'y': top + word['rect']['y'] * y_scale,
                    'width': word['rect']['width'] * x_scale, 'height': word['rect']['height'] * y_scale}}
                    for word in result['words']]

            with ThreadPoolExecutor(max_workers=2) as pool:
                left = pool.submit(read_crop, 'left', 0, 0, split, height)
                right = pool.submit(read_crop, 'right', split, 0, width, height)
                left_words, right_words = left.result(), right.result()
                counts = [row for row in _rows(right_words)
                          if re.fullmatch(r'共[1-9][0-9]?名干员', _letters(row['text']))]
                if expected and len(counts) == 1:
                    box = counts[0]['rect']
                    top = max(0, int(box['y'] - box['height'] * 14))
                    bottom = min(height, math.ceil(box['y'] + box['height'] * 2))
                    closeup = read_crop('preview', split - 4, top, width, bottom, sharpen=True)
                    title = _letters(expected.get('title'))
                    if title and title in _letters(' '.join(row['text'] for row in _rows(closeup))):
                        right_words = [word for word in right_words if not top <= word['rect']['y'] < bottom] + closeup
                return {'width': width, 'height': height, 'words': left_words + right_words}


def _application_preview_pixels(before, first, last):
    """Verify one fixed physical preview ROI on three genuine saved captures."""
    import cv2
    import numpy as np
    try:
        keys = ('hwnd', 'pid', 'creation_time', 'executable', 'client_rect', 'dpi')
        if any(any(frame['window'][key] != before['window'][key] for key in keys)
               for frame in (first, last)):
            return False
        boxes = [frame['regions']['preview'] for frame in (first,last)]
        for box in boxes:
            values=[box[key] for key in ('x','y','width','height')]
            if (any(type(value) not in (int,float) or not math.isfinite(value) for value in values)
                    or min(values[:2])<0 or min(values[2:])<=0):
                return False
        # Model line/word boxes can drift vertically on identical pixels.
        # Keep a fixed measured horizontal pane and compare its vertical union
        # on ALL three actual images; never compare differently cropped images.
        if any(box[key]!=boxes[0][key] for box in boxes[1:] for key in ('x','width')):
            return False
        x,width=boxes[0]['x'],boxes[0]['width']
        y=min(box['y'] for box in boxes)
        height=max(box['y']+box['height'] for box in boxes)-y
        if x < before['window']['client_rect']['width']*.35:
            return False
        # OCR unions have fractional edges. Compare the same outward-rounded
        # physical pixels; never treat numeric geometry as an unverifiable ROI.
        right, bottom = math.ceil(x + width), math.ceil(y + height)
        x, y = math.floor(x), math.floor(y)
        width, height = right-x, bottom-y
        hashes = []
        for frame in (before, first, last):
            path = Path(frame['image_path']).resolve(strict=True)
            if not 24 <= path.stat().st_size <= 32 * 1024 * 1024:
                return False
            raw = path.read_bytes()
            if (raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR'
                    or hashlib.sha256(raw).hexdigest() != frame['image_sha256']):
                return False
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            geometry = frame['window']['client_rect']
            if (image is None or image.shape != (geometry['height'], geometry['width'], 3)
                    or x + width > image.shape[1] or y + height > image.shape[0]):
                return False
            hashes.append(hashlib.sha256(image[y:y + height, x:x + width].tobytes()).hexdigest())
        return hashes[1] == hashes[2] and hashes[0] != hashes[2]
    except (KeyError, TypeError, ValueError, OSError, cv2.error):
        return False


def verify_import(before, after, *, expected, code_entry=None, input_issued=True, load_receipt=None):
    """Require source-bound code entry and two fresh, stable loaded previews.

    MAA may replace ``prts://…`` with its cache path after loading.  In that case
    ``code_entry`` must be the measured screenshot immediately after typing and
    before resolution.  A model/keyboard success flag is never a substitute.
    Returning ``already_loaded`` does not attest a new import operation.
    """
    exp = _expected(expected)
    frames = after if isinstance(after, (list, tuple)) else [after]
    report = {'verified': False, 'status': 'unverified', 'reasons': [], 'evidence': {}}
    if not exp['code'] or not exp['title'] or not exp['stage_code']:
        report['reasons'].append('expected_code_title_stage_required')
        return report
    if len(frames) < 2 or not all(isinstance(frame, dict) and frame.get('ok') for frame in frames[-2:]):
        report['reasons'].append('two_fresh_readbacks_required')
        return report
    first, last = frames[-2:]
    if not isinstance(before, dict) or not before.get('ok'):
        report['reasons'].append('before_snapshot_required')
        return report
    application_verified = False
    application_pixels_verified = False
    source = expected.get('source_document', {})
    expected_count = (len(source['opers']) + len(source.get('groups', []))
                      if isinstance(source, dict) and isinstance(source.get('opers'), list)
                      and isinstance(source.get('groups', []), list) else len(exp['operator_names']))
    if load_receipt is not None:
        try:
            from .maa_load_receipt import verify_receipt
            check = verify_receipt(load_receipt, {**expected, 'target_identity': before.get('window', {})})
            application_verified = check.get('verified') is True
            if application_verified:
                application_pixels_verified = _application_preview_pixels(before, first, last)
        except (ImportError, OSError, ValueError, TypeError, KeyError):
            pass
        if not application_verified:
            report['reasons'].append('application_receipt_invalid')
        elif (source.get('doc', {}).get('title') != expected.get('title')
              or before.get('ok') is not True or before.get('field_empty') is not True
              or before.get('operator_count') is not None
              or not all(frame.get('ok') is True and frame.get('idle') is True and frame.get('running') is False
                         and frame.get('expected_stage_visible') is True and frame.get('single_job') is True
                         and type(frame.get('operator_count')) is int and frame['operator_count'] == expected_count
                         for frame in (first, last)) or not application_pixels_verified):
            report['reasons'].append('application_preview_pixels_unverified')
    # Caller supplies native screenshot IDs.  For offline image replay the
    # resolved image path may identify an independent capture instead.
    ids = [frame.get('screenshot_id') or frame.get('image_path') for frame in (first, last)]
    if not all(ids) or ids[0] == ids[1]:
        report['reasons'].append('readbacks_not_independent')
    for frame in (first, last):
        if frame.get('page') != 'copilot' or not frame.get('idle') or frame.get('running'):
            report['reasons'].append('copilot_idle_not_verified')
        if (not frame.get('expected_title_visible') and not application_verified) or not frame.get('expected_stage_visible'):
            report['reasons'].append('expected_preview_not_visible')
        # A loaded preview can have one name clipped by its portrait. Exact
        # code/title/stage and the visible roster count still identify the job;
        # the requirements themselves are validated against the source and Box.
        roster_visible = (frame.get('operator_count') == len(exp['operator_names'])
                          and bool(frame.get('expected_operators_visible'))
                          and len(frame.get('missing_operators', [])) == 1
                          and len(exp['operator_names']) >= 2)
        if frame.get('missing_operators') and not roster_visible:
            report['reasons'].append('expected_operators_not_visible')
        if frame.get('errors'):
            report['reasons'].append('maa_error_visible')
    if (not application_pixels_verified
            and (not first.get('preview_signature') or first.get('preview_signature') != last.get('preview_signature'))):
        report['reasons'].append('preview_not_stable')
    entry = code_entry if isinstance(code_entry, dict) else last
    exact_echo = exp['code'] in entry.get('codes', [])
    expected_id = exp['code'].split('//', 1)[1]
    numeric_echo = all(frame.get('code_id_readback') == expected_id for frame in (first, last))
    if not exact_echo and not numeric_echo:
        report['reasons'].append('real_code_echo_required')
    if (exp['code'] in before.get('codes', []) and before.get('expected_title_visible')
            and before.get('expected_stage_visible') and not before.get('missing_operators')):
        report.update(status='already_loaded')
        report['reasons'].append('same_code_preview_predates_input')
    elif not input_issued:
        report['reasons'].append('new_input_not_issued')
    else:
        changed = (last.get('preview_sha256') and before.get('preview_sha256')
                   and last['preview_sha256'] != before['preview_sha256'])
        appeared = (not before.get('expected_title_visible') and last.get('expected_title_visible')
                    and before.get('image_sha256') and last.get('image_sha256')
                    and before['image_sha256'] != last['image_sha256'])
        if not (changed or appeared or application_pixels_verified):
            report['reasons'].append('no_new_preview_evidence')
    report['reasons'] = list(dict.fromkeys(report['reasons']))
    report['evidence'] = {'code': exp['code'], 'title': exp['title'], 'stage_code': exp['stage_code'],
                          'code_readback': 'exact_text' if exact_echo else 'exact_id_and_new_preview',
                          'before': before.get('screenshot_id', before.get('image_path')),
                          'code_entry': entry.get('screenshot_id', entry.get('image_path')),
                          'after': ids, 'preview_signature': last.get('preview_signature'),
                          'transport': 'screenshot_ocr', 'start_clicked': False,
                          'preview_verified_by': 'maa_log_cache_and_native_pixels' if application_pixels_verified else 'visual_signature',
                          'title_verified_by': 'maa_log_cache' if application_verified else 'visual_exact'}
    if not report['reasons']:
        report.update(verified=True, status='import_verified')
    return report

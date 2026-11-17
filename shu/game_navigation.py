"""Conservative page evidence from an existing image's local OCR only.

Candidate rectangles are in source-image pixels. They describe visible text;
they never grant permission to click, establish difficulty, or start a battle.
Only matched navigation labels are returned, not the OCR transcript.
"""
from __future__ import annotations

import math
import re
import unicodedata

from .local_ocr import LocalOcr, LocalOcrError, MAX_DIMENSION, MAX_WORDS

_DASHES = str.maketrans({char: '-' for char in '‐‑‒–—−﹣－'})
_STAGE_BODY = r'(?:[A-Z]{1,4}\d{0,2}(?:-[A-Z]{1,3})?-[A-Z]{0,2}\d{1,3}|\d{1,2}-\d{1,3})'
_STAGES = re.compile(r'(?<![A-Z0-9_-])' + _STAGE_BODY + r'(?![A-Z0-9_-])')
_CHAPTER = re.compile(r'第[一二三四五六七八九十百0-9]{1,4}[章幕节]')
_LABELS = {
    'start': ('START',),
    'wake_up': ('开始唤醒',),
    'terminal': ('终端',),
    'operators': ('干员',),
    'base': ('基建',),
    'home_shop': ('采购中心',),
    'home_recruit': ('公开招募',),
    'home_depot': ('仓库',),
    'chapter_entry': ('进入', '进入活动', '活动关卡', '关卡选择', '章节', '开始探索', '前往行动'),
    'start_action': ('开始行动',),
    'detail_hint': ('详情', '敌方情报', '演习', '代理指挥', '推荐平均等级'),
    'squad': ('编队', '确认编队', '快捷编队'),
    'settlement_end': ('行动结束', '任务完成'),
    'settlement_reward': ('结算', '作战奖励', '获得奖励', '信赖提升'),
    'login': ('验证码', '输入密码', '请输入密码'),
    'dialog': ('公告', '活动公告', '签到', '每日签到'),
    'close': ('关闭', '关闭公告', '关闭窗口', '知道了'),
    'difficulty_label': ('普通', '标准', '突袭', '磨难'),
}


def _normal(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text).translate(_DASHES)).upper()


def _valid_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _validated_words(ocr):
    if not isinstance(ocr, dict):
        return None
    width, height = ocr.get('width'), ocr.get('height')
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION):
        return None
    words = ocr.get('words')
    if not isinstance(words, list) or len(words) > MAX_WORDS:
        return None
    total = 0
    for word in words:
        if not isinstance(word, dict):
            return None
        text, rect = word.get('text'), word.get('rect')
        if not isinstance(text, str) or not text.strip() or len(text) > 512 or not isinstance(rect, dict):
            return None
        total += len(text)
        if total > 65536:
            return None
        x, y, w, h = (rect.get(key) for key in ('x', 'y', 'width', 'height'))
        if (not all(_valid_number(value) for value in (x, y, w, h))
                or w <= 0 or h <= 0 or x + w > width or y + h > height):
            return None
    return words


def _lines(words):
    """Join nearby OCR character boxes, retaining every source-word index."""
    rows = []
    for index in sorted(range(len(words)), key=lambda i: (words[i]['rect']['y'], words[i]['rect']['x'])):
        box = words[index]['rect']
        center = box['y'] + box['height'] / 2
        row = next((row for row in rows if abs(center - row['center']) <= min(box['height'], row['height']) * .4), None)
        if row is None:
            row = {'center': center, 'height': box['height'], 'indices': []}
            rows.append(row)
        row['indices'].append(index)
    for row in rows:
        text, positions, previous = '', [], None
        for index in sorted(row['indices'], key=lambda i: words[i]['rect']['x']):
            box = words[index]['rect']
            if previous is not None:
                gap = box['x'] - previous['x'] - previous['width']
                if gap > max(4, min(box['height'], previous['height']) * .75):
                    text += ' '
                    positions.append(None)
            normalized = _normal(words[index]['text'])
            text += normalized
            positions.extend([index] * len(normalized))
            previous = box
        yield text, positions


def _candidate(kind, match, positions, words):
    indices = list(dict.fromkeys(index for index in positions[match.start():match.end()] if index is not None))
    if not indices:
        return None
    actual = ''.join(words[index]['text'] for index in indices)
    # A substring's box cannot be recovered from a larger OCR word. Do not
    # export unrelated adjacent text or invent a more precise rectangle.
    if _normal(actual) != _normal(match.group()):
        return None
    boxes = [words[index]['rect'] for index in indices]
    x, y = min(box['x'] for box in boxes), min(box['y'] for box in boxes)
    right = max(box['x'] + box['width'] for box in boxes)
    bottom = max(box['y'] + box['height'] for box in boxes)
    return {'kind': kind, 'text': actual, 'rect': {'x': x, 'y': y, 'width': right - x, 'height': bottom - y}}


def _explicit(target, name):
    value = target.get(name) if isinstance(target, dict) else None
    return _normal(value) if isinstance(value, str) and 0 < len(value.strip()) <= 80 else ''


def classify_page(ocr, target=None) -> dict:
    """Return only defensible page signatures and measured text candidates.

    ``target`` may contain an explicit ``activity_name``, ``stage_code`` and
    ``difficulty``. Difficulty remains unverified even when its label is visible.
    Conflicting signatures or an incomplete signature yield ``unknown``.
    """
    result = {'page_type': 'unknown', 'candidates': [], 'evidence': [],
              'target_stage_visible': False, 'difficulty_status': 'unverified',
              'read_only': True, 'actionable': False, 'coordinate_space': 'image',
              'width': None, 'height': None, 'reason': 'invalid_ocr', 'possible_pages': []}
    words = _validated_words(ocr)
    if words is None:
        return result
    result.update(width=ocr['width'], height=ocr['height'], reason='insufficient_evidence')
    activity, stage = _explicit(target, 'activity_name'), _explicit(target, 'stage_code')
    if not re.fullmatch(_STAGE_BODY, stage):
        stage = ''
    patterns = []
    for kind, labels in _LABELS.items():
        for label in labels:
            expression = re.escape(_normal(label))
            if kind == 'start':
                expression = r'(?<![A-Z0-9_-])' + expression + r'(?![A-Z0-9_-])'
            patterns.append((kind, re.compile(expression)))
    patterns.extend([('stage', _STAGES), ('chapter_entry', _CHAPTER)])
    if activity:
        patterns.append(('activity', re.compile(re.escape(activity))))
    candidates, seen = [], set()
    for text, positions in _lines(words):
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                item = _candidate(kind, match, positions, words)
                if item is None:
                    continue
                if kind == 'stage':
                    code = _normal(item['text'])
                    if code.startswith(('UID-', 'ID-')):
                        continue
                    item['is_target'] = bool(stage and code == stage)
                key = (kind, tuple(item['rect'].values()), item['text'])
                if key not in seen:
                    seen.add(key)
                    candidates.append(item)
                if len(candidates) > 64:
                    result['reason'] = 'candidate_limit_exceeded'
                    return result
    candidates.sort(key=lambda item: (item['rect']['y'], item['rect']['x'], item['kind']))
    result['candidates'] = candidates
    result['evidence'] = [dict(item) for item in candidates if item['kind'] not in ('stage', 'activity', 'difficulty_label')]
    kinds = {item['kind'] for item in candidates}
    result['target_stage_visible'] = any(item.get('is_target') for item in candidates)
    signatures = set()
    for kind in ('start', 'wake_up'):
        if kind in kinds:
            signatures.add(kind)
    home_primary = kinds & {'terminal', 'operators', 'base'}
    if (len(home_primary) >= 2 or
            home_primary and kinds & {'home_shop', 'home_recruit', 'home_depot'}):
        signatures.add('home')
    if {'activity', 'chapter_entry'} <= kinds:
        signatures.add('event_overview')
    if 'start_action' in kinds:
        if 'squad' in kinds:
            signatures.add('squad')
        elif result['target_stage_visible']:
            signatures.add('stage_detail')
    if {'settlement_end', 'settlement_reward'} <= kinds:
        signatures.add('settlement')
    if ('stage' in kinds and 'start_action' not in kinds and 'detail_hint' not in kinds
            and 'settlement' not in signatures):
        signatures.add('stage_map')
    if 'login' in kinds:
        signatures.add('login_required')
    # Home shortcuts can say "签到" or "公告". A separate visible dismiss
    # label corroborates a dialog; an unlabelled X is never synthesized.
    if {'dialog', 'close'} <= kinds:
        signatures.add('dialog')
    result['possible_pages'] = sorted(signatures)
    if len(signatures) == 1:
        result['page_type'] = next(iter(signatures))
        result['reason'] = 'matched_read_only_signature'
    elif len(signatures) > 1:
        result['reason'] = 'conflicting_page_signatures'
    return result


def _overlap(a, b):
    area = (max(0, min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x'])) *
            max(0, min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y'])))
    return area / (a['width'] * a['height'] + b['width'] * b['height'] - area)


def _classify_regions(recognized, target):
    """Fuse only complete measured labels, never entire account transcripts."""
    unknown = classify_page({}, target)
    if not isinstance(recognized, dict):
        return unknown
    width, height = recognized.get('width'), recognized.get('height')
    if _validated_words({'width': width, 'height': height, 'words': []}) is None:
        return unknown
    regions = recognized.get('regions')
    if not isinstance(regions, list) or len(regions) != 2:
        return unknown
    words = []
    for region in regions:
        if not isinstance(region, dict):
            return unknown
        observation, origin, scale = (region.get(key) for key in ('observation', 'origin', 'scale'))
        if _validated_words(observation) is None or not isinstance(origin, dict) or not isinstance(scale, dict):
            return unknown
        x, y, sx, sy = origin.get('x'), origin.get('y'), scale.get('x'), scale.get('y')
        if (not all(_valid_number(value) for value in (x, y, sx, sy)) or min(sx, sy) <= 0
                or max(sx, sy) > 2 or x >= width or y >= height
                or observation['width'] / sx + x > width + .01
                or observation['height'] / sy + y > height + .01):
            return unknown
        candidates = classify_page(observation, target)['candidates']
        for candidate in candidates:
            box = candidate['rect']
            projected = {'x': x + box['x'] / sx, 'y': y + box['y'] / sy,
                         'width': box['width'] / sx, 'height': box['height'] / sy}
            if any(_normal(word['text']) == _normal(candidate['text']) and _overlap(word['rect'], projected) >= .3 for word in words):
                continue
            words.append({'text': candidate['text'], 'rect': projected})
    return classify_page({'width': width, 'height': height, 'words': words}, target)


class GameNavigation:
    def __init__(self, *, ocr=None):
        self._ocr = ocr if ocr is not None else LocalOcr()

    def observe(self, image_path, target=None, cancel_event=None) -> dict:
        if cancel_event is not None and cancel_event.is_set():
            raise LocalOcrError('ocr_cancelled')
        navigation = getattr(self._ocr, 'recognize_navigation', None)
        recognized = (navigation(image_path, cancel_event=cancel_event) if callable(navigation)
                      else self._ocr.recognize(image_path, cancel_event=cancel_event))
        if cancel_event is not None and cancel_event.is_set():
            raise LocalOcrError('ocr_cancelled')
        return _classify_regions(recognized, target) if callable(navigation) else classify_page(recognized, target)

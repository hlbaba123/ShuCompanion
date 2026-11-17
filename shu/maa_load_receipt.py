"""Fresh, read-only MAA application evidence; no GUI, model or game access.

The caller supplies freshly checked native process identities and separately
verifies screenshots/input receipts. This module never claims visual title
recognition: its exact title channel is MAA's own new log and loaded cache.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import time


_MAA_EXECUTABLE = r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe'
_MAA_ROOT = Path(_MAA_EXECUTABLE).parent
_LOG_PATH = _MAA_ROOT / 'debug' / 'gui.log'
_CACHE_PATH = _MAA_ROOT / 'cache' / '_temp_copilot.json'
_MAX_DOCUMENT = 8 * 1024 * 1024
_MAX_LOG = 2 * 1024 * 1024
_MAX_LOG_PREFIX = 64 * 1024 * 1024
_MAX_WINDOW = 180
_CODE = re.compile(r'prts://([1-9][0-9]{0,11})\Z')
_PREFIX = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\]'
                     r'\[([A-Z]+)\]\[([^\]\r\n]+)\][ \t]*<(\d+)> ?(.*)$')
_FAKE_PREFIX = re.compile(r'^\[\d{4}-\d\d-\d\d[ T]\d\d:\d\d:', re.M)
_GET = re.compile(r'^HTTP: (\S+) GET https://prts\.maa\.plus/copilot/get/'
                  r'([1-9][0-9]{0,11})(?:[ \t].*)?$')
_COUNT = re.compile(r'^共\s*([1-9][0-9]?)\s*名干员$')
_DTO_BASE = ('https://raw.githubusercontent.com/MaaAssistantArknights/MaaAssistantArknights/'
             'v6.21.4/src/MaaWpfGui/')
_DTO_SOURCES = [_DTO_BASE + 'Models/Copilot/CopilotBase.cs',
                _DTO_BASE + 'Models/Copilot/CopilotModel.cs',
                _DTO_BASE + 'ViewModels/UI/CopilotViewModel.cs']


class MaaLoadReceiptError(RuntimeError):
    def __init__(self, code):
        self.code = str(code) if re.fullmatch(r'[a-z_]{1,60}', str(code)) else 'receipt_invalid'
        super().__init__('MAA 应用回执未核实（' + self.code + '）。')


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite')))


def _identity(target):
    if (not isinstance(target, dict) or any(type(target.get(key)) is not int or target[key] <= 0
                                          for key in ('hwnd', 'pid', 'creation_time'))
            or not isinstance(target.get('executable'), str)
            or ntpath.normcase(ntpath.normpath(target['executable'])) != ntpath.normcase(_MAA_EXECUTABLE)):
        raise MaaLoadReceiptError('binding_changed')
    return {key: target[key] for key in ('hwnd', 'pid', 'creation_time', 'executable')}


def _canonical(document):
    """Preserve all fields; normalize only observed, documented defaults.

    Local protocol/copilot-schema.md specifies skill_usage=0,
    requirements.module=-1 and action.type=Deploy. The MAA cache also
    materializes an absent, empty groups list. Fixed v6.21.4 CopilotBase /
    CopilotModel have no version property, and CopilotViewModel serializes
    that typed DTO to the cache (lines 1349-1350). Only the observed station
    metadata version=3 may be absent there; unknown versions are rejected.
    No other unknown field is dropped.
    """
    try:
        raw = _bytes(document)
        if len(raw) > _MAX_DOCUMENT:
            raise ValueError('document limit')
        value = _json(raw)
        if (not isinstance(value, dict) or not isinstance(value.get('stage_name'), str)
                or not value['stage_name'] or not isinstance(value.get('doc'), dict)
                or not isinstance(value['doc'].get('title'), str) or not value['doc']['title']
                or not isinstance(value['doc'].get('details'), str)
                or not isinstance(value.get('opers'), list)
                or not isinstance(value.get('actions'), list) or not value['actions']):
            raise ValueError('document shape')
        if 'version' in value:
            if type(value['version']) is not int or value['version'] != 3:
                raise MaaLoadReceiptError('unsupported_source_version')
            del value['version']
        title, details = value['doc']['title'], value['doc']['details']
        if (len(title) > 1000 or len(details) > 200000 or title != title.strip()
                or '\r' in title or '\n' in title or '\r' in details
                or _FAKE_PREFIX.search(title) or _FAKE_PREFIX.search(details)
                or any(ord(char) < 32 and char not in '\n\t' for char in title + details)):
            raise ValueError('unsafe log text')
        value.setdefault('groups', [])
        if not isinstance(value['groups'], list):
            raise ValueError('groups shape')
        operators = list(value['opers'])
        for group in value['groups']:
            if not isinstance(group, dict) or not isinstance(group.get('opers'), list):
                raise ValueError('group shape')
            operators.extend(group['opers'])
        for operator in operators:
            if not isinstance(operator, dict) or not isinstance(operator.get('name'), str) or not operator['name']:
                raise ValueError('operator shape')
            operator.setdefault('skill_usage', 0)
            # CopilotModel.Oper.SkillTimes has [DefaultValue(1)] and the GUI
            # writes DefaultValueHandling.Ignore. Omission and explicit integer
            # 1 are identical; retain every nondefault/unknown field and type.
            if type(operator.get('skill_times')) is int and operator['skill_times']==1:
                del operator['skill_times']
            requirements = operator.get('requirements')
            if requirements is not None:
                if not isinstance(requirements, dict):
                    raise ValueError('requirements shape')
                requirements.setdefault('module', -1)
                # Typed MAA Requirements uses zero defaults; its cache is
                # serialized with DefaultValueHandling.Ignore (e.g. Lancet-2
                # elite=0 is omitted). Only these documented fields normalize.
                for field in ('elite','level','skill_level','potentiality'):
                    requirements.setdefault(field,0)
        for action in value['actions']:
            if not isinstance(action, dict):
                raise ValueError('action shape')
            action.setdefault('type', 'Deploy')
            # CopilotModel.Action.SkillTimes uses the same explicit default.
            if type(action.get('skill_times')) is int and action['skill_times']==1:
                del action['skill_times']
            # These Action int properties use CLR's default 0 and are omitted
            # by the pinned GUI serializer. Cooling instead defaults to -1,
            # so an explicit cooling=0 remains significant and is retained.
            for field in ('skill_usage','pre_delay'):
                if type(action.get(field)) is int and action[field]==0:
                    del action[field]
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, KeyError):
        raise MaaLoadReceiptError('source_invalid') from None


def _cache_body(document):
    """Compare typed MAA cache defaults without changing source fingerprints.

    Fixed 901e01c CopilotModel.Action uses CLR int zero for Kills, Costs and
    CostChanges (lines 228-241); CopilotViewModel writes the typed DTO with
    DefaultValueHandling.Ignore (line 1452). Only exact integer zero and an
    omitted one of these three conditions are equivalent. Keep nonzero,
    mistyped, unknown and every other action field (notably cooling=0).
    https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/901e01c600f4895ac4e7e48a9b471fc9f52af301/src/MaaWpfGui/Models/Copilot/CopilotModel.cs
    """
    value = _canonical(document)
    for action in value['actions']:
        for field in ('kills', 'costs', 'cost_changes'):
            if type(action.get(field)) is int and action[field] == 0:
                del action[field]
    return value


def _schema_metadata(document):
    return {'maa_gui_version': 'v6.21.4', 'source_version': document.get('version'),
            'station_version_rule': 'only integer version=3 is nonexecution station metadata',
            'dto_sources': list(_DTO_SOURCES)}


def _elite_load_metadata(source):
    """Expected load-time changes, never applied to the observed cache.

    Audited v6.21.4 and v6.22.0 CopilotViewModel.ParseCopilot: infer the
    minimum elite from skill, skill level and module. An exact corresponding
    EliteEmpty/EliteAdjust message is required before allowing this variant.
    Other parser corrections (skills/actions/aliases) remain unsupported.
    """
    value, messages = copy.deepcopy(source), []
    operators = list(value['opers'])
    for group in value['groups']:
        operators.extend(group['opers'])
    for operator in operators:
        skill, requirements = operator.get('skill', 0), operator.get('requirements')
        if type(skill) is not int or not 0 <= skill <= 3:
            raise MaaLoadReceiptError('unsupported_load_normalization')
        level = 0 if requirements is None else requirements.get('skill_level', 0)
        module = -1 if requirements is None else requirements.get('module', -1)
        elite = 0 if requirements is None else requirements.get('elite', 0)
        # Fixed v6.22.0 CopilotModel.GetModuleInfo and protocol/copilot-schema.md
        # enumerate 1..5 (chi, gamma, alpha, Delta, beta). This is the module
        # type, not its enhancement level. Keep the value in the exact body;
        # ParseCopilotAsync only uses Module > 0 to infer elite 2.
        # https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.22.0/src/MaaWpfGui/Models/Copilot/CopilotModel.cs
        # https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.22.0/docs/zh-cn/protocol/copilot-schema.md
        if (any(type(n) is not int for n in (level, module, elite))
                or not 0 <= level <= 10 or not -1 <= module <= 5 or not 0 <= elite <= 2):
            raise MaaLoadReceiptError('unsupported_load_normalization')
        inferred = max(skill - 1, 0 if level <= 4 else 1 if level <= 7 else 2,
                       2 if module > 0 else 0)
        if inferred <= elite:
            continue
        name = operator['name']
        if any(ord(char) < 32 for char in name):
            raise MaaLoadReceiptError('unsupported_load_normalization')
        if requirements is None:
            operator['requirements'] = {'elite': inferred, 'module': -1}
            messages.append(f'{name} 未设置精英等级要求, 但其他属性要求 精{inferred}, 已自动补全')
        else:
            messages.append(f'{name} 设置为要求 精{elite}, 但是其他属性要求 精{inferred}, 已自动调整')
            requirements['elite'] = inferred
    return _canonical(value), messages


def _load_metadata(source):
    """Predict audited parser corrections on the source, never on observations.

    CopilotViewModel.ParseCopilot (v6.21.4 and 901e01c...) processes elite
    requirements before four positional action types. For these actions a
    location takes precedence over a simultaneously supplied operator/name;
    the parser removes Name and Role and emits one exact WRN per action.
    Every other action field and every unknown field remains significant.
    """
    value, messages = _elite_load_metadata(source)
    action_corrected = False
    for action in value['actions']:
        if (action['type'] not in {'Skill', 'Retreat', 'BulletTime', 'SkillUsage'}
                or action.get('location') is None or action.get('name') is None):
            continue
        location, name, role = action['location'], action['name'], action.get('role')
        if (not isinstance(location, list) or len(location) != 2
                or any(type(number) is not int for number in location)
                or not isinstance(name, str) or role is not None and not isinstance(role, str)):
            raise MaaLoadReceiptError('unsupported_load_normalization')
        position = ','.join(str(number) for number in location)
        messages.append(f"{action['type']}[{position}] 同时设置了位置和干员, 将按位置执行")
        del action['name']
        action.pop('role', None)
        action_corrected = True
    normalization = ('maa_parser_corrections_v2' if action_corrected
                     else 'maa_elite_requirements_v1' if messages else None)
    corrected = action_corrected or any(message.endswith('已自动调整') for message in messages)
    return _canonical(value), messages, normalization, corrected


def _state(stat):
    if not stat.st_ino:
        raise MaaLoadReceiptError('file_identity_unavailable')
    return {'file_identity': {'device': stat.st_dev, 'inode': stat.st_ino},
            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _prefix_hash(stream, length):
    if type(length) is not int or not 0 <= length <= _MAX_LOG_PREFIX:
        raise MaaLoadReceiptError('log_baseline_limit')
    stream.seek(0)
    digest, remaining = hashlib.sha256(), length
    while remaining:
        chunk = stream.read(min(65536, remaining))
        if not chunk:
            raise MaaLoadReceiptError('log_prefix_changed')
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _publish(directory, kind, value):
    directory = Path(directory).resolve()
    if directory == _MAA_ROOT.resolve() or directory.is_relative_to(_MAA_ROOT.resolve()):
        raise MaaLoadReceiptError('evidence_directory_invalid')
    directory.mkdir(parents=True, exist_ok=True)
    raw = _bytes(value)
    digest = _hash(raw)
    path = directory / (kind + '-' + digest + '.json')
    try:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _hash(path.read_bytes()) != digest:
            raise MaaLoadReceiptError('evidence_changed') from None
    return {'path': str(path), 'sha256': digest}


def capture_baseline(target_identity, source_document, code, directory):
    """Call immediately before the one authorized replace_code operation.

    Only the two fixed MAA application paths are read. The returned value is
    JSON serializable; the original document is retained without corrections.
    Native identity discovery and exclusive input ownership belong to caller.
    """
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise MaaLoadReceiptError('code_invalid')
    identity, source = _identity(target_identity), _canonical(source_document)
    started = time.time()
    try:
        with _LOG_PATH.open('rb') as stream:
            state = _state(os.fstat(stream.fileno()))
            prefix_sha256 = _prefix_hash(stream, state['size'])
            stream.seek(max(0, state['size'] - 1))
            last = stream.read(1)
        if _state(_LOG_PATH.stat()) != state:
            raise MaaLoadReceiptError('log_changed_during_baseline')
    except OSError:
        raise MaaLoadReceiptError('log_unreadable') from None
    try:
        old_cache = _read_cache()
        cache_before = {key: old_cache[key] for key in ('file_identity', 'size', 'mtime_ns', 'sha256')}
        cache_before['exists'] = True
    except FileNotFoundError:
        cache_before = {'exists': False}
    except (OSError, MaaLoadReceiptError):
        raise MaaLoadReceiptError('cache_baseline_unreadable') from None
    baseline = {'schema': 'maa_load_baseline/v1', 'code': code, 'target_identity': identity,
                'started_at': started, 'source_document': copy.deepcopy(source_document),
                'source_semantic_sha256': _hash(_bytes(source)),
                'schema_metadata': _schema_metadata(source_document),
                'log': {**state, 'path': str(_LOG_PATH.resolve()), 'offset': state['size'],
                        'prefix_sha256': prefix_sha256,
                        'ended_with_newline': not state['size'] or last == b'\n'},
                'cache_path': str(_CACHE_PATH.resolve()), 'cache_before': cache_before}
    baseline['evidence_ref'] = _publish(directory, 'maa-load-baseline', baseline)
    return baseline


def _read_log(baseline):
    with _LOG_PATH.open('rb') as stream:
        state = _state(os.fstat(stream.fileno()))
        if state['file_identity'] != baseline['log']['file_identity']:
            raise MaaLoadReceiptError('log_rotated')
        offset = baseline['log']['offset']
        if state['size'] < offset:
            raise MaaLoadReceiptError('log_truncated')
        if state['size'] - offset > _MAX_LOG:
            raise MaaLoadReceiptError('log_limit')
        prefix_sha256 = _prefix_hash(stream, offset)
        if prefix_sha256 != baseline['log']['prefix_sha256']:
            raise MaaLoadReceiptError('log_prefix_changed')
        stream.seek(offset)
        raw = stream.read(_MAX_LOG + 1)
        after = _state(os.fstat(stream.fileno()))
    if (len(raw) > _MAX_LOG or after['file_identity'] != state['file_identity']
            or after['size'] < offset + len(raw)
            or _state(_LOG_PATH.stat())['file_identity'] != state['file_identity']):
        raise MaaLoadReceiptError('log_changed')
    return {**after, 'offset': offset, 'end_offset': offset + len(raw), 'prefix_sha256': prefix_sha256,
            'bytes_b64': base64.b64encode(raw).decode('ascii'), 'sha256': _hash(raw)}


def _read_cache():
    with _CACHE_PATH.open('rb') as stream:
        before = _state(os.fstat(stream.fileno()))
        if not 0 < before['size'] <= _MAX_DOCUMENT:
            raise MaaLoadReceiptError('cache_unreadable')
        raw = stream.read(_MAX_DOCUMENT + 1)
        after = _state(os.fstat(stream.fileno()))
    if before != after or after != _state(_CACHE_PATH.stat()) or len(raw) != before['size']:
        raise MaaLoadReceiptError('cache_changed')
    return {**after, 'bytes_b64': base64.b64encode(raw).decode('ascii'), 'sha256': _hash(raw)}


def _result(status, code, **facts):
    return {'status': status, 'verified': status == 'verified', 'code': code, **facts}


def _decode(item, maximum):
    raw = base64.b64decode(item['bytes_b64'], validate=True)
    if len(raw) > maximum or _hash(raw) != item['sha256']:
        raise MaaLoadReceiptError('receipt_bytes_changed')
    return raw


def _log_sequence(raw, baseline, captured_at, source):
    text = raw.decode('utf-8')
    if not baseline['log']['ended_with_newline']:
        text = text.partition('\n')[2]
    if not text or not text.endswith('\n'):
        return _result('pending', 'log_incomplete')
    lines = text.split('\n')[:-1]
    lines = [line[:-1] if line.endswith('\r') else line for line in lines]
    parsed, requests = {}, []
    for index, line in enumerate(lines):
        match = _PREFIX.fullmatch(line)
        if not match:
            continue
        stamp, level, logger, thread, message = match.groups()
        moment = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f').timestamp()
        parsed[index] = (moment, level, logger, thread, message)
        if logger == 'HttpResponseLoggingExtension' and '/copilot/get/' in message:
            get = _GET.fullmatch(message)
            if not get:
                return _result('rejected', 'request_format_invalid')
            requests.append((index, get, moment, thread))
    if not requests:
        return _result('pending', 'request_not_seen')
    if len(requests) != 1:
        return _result('rejected', 'multiple_job_requests')
    start, get, moment, thread = requests[0]
    if (parsed[start][1] != 'INF' or get.group(1) != 'OK'
            or get.group(2) != _CODE.fullmatch(baseline['code']).group(1)):
        return _result('rejected', 'job_request_mismatch')
    if not baseline['started_at'] - .002 <= moment <= captured_at + .002:
        return _result('rejected', 'log_time_outside_input_window')
    for index, event in parsed.items():
        if index <= start or event[2] not in {'CopilotViewModel', 'HttpResponseLoggingExtension'}:
            continue
        if event[1] in {'ERR', 'FTL', 'FATAL'}:
            return _result('rejected', 'load_error_after_request')
        if event[0] < moment or event[0] > captured_at + .002:
            return _result('rejected', 'log_time_outside_input_window')

    cursor, used = start + 1, []
    _, warnings, expected_normalization, _ = _load_metadata(source)
    warning_levels = {message:('INF' if message.endswith('已自动补全') else 'WRN')
                      for message in warnings}

    def next_message(*, normalization=False):
        nonlocal cursor
        while cursor < len(lines):
            index, cursor = cursor, cursor + 1
            event = parsed.get(index)
            if event is not None and event[2] == 'CopilotViewModel':
                expected_level = warning_levels.get(event[4],'INF') if normalization else 'INF'
                if event[1] != expected_level or event[3] != thread:
                    raise MaaLoadReceiptError('metadata_identity_mismatch')
                used.append(index)
                return event[4]
            if event is None and lines[index]:
                raise MaaLoadReceiptError('unexpected_log_continuation')
        return None

    title = next_message(normalization=True)
    if title is None:
        return _result('pending', 'metadata_incomplete')
    normalization = None
    if title != source['doc']['title']:
        if warnings and title == warnings[0]:
            for warning in warnings[1:]:
                message = next_message(normalization=True)
                if message is None:
                    return _result('pending', 'metadata_incomplete')
                if message != warning:
                    return _result('rejected', 'load_normalization_mismatch')
            normalization = expected_normalization
            title = next_message()
            if title is None:
                return _result('pending', 'metadata_incomplete')
    if title != source['doc']['title']:
        return _result('rejected', 'exact_title_mismatch')
    details = source['doc']['details'].split('\n')
    first = next_message()
    if first is None:
        return _result('pending', 'metadata_incomplete')
    if first != details[0]:
        return _result('rejected', 'exact_details_mismatch')
    for continuation in details[1:]:
        if cursor >= len(lines):
            return _result('pending', 'metadata_incomplete')
        if lines[cursor] != continuation or cursor in parsed:
            return _result('rejected', 'exact_details_mismatch')
        used.append(cursor)
        cursor += 1
    separator = next_message()
    if separator is None:
        return _result('pending', 'metadata_incomplete')
    if not re.fullmatch(r'-{8,}', separator):
        return _result('rejected', 'metadata_separator_mismatch')
    count = next_message()
    if count is None:
        return _result('pending', 'metadata_incomplete')
    match = _COUNT.fullmatch(count)
    expected_count = len(source['opers']) + len(source['groups'])
    if not match or int(match.group(1)) != expected_count:
        return _result('rejected', 'roster_count_mismatch')
    if any(index >= cursor and event[2] == 'CopilotViewModel' for index, event in parsed.items()):
        return _result('rejected', 'interleaved_load_metadata')
    return _result('verified', 'maa_load_verified', operator_count=expected_count,
                   log_request_line=start, log_metadata_lines=used,
                   cache_normalization=normalization)


def verify_receipt(payload, expected):
    """Pure offline validation of bytes inside one trusted artifact.

    expected requires code/source_document, and may additionally bind the
    target_identity already checked against the caller's screenshot evidence.
    Artifact-reference hashes/provenance and screenshot/input checks remain
    the caller's responsibility; this function never substitutes for them.
    """
    try:
        if not isinstance(payload, dict) or payload.get('schema') != 'maa_load_receipt/v1':
            raise MaaLoadReceiptError('receipt_invalid')
        baseline = payload['baseline']
        if (baseline.get('schema') != 'maa_load_baseline/v1' or expected['code'] != baseline['code']
                or not _CODE.fullmatch(baseline['code'])):
            raise MaaLoadReceiptError('receipt_binding_mismatch')
        identity = _identity(baseline['target_identity'])
        if (_identity(payload['current_identity']) != identity
                or ('target_identity' in expected and _identity(expected['target_identity']) != identity)):
            raise MaaLoadReceiptError('binding_changed')
        source, expected_source = _canonical(baseline['source_document']), _canonical(expected['source_document'])
        if (_bytes(source) != _bytes(expected_source)
                or baseline['source_semantic_sha256'] != _hash(_bytes(expected_source))
                or baseline['schema_metadata'] != _schema_metadata(baseline['source_document'])
                or baseline['schema_metadata'] != _schema_metadata(expected['source_document'])):
            raise MaaLoadReceiptError('source_body_changed')
        now, started = payload['captured_at'], baseline['started_at']
        if (type(now) not in (int, float) or type(started) not in (int, float)
                or not math.isfinite(now) or not math.isfinite(started) or not 0 <= now - started <= _MAX_WINDOW):
            raise MaaLoadReceiptError('receipt_window_expired')
        if (baseline['log']['path'] != str(_LOG_PATH.resolve())
                or baseline['cache_path'] != str(_CACHE_PATH.resolve())):
            raise MaaLoadReceiptError('receipt_path_mismatch')
        log = payload['log']
        raw = _decode(log, _MAX_LOG)
        if (log['file_identity'] != baseline['log']['file_identity']
                or type(baseline['log']['offset']) is not int or baseline['log']['offset'] < 0
                or baseline['log']['size'] != baseline['log']['offset']
                or log['prefix_sha256'] != baseline['log']['prefix_sha256']
                or log['offset'] != baseline['log']['offset']
                or log['end_offset'] != log['offset'] + len(raw) or log['size'] < log['end_offset']):
            raise MaaLoadReceiptError('log_identity_or_offset_changed')
        log_state = {key: log[key] for key in ('file_identity', 'size', 'mtime_ns')}
        if log['size'] != log['end_offset'] or payload['log_final_state'] != log_state:
            return _result('pending', 'log_changed_during_read')
        sequence = _log_sequence(raw, baseline, now, source)
        if not sequence['verified']:
            return sequence
        cache = payload.get('cache')
        if cache is None:
            return _result('pending', 'cache_unavailable')
        cache_raw = _decode(cache, _MAX_DOCUMENT)
        if payload['cache_final_state'] != {key: cache[key] for key in ('file_identity', 'size', 'mtime_ns')}:
            return _result('pending', 'cache_changed_during_read')
        old_cache = baseline['cache_before']
        if type(old_cache.get('exists')) is not bool:
            raise MaaLoadReceiptError('cache_baseline_invalid')
        if (old_cache['exists'] and all(cache[key] == old_cache[key]
                                       for key in ('file_identity', 'size', 'mtime_ns', 'sha256'))):
            return _result('pending', 'cache_generation_not_advanced')
        cache_document = _json(cache_raw)
        expected_cache = source
        if sequence.get('cache_normalization') in {'maa_elite_requirements_v1', 'maa_parser_corrections_v2'}:
            expected_cache, _, _, _ = _load_metadata(source)
        if (cache['size'] != len(cache_raw)
                or _bytes(_cache_body(cache_document)) != _bytes(_cache_body(expected_cache))):
            raise MaaLoadReceiptError('source_body_changed')
        return {**sequence, 'title_verified_by': 'maa_log_cache', 'mystery_code': baseline['code'],
                'target_identity': identity, 'source_semantic_sha256': baseline['source_semantic_sha256'],
                'schema_metadata': {**baseline['schema_metadata'], 'cache_version': cache_document.get('version')}}
    except MaaLoadReceiptError as error:
        return _result('rejected', error.code)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        return _result('rejected', 'receipt_invalid')


def collect_receipt(baseline, current_identity, directory):
    """Read a bounded appended log slice and stable cache; publish provenance.

    pending never authorizes a retry of input. Callers may poll these read-only
    files after their same single input, within the original 180 second window.
    """
    try:
        identity = _identity(current_identity)
        if identity != _identity(baseline['target_identity']):
            raise MaaLoadReceiptError('binding_changed')
        payload = {'schema': 'maa_load_receipt/v1', 'baseline': copy.deepcopy(baseline),
                   'current_identity': identity, 'log': _read_log(baseline),
                   'cache': None, 'cache_final_state': None}
        try:
            payload['cache'] = _read_cache()
        except (OSError, MaaLoadReceiptError):
            pass
        payload['log_final_state'] = _state(_LOG_PATH.stat())
        if payload['cache'] is not None:
            try:
                payload['cache_final_state'] = _state(_CACHE_PATH.stat())
            except OSError:
                pass
        payload['captured_at'] = time.time()
        result = verify_receipt(payload, {'code': baseline['code'],
                                         'source_document': baseline['source_document'],
                                         'target_identity': identity})
        ref = _publish(directory, 'maa-load-receipt', payload)
        return {**result, 'receipt_ref': ref,
                'evidence_refs': [baseline['evidence_ref'], ref]}
    except MaaLoadReceiptError as error:
        return _result('rejected', error.code, evidence_refs=[])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return _result('rejected', 'application_evidence_unreadable', evidence_refs=[])

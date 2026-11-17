"""Read an explicitly supplied roster table; never evaluate Excel formulas.

This is an offline user-data import, not a game scan or an account API. The
original workbook and a cell-addressed normalized snapshot remain immutable.
Missing cells stay unknown, and rows outside the supplied table stay unknown.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .maa_facts import _operator

_NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
       'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
_HEADERS = ('干员名称', '星级', '职业', '干员 ID', '分支', '是否已招募', '等级',
            '精英化等级', '潜能等级', '通用技能等级')
_MODULES = (('χ', '1'), ('γ', '2'), ('α', '3'), ('Δ', '4'), ('β', '5'))
_MAX_BYTES = 4 * 1024 * 1024
_MAX_XML = 16 * 1024 * 1024


def _xml(zipped, name):
    try:
        member = zipped.getinfo(name)
    except KeyError as exc:
        raise ValueError('干员表缺少所需工作簿内容') from exc
    if member.file_size > _MAX_XML:
        raise ValueError('干员表内容超出读取范围')
    raw = zipped.read(member)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('干员表包含不支持的 XML 定义')
    return ET.fromstring(raw)


def _number(value, low, high, label):
    if value is None or value == '':
        return None
    text = str(value).strip()
    if not re.fullmatch(r'\d+(?:\.0+)?', text):
        raise ValueError(f'{label}不是整数')
    number = int(text.split('.')[0])
    if not low <= number <= high:
        raise ValueError(f'{label}超出范围')
    return number


def _cells(row, strings):
    result = {}
    for cell in row.findall('s:c', _NS):
        address = cell.get('r', '')
        if not re.fullmatch(r'[A-Z]{1,3}\d{1,7}', address):
            raise ValueError('干员表单元格地址不正确')
        column = re.sub(r'\d+$', '', address)
        if column in result:
            raise ValueError('干员表含重复单元格')
        if cell.find('s:f', _NS) is not None:
            raise ValueError(f'{address}包含公式；请提供已导出的数值表')
        kind = cell.get('t')
        value = cell.findtext('s:v', '', _NS)
        if kind == 'inlineStr':
            value = ''.join(cell.find('s:is', _NS).itertext()) if cell.find('s:is', _NS) is not None else ''
        elif kind == 's':
            index = _number(value, 0, len(strings) - 1, '共享文本索引')
            if index is None:
                raise ValueError('干员表共享文本索引缺失')
            value = strings[index]
        elif kind not in (None, 'n', 'str'):
            raise ValueError(f'{address}使用不支持的单元格类型')
        result[column] = value.strip() if isinstance(value, str) else value
    return result


def parse_roster_xlsx(raw):
    """Return typed facts and source cells from one recognized worksheet.

    ``complete`` is deliberately false: even an all-operator export cannot
    prove that a later operator, missing from this file, is not owned.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_BYTES:
        raise ValueError('干员表文件为空或超过4MB')
    try:
        with ZipFile(io.BytesIO(raw)) as zipped:
            members = zipped.infolist()
            if (len(members) > 500 or len({m.filename for m in members}) != len(members)
                    or sum(m.file_size for m in members) > _MAX_XML * 2):
                raise ValueError('干员表容器内容异常')
            strings = []
            if 'xl/sharedStrings.xml' in zipped.namelist():
                strings = [''.join(item.itertext()) for item in _xml(zipped, 'xl/sharedStrings.xml').findall('s:si', _NS)]
            workbook = _xml(zipped, 'xl/workbook.xml')
            relationships = _xml(zipped, 'xl/_rels/workbook.xml.rels')
            targets = {item.get('Id'): item.get('Target') for item in relationships
                       if item.get('TargetMode') != 'External'}
            matches = []
            for sheet in workbook.findall('s:sheets/s:sheet', _NS):
                target = targets.get(sheet.get('{' + _NS['r'] + '}id'))
                if not isinstance(target, str):
                    continue
                path = PurePosixPath(target)
                if '..' in path.parts or '\\' in target:
                    raise ValueError('干员表工作表路径不正确')
                name = target.lstrip('/') if target.startswith('/') else str(PurePosixPath('xl') / path)
                if not name.startswith('xl/worksheets/') or not name.endswith('.xml'):
                    continue
                root = _xml(zipped, name)
                rows = root.findall('s:sheetData/s:row', _NS)
                if not rows:
                    continue
                header = _cells(rows[0], strings)
                if set(_HEADERS) <= set(header.values()):
                    matches.append((sheet.get('name'), rows, header))
            if len(matches) != 1:
                raise ValueError('需要且只能有一张包含干员名称、招募状态和练度列的表')
            sheet, rows, header = matches[0]
            if len(set(header.values())) != len(header):
                raise ValueError('干员表包含重复表头')
            columns = {value: key for key, value in header.items()}
            if not 1 < len(rows) <= 2001:
                raise ValueError('干员表记录数量超出范围')
            operators, refs = [], []
            for row in rows[1:]:
                cells = _cells(row, strings)
                if not any(cells.values()):
                    continue
                row_number = _number(row.get('r'), 1, 1048576, '行号')
                values = {label: cells.get(column, '') for label, column in columns.items()}
                owned_text = values['是否已招募']
                if owned_text not in ('是', '否', ''):
                    raise ValueError(f'第{row_number}行招募状态必须为是、否或空白')
                owned = {'是': True, '否': False, '': None}[owned_text]
                operator = {'id': values['干员 ID'] or None,
                            'name': values['干员名称'] or None,
                            'owned': owned, 'role': values['职业'] or None,
                            'rarity': _number(values['星级'], 1, 6, '星级'),
                            'mastery': {}, 'modules': {}}
                if owned is True:
                    for label, field, low, high in (
                        ('等级', 'level', 1, 100), ('精英化等级', 'elite', 0, 2),
                        ('潜能等级', 'potential', 1, 6), ('通用技能等级', 'skill_level', 1, 7)):
                        operator[field] = _number(values[label], low, high, f'第{row_number}行{label}')
                    for skill in range(1, 4):
                        label = f'{skill}技能专精等级'
                        if values.get(f'{skill}技能名称') and label in values:
                            value = _number(values[label], 0, 3, label)
                            if value is not None:
                                operator['mastery'][str(skill)] = value
                    # MAA v6.21.4: χ,γ,α,Δ,β -> 1,2,3,4,5, not Excel column order.
                    for symbol, branch in _MODULES:
                        label = symbol + '模组等级'
                        if values.get(symbol + '模组名称') and label in values:
                            value = _number(values[label], 0, 3, label)
                            if value is not None:
                                operator['modules'][branch] = value
                operators.append(_operator(operator))
                refs.append({'id': operator['id'], 'name': operator['name'],
                             'sheet': sheet, 'row': row_number,
                             'cells': {label: columns[label] + str(row_number) for label in columns},
                             'values': values})
    except (BadZipFile, ET.ParseError) as exc:
        raise ValueError('干员表不是可读取的XLSX文件') from exc
    identities = [operator['id'] or operator['name'] for operator in operators]
    if not operators or len(set(identities)) != len(identities):
        raise ValueError('干员表为空或包含重复干员身份')
    return {'format': 'shu-user-roster-xlsx-v1', 'sheet': sheet,
            'complete': False, 'explicit_ownership': True,
            'operators': operators, 'source_rows': refs}


def _immutable(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('已有干员资料与源文件不一致')
        return
    with path.open('xb') as stream:
        stream.write(raw)


def import_user_roster(source_path, tools, profile_id='local-default'):
    """Explicit developer/user import only; not an arbitrary agent file tool."""
    path = Path(source_path).resolve(strict=True)
    if path.suffix.casefold() != '.xlsx' or path.stat().st_size > _MAX_BYTES:
        raise ValueError('请选择4MB以内的XLSX干员表')
    raw = path.read_bytes()
    parsed = parse_roster_xlsx(raw)
    digest = hashlib.sha256(raw).hexdigest()
    directory = (tools.directory / 'roster-imports' / 'user-xlsx' / digest).resolve()
    if not directory.is_relative_to(tools.directory):
        raise ValueError('干员档案路径超出资料目录')
    directory.mkdir(parents=True, exist_ok=True)
    copy = directory / 'source.xlsx'
    normalized = directory / 'roster.json'
    _immutable(copy, raw)
    snapshot = dict(parsed, source={'path': str(path), 'sha256': digest,
                                   'file_modified_at': path.stat().st_mtime})
    if normalized.exists():
        prior = json.loads(normalized.read_text(encoding='utf-8'))
        if prior.get('source', {}).get('sha256') != digest or any(
                prior.get(key) != value for key, value in parsed.items()):
            raise ValueError('已有干员表归一化资料与原件不一致')
        snapshot = prior
    else:
        _immutable(normalized, json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                         indent=2).encode('utf-8'))
    source = {'kind': 'user-roster-spreadsheet', 'source_version': parsed['format'],
              'observed_at': snapshot['source']['file_modified_at'],
              'evidence_ref': str(copy) + '#sha256=' + digest,
              'scan_id': 'user-xlsx-' + digest}
    box = tools.facts.publish(profile_id, parsed['operators'], False, source,
                              explicit_ownership=True)
    counts = Counter('owned' if row['owned'] is True else 'unowned' if row['owned'] is False
                     else 'unknown' for row in parsed['operators'])
    receipt = {'profile_id': profile_id, 'batch_id': box['batch_id'],
               'box_revision': box['box_revision'], 'source_path': snapshot['source']['path'],
               'source_sha256': digest, 'archived_source': str(copy),
               'normalized': str(normalized), 'sheet': parsed['sheet'],
               'operator_count': len(parsed['operators']), 'ownership': dict(counts),
               'elite_counts_owned': dict(Counter(str(row['elite']) for row in parsed['operators'] if row['owned'] is True)),
               'roster_wide_complete': False,
               'account_binding': 'user_supplied_not_live_account_verified',
               'game_inputs': 0, 'network_requests': 0}
    _immutable(directory / 'import-receipt.json', json.dumps(receipt, ensure_ascii=False,
                    sort_keys=True, indent=2).encode('utf-8'))
    return receipt

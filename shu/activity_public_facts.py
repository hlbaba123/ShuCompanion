"""Join station members to public game-data facts, without touching the game.

Opaque IDs are never trimmed to guess difficulty. The public table explicitly
names NORMAL / FOUR_STAR and links their shared levelId. Cached source hashes
and extracted rows make the mapping independently inspectable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from urllib import request

BASE = 'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel/'
MAX_BYTES = 32 * 1024 * 1024
TTL = 3600


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _publish(directory, value):
    directory.mkdir(parents=True, exist_ok=True)
    raw = _encode(value)
    digest = hashlib.sha256(raw).hexdigest()
    target = directory / (digest + '.json')
    temporary = directory / (uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    return {'path': str(target), 'sha256': digest}


def _hashed_reference(value):
    return (isinstance(value, dict) and isinstance(value.get('path'), str) and bool(value['path'])
            and isinstance(value.get('sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is not None)


def _transport(url):
    # Honor the same user/system network route as the browser. GitHub raw is
    # often unreachable on the otherwise-working direct PRTS route.
    opener = request.build_opener()
    with opener.open(request.Request(url, headers={'User-Agent': 'ShuCompanion/public-facts',
                        'Accept-Encoding': 'gzip'}), timeout=20) as response:
        raw = (gzip.GzipFile(fileobj=response).read(MAX_BYTES + 1)
               if response.headers.get('Content-Encoding') == 'gzip' else response.read(MAX_BYTES + 1))
    if len(raw) > MAX_BYTES:
        raise ValueError('公开资料超过大小限制')
    return raw


def _table(name, directory, transport):
    manifest = directory / (name + '.manifest.json')
    try:
        record = json.loads(manifest.read_text(encoding='utf-8'))
        path = Path(record['ref']['path']).resolve()
        if not path.is_relative_to(directory) or not 0 <= time.time() - record['fetched_at'] <= TTL:
            raise ValueError()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['ref']['sha256']:
            raise ValueError()
        saved = json.loads(raw)
        if saved['source'] != BASE + name + '.json':
            raise ValueError()
        return saved, record['ref']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raw = transport(BASE + name + '.json')
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('公开资料响应无效')
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError('公开资料格式无效')
    saved = {'source': BASE + name + '.json', 'raw_sha256': hashlib.sha256(raw).hexdigest(), 'body': body}
    ref = _publish(directory, saved)
    temporary = directory / (uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(_encode({'ref': ref, 'fetched_at': time.time()}))
    temporary.replace(manifest)
    return saved, ref


def stage_facts(catalog: dict, cache_dir: Path, *, transport=None, now=None) -> dict:
    directory = Path(cache_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    moment = time.time() if now is None else now
    stages, stage_ref = _table('stage_table', directory, transport or _transport)
    zones, zone_ref = _table('zone_table', directory, transport or _transport)
    stage_table = stages['body'].get('stages')
    if not isinstance(stage_table, dict) or not isinstance(catalog.get('stages'), list):
        raise ValueError('目录或公开关卡表结构无效')
    zone_table = zones['body'].get('zones', {})
    valid = zones['body'].get('zoneValidInfo', {})
    if not isinstance(zone_table, dict) or not isinstance(valid, dict):
        raise ValueError('公开区域表结构无效')
    mappings, unknown, groups, constraints, exclusions = [], [], {}, [], []
    navigation_regions = {}
    by_level, all_modes = {}, {}
    for row in stage_table.values():
        if isinstance(row, dict) and row.get('levelId'):
            all_modes.setdefault(row['levelId'].casefold(), set()).add(row.get('difficulty'))
        if isinstance(row, dict) and row.get('difficulty') == 'NORMAL' and row.get('levelId'):
            by_level.setdefault(row['levelId'].casefold(), []).append(row)
    members = {s.get('stage_id') for s in catalog['stages']}
    for member in catalog['stages']:
        sid = member.get('stage_id')
        raw = stage_table.get(sid)
        code = member.get('display_code', member.get('code'))
        if not isinstance(raw, dict):
            unknown.append({'stage_id': sid, 'reason': 'public_stage_missing'})
            continue
        if raw.get('stageId') != sid or str(raw.get('code', '')).casefold() != str(code).casefold():
            unknown.append({'stage_id': sid, 'reason': 'public_code_conflict'})
            continue
        difficulty = {'NORMAL': 1, 'FOUR_STAR': 2}.get(raw.get('difficulty'))
        query_key = sid
        if difficulty == 2:
            normals = by_level.get(str(raw.get('levelId', '')).casefold(), [])
            if len(normals) != 1 or normals[0].get('code') != raw.get('code'):
                difficulty = None
            else:
                query_key = normals[0]['stageId']
        if difficulty is None:
            unknown.append({'stage_id': sid, 'reason': 'public_mode_unmapped'})
        else:
            domain = all_modes.get(str(raw.get('levelId', '')).casefold(), set())
            domain_complete = bool(domain) and domain <= {'NORMAL', 'FOUR_STAR'}
            mappings.append({'stage_id': sid, 'query_key': query_key, 'difficulty': difficulty,
                             'public_level_id': raw.get('levelId'),
                             'difficulty_domain_complete': domain_complete,
                             'difficulty_domain': sorted({'NORMAL':1,'FOUR_STAR':2}[mode] for mode in domain)
                                 if domain_complete else [],
                             'source_refs': [stage_ref]})
        zone_id = raw.get('zoneId')
        zone = zone_table.get(zone_id, {}) if isinstance(zone_id, str) else {}
        zone_conflict = False
        if zone_id and isinstance(zone, dict) and zone:
            if zone.get('zoneID') not in (None, zone_id):
                unknown.append({'stage_id':sid, 'reason':'public_zone_identity_conflict'})
                zone_conflict = True
                zone = {}
        if zone_id and isinstance(zone, dict) and zone:
            label = next((zone.get(k) for k in ('zoneNameSecond', 'zoneNameFirst', 'zoneNameTitleCurrent')
                          if isinstance(zone.get(k), str) and zone[k].strip()), zone_id)
            group = groups.setdefault(zone_id, {'group_id': zone_id, 'label': label,
                'stage_ids': [], 'source_refs': [stage_ref, zone_ref], 'zone_index': zone.get('zoneIndex', 0)})
            group['stage_ids'].append(sid)
            if zone.get('zoneID') != zone_id:
                unknown.append({'stage_id':sid, 'reason':'public_zone_identity_unverified'})
            elif not all(_hashed_reference(ref) for ref in (stage_ref, zone_ref)):
                unknown.append({'stage_id':sid, 'reason':'public_zone_source_unverified'})
            else:
                # Zone membership can partition an import plan. It is not a
                # claim that MAA navigated this map or the account can enter it.
                region = navigation_regions.setdefault(zone_id, {
                    'navigation_region_id':'public-zone:' + zone_id,
                    'stage_ids':[], 'source_refs':[stage_ref,zone_ref],
                    'basis':'public_zone_membership', 'navigation_verified':False})
                region['stage_ids'].append(sid)
        elif not zone_conflict:
            unknown.append({'stage_id':sid, 'reason':'public_zone_missing'})
        for prerequisite in raw.get('unlockCondition', []):
            if isinstance(prerequisite, dict) and prerequisite.get('stageId') in members:
                constraints.append({'before': prerequisite['stageId'], 'after': sid, 'source_refs': [stage_ref]})
        if (raw.get('stageType') in ('GUIDE', 'STORY') or raw.get('appearanceStyle') == 'TRAINING'
                or raw.get('isStoryOnly') is True):
            reason = ('教学关卡（公开appearanceStyle=TRAINING）' if raw.get('appearanceStyle') == 'TRAINING'
                      else '剧情节点（公开isStoryOnly=true）' if raw.get('isStoryOnly') is True
                      else '公开关卡类型为' + raw['stageType'])
            exclusions.append({'stage_id': sid, 'reason': reason,
                               'source_refs': [stage_ref]})
    phase_links = []
    for group in groups.values():
        opening = valid.get(group['group_id'], {})
        start, end = opening.get('startTs'), opening.get('endTs')
        if type(start) in (int, float) and type(end) in (int, float):
            phase_links.append({'phase_id': group['group_id'], 'stage_ids': list(group['stage_ids']),
                'opening_status': 'closed' if moment < start else 'ended' if moment > end else 'open',
                'opens_at': datetime.fromtimestamp(start, timezone.utc).isoformat(),
                'ends_at': datetime.fromtimestamp(end, timezone.utc).isoformat(),
                'source_refs': [stage_ref, zone_ref]})
    # Missing zone facts stay visible in their actual catalog activity group.
    assigned = {s for g in groups.values() for s in g['stage_ids']}
    if members - assigned:
        groups['catalog-unmapped'] = {'group_id': 'catalog-unmapped',
            'label': catalog.get('event_name', '目录未映射分区'), 'stage_ids': sorted(members - assigned),
            'source_refs': [stage_ref], 'zone_index': 999999}
    return {'ok': True, 'tool': 'catalog_stage_facts', 'query_mappings': mappings,
            'groups': sorted(groups.values(), key=lambda g: (g['zone_index'], g['group_id'])),
            'navigation_regions':list(navigation_regions.values()),
            'phase_links': phase_links, 'order_constraints': constraints,
            'default_excluded_stages': exclusions, 'unknown': unknown, 'source_refs': [stage_ref, zone_ref]}

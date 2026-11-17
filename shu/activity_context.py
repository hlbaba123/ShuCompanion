"""Credential-free activity stage discovery from public level categories.

An activity name must come from the conversation or a model's actual research;
there are no embedded event names, code prefixes or availability assumptions.
The full matched catalog remains a hashed artifact. Only the first 200 stages
are returned to a model, with explicit truncation when that is insufficient.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from .level_catalog import (LevelCatalogError, _safe_path, _text, _validate_query,
                            load_level_catalog, public_stage_record)


MAX_ACTIVITY_STAGES = 200
MAX_EVENT_CANDIDATES = 200


def read_activity_catalog(reference: dict, *, cache_root: Path) -> dict:
    """Read the complete local catalog after boundary and content-hash checks.

    A model-facing preview is never substituted for this complete snapshot.
    The caller supplies the application-owned cache root, never a model path.
    """
    try:
        path = _safe_path(Path(reference['path']), cache_root)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('catalog_size')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('catalog_hash')
        catalog = json.loads(raw)
        if (not isinstance(catalog, dict) or not isinstance(catalog.get('stages'), list)
                or catalog.get('total_stages') != len(catalog['stages'])):
            raise ValueError('catalog_truncated')
        return catalog
    except (OSError, KeyError, TypeError, ValueError):
        raise LevelCatalogError('cache') from None


def activity_catalog_page(reference: dict, *, cache_root: Path, offset: int = 0,
                          limit: int = MAX_ACTIVITY_STAGES) -> dict:
    """Bounded projection over the same complete, hashed activity snapshot."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_ACTIVITY_STAGES:
        raise LevelCatalogError('query')
    catalog = read_activity_catalog(reference, cache_root=cache_root)
    stages = catalog['stages'][offset:offset + limit]
    end = offset + len(stages)
    return {'stages': stages, 'offset': offset, 'total_stages': len(catalog['stages']),
            'next_offset': end if end < len(catalog['stages']) else None,
            'has_more': end < len(catalog['stages']), 'stage_catalog_ref': dict(reference)}


def _publish_catalog(directory: Path, catalog: dict) -> dict:
    raw = json.dumps(catalog, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    temporary = None
    try:
        target = _safe_path(directory / f'activity-stages-{digest}.json', directory)
        temporary = _safe_path(directory / f'.activity-stages-{uuid.uuid4().hex}.tmp', directory)
        with temporary.open('wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except OSError:
        raise LevelCatalogError('cache') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {'path': str(target), 'sha256': digest}


def activity_stages(query: str, cache_dir: Path, *, transport=None,
                    cache_root: Path | None = None) -> dict:
    """Resolve a public ``cat_two`` activity and retain its complete stage list.

    Exact category names take precedence. A unique contained name is allowed;
    ambiguous names require the caller to choose an actual returned category.
    Catalog membership establishes neither current opening nor account unlocks.
    ``cache_root`` is application-owned and is never a model tool argument.
    """
    query = _validate_query(query)
    snapshot = load_level_catalog(cache_dir, transport=transport, cache_root=cache_root)
    directory = _safe_path(Path(cache_dir), cache_root)
    needle = query.casefold()
    categories = {}
    for row in snapshot['rows']:
        # Preserve category labels as public data; only surrounding whitespace
        # is ignored. Stage names/codes cannot accidentally select an activity.
        event = (row.get('cat_two') or '').strip()
        if event:
            categories.setdefault(event, []).append(row)
    exact = [event for event in categories if event.casefold() == needle]
    matched = exact or [event for event in categories if needle in event.casefold()]
    events, stages, seen = [], [], set()
    for event in matched:
        count = 0
        for row in categories[event]:
            identity = (event, row['stage_id'], str(row['level_id']), row.get('difficulty', 0),
                        row.get('branch', ''), row.get('edition', ''))
            if identity in seen:
                continue
            seen.add(identity)
            record = public_stage_record(row, snapshot)
            record['event'] = event
            stages.append(record)
            count += 1
        events.append({'event_name': event, 'total_stages': count})
    selected = len(matched) == 1
    event_name = matched[0] if selected else ''
    # Query strings are bounded at 128 characters. An oversized or control-
    # containing remote label cannot be safely selected by its true category;
    # retain it in the artifact and report that limitation, never rename it.
    resolved = selected and len(event_name) <= 128 and _text(event_name, 128) == event_name
    catalog = {'schema_version': 1, 'tool': 'activity_stages', 'query': query,
        'source': snapshot['source'], 'raw_sha256': snapshot['raw_sha256'],
        'event_name': event_name, 'event_candidates': events, 'stages': stages,
        'total_stages': len(stages), 'catalog_complete': bool(resolved), 'has_more': False,
        'untrusted_content': True, 'content_is_instruction': False,
        'account_unlock_verified': False, 'opening_status_verified': False}
    reference = _publish_catalog(directory, catalog)
    visible = stages[:MAX_ACTIVITY_STAGES] if resolved else []
    has_more = resolved and len(stages) > len(visible)
    visible_events = []
    for event in events[:MAX_EVENT_CANDIDATES]:
        label = _text(event['event_name'], 128)
        candidate = {'event_name': label, 'total_stages': event['total_stages']}
        if label != event['event_name']:
            candidate['name_truncated'] = True
        visible_events.append(candidate)
    return {'ok': resolved, 'tool': 'activity_stages', 'query': query,
        'code': ('activity_resolved' if resolved else 'activity_label_unsupported' if selected
                 else 'activity_ambiguous' if matched else 'activity_not_found'),
        'event_name': event_name if resolved else '', 'event_candidates': visible_events,
        'total_events': len(events), 'has_more_events': len(events) > MAX_EVENT_CANDIDATES,
        'stages': visible, 'total_stages': len(stages), 'returned_stages': len(visible),
        'has_more': has_more, 'catalog_complete': resolved and not has_more,
        'stage_catalog_ref': reference, 'source': snapshot['source'],
        'raw_sha256': snapshot['raw_sha256'], 'cache': snapshot['cache'],
        'fetched_at': snapshot['fetched_at'], 'untrusted_content': True,
        'content_is_instruction': False, 'account_unlock_verified': False,
        'opening_status_verified': False}

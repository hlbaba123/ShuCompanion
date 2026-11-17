"""Persist complete public candidate summaries; return small model projections.

No credentials, model clients, roster filtering, actions or execution live here.
The caller supplies a stage identity resolved from the public level selector and
the fixed-origin catalog callable. A two-page batch is resumable, never a claim
that the first hundred rows exhaust the site. Content-addressed files preserve
earlier evidence; only the manifest of the current query is replaced atomically.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .copilot_catalog import CopilotCatalogError, MAX_PAGE_SIZE


TTL_SECONDS = 300
MAX_POOL_BYTES = 32 * 1024 * 1024
MAX_BATCH_PAGES = 2
PREVIEW_LIMIT = 10
SUMMARY_FIELDS = ('id', 'page_url', 'mystery_code', 'source', 'title', 'stage_name',
                  'available', 'rating_level', 'upload_time', 'operators', 'groups',
                  'hints', 'requires_download', 'schema_version', 'difficulty',
                  'requirements_complete', 'parse_issues', 'source_counts', 'candidate_locator',
                  'catalog_source', 'stage_match_verified', 'difficulty_match', 'declared_difficulty',
                  'inferred_difficulty', 'difficulty_basis', 'difficulty_proof')


class CopilotPoolError(ValueError):
    def __init__(self, message: str, *, io_operation=None, os_error=None):
        super().__init__(message)
        self.error_classification = 'local_io_error' if os_error is not None else 'evidence_unavailable'
        if os_error is not None:
            self.io_operation = io_operation
            # OS messages and filenames can contain private paths or credentials.
            # Only numeric system codes cross the research/model boundary.
            for field in ('errno', 'winerror'):
                value = getattr(os_error, field, None)
                if type(value) is int:
                    setattr(self, field, value)


def _io_path(path: Path) -> Path:
    """Private Windows spelling; references retain canonical ordinary paths."""
    if os.name != 'nt':
        return path
    value = str(path)
    if value.startswith('\\\\?\\'):
        return path
    if value.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + value[2:])
    return Path('\\\\?\\' + value)


def _canonical_path(path: Path) -> Path:
    # Resolve junctions using the same extended spelling as the eventual IO.
    # Returning ordinary paths keeps old pool references and root checks valid.
    resolved = str(_io_path(path.absolute()).resolve())
    if os.name == 'nt':
        if resolved.startswith('\\\\?\\UNC\\'):
            resolved = '\\\\' + resolved[8:]
        elif resolved.startswith('\\\\?\\'):
            resolved = resolved[4:]
    return Path(resolved)


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def preview(items: list[dict]) -> list[dict]:
    """Preview is intentionally small; every requirement remains in the pool."""
    return [{'id': item['id'], 'title': str(item.get('title', ''))[:96],
             'mystery_code': item.get('mystery_code'), 'available': item.get('available'),
             'operator_names': [str(op.get('name', ''))[:24] for op in item.get('operators', [])[:8]
                                if isinstance(op, dict)],
             'group_count': len(item.get('groups', [])),
             'requirements_in_pool': True}
            for item in items[:PREVIEW_LIMIT]]


class CopilotPool:
    def __init__(self, cache_dir: Path):
        try:
            self.root = _canonical_path(Path(cache_dir))
            self.directory = self._path(self.root / 'candidate-pools')
        except OSError as exc:
            raise CopilotPoolError('候选池资料目录无法访问。',
                                   io_operation='resolve', os_error=exc) from None

    def _path(self, path: Path) -> Path:
        path = _canonical_path(path)
        if not path.is_relative_to(self.root):
            raise CopilotPoolError('候选池路径不在应用资料目录内。')
        return path

    @staticmethod
    def identity(stage: str | None, document: str | None) -> str:
        return hashlib.sha256(_encode({'stage_id': stage, 'document': document,
                                      'type': 'PRTS', 'order_by': 'id', 'schema': 1})).hexdigest()

    def read(self, ref: dict) -> dict:
        """Read only a content-addressed file under our root and verify its hash."""
        if not isinstance(ref, dict):
            raise CopilotPoolError('候选池引用无效。')
        pool_id, digest = ref.get('pool_id'), ref.get('sha256')
        if (not isinstance(pool_id, str) or not re.fullmatch('[0-9a-f]{64}', pool_id)
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise CopilotPoolError('候选池引用无效。')
        try:
            path = self._path(self.directory / f'{digest}.json')
            supplied_path = ref.get('path')
            if (not isinstance(supplied_path, str) or not Path(supplied_path).is_absolute()
                    or self._path(Path(supplied_path)) != path):
                raise ValueError()
            with _io_path(path).open('rb') as stream:
                raw = stream.read(MAX_POOL_BYTES + 1)
            if len(raw) > MAX_POOL_BYTES or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError()
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get('schema') != 1
                    or data.get('pool_id') != pool_id
                    or not isinstance(data.get('candidates'), list)
                    or not isinstance(data.get('coverage'), dict)
                    or data.get('stage_id') is not None and not isinstance(data['stage_id'], str)
                    or data.get('document') is not None and not isinstance(data['document'], str)
                    or self.identity(data.get('stage_id'), data.get('document')) != pool_id):
                raise ValueError()
            return data
        except OSError as exc:
            raise CopilotPoolError('候选池文件无法读取。',
                                   io_operation='read', os_error=exc) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CopilotPoolError('候选池文件缺失、已变化或内容无效。') from None

    def _cached(self, pool_id: str) -> dict | None:
        try:
            path = self._path(self.directory / f'{pool_id}.manifest.json')
            if _io_path(path).stat().st_size > 4096:
                return None
            ref = json.loads(_io_path(path).read_text(encoding='utf-8'))
            if not isinstance(ref, dict) or ref.get('pool_id') != pool_id:
                return None
            pool = self.read(ref)
            if not 0 <= time.time() - pool.get('updated_at', -1) < TTL_SECONDS:
                return None
            return pool
        except CopilotPoolError as exc:
            if exc.error_classification == 'local_io_error' and getattr(exc, 'errno', None) != errno.ENOENT:
                raise
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CopilotPoolError('候选池缓存无法读取。',
                                   io_operation='read', os_error=exc) from None
        except (ValueError, TypeError, RecursionError):
            return None

    def _save(self, pool: dict) -> dict:
        raw = _encode(pool)
        if len(raw) > MAX_POOL_BYTES:
            raise CopilotPoolError('候选池超过单个证据文件大小上限；已有证据保持可读。')
        digest = hashlib.sha256(raw).hexdigest()
        temporary = None
        try:
            path = self._path(self.directory / f'{digest}.json')
            ref = {'pool_id': pool['pool_id'], 'path': str(path), 'sha256': digest}
            _io_path(self.directory).mkdir(parents=True, exist_ok=True)
            temporary = self._path(self.directory / f'.{uuid.uuid4().hex}.tmp')
            _io_path(temporary).write_bytes(raw)
            _io_path(temporary).replace(_io_path(path))
            _io_path(temporary).write_bytes(_encode(ref))
            _io_path(temporary).replace(_io_path(self._path(self.directory / f"{pool['pool_id']}.manifest.json")))
        except OSError as exc:
            raise CopilotPoolError('候选池证据无法保存。',
                                   io_operation='write', os_error=exc) from None
        finally:
            if temporary is not None:
                try:
                    _io_path(temporary).unlink(missing_ok=True)
                except OSError:
                    # Cleanup cannot replace the original safe failure.
                    pass
        return ref

    def collect(self, stage: str | None, document: str | None, *, query,
                cancel_event=None, refresh: bool = False, resume_ref: dict | None = None,
                max_pages: int = MAX_BATCH_PAGES, before_fetch=None,
                stage_aliases=None) -> tuple[dict, dict]:
        if type(max_pages) is not int or not 0 <= max_pages <= MAX_BATCH_PAGES:
            raise CopilotPoolError('单次目录批次数无效。')
        pool_id = self.identity(stage, document)
        pool = None if refresh else self._cached(pool_id)
        if pool is None and not refresh and resume_ref is not None and resume_ref.get('pool_id') == pool_id:
            previous = self.read(resume_ref)
            # An explicitly handed-off incomplete pool remains resumable after
            # the freshness TTL. New page totals still detect catalogue changes.
            if previous['coverage'].get('next_page') is not None:
                pool = previous
        cached = pool is not None
        if pool is None:
            pool = {'schema': 1, 'pool_id': pool_id, 'stage_id': stage, 'document': document,
                    'created_at': time.time(), 'updated_at': time.time(),
                    'candidates': [], 'sources': [], 'unverified_candidates': [],
                    'coverage': {'total': None, 'collected': 0, 'complete': False,
                                 'exhausted': False, 'next_page': 1, 'pages_received': 0,
                                 'raw_fetched': 0, 'unverified': 0, 'total_changed': False}}
        coverage = pool['coverage']
        error = None
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        fetched = 0
        attempted = 0
        while coverage['next_page'] is not None and fetched < max_pages and not cancelled:
            page_number = coverage['next_page']
            if before_fetch is not None and before_fetch(page_number) is not True:
                break
            attempted += 1
            try:
                response = query(stage, document=document, page=page_number,
                                 limit=MAX_PAGE_SIZE, full_summary=True, order_by='id')
            except CopilotCatalogError as exc:
                error = exc.public_message
                break
            except Exception:
                error = '公开作业目录暂时不可用；已收集候选可继续读取。'
                break
            fetched += 1
            total = response.get('total')
            if type(total) is int and total >= 0:
                if coverage['total'] is not None and total != coverage['total']:
                    coverage['total_changed'] = True
                coverage['total'] = total
            source = response.get('source')
            if isinstance(source, str) and source not in pool['sources']:
                pool['sources'].append(source)
            seen = {item['id'] for item in pool['candidates']}
            unverified = {item['id'] for item in pool['unverified_candidates']}
            for item in response.get('candidates', []):
                if isinstance(item, dict) and type(item.get('id')) is int:
                    item = {**{key: copy.deepcopy(item[key]) for key in SUMMARY_FIELDS if key in item},
                            'catalog_source': source}
                    # The public selector identity remains the hard stage check.
                    aliases = {str(value).casefold() for value in (stage_aliases or [stage])}
                    if stage is not None and str(item.get('stage_name', '')).casefold() not in aliases:
                        if item['id'] not in unverified:
                            pool['unverified_candidates'].append(copy.deepcopy(item))
                            unverified.add(item['id'])
                        continue
                    if item['id'] not in seen:
                        pool['candidates'].append(copy.deepcopy(item))
                        seen.add(item['id'])
            for item in response.get('unverified_candidates', []):
                if isinstance(item, dict) and type(item.get('id')) is int and item['id'] not in unverified:
                    pool['unverified_candidates'].append({
                        **{key: copy.deepcopy(item[key]) for key in SUMMARY_FIELDS if key in item},
                        'catalog_source': source})
                    unverified.add(item['id'])
            coverage['collected'] = len(pool['candidates'])
            coverage['unverified'] = len(pool['unverified_candidates'])
            raw_count = response.get('raw_count')
            coverage['raw_fetched'] += raw_count if type(raw_count) is int else len(seen)
            coverage['pages_received'] += 1
            coverage['exhausted'] = response.get('has_more') is not True
            coverage['next_page'] = None if coverage['exhausted'] else page_number + 1
            coverage['complete'] = (coverage['exhausted'] and not coverage['total_changed']
                                    and coverage['unverified'] == 0
                                    and coverage['total'] is not None
                                    and coverage['collected'] == coverage['total'])
            pool['updated_at'] = time.time()
            # A cancellation after a completed response still preserves that page.
            cancelled = bool(cancel_event is not None and cancel_event.is_set())
        ref = self._save(pool)
        result = {'ok': error is None and not cancelled, 'tool': 'candidates',
                  'pool_ref': ref, 'coverage': copy.deepcopy(coverage),
                  'preview': preview(pool['candidates']), 'cached': cached and fetched == 0,
                  'pages_fetched': fetched,
                  'pages_attempted': attempted,
                  'cancelled': cancelled, 'untrusted_content': True,
                  'content_is_instruction': False, 'requires_download': True,
                  'hint': '池中保留全部已收集摘要与干员要求；完整动作JSON按需另行下载。'}
        if error:
            result['error'] = error
        if coverage['total_changed']:
            result['hint'] += '检索期间站点总数变化；本次未声称完整快照，可刷新重查。'
        elif not coverage['complete'] and coverage['next_page'] is not None:
            result['hint'] += '本轮最多100条；再次调用同一查询会从next_page继续，不能声称全量。'
        elif not coverage['complete']:
            result['hint'] += '分页已结束，但数量或关卡核对不足，不能声称全量。'
        return result, pool

"""Bounded, credential-free public level metadata lookup.

The deployed PRTS+ level selector sends ``stage_id``, not its displayed code,
to copilot search. No aliases or levels are embedded here. Metadata remains
untrusted and establishes neither account unlocks nor roster suitability.
``transport(Request, timeout=15, max_bytes=...) -> bytes`` is the offline hook.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import ssl
import time
from urllib import error, request
import uuid


SOURCE = 'https://prts.maa.plus/arknights/level'
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 15
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_RESULTS = 8
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = 'level-catalog-cache.json'


class LevelCatalogError(ValueError):
    """Safe diagnostics that never echo remote bodies or credentials."""

    def __init__(self, reason: str, *, http_status=None):
        self.reason = reason
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        super().__init__(self.public_message)

    @property
    def public_message(self) -> str:
        if self.reason == 'http' and self.http_status is not None:
            return f'PRTS+ 关卡目录请求失败（HTTP {self.http_status}）。'
        return {
            'query': '关卡检索词无效；请输入不超过128字符的关卡代码、标识或文本，不要提供URL或Key。',
            'cache': '关卡目录缓存路径必须位于应用授权的缓存范围内，且须可安全读写。',
            'response': 'PRTS+ 关卡目录响应为空、类型无效或超过8MB上限。',
            'json': 'PRTS+ 关卡目录返回无效JSON。',
            'schema': 'PRTS+ 关卡目录返回错误或不支持的响应结构。',
            'timeout': 'PRTS+ 关卡目录请求超时。',
            'network': 'PRTS+ 关卡目录网络请求失败。',
        }.get(self.reason, 'PRTS+ 关卡目录请求失败。')


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(req: request.Request, *, timeout: int, max_bytes: int) -> bytes:
    opener = request.build_opener(
        request.ProxyHandler({}), _NoRedirect(),
        request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=timeout) as response:
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > max_bytes:
            raise LevelCatalogError('response')
        return response.read(max_bytes + 1)


def _invalid_constant(_):
    raise ValueError('Non-finite JSON number')


def _parse(raw: bytes) -> list[dict]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise LevelCatalogError('response')
    try:
        value = json.loads(raw.decode('utf-8-sig'), parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise LevelCatalogError('json') from None
    if (not isinstance(value, dict) or type(value.get('status_code')) is not int
            or value['status_code'] != 200 or not isinstance(value.get('data'), list)):
        raise LevelCatalogError('schema')
    rows = value['data']
    for row in rows:
        if not isinstance(row, dict):
            raise LevelCatalogError('schema')
        stage, level = row.get('stage_id'), row.get('level_id')
        if (not isinstance(stage, str) or not stage.strip() or len(stage) > 128
                or any(ord(c) < 32 for c in stage)
                or not ((isinstance(level, str) and level.strip() and len(level) <= 128
                         and all(ord(c) >= 32 for c in level))
                        or (type(level) is int and 0 <= level <= 999999999999))
                or any(row.get(key) is not None and not isinstance(row[key], str)
                       for key in ('cat_one', 'cat_two', 'cat_three', 'name'))):
            raise LevelCatalogError('schema')
        if ('difficulty' in row and (type(row['difficulty']) is not int
                                     or row['difficulty'] not in (0, 1, 2, 3))):
            raise LevelCatalogError('schema')
        if any(key in row and (not isinstance(row[key], str) or len(row[key]) > 128
                               or any(ord(c) < 32 for c in row[key]))
               for key in ('branch', 'edition', 'query_key', 'phase_id', 'navigation_region_id')):
            raise LevelCatalogError('schema')
    return rows


def _validate_query(query) -> str:
    if not isinstance(query, str):
        raise LevelCatalogError('query')
    if (not query.strip() or len(query) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in query)
            or re.search(r'(?i)(?:[a-z][a-z0-9+.-]*://|www\.|^//|^sk-|\bbearer\b|'
                         r'api[_ -]?key|\btoken\b|\bpassword\b|\bsecret\b)', query)):
        raise LevelCatalogError('query')
    return query.strip()


def _safe_path(path: Path, root: Path | None = None) -> Path:
    resolved = path.resolve()
    boundary = Path(WORKSPACE_ROOT if root is None else root).resolve()
    if not resolved.is_relative_to(boundary):
        raise LevelCatalogError('cache')
    return resolved


def _read_cache(directory: Path, now: float):
    try:
        manifest_path = _safe_path(directory / _MANIFEST, directory)
        if manifest_path.stat().st_size > 4096:
            return None
        metadata = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(metadata, dict):
            return None
        digest, fetched = metadata.get('sha256'), metadata.get('fetched_at')
        if (metadata.get('source') != SOURCE or not isinstance(digest, str)
                or not re.fullmatch('[0-9a-f]{64}', digest)
                or type(fetched) not in (int, float) or not 0 <= now - fetched < CACHE_TTL_SECONDS):
            return None
        data_path = _safe_path(directory / f'level-catalog-{digest}.json', directory)
        with data_path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if hashlib.sha256(raw).hexdigest() != digest:
            return None
        return _parse(raw), digest, fetched
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _write_cache(directory: Path, raw: bytes, digest: str, fetched: float):
    """Commit a manifest last, preserving the previous valid snapshot on failure."""
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        raw_path = _safe_path(directory / f'level-catalog-{digest}.json', directory)
        manifest_path = _safe_path(directory / _MANIFEST, directory)
        temporary = _safe_path(directory / f'.level-catalog-{uuid.uuid4().hex}.tmp', directory)
        temporary.write_bytes(raw)
        temporary.replace(raw_path)
        metadata = {'source': SOURCE, 'sha256': digest, 'fetched_at': fetched}
        temporary.write_text(json.dumps(metadata), encoding='utf-8')
        temporary.replace(manifest_path)
    except OSError:
        raise LevelCatalogError('cache') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _text(value, limit: int) -> str:
    return ''.join(c for c in (value or '') if c >= ' ' and ord(c) != 127)[:limit].strip()


def public_stage_record(row: dict, snapshot: dict) -> dict:
    """Preserve source identity without interpreting opaque IDs or code suffixes."""
    result = {'stage_id': row['stage_id'], 'level_id': row['level_id'],
              'code': _text(row.get('cat_three'), 64), 'name': _text(row.get('name'), 64),
              'event': _text(row.get('cat_two'), 128), 'category': _text(row.get('cat_one'), 48),
              'difficulty': row.get('difficulty', 0), 'branch': row.get('branch', ''),
              'edition': row.get('edition', ''), 'query_key': row.get('query_key') or row['stage_id'],
              'source_refs': [{'source': snapshot['source'], 'raw_sha256': snapshot['raw_sha256']}]}
    for key in ('phase_id', 'navigation_region_id'):
        if key in row:
            result[key] = row[key]
    return result


def load_level_catalog(cache_dir: Path, *, transport=None, cache_root: Path | None = None,
                       fallback_cache_dirs=()) -> dict:
    """Load the complete validated public snapshot for bounded search consumers.

    A failed refresh raises an accurate safe diagnostic; it never overwrites the
    previous cache with an error response or silently labels stale data fresh.
    No model, game files, environment credentials or account endpoints are read.
    The application may explicitly authorize its configured data/cache root;
    that path is not a model tool argument. Without it, the source workspace
    boundary remains the default. Derived files stay inside the chosen cache
    directory, including when the profile is outside a frozen installation.
    ``fallback_cache_dirs`` are separately authorized application-owned legacy
    directories. They are read only with the same TTL/hash/schema validation;
    refreshed snapshots are written solely to cache_dir. No model tool supplies
    these paths.
    """
    try:
        directory = _safe_path(Path(cache_dir), cache_root)
    except (OSError, TypeError, ValueError):
        raise LevelCatalogError('cache') from None
    now = time.time()
    cached = _read_cache(directory, now)
    if (not isinstance(fallback_cache_dirs, (tuple, list)) or len(fallback_cache_dirs) > 8):
        raise LevelCatalogError('cache')
    if cached is None:
        for legacy in fallback_cache_dirs:
            try:
                legacy = Path(legacy).resolve()
                if legacy == directory:
                    continue
                cached = _read_cache(legacy, now)
            except (OSError, ValueError, TypeError):
                continue
            if cached is not None:
                break
    if cached is not None:
        rows, digest, fetched = cached
        cache_status = 'hit'
    else:
        req = request.Request(SOURCE, method='GET', headers={
            'Accept': 'application/json', 'Accept-Encoding': 'identity',
            'User-Agent': 'ShuCompanion/0.1 (public level metadata)',
        })
        try:
            raw = (transport or _http_transport)(req, timeout=TIMEOUT_SECONDS, max_bytes=MAX_BYTES)
        except LevelCatalogError:
            raise
        except error.HTTPError as exc:
            raise LevelCatalogError('http', http_status=exc.code) from None
        except TimeoutError:
            raise LevelCatalogError('timeout') from None
        except error.URLError as exc:
            raise LevelCatalogError('timeout' if isinstance(exc.reason, TimeoutError) else 'network') from None
        except Exception:
            raise LevelCatalogError('network') from None
        rows = _parse(raw)
        digest, fetched = hashlib.sha256(raw).hexdigest(), now
        _write_cache(directory, raw, digest, fetched)
        cache_status = 'refreshed'
    return {'rows': rows, 'source': SOURCE, 'raw_sha256': digest,
            'cache': cache_status, 'fetched_at': fetched}


def find_levels(query: str, cache_dir: Path, *, transport=None, cache_root: Path | None = None,
                fallback_cache_dirs=()) -> dict:
    """Search exact public code/IDs or category/name text, returning at most eight.

    Activity-wide consumers use the same validated snapshot through
    ``load_level_catalog`` rather than mistaking this bounded selector for a
    complete activity stage list.
    """
    query = _validate_query(query)
    snapshot = load_level_catalog(cache_dir, transport=transport, cache_root=cache_root,
                                  fallback_cache_dirs=fallback_cache_dirs)
    rows = snapshot['rows']
    needle = query.casefold()
    matches = []
    seen = set()
    for row in rows:
        exact = needle in (row['stage_id'].casefold(), str(row['level_id']).casefold(),
                           (row.get('cat_three') or '').strip().casefold())
        contains = any(needle in (row.get(key) or '').casefold()
                       for key in ('name', 'cat_two', 'cat_one'))
        identity = (row['stage_id'], str(row['level_id']), row.get('difficulty', 0),
                    row.get('branch', ''), row.get('edition', ''))
        if not (exact or contains) or identity in seen:
            continue
        seen.add(identity)
        record = public_stage_record(row, snapshot)
        record['match'] = 'exact_code_or_id' if exact else 'text_contains'
        matches.append((0 if exact else 1, record))
    matches.sort(key=lambda match: match[0])
    result = {
        'query': query, 'source': SOURCE, 'levels': [row for _, row in matches[:MAX_RESULTS]],
        'total_matches': len(matches), 'has_more': len(matches) > MAX_RESULTS,
        'cache': snapshot['cache'], 'fetched_at': snapshot['fetched_at'],
        'raw_sha256': snapshot['raw_sha256'],
        'untrusted_content': True, 'content_is_instruction': False,
        'account_unlock_verified': False, 'roster_verified': False,
    }
    # Keep full IDs intact, reducing returned rows only for pathological Unicode.
    while len(json.dumps(result, ensure_ascii=False)) > 6000 and result['levels']:
        result['levels'].pop()
        result['has_more'] = True
    return result

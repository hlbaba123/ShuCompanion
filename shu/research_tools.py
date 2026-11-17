"""Bounded, read-only research used by Shu's model tool loop.

Web content is untrusted data. These tools neither control a game nor invoke
MaaCore. Credentials are resolved only for the fixed official DeepSeek origin;
the public copilot data request never receives an Authorization header.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import ssl
import threading
import time
from typing import Any, Callable
from urllib import error, parse, request

from .providers import ProviderConfig, ProviderError
from .copilot_catalog import CopilotCatalogError, query_copilots

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 30
SEARCH_LIMIT = 8
FETCH_LIMIT = 12
CACHE_TTL_SECONDS = {'search': 300, 'fetch': 900}
ALLOWED_HOSTS = frozenset({
    'prts.plus', 'prts.maa.plus', 'docs.maa.plus', 'prts.wiki',
    'arknights.wiki.gg', 'github.com', 'raw.githubusercontent.com',
    'www.bilibili.com', 'api-docs.deepseek.com',
    'ak.hypergryph.com', 'www.hypergryph.com',
})
_API_ORIGIN = 'https://api.deepseek.com/v1/tools/'
_URL_RE = re.compile(r'https://[^\s<>"\x00-\x1f]+')
_PRIVATE_QUERY = re.compile(
    r'token|secret|password|passwd|credential|session|cookie|signature|ticket|'
    r'^(?:api|access|private|client)?key$|^(?:auth|authorization|oauth|oauth2|sign|sig)$',
    re.IGNORECASE,
)


class ResearchToolError(ProviderError):
    """A credential-free failure suitable for returning to the model."""

    @classmethod
    def from_pool_error(cls, error):
        result = cls(str(error))
        result.error_classification = error.error_classification
        operation = getattr(error, 'io_operation', None)
        if operation in ('read', 'write', 'resolve'):
            result.io_operation = operation
        for field in ('errno', 'winerror'):
            value = getattr(error, field, None)
            if type(value) is int:
                setattr(result, field, value)
        return result


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _require_deepseek_research_origin(config):
    """A custom model gateway's credential is not a DeepSeek tools credential."""
    address = getattr(config, 'base_url', '')
    try:
        parts = parse.urlsplit(address)
        allowed = (parts.scheme == 'https' and parts.hostname == 'api.deepseek.com'
                   and parts.port in (None, 443) and parts.username is None
                   and parts.password is None and not parts.query and not parts.fragment
                   and not any(character.isspace() for character in address))
    except (AttributeError, TypeError, ValueError):
        allowed = False
    if not allowed:
        failure = ResearchToolError('当前模型使用自定义接口，尚未配置独立 DeepSeek 联网检索凭据；'
                                    '未发送模型网关 Key。仍可使用无需凭据的 PRTS 作业检索。')
        failure.code = 'deepseek_research_credentials_required'
        failure.error_classification = failure.code
        raise failure


def _public_url(url: str) -> str:
    if (not isinstance(url, str) or not url or len(url) > 2048
            or any(character.isspace() or ord(character) < 32 for character in url)
            or '\\' in url):
        raise ResearchToolError('资料地址无效。')
    try:
        parts = parse.urlsplit(url)
        port = parts.port
    except (ValueError, AttributeError):
        raise ResearchToolError('资料地址无效。') from None
    # Exact public-host allowlist excludes IP literals, localhost, subdomains,
    # trailing dots and URL parser tricks. Explicit ports are never accepted.
    if (parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS
            or parts.username is not None or parts.password is not None
            or port is not None or ':' in parts.netloc or parts.fragment
            or parts.netloc.lower() != parts.hostname):
        raise ResearchToolError('仅允许无凭据、无端口的白名单公网 HTTPS 资料地址。')
    try:
        pairs = parse.parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False,
                                max_num_fields=30)
    except ValueError:
        raise ResearchToolError('资料地址查询参数无效。') from None
    for name, value in pairs:
        normalized = re.sub(r'[^a-z0-9]', '', name.lower())
        if (_PRIVATE_QUERY.search(normalized) or re.search(r'(?i)\bsk-[a-z0-9_.-]{6,}|bearer\s', value)
                or re.search(r'(?i)(?:https?:)?//', value)):
            raise ResearchToolError('资料地址不得包含私密参数或嵌套跳转地址。')
    return parse.urlunsplit(('https', parts.hostname, parts.path or '/', parts.query, ''))


def _bounded(value: Any, budget: int = 4000) -> Any:
    """Bound all untrusted strings cumulatively, including mapping keys."""
    remaining = budget
    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        if depth > 8 or remaining <= 0:
            return None
        if isinstance(item, str):
            text = item[:min(4000, remaining)]
            if len(text) < len(item) and text:
                # Do not turn a clipped numeric URL into a different valid ID.
                matches = list(_URL_RE.finditer(text))
                if (matches and matches[-1].end() == len(text)
                        and re.match(r'[^\s<>"\x00-\x1f]', item[len(text)])):
                    text = text[:matches[-1].start()]
            remaining -= len(text)
            return text
        if isinstance(item, dict):
            output = {}
            for key, child in list(item.items())[:100]:
                if remaining <= 0:
                    break
                safe_key = str(key)[:min(128, remaining)]
                remaining -= len(safe_key)
                output[safe_key] = visit(child, depth + 1)
            return output
        if isinstance(item, list):
            output = []
            for child in item[:60]:
                if remaining <= 0:
                    break
                output.append(visit(child, depth + 1))
            return output
        if item is None or isinstance(item, (int, float, bool)):
            return item
        return None
    return visit(value)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _research_data(value: Any, operation: str) -> Any:
    """Keep every requested search card before spending space on its body.

    Search services use several wrappers. Preserve their shape and actual
    metadata rather than inventing a normalized source or letting the first
    page's content consume the budget for all later result URLs.
    """
    if operation != 'search':
        return _bounded(value)

    def card(item):
        if not isinstance(item, dict):
            return _bounded(item, budget=800)
        fields = {}
        for key in ('url', 'link', 'title', 'date', 'authority', 'rerank_score',
                    'published_at', 'publish_date', 'publish_time', 'updated_at', 'source', 'author', 'snippet',
                    'summary', 'description'):
            if key in item:
                limit = 640 if key in {'url', 'link'} else 240
                fields[key] = _bounded(item[key], budget=limit)
        for key in ('content', 'text', 'body', 'raw_content'):
            if key in item:
                fields[key] = _bounded(item[key], budget=800)
        fields.update(_bounded({key: child for key, child in item.items()
                                if key not in fields}, budget=200))
        return _bounded(fields, budget=2000)

    def shape(item, depth=0):
        if isinstance(item, list):
            return [card(child) for child in item[:5]], True
        if isinstance(item, dict) and depth < 4:
            for key in ('results', 'search_results', 'items', 'data'):
                if isinstance(item.get(key), (dict, list)):
                    shaped, found = shape(item[key], depth + 1)
                    if found:
                        remainder = _bounded({name: child for name, child in item.items()
                                              if name != key}, budget=400)
                        return {key: shaped, **remainder}, True
        return None, False

    shaped, found = shape(value)
    return _bounded(shaped, budget=12000) if found else _bounded(value)


def _copilot_source(url: str) -> tuple[str, str] | None:
    """Canonical endpoint plus source; identifiers always originate in data."""
    try:
        safe = _public_url(url)
    except ResearchToolError:
        return None
    parts = parse.urlsplit(safe)
    if parts.hostname == 'prts.maa.plus' and not parts.query:
        match = re.fullmatch(r'/copilot/get/([1-9][0-9]{0,11})', parts.path)
        if match:
            return safe, safe
    if parts.hostname == 'prts.plus' and parts.path == '/':
        pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
        ids = [value for name, value in pairs if name == 'op']
        if len(pairs) == len(ids) == 1 and re.fullmatch(r'[1-9][0-9]{0,11}', ids[0]):
            return 'https://prts.maa.plus/copilot/get/' + ids[0], safe
    return None


class _HTTPTransport:
    """Callable(Request, timeout, max_bytes) -> bytes; offline tests replace it."""
    def __init__(self):
        self.opener = request.build_opener(
            _NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self, req: request.Request, *, timeout: int, max_bytes: int) -> bytes:
        with self.opener.open(req, timeout=timeout) as response:
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > max_bytes:
                raise ResearchToolError('资料响应超过 2 MB 上限。')
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ResearchToolError('资料响应超过 2 MB 上限。')
        return raw


class ResearchTools:
    """One experiment's limits, cache and model-visible discovery provenance.

    The optional transport callable accepts a urllib Request and keyword
    ``timeout``/``max_bytes``, and returns JSON bytes. It is for offline tests.
    Fresh cache hits do not resolve credentials or consume the 8/12 request
    budgets. Search entries expire after five minutes, fetched pages after
    fifteen minutes; legacy or invalid timestamps always require a new request.
    """
    def __init__(self, config: ProviderConfig, cache_dir: Path, *, transport: Callable | None = None,
                 catalog_query: Callable | None = None, level_query: Callable | None = None,
                 public_cache_root: Path | None = None):
        self.config = config
        self.cache_dir = Path(cache_dir)
        # Application-owned public response caches may use a short shared root.
        # Downloads and source/pool evidence remain bound to the task directory.
        try:
            self.public_cache_root = None if public_cache_root is None else Path(public_cache_root).resolve()
        except (OSError, TypeError, ValueError):
            raise ResearchToolError('公共资料缓存目录无效。') from None
        self._pool_directory = self.cache_dir
        # Preparing Windows' certificate store can be slow. Ordinary chat and
        # fresh cache hits never need a connection, so defer this until IO.
        self.transport = transport
        # An injected catalog adapter replaces the entire site lookup in tests.
        # The production default first resolves display codes through the real
        # public level selector; sending a display code directly is silently
        # ignored by the deployed site and returns unrelated global entries.
        self.catalog_query = catalog_query
        self.level_query = level_query
        self.counters = {'search': 0, 'fetch': 0, 'download_copilot': 0, 'catalog': 0, 'levels': 0}
        self._catalog_cache: dict[str, dict] = {}
        self._level_results: dict[str, dict] = {}
        self._level_scope: frozenset[str] = frozenset()
        self._level_attempts: dict[str,int] = {}
        self._catalog_resolutions: dict[str, dict] = {}
        self._pool_refs: dict[str, dict] = {}
        self._pool_queries: dict[str, str] = {}
        self.discovered_copilot_urls: set[str] = set()
        self.discovery_sources: dict[str, list[dict]] = {}
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()

    @property
    def stats(self) -> dict:
        return dict(self.counters)

    def set_pool_directory(self, directory: Path) -> None:
        """Bind pools to the application's task directory, independent of dispatch.

        This is an application setup API, never a model tool or arbitrary file
        reader. Downloads and source artifacts keep using the original
        dispatch-local cache_dir; public response caching is configured separately.
        """
        try:
            root = Path(directory).resolve()
        except (OSError, TypeError, ValueError):
            raise ResearchToolError('候选池资料目录无效。') from None
        with self._lock:
            if self._pool_refs and root != self._pool_directory.resolve():
                raise ResearchToolError('已登记候选池后不能切换资料目录。')
            self._pool_directory = root

    def set_level_scope(self, codes) -> None:
        """Application-owned per-stage allowance for an explicit multi-stage goal.

        This is not exposed as a model tool. General searches retain the old
        small budget; enumerated task stages each get two bounded attempts.
        """
        if (not isinstance(codes,(list,tuple)) or len(codes)>200 or any(
                not isinstance(c,str) or not re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,3}',c) for c in codes)):
            raise ResearchToolError('关卡查询范围需要是任务已登记的有界代码列表。')
        with self._lock:
            self._level_scope=frozenset(c.casefold() for c in codes)

    def register_pool_ref(self, ref: dict) -> dict:
        """Restore an app-owned handoff only after root, hash and query checks."""
        from .copilot_pool import CopilotPool, CopilotPoolError
        with self._lock:
            try:
                pool = CopilotPool(self._pool_directory).read(ref)
            except CopilotPoolError as exc:
                raise ResearchToolError.from_pool_error(exc) from None
            self._pool_refs[ref['pool_id']] = copy.deepcopy(ref)
            for term in (pool.get('stage_id'), pool.get('document')):
                if isinstance(term, str):
                    self._pool_queries[term.casefold()] = ref['pool_id']
            sources = pool.get('sources') or ['https://prts.maa.plus/copilot/query']
            for item in pool['candidates']:
                self._discover(item, operation='candidates', source=item.get('catalog_source') or sources[0])
            return {'ok': True, 'pool_ref': copy.deepcopy(ref),
                    'coverage': copy.deepcopy(pool['coverage'])}

    def _json_request(self, req: request.Request) -> tuple[Any, bytes]:
        try:
            with self._lock:
                if self.transport is None:
                    self.transport = _HTTPTransport()
                transport = self.transport
            raw = transport(req, timeout=TIMEOUT_SECONDS, max_bytes=MAX_BYTES)
            if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
                raise ResearchToolError('资料响应为空、类型无效或超过 2 MB 上限。')
            value = json.loads(raw.decode('utf-8-sig'),
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, (dict, list)):
                raise ResearchToolError('资料接口未返回 JSON 对象或列表。')
            if isinstance(value, dict) and value.get('error'):
                raise ResearchToolError('资料服务返回错误，未采用远端错误文本。')
            return value, raw
        except ResearchToolError:
            raise
        except error.HTTPError as exc:
            # Never echo request headers, server error text or exception text.
            raise ResearchToolError(f'资料请求失败（HTTP {int(exc.code)}）。') from None
        except Exception:
            raise ResearchToolError('资料请求失败、超时或 JSON 内容无效。') from None

    def _cache_path(self, key: str, *, legacy=False) -> Path:
        root = self.cache_dir if legacy or self.public_cache_root is None else self.public_cache_root
        path = root / 'research' / (hashlib.sha256(key.encode()).hexdigest() + '.json')
        if not legacy and self.public_cache_root is not None:
            path = path.resolve()
            if not path.is_relative_to(self.public_cache_root):
                raise ResearchToolError('公共资料缓存文件必须位于应用指定的缓存目录内。')
        return path

    def _read_cache(self, key: str) -> dict | None:
        def fresh(record):
            if (not isinstance(record, dict) or record.get('cache_key') != key
                    or record.get('schema') != 1 or not isinstance(record.get('result'), dict)):
                return False
            written_at = record.get('written_at')
            ttl = CACHE_TTL_SECONDS.get(key.split(':', 1)[0], 0)
            now = time.time()
            if (type(written_at) not in (int, float)
                    or type(written_at) is float and not math.isfinite(written_at)
                    or written_at < 0 or written_at > now or not 0 <= now - written_at < ttl):
                return False
            return True

        record = self._cache.get(key)
        if fresh(record):
            return copy.deepcopy(record['result'])
        paths = [self._cache_path(key)]
        if self.public_cache_root is not None:
            legacy = self._cache_path(key, legacy=True)
            if legacy.resolve() != paths[0].resolve():
                paths.append(legacy)
        for path in paths:
            try:
                if path.stat().st_size > MAX_BYTES:
                    continue
                record = json.loads(path.read_text(encoding='utf-8'))
                if not fresh(record):
                    continue
            except (OSError, ValueError, TypeError, UnicodeError):
                continue
            self._cache[key] = copy.deepcopy(record)
            return copy.deepcopy(record['result'])
        return None

    def _write_cache(self, key: str, result: dict) -> None:
        record = {'schema': 1, 'cache_key': key, 'written_at': time.time(),
                  'result': copy.deepcopy(result)}
        self._cache[key] = record
        try:
            path = self._cache_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record,
                                       ensure_ascii=False, allow_nan=False), encoding='utf-8')
        except (OSError, ValueError, TypeError):
            raise ResearchToolError('资料缓存未能保存。') from None

    def _discover(self, data: Any, *, operation: str, source: str, original: Any = None) -> list[dict]:
        def candidates(value):
            return {match.group(0).rstrip('.,;:!?)\u3002\uff0c\uff1b\uff09\u3011]')
                    for text in _strings(value) for match in _URL_RE.finditer(text)}
        urls = candidates(data)
        if original is not None:
            urls &= candidates(original)  # a clipped URL must not invent an ID
        if operation == 'fetch':
            urls.add(source)  # the actual fetched URL is also shown in the result
        found = []
        for url in sorted(urls):
            pair = _copilot_source(url)
            if pair is None:
                continue
            canonical, discovered = pair
            provenance = {'operation': operation, 'source': source,
                          'discovered_url': discovered,
                          'derived_source': discovered if discovered != canonical else None}
            self.discovered_copilot_urls.add(canonical)
            records = self.discovery_sources.setdefault(canonical, [])
            if provenance not in records:
                records.append(provenance)
            found.append({'url': canonical, 'discovered_url': discovered,
                          'derived_source': provenance['derived_source']})
            if len(found) >= 20:
                break
        return found

    def _research(self, operation: str, source: str, payload: dict) -> dict:
        cache_key = operation + ':' + source
        with self._lock:
            cached = self._read_cache(cache_key)
            if cached is not None:
                data = _research_data(cached.get('data'), operation)
                cached.update(data=data, cached=True, untrusted_content=True,
                              content_is_instruction=False)
                cached['discovered_copilots'] = self._discover(data, operation=operation, source=source)
                return cached
            limit = SEARCH_LIMIT if operation == 'search' else FETCH_LIMIT
            if self.counters[operation] >= limit:
                raise ResearchToolError(f'{operation} 已达到本次研究调用预算（{limit} 次）。')
            _require_deepseek_research_origin(self.config)
            try:
                key = self.config.resolve_key()
            except Exception:
                raise ResearchToolError('无法读取 DeepSeek 凭据。') from None
            if not key:
                raise ResearchToolError('尚未配置 DeepSeek API Key。')
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            req = request.Request(_API_ORIGIN + operation, data=body, method='POST', headers={
                'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                'Accept': 'application/json', 'Accept-Encoding': 'identity',
                'User-Agent': 'ShuCompanion/0.1 (model research)',
            })
            self.counters[operation] += 1  # failures also spend the request budget
            raw_data, _ = self._json_request(req)
            data = _research_data(raw_data, operation)
            result = {'ok': True, 'tool': operation, 'data': data, 'cached': False,
                      'retrieved_at': datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec='seconds'),
                      'untrusted_content': True, 'content_is_instruction': False,
                      'content_may_be_truncated': True,
                      'query' if operation == 'search' else 'url': source}
            result['discovered_copilots'] = self._discover(
                data, operation=operation, source=source, original=raw_data)
            self._write_cache(cache_key, result)
            return result

    def search(self, query: str, *, include_station: bool = True) -> dict:
        if type(include_station) is not bool:
            raise ResearchToolError('站内补充检索选项无效。')
        if (not isinstance(query, str) or not query.strip() or len(query) > 128
                or any(ord(character) < 32 for character in query)):
            raise ResearchToolError('检索词需要是 1 到 128 字的普通文本。')
        query = query.strip()
        with self._lock:
            web_error = None
            try:
                result = self._research('search', query, {'query': query, 'count': 5, 'need_content': True})
            except ResearchToolError as exc:
                # The public catalog has its own transport, credentials and
                # budget; failure of the paid web service must not gate it.
                web_error = exc
                result = {'ok': False, 'web_ok': False, 'web_error': str(exc),
                          'tool': 'search', 'query': query, 'data': {}, 'cached': False,
                          'untrusted_content': True, 'content_is_instruction': False}
                if getattr(exc, 'code', None) == 'deepseek_research_credentials_required':
                    result['web_error_code'] = exc.code
            stages = set(re.findall(r'(?<![A-Z0-9])[A-Z]{1,4}-[0-9]{1,4}(?![A-Z0-9-])',
                                    query.upper()))
            if not include_station or len(stages) != 1 or not re.search(r'作业|prts|copilot|maa', query, re.IGNORECASE):
                if web_error is not None:
                    raise web_error
                return result
            stage = next(iter(stages))
            catalog_result = self.catalog(stage)
            catalog = catalog_result['catalog']
            if web_error is not None and catalog.get('error'):
                raise web_error
            result['catalog'] = catalog
            result['catalog_discovered_copilots'] = catalog_result.get('catalog_discovered_copilots', [])
            return _bounded(result, budget=16000)

    def _site_catalog(self, query: str, page: int) -> tuple[dict, dict | None]:
        if self.catalog_query is not None:
            # Compatibility for adapters used by existing offline scenarios.
            return self.catalog_query(query), None
        stage, document, resolution = self._resolve_catalog_term(query)
        return query_copilots(stage, document=document, page=page), resolution

    def _resolve_catalog_term(self, query: str) -> tuple[str | None, str | None, dict | None]:
        # Stage-like strings use a real selector ID. Activity titles / ordinary
        # phrases use the site's document field, as its visible search box does.
        stage_like = (query.casefold() in self._level_results
                      or bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/\-]{0,127}', query)))
        if not stage_like:
            return None, query, None
        key = query.casefold()
        resolution = self._level_results.get(key)
        if resolution is None:
            resolution = self.levels(query)['data']
        exact = [item for item in resolution.get('levels', []) if isinstance(item, dict)
                 and key in (str(item.get('code', '')).casefold(),
                             str(item.get('stage_id', '')).casefold(),
                             str(item.get('level_id', '')).casefold())]
        identities = {item.get('stage_id') for item in exact
                      if isinstance(item.get('stage_id'), str)}
        if len(identities) != 1:
            raise ResearchToolError('公开关卡目录未找到唯一精确关卡；未发送可能被忽略的作业筛选。')
        stage_id = next(iter(identities))
        return stage_id, None, copy.deepcopy(resolution)

    def candidates(self, stage_or_keyword: str, *, cancel_event=None, refresh: bool = False) -> dict:
        """Collect complete summaries in a resumable pool; show only a preview.

        Each invocation collects at most two public pages of fifty rows. Calling
        the same query continues an incomplete pool. Complete evidence files are
        immutable and reused for five minutes; refresh explicitly starts anew.
        No account roster is read and no candidate is selected for the model.
        """
        if (not isinstance(stage_or_keyword, str) or not stage_or_keyword.strip()
                or len(stage_or_keyword) > 128 or any(ord(c) < 32 for c in stage_or_keyword)
                or re.search(r'(?i)https?://|www\.|bearer\s|\bsk-[a-z0-9_.-]{6,}', stage_or_keyword)
                or stage_or_keyword.strip().isdigit()
                or stage_or_keyword.strip().lower().startswith(('sk-', 'bearer', 'http', 'token', 'password'))):
            raise ResearchToolError('候选池检索词必须是关卡代码、内部标识或不含凭据的站内文本。')
        if type(refresh) is not bool:
            raise ResearchToolError('候选池刷新选项无效。')
        if cancel_event is not None and cancel_event.is_set():
            return {'ok': False, 'tool': 'candidates', 'cancelled': True,
                    'untrusted_content': True, 'content_is_instruction': False}
        from .copilot_pool import CopilotPool, CopilotPoolError
        query = stage_or_keyword.strip()
        with self._lock:
            stage_id, document, resolution = self._resolve_catalog_term(query)
            def public_page(*args, **kwargs):
                self.counters['candidate_pages'] = self.counters.get('candidate_pages', 0) + 1
                return query_copilots(*args, **kwargs)
            try:
                pool_id = CopilotPool.identity(stage_id, document)
                result, pool = CopilotPool(self._pool_directory).collect(
                    stage_id, document, query=public_page, cancel_event=cancel_event, refresh=refresh,
                    resume_ref=self._pool_refs.get(pool_id))
            except CopilotPoolError as exc:
                raise ResearchToolError.from_pool_error(exc) from None
            result['query'] = query
            if resolution is not None:
                result['stage_resolution'] = resolution
            ref = result['pool_ref']
            self._pool_refs[ref['pool_id']] = copy.deepcopy(ref)
            self._pool_queries[query.casefold()]=ref['pool_id']
            # Preserve discovered-source gating for every stored candidate, not
            # only the ten rows shown in the compact model preview.
            sources = pool.get('sources') or ['https://prts.maa.plus/copilot/query']
            for item in pool['candidates']:
                self._discover(item, operation='candidates', source=item.get('catalog_source') or sources[0])
            return result

    def candidates_for_stage(self, resolved_stage: dict, *, cancel_event=None,
                             cache_only: bool = False, resume_ref=None, before_fetch=None,
                             timeout_seconds: float | None = None, refresh: bool = False) -> dict:
        """Application-only exact-stage lookup, one resumable public batch of <=50.

        Call cache_only first to screen existing evidence. before_fetch(page) must
        reserve the persistent budget and return True before the single request.
        A bounded retry can refresh its first network batch, then resume normally.
        No display-code uniqueness lookup, full document GET or actions storage.
        """
        from .copilot_pool import CopilotPool, CopilotPoolError, preview
        if timeout_seconds is not None and (type(timeout_seconds) not in (int, float)
                or not 0 < timeout_seconds <= 30):
            raise ResearchToolError('单批请求超时须在剩余30秒预算内。')
        if (not isinstance(resolved_stage, dict) or type(cache_only) is not bool
                or type(refresh) is not bool or cache_only and refresh):
            raise ResearchToolError('已解析关卡记录无效。')
        query_key = resolved_stage.get('query_key')
        if (not isinstance(query_key, str) or not re.fullmatch(
                r'[A-Za-z0-9\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff_\-#]{0,127}', query_key)
                or query_key.isdigit()):
            raise ResearchToolError('已解析关卡缺少真实query_key。')
        target = resolved_stage.get('difficulty')
        if type(target) is not int or target not in (0, 1, 2, 3):
            target = None
        aliases = {query_key.casefold()}
        for field in ('display_code', 'display_identity', 'stage_id', 'level_id'):
            value = resolved_stage.get(field)
            if isinstance(value, str) and value:
                aliases.add(value.casefold())
        with self._lock:
            def public_page(*args, **kwargs):
                self.counters['candidate_pages'] = self.counters.get('candidate_pages', 0) + 1
                if timeout_seconds is not None:
                    from .copilot_catalog import _http_transport
                    kwargs['transport'] = lambda req, timeout, max_bytes: _http_transport(
                        req, timeout=min(timeout, timeout_seconds), max_bytes=max_bytes)
                return query_copilots(*args, **kwargs)
            try:
                pools = CopilotPool(self._pool_directory)
                pool_id = pools.identity(query_key, None)
                result, pool = pools.collect(query_key, None, query=public_page,
                    cancel_event=cancel_event, max_pages=0 if cache_only else 1,
                    refresh=refresh,
                    resume_ref=resume_ref or self._pool_refs.get(pool_id),
                    before_fetch=before_fetch, stage_aliases=aliases)
                # Retain unverified/different-mode rows for unknown accounting.
                all_rows = {item['id']: item for item in [
                    *pool['candidates'], *pool.get('unverified_candidates', [])]}
                verified, unknown = [], []
                for item in all_rows.values():
                    item['stage_match_verified'] = str(item.get('stage_name', '')).casefold() in aliases
                    mode = item.get('difficulty')
                    declared_present = 'declared_difficulty' in item
                    declared = item.get('declared_difficulty', mode)
                    if declared_present:
                        item['declared_difficulty'] = declared
                    for field in ('inferred_difficulty', 'difficulty_basis', 'difficulty_proof'):
                        item.pop(field, None)
                    if (declared_present and declared in (None, 0) and type(declared) is not bool and target in (1, 2, 3)
                            and resolved_stage.get('difficulty_domain_complete') is True
                            and resolved_stage.get('difficulty_domain') == [target]
                            and resolved_stage.get('public_level_id')
                            and resolved_stage.get('difficulty_domain_source_refs')):
                        item['inferred_difficulty'] = target
                        item['difficulty_basis'] = 'unique_public_stage_mode'
                        item['difficulty_proof'] = copy.deepcopy(resolved_stage['difficulty_domain_source_refs'])
                    effective = item.get('inferred_difficulty', mode)
                    item['difficulty_match'] = ('unknown' if type(effective) is not int
                        or effective not in (1, 2, 3) or target not in (1, 2, 3)
                        else 'compatible' if effective & target == target else 'incompatible')
                    item['candidate_locator'] = {**item.get('candidate_locator', {}),
                        'id': item['id'], 'source': item.get('source'),
                        'stage_name': item.get('stage_name'), 'difficulty': mode,
                        'resolved_stage': copy.deepcopy(resolved_stage)}
                    (verified if item['stage_match_verified'] else unknown).append(item)
                pool['candidates'], pool['unverified_candidates'] = verified, unknown
                pool['coverage']['collected'] = len(verified)
                pool['coverage']['unverified'] = len(unknown)
                pool['coverage']['complete'] = (pool['coverage']['exhausted']
                    and not pool['coverage']['total_changed'] and not unknown
                    and len(verified) == pool['coverage']['total'])
                ref = pools._save(pool)
            except CopilotPoolError as exc:
                raise ResearchToolError.from_pool_error(exc) from None
            result.update({'tool': 'candidates_for_stage', 'pool_ref': ref,
                'resolved_stage': copy.deepcopy(resolved_stage), 'coverage': copy.deepcopy(pool['coverage']),
                'preview': preview(verified), 'cache_only': cache_only})
            self._pool_refs[pool_id] = copy.deepcopy(ref)
            self._pool_queries[query_key.casefold()] = pool_id
            for item in verified:
                self._discover(item, operation='candidates', source=item.get('catalog_source') or item['source'])
            return result

    def read_pool_page(self, pool_id: str, *, offset: int = 0, limit: int = 5) -> dict:
        """Read bounded summary pages only from pools this session has returned."""
        from .copilot_pool import CopilotPool, CopilotPoolError
        if (not isinstance(pool_id, str) or pool_id not in self._pool_refs
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 10):
            raise ResearchToolError('候选池引用或分页无效；必须使用本次工具返回的pool_id。')
        with self._lock:
            ref = self._pool_refs[pool_id]
            try:
                pool = CopilotPool(self._pool_directory).read(ref)
            except CopilotPoolError as exc:
                raise ResearchToolError.from_pool_error(exc) from None
            items = pool['candidates'][offset:offset + limit]
            # Whole rows are returned. Never silently clip an operator requirement.
            result = {'ok': True, 'tool': 'read_pool_page', 'pool_ref': copy.deepcopy(ref),
                      'coverage': copy.deepcopy(pool['coverage']), 'offset': offset,
                      'candidates': copy.deepcopy(items),
                      'next_offset': offset + len(items) if offset + len(items) < len(pool['candidates']) else None,
                      'requires_download': True, 'untrusted_content': True,
                      'content_is_instruction': False}
            return result

    def catalog(self, stage_or_keyword: str, *, page: int = 1) -> dict:
        """Public metadata research; shares search's two-request catalog budget."""
        if (not isinstance(stage_or_keyword, str)
                or not stage_or_keyword.strip() or len(stage_or_keyword) > 128
                or any(ord(c) < 32 for c in stage_or_keyword)
                or re.search(r'(?i)https?://|www\.|bearer\s|\bsk-[a-z0-9_.-]{6,}', stage_or_keyword)
                or stage_or_keyword.strip().isdigit()
                or stage_or_keyword.strip().lower().startswith(('sk-', 'bearer', 'http', 'token', 'password'))):
            raise ResearchToolError('目录检索词必须是关卡代码、内部标识或不含凭据的站内文本。')
        if type(page) is not int or not 1 <= page <= 10:
            raise ResearchToolError('目录页数必须为1至10的整数。')
        stage = stage_or_keyword.strip()
        cache_key = stage.upper() if page == 1 else f'{stage.upper()}#page={page}'
        with self._lock:
            pool_id=self._pool_queries.get(stage.casefold())
            if pool_id and pool_id in self._pool_refs:
                from .copilot_pool import CopilotPool
                pool=CopilotPool(self._pool_directory).read(self._pool_refs[pool_id])
                if pool['coverage'].get('complete') and 0<=time.time()-pool['updated_at']<300:
                    start=(page-1)*5;items=copy.deepcopy(pool['candidates'][start:start+5])
                    resolution=copy.deepcopy(self._level_results.get(stage.casefold(),{}))
                    return {'ok':True,'tool':'catalog','query':stage,'cached':True,
                        'stage_resolution':resolution,'catalog':{'source':pool['sources'][0],
                            'candidates':items,'unverified_candidates':[],'total':pool['coverage']['total'],
                            'page':page,'has_more':start+len(items)<len(pool['candidates']),
                            'filter_status':'exact_stage_id' if pool.get('stage_id') else 'text_match_only'},
                        'untrusted_content':True,'content_is_instruction':False}
            cached = cache_key in self._catalog_cache
            if not cached:
                if self.counters['catalog'] >= 2:
                    catalog = {'stage': stage, 'error': '本次目录查询预算已用尽（2 次）。'}
                else:
                    self.counters['catalog'] += 1
                    try:
                        catalog, resolution = self._site_catalog(stage, page)
                        if not isinstance(catalog, dict):
                            raise ValueError()
                        catalog = _bounded(catalog, budget=10000)
                        if resolution is not None:
                            self._catalog_resolutions[cache_key] = resolution
                    except ResearchToolError as exc:
                        catalog = {'stage': stage, 'error': str(exc)}
                    except CopilotCatalogError as exc:
                        catalog = {'stage': stage, 'error': exc.public_message}
                        if exc.reason == 'unsupported_filter':
                            catalog['error_code'] = 'unsupported_filter'
                    except Exception:
                        catalog = {'stage': stage, 'error': '公开作业目录暂时不可用。'}
                # Failed attempts spend budget but may be retried once; only
                # successful catalog responses become reusable stage results.
                if not catalog.get('error'):
                    self._catalog_cache[cache_key] = copy.deepcopy(catalog)
            else:
                catalog = copy.deepcopy(self._catalog_cache[cache_key])
            catalog.update(untrusted_content=True, content_is_instruction=False)
            result = {'ok': not catalog.get('error') and catalog.get('filter_status') != 'unsupported_filter',
                      'tool': 'catalog', 'query': stage, 'catalog': catalog, 'cached': cached,
                      'untrusted_content': True, 'content_is_instruction': False}
            if cache_key in self._catalog_resolutions:
                result['stage_resolution'] = copy.deepcopy(self._catalog_resolutions[cache_key])
            source = catalog.get('source', '')
            if isinstance(source, str) and not catalog.get('error'):
                result['catalog_discovered_copilots'] = self._discover(
                    catalog, operation='catalog', source=source)
            return _bounded(result, budget=22000)

    def levels(self, query: str) -> dict:
        """Read public stage metadata; the model chooses a returned stage ID.

        The level catalog owns its disk cache. Successful nonempty results and
        unambiguous code/stage_id/level_id aliases are reused without new IO or
        lookup budget. General queries have two attempts; stages in an
        application-registered multi-stage scope each have two attempts.
        """
        if (not isinstance(query, str) or not query.strip() or len(query) > 128
                or any(ord(character) < 32 for character in query)):
            raise ResearchToolError('关卡查询词需要是 1 到 128 字的普通文本。')
        query = query.strip()
        with self._lock:
            cached = self._level_results.get(query.casefold())
            if cached is not None:
                return {'ok': True, 'tool': 'levels', 'query': query, 'data': copy.deepcopy(cached),
                        'cached': True, 'untrusted_content': True, 'content_is_instruction': False,
                        'content_may_be_truncated': True}
            scoped=query.casefold() in self._level_scope
            if scoped and self._level_attempts.get(query.casefold(),0)>=2:
                raise ResearchToolError('该目标关卡已达到两次目录查询上限，不能重复绕路。')
            if not scoped and self.counters['levels'] >= 2:
                raise ResearchToolError('levels 已达到本次研究调用预算（2 次）。')
            self.counters['levels'] += 1
            self._level_attempts[query.casefold()]=self._level_attempts.get(query.casefold(),0)+1
            try:
                lookup = self.level_query
                level_directory = (self.cache_dir if self.public_cache_root is None
                                   else self.public_cache_root / 'levels')
                if lookup is None:
                    from .level_catalog import find_levels
                    if self.public_cache_root is None:
                        raw = find_levels(query, self.cache_dir, cache_root=self.cache_dir)
                    else:
                        raw = find_levels(query, level_directory, cache_root=self.public_cache_root,
                                          fallback_cache_dirs=(self.cache_dir,))
                else:
                    raw = lookup(query, level_directory)
                if not isinstance(raw, dict) or not isinstance(raw.get('levels'), list):
                    raise ValueError('invalid level catalog result')
                budget = 5400
                while True:
                    data = _bounded(raw, budget=budget)
                    data.update(untrusted_content=True, content_is_instruction=False)
                    if len(json.dumps(data, ensure_ascii=False, allow_nan=False)) <= 6000:
                        break
                    budget //= 2
            except Exception as exc:
                from .level_catalog import LevelCatalogError
                if isinstance(exc, LevelCatalogError):
                    raise ResearchToolError(exc.public_message) from None
                raise ResearchToolError('关卡目录查询失败或结果无效。') from None
            if data.get('levels'):
                self._level_results[query.casefold()] = copy.deepcopy(data)
                aliases = {}
                for level in data['levels']:
                    if not isinstance(level, dict):
                        continue
                    for field in ('code', 'stage_id', 'level_id'):
                        value = level.get(field)
                        if isinstance(value, str) and value.strip():
                            aliases.setdefault(value.strip().casefold(), []).append(level)
                for alias, matching in aliases.items():
                    if len({item.get('stage_id') for item in matching}) == 1:
                        alias_data = copy.deepcopy(data)
                        alias_data['levels'] = copy.deepcopy(matching)
                        self._level_results[alias] = alias_data
            return {'ok': True, 'tool': 'levels', 'query': query, 'data': data,
                    'untrusted_content': True, 'content_is_instruction': False,
                    'content_may_be_truncated': True}

    def fetch(self, url: str) -> dict:
        url = _public_url(url)
        if _copilot_source(url) is not None:
            # A discovered public copilot is JSON, not a web page. The existing
            # downloader enforces provenance and never resolves the model key.
            return self.download_copilot(url)
        return self._research('fetch', url, {'url': url})

    def download_copilot(self, url: str) -> dict:
        safe = _public_url(url)
        pair = _copilot_source(safe)
        with self._lock:
            if pair is None or pair[0] not in self.discovered_copilot_urls:
                raise ResearchToolError('只能下载本次模型 search/fetch 结果已发现的官方作业数据地址。')
            canonical, _ = pair
            sources = copy.deepcopy(self.discovery_sources[canonical])
            # An alias must itself have appeared in the model's returned data.
            if safe != canonical and not any(item['discovered_url'] == safe for item in sources):
                raise ResearchToolError('该作业页面地址尚未出现在模型资料结果中。')
            cached = self._cache.get('download:' + canonical)
            if cached:
                try:
                    cached_path = Path(cached['path'])
                    if (cached_path.stat().st_size > MAX_BYTES
                            or hashlib.sha256(cached_path.read_bytes()).hexdigest() != cached['sha256']):
                        raise ResearchToolError('已下载作业缓存已变化，未使用旧摘要。')
                except OSError:
                    raise ResearchToolError('已下载作业缓存不可读取。') from None
                return dict(copy.deepcopy(cached), cached=True)
            req = request.Request(canonical, method='GET', headers={
                'Accept': 'application/json', 'Accept-Encoding': 'identity',
                'User-Agent': 'ShuCompanion/0.1 (model copilot data research)',
            })
            self.counters['download_copilot'] += 1
            data, raw = self._json_request(req)
            identifier = parse.urlsplit(canonical).path.rsplit('/', 1)[1]
            if isinstance(data, dict):
                statuses = [data[key] for key in ('status_code', 'statusCode') if key in data]
                if any(type(status) is not int or status != 200 for status in statuses):
                    raise ResearchToolError('作业服务未返回成功状态，未保存。')
                wrapper = data.get('data')
                if (isinstance(wrapper, dict) and 'id' in wrapper
                        and (type(wrapper['id']) is not int or str(wrapper['id']) != identifier)):
                    raise ResearchToolError('作业服务返回的编号与请求不一致，未保存。')
            # The public service wraps the full copilot object in data.content.
            copilot = data
            if isinstance(data, dict) and isinstance(data.get('data'), dict):
                wrapper = data['data']
                copilot = wrapper.get('content', wrapper)
            if isinstance(copilot, str):
                try:
                    copilot = json.loads(copilot, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, TypeError):
                    raise ResearchToolError('作业 content 不是有效 JSON。') from None
            if (not isinstance(copilot, dict) or not isinstance(copilot.get('stage_name'), str)
                    or not copilot['stage_name'].strip() or len(copilot['stage_name']) > 300
                    or not isinstance(copilot.get('actions'), list)
                    or not copilot['actions'] or len(copilot['actions']) > 10000
                    or any(not isinstance(action, dict) for action in copilot['actions'])):
                raise ResearchToolError('作业缺少有效 stage_name/actions，未保存。')
            encoded = json.dumps(copilot, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > MAX_BYTES:
                raise ResearchToolError('完整作业超过 2 MB 上限。')
            digest = hashlib.sha256(encoded).hexdigest()
            path = self.cache_dir.resolve() / 'copilots' / (identifier + '-' + digest[:12] + '.json')
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(encoded)
            except OSError:
                raise ResearchToolError('完整作业文件无法保存。') from None
            operators = copilot.get('opers', [])
            names = [item['name'][:100] for item in operators if isinstance(item, dict)
                     and isinstance(item.get('name'), str)] if isinstance(operators, list) else []
            groups = copilot.get('groups', [])
            if isinstance(groups, list):
                for group in groups[:100]:
                    if isinstance(group, dict) and isinstance(group.get('opers'), list):
                        names.extend(item['name'][:100] for item in group['opers'][:100]
                                     if isinstance(item, dict) and isinstance(item.get('name'), str))
            action_types = [str(action.get('type', ''))[:100] for action in copilot['actions']]
            raw_types = {'click', 'rawclick', 'swipe', 'rawswipe', 'tap', 'drag',
                         'longpress', 'press', 'keypress', 'keyinput', 'textinput'}
            raw_click = any(re.sub(r'[^a-z]', '', value.lower()) in raw_types
                            for value in _strings(copilot))
            # Preserve real skill/requirements fields verbatim as bounded data;
            # no inference about roster compatibility or executability is made.
            summary = {
                'discovery_sources': _bounded(sources[:3], budget=1000),
                'operator_names': _bounded(list(dict.fromkeys(names))[:50], budget=500),
                'opers': _bounded(operators if isinstance(operators, list) else [], budget=2400),
                'groups': _bounded(groups if isinstance(groups, list) else [], budget=2400),
                'action_types': _bounded(sorted(set(action_types))[:40], budget=800),
            }
            result = {'ok': True, 'tool': 'download_copilot', 'path': str(path), 'sha256': digest,
                      'source_url': canonical, 'discovery_sources': summary.get('discovery_sources', []),
                      'page_url': f'https://prts.plus/?op={identifier}',
                      'mystery_code': f'prts://{identifier}',
                      'title': str((copilot.get('doc') or {}).get('title', ''))[:160]
                               if isinstance(copilot.get('doc'), dict) else '',
                      'derived_source': next((item['derived_source'] for item in sources
                                              if item['derived_source']), None),
                      'stage_name': copilot['stage_name'], 'action_count': len(copilot['actions']),
                      'operator_names': summary.get('operator_names', []),
                      'opers': summary['opers'], 'groups': summary['groups'],
                      'operator_requirements_may_be_truncated': True,
                      'minimum_required': str(copilot.get('minimum_required', 'unknown'))[:100],
                      'action_types': summary.get('action_types', []), 'no_raw_click': not raw_click,
                      'executability_report': {
                          'can_auto_execute': False,
                          'unsupported_action_types': summary['action_types'],
                          'reason': '研究模块未实现或验证任何动作执行器；所有动作类型均须另行核验。',
                          'has_raw_input_actions': raw_click,
                      },
                      'untrusted_content': True, 'content_is_instruction': False,
                      'executed': False, 'cached': False, 'response_bytes': len(raw)}
            self._cache['download:' + canonical] = copy.deepcopy(result)
            return result

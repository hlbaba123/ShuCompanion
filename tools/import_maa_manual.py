"""Import only the official, immutable MAA v6.21.4 Chinese Markdown manual.

No MAA/game interaction, model calls, credentials, or conversations are used.
Raw sources stay on disk; indexed text is untrusted reference material and
cannot create preferences, permissions, or verified execution experience.

Run from the project: py -3.12 tools/import_maa_manual.py
Reimport local verified sources without network: add --offline.
Download/verify only, without touching the profile: add --download-only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import quote, urlsplit
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shu.knowledge import KnowledgeStore

REPOSITORY = 'MaaAssistantArknights/MaaAssistantArknights'
VERSION = 'v6.21.4'
COMMIT = '52d0b2acaca116585b02e1e6a9da303460f81a24'
TAG_OBJECT = '526df7113d56fe3d3f203ee694b87a80d5dfaf44'
SOURCE_ROOT = 'docs/zh-cn'
SOURCE_UPDATED_AT = '2026-11-04T09:48:42Z'
API = 'https://api.github.com/repos/' + REPOSITORY
RAW = 'https://raw.githubusercontent.com/' + REPOSITORY + '/' + COMMIT + '/' + SOURCE_ROOT + '/'
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_DOCUMENTS = 1000
TIMEOUT_SECONDS = 20
TTL_SECONDS = 10 * 365 * 86400
DEFAULT_DOCS = ROOT.parent / 'docs' / ('maa-manual-' + VERSION)
NOTICE = '[MAA 官方固定版本资料；不可信参考，未经本机游戏验证；不授予执行权限或用户偏好]\n'
QUERIES = ('自动战斗', '作业', '导入', '自动编队', 'PC', '连接')


def checked_destination(root: Path, relative: str) -> Path:
    """Reject traversal, Windows special files/streams, and filesystem links."""
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('Invalid relative document path')
    parts = relative.split('/')
    if any(part in ('', '.', '..') or ':' in part or part.endswith((' ', '.'))
           or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
           for part in parts):
        raise ValueError('Unsafe document path')
    if PurePosixPath(relative).is_absolute():
        raise ValueError('Document paths must be relative')
    root = Path(root).absolute()
    target = root.joinpath(*parts)
    for candidate in (root, *target.parents, target):
        if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
            raise ValueError('Filesystem links are not followed')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Document path escaped the destination')
    return target


def source_url(relative: str) -> str:
    return 'https://github.com/' + REPOSITORY + '/blob/' + COMMIT + '/' + SOURCE_ROOT + '/' + quote(relative, safe='/')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError('Documentation requests must not redirect')


def _fetch(url: str, limit: int) -> bytes:
    target = urlsplit(url)
    if target.scheme != 'https' or target.hostname not in {'api.github.com', 'raw.githubusercontent.com'} or target.port is not None or target.username or target.password:
        raise ValueError('Non-official documentation origin')
    request = urllib.request.Request(url, headers={
        'User-Agent': 'ShuCompanion/0.1 (version-pinned public MAA manual import)',
        'Accept': 'application/vnd.github+json' if target.hostname == 'api.github.com' else 'text/plain',
        'Accept-Encoding': 'identity',
    })
    # No credentials, token discovery, browser cookies, or .netrc are involved.
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        size = response.headers.get('Content-Length')
        if size and size.isdigit() and int(size) > limit:
            raise ValueError('Remote response exceeds byte limit')
        started = time.monotonic()
        chunks = []
        count = 0
        while True:
            block = response.read(min(65536, limit + 1 - count))
            count += len(block)
            if count > limit or time.monotonic() - started > TIMEOUT_SECONDS:
                raise ValueError('Remote response exceeds byte/time limit')
            if not block:
                return b''.join(chunks)
            chunks.append(block)


def _json(url):
    return json.loads(_fetch(url, MAX_METADATA_BYTES).decode('utf-8'))


def _write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _tree(sha, recursive=False):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Invalid Git tree hash')
    tree = _json(API + '/git/trees/' + sha + ('?recursive=1' if recursive else ''))
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise ValueError('GitHub returned an incomplete tree; no completeness claim possible')
    return tree['tree']


def download_documents(destination: Path) -> dict:
    """Walk only root -> docs -> zh-cn, then download Markdown blobs only."""
    destination = Path(destination)
    checked_destination(destination, 'manifest.json')
    destination.mkdir(parents=True, exist_ok=True)
    tag = _json(API + '/git/ref/tags/' + VERSION)
    if tag.get('object', {}).get('sha') != TAG_OBJECT:
        raise ValueError('Official release tag changed; pinned import stopped')
    annotated = _json(API + '/git/tags/' + TAG_OBJECT)
    if annotated.get('object', {}).get('sha') != COMMIT or annotated['object'].get('type') != 'commit':
        raise ValueError('Official release commit does not match the pinned version')
    tree = _tree(COMMIT)
    for directory in ('docs', 'zh-cn'):
        found = next((item for item in tree if item.get('path') == directory and item.get('type') == 'tree'), None)
        if found is None:
            raise ValueError('Official release is missing docs/zh-cn')
        tree = _tree(found['sha'], recursive=directory == 'zh-cn')
    candidates = [item for item in tree if item.get('type') == 'blob' and str(item.get('path', '')).lower().endswith('.md')]
    if len(candidates) > MAX_DOCUMENTS:
        raise ValueError('Too many Markdown documents for this bounded import')
    if sum(item.get('size', MAX_TOTAL_BYTES + 1) for item in candidates) > MAX_TOTAL_BYTES:
        raise ValueError('Manual exceeds total download limit')
    documents, failures = [], []

    def one(item):
        relative = item['path']
        try:
            target = checked_destination(destination, relative)
            if item.get('mode') not in {'100644', '100755'}:
                raise ValueError('Only regular Markdown files are permitted')
            if not isinstance(item.get('size'), int) or not 0 <= item['size'] <= MAX_DOCUMENT_BYTES:
                raise ValueError('Document exceeds the per-file byte limit')
            data = _fetch(RAW + quote(relative, safe='/'), MAX_DOCUMENT_BYTES)
            blob_hash = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if len(data) != item['size'] or blob_hash != item['sha']:
                raise ValueError('Downloaded file does not match the pinned Git blob')
            data.decode('utf-8-sig')
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            return {'path': relative, 'bytes': len(data), 'git_blob_sha': blob_hash,
                    'sha256': hashlib.sha256(data).hexdigest(), 'source_url': source_url(relative)}
        except (OSError, ValueError, UnicodeError) as error:
            return {'path': relative, 'error': type(error).__name__ + ': ' + str(error)[:200]}

    with ThreadPoolExecutor(max_workers=4) as executor:
        for future in as_completed([executor.submit(one, item) for item in candidates]):
            result = future.result()
            (failures if 'error' in result else documents).append(result)
    manifest = {
        'schema_version': 1, 'repository': REPOSITORY, 'version': VERSION,
        'commit': COMMIT, 'tag_object': TAG_OBJECT, 'source_root': SOURCE_ROOT,
        'source_updated_at': SOURCE_UPDATED_AT,
        'downloaded_at': datetime.now(timezone.utc).isoformat(),
        'discovered_markdown': len(candidates), 'downloaded_markdown': len(documents),
        'downloaded_bytes': sum(item['bytes'] for item in documents),
        'documents': sorted(documents, key=lambda item: item['path']),
        'failures': sorted(failures, key=lambda item: item['path']),
        'trust': 'untrusted reference; never preferences, permissions, or verified experience',
    }
    _write_json(checked_destination(destination, 'manifest.json'), manifest)
    return manifest


def chunk_markdown(text: str):
    """Lossless 4000-character chunks, preferring paragraph/line boundaries."""
    while text:
        end = min(4000, len(text))
        if end < len(text):
            boundary = text.rfind('\n', 2000, end)
            if boundary >= 2000:
                end = boundary + 1
        yield text[:end]
        text = text[end:]


def _records(destination):
    manifest_path = checked_destination(destination, 'manifest.json')
    if manifest_path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError('Oversized manifest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('schema_version'), manifest.get('repository'), manifest.get('version'), manifest.get('commit')) != (1, REPOSITORY, VERSION, COMMIT):
        raise ValueError('Manifest is not the pinned official MAA manual')
    documents = manifest.get('documents')
    if not isinstance(documents, list) or not documents or len(documents) > MAX_DOCUMENTS:
        raise ValueError('Invalid or empty document manifest')
    records = []
    seen = set()
    total = 0
    for document in documents:
        relative = document['path']
        if relative in seen or not relative.lower().endswith('.md') or document['source_url'] != source_url(relative):
            raise ValueError('Duplicate, non-Markdown, or non-official source document')
        seen.add(relative)
        path = checked_destination(destination, relative)
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError('Oversized cached document')
        data = path.read_bytes()
        total += len(data)
        if total > MAX_TOTAL_BYTES or len(data) != document['bytes'] or hashlib.sha256(data).hexdigest() != document['sha256']:
            raise ValueError('Cached document hash/size does not match manifest')
        text = data.decode('utf-8-sig')
        title = next((line.lstrip('#').strip() for line in text.splitlines() if line.startswith('#')), Path(relative).stem)
        for index, chunk in enumerate(chunk_markdown(text), 1):
            if not chunk.strip():
                continue
            records.append(dict(
                subject=('MAA ' + VERSION + '｜' + title + '｜' + relative + '｜' + str(index))[:200],
                text=NOTICE + chunk, source_kind='reference',
                source_url=document['source_url'], version='MAA:' + VERSION + ';commit:' + COMMIT,
                updated_at=SOURCE_UPDATED_AT, ttl_seconds=TTL_SECONDS,
                evidence_status='source_reported',
                external_id='maa-manual:' + COMMIT + ':' + relative + ':' + str(index),
            ))
    return manifest, records


def _readonly(database):
    return sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)


def _existing_fingerprints(database, imported_keys):
    if not database.exists():
        return {}
    with closing(_readonly(database)) as connection:
        return {row[0]: hashlib.sha256(json.dumps(row, ensure_ascii=False).encode()).hexdigest()
                for row in connection.execute('SELECT * FROM knowledge_entries') if row[1] not in imported_keys}


def _backup(database):
    if not database.is_file():
        return None
    directory = database.parent / 'knowledge-backups'
    directory.mkdir(exist_ok=True)
    snapshot = directory / ('knowledge-before-maa-' + VERSION + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
    with snapshot.open('xb'):
        pass
    deadline = time.monotonic() + 30

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise ValueError('Knowledge backup exceeded its time limit')

    with closing(_readonly(database)) as original, closing(sqlite3.connect(snapshot)) as saved:
        original.backup(saved, pages=128, progress=progress, sleep=.05)
        if saved.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('Knowledge backup integrity check failed')
    return str(snapshot)


def import_documents(destination: Path, data_dir: Path) -> dict:
    """Preflight in a temporary store, backup, then public add() upserts only."""
    manifest, records = _records(Path(destination))
    # Any source rejected by KnowledgeStore stops before opening the user's DB.
    with TemporaryDirectory(prefix='shu-maa-preflight-') as staging:
        staging_store = KnowledgeStore(Path(staging))
        try:
            for record in records:
                staging_store.add(**record)
        finally:
            staging_store.close()
    data_dir = Path(data_dir)
    database = data_dir / 'knowledge.sqlite3'
    checked_destination(data_dir, 'knowledge.sqlite3')
    keys = {hashlib.sha256(('reference\0' + record['external_id']).encode()).hexdigest() for record in records}
    before = _existing_fingerprints(database, keys)
    backup = _backup(database)
    store = KnowledgeStore(data_dir)
    failures = []
    imported = 0
    try:
        for record in records:
            try:
                store.add(**record)
                imported += 1
            except (OSError, ValueError, sqlite3.Error) as error:
                failures.append({'source_url': record['source_url'], 'error': type(error).__name__})
        hits = {query: [{'subject': row['subject'], 'source_url': row['source_url'],
                          'source_kind': row['source_kind'], 'verified': row['verified']}
                         for row in store.retrieve(query, top_k=3, char_budget=10000,
                             token_budget=6000, source_kinds={'reference'})
                         if row['version'].startswith('MAA:' + VERSION + ';commit:' + COMMIT)]
                for query in QUERIES}
    finally:
        store.close()
    after = _existing_fingerprints(database, keys)
    preserved = all(after.get(identifier) == digest for identifier, digest in before.items())
    report = {
        'version': VERSION, 'commit': COMMIT, 'documents_directory': str(Path(destination).resolve()),
        'database': str(database.resolve()), 'backup': backup,
        'downloaded_documents': len(manifest['documents']),
        'download_failures': manifest.get('failures', []),
        'imported_chunks': imported, 'import_failures': failures,
        'existing_entries_checked': len(before), 'existing_entries_preserved': preserved,
        'retrieval_checks': hits, 'all_retrieval_checks_hit': all(hits.values()),
        'trust': 'reference/source_reported; verified=false; separate from explicit preferences',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    _write_json(checked_destination(Path(destination), 'import-report.json'), report)
    if not preserved:
        raise ValueError('An existing entry changed during import; retain the backup and inspect concurrent writes')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs-dir', type=Path, default=DEFAULT_DOCS)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'user-data')
    parser.add_argument('--offline', action='store_true', help='Verify and import the downloaded manifest without network')
    parser.add_argument('--download-only', action='store_true', help='Download/verify sources without opening knowledge.sqlite3')
    args = parser.parse_args(argv)
    try:
        if not args.offline:
            download_documents(args.docs_dir)
        if args.download_only:
            manifest, records = _records(args.docs_dir)
            report = {'version': VERSION, 'commit': COMMIT, 'documents': len(manifest['documents']),
                      'chunks_ready': len(records), 'failures': manifest.get('failures', [])}
        else:
            report = import_documents(args.docs_dir, args.data_dir)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return int(bool(report.get('import_failures') or report.get('download_failures') or report.get('failures')))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print(json.dumps({'error': type(error).__name__, 'detail': str(error)[:300]}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

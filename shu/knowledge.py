"""Local, budgeted retrieval of attributed facts and explicit user memories.

No network, model calls, image storage, game access, or implicit transcript
mining happens here. The trusted caller supplies short visible facts. Retrieved
text is reference data, never an instruction or a permission grant.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, urlsplit


SOURCE_KINDS = frozenset({'reference', 'observed_user_game', 'user_preference', 'task_experience', 'model_inference'})
_STATUS = {
    'reference': {'unverified', 'source_reported'},
    'observed_user_game': {'unverified', 'observed'},
    'user_preference': {'user_stated'},
    'task_experience': {'unverified', 'user_stated', 'observed'},
    'model_inference': {'unverified'},
}
_TTL = {'reference': 7*86400, 'observed_user_game': 7*86400,
        'task_experience': 30*86400, 'model_inference': 86400, 'user_preference': None}
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+|[a-z0-9]+(?:[-_][a-z0-9]+)*')


def _normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold().strip()


def search_terms(text: str) -> list[str]:
    """Small Chinese character/bigram index plus Latin words and stage codes.

    This is lexical retrieval, not semantic understanding. It intentionally
    needs no tokenizer download, embeddings, network, or model service.
    """
    terms = []
    for match in _CJK.finditer(_normalize(text)):
        word = match.group()
        if '\u3400' <= word[0] <= '\u9fff':
            if len(word) <= 16:
                terms.append(word)
            if len(word) > 1:
                terms.extend(word[index:index+2] for index in range(len(word)-1))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))


def estimate_tokens(text: str) -> int:
    """Provider-independent estimate, not DeepSeek's actual billed tokenizer."""
    ascii_count = sum(ord(character) < 128 for character in text)
    return math.ceil(ascii_count/3) + len(text) - ascii_count


def _timestamp(value, fallback: float) -> tuple[str, float]:
    if value is None:
        epoch = fallback
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('updated_at 必须包含时区')
        epoch = parsed.timestamp()
    else:
        raise ValueError('updated_at 需要是时间戳或带时区 ISO 日期')
    if not math.isfinite(epoch):
        raise ValueError('updated_at 无效')
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds'), epoch


def _positive(value: int, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f'{label} 超出允许范围')
    return value


class KnowledgeStore:
    """Thread-safe SQLite FTS5 store in ``data_dir/knowledge.sqlite3``.

    Evidence status describes provenance, not universal truth. ``observed``
    requires a caller-supplied visible result/evidence ID; a reference or model
    inference can never claim it. Source timestamps and local cache times are
    kept separate, so an old but freshly checked page is not instantly stale.
    """
    def __init__(self, data_dir: Path, *, clock=time.time, secrets=()):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir/'knowledge.sqlite3'
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._secrets = self._validated_secrets(secrets)
        self._db = sqlite3.connect(self.database_path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id INTEGER PRIMARY KEY, external_key TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL, text TEXT NOT NULL,
                source_kind TEXT NOT NULL, source_url TEXT NOT NULL,
                version TEXT NOT NULL, updated_at TEXT NOT NULL,
                cached_at REAL NOT NULL, expires_at REAL,
                evidence_status TEXT NOT NULL, evidence_id TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_search USING fts5(subject_terms, text_terms);
        ''')

    @staticmethod
    def _validated_secrets(values):
        if isinstance(values, (str, bytes)):
            raise TypeError('secrets 需要字符串集合')
        values = tuple(values)
        if any(not isinstance(value, str) for value in values):
            raise TypeError('secrets 需要字符串集合')
        return tuple(sorted({value for value in values if value}, key=len, reverse=True))

    def _require_open(self):
        if self._closed:
            raise RuntimeError('知识库已经关闭')

    def _redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, '[Key 已隐藏]')
        text = re.sub(r'\bsk-[A-Za-z0-9_.\-]{8,}', '[Key 已隐藏]', text)
        text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9_.~+/=\-]+', 'Bearer [Key 已隐藏]', text)
        return re.sub(r'''(?ix)(\b(?:DEEPSEEK_API_KEY|API_KEY|api-key)\b\s*[:=]\s*)
                         (?:"[^"\r\n]*"|'[^'\r\n]*'|[A-Za-z0-9_.~+/=\-]+)''',
                      r'\1[Key 已隐藏]', text)

    def set_secrets(self, values):
        selected = self._validated_secrets(values)
        with self._lock:
            self._require_open()
            self._secrets = selected
            # New exact secrets are removed from existing entries/indexes too.
            with self._db:
                for row in self._db.execute('SELECT * FROM knowledge_entries').fetchall():
                    fields = [self._redact(row[name]) for name in ('subject', 'text', 'source_url', 'version', 'evidence_id')]
                    if fields != [row[name] for name in ('subject', 'text', 'source_url', 'version', 'evidence_id')]:
                        self._db.execute('UPDATE knowledge_entries SET subject=?,text=?,source_url=?,version=?,evidence_id=? WHERE id=?', (*fields,row['id']))
                        self._index(row['id'], fields[0], fields[1])

    def _index(self, identifier, subject, text):
        self._db.execute('DELETE FROM knowledge_search WHERE rowid=?', (identifier,))
        self._db.execute('INSERT INTO knowledge_search(rowid,subject_terms,text_terms) VALUES(?,?,?)',
                         (identifier, ' '.join(search_terms(subject)), ' '.join(search_terms(text))))

    def add(self, subject: str, text: str, *, source_kind='reference', source_url='', version='',
            updated_at=None, ttl_seconds=None, evidence_status='unverified', evidence_id='', external_id=None) -> dict:
        if source_kind not in SOURCE_KINDS:
            raise ValueError('知识来源类型不受支持')
        if evidence_status not in _STATUS[source_kind]:
            raise ValueError('来源不能声明这种验证状态；模型推断不能标成已验证')
        for name, value, length in (('subject', subject, 200), ('text', text, 6000),
                                     ('source_url', source_url, 1000), ('version', version, 200),
                                     ('evidence_id', evidence_id, 300)):
            if not isinstance(value, str) or len(value) > length or '\x00' in value:
                raise ValueError(f'{name} 必须是有界纯文本')
        if not subject.strip() or not text.strip():
            raise ValueError('知识主题和内容不能为空')
        if re.search(r'(?i)data:image|base64,|<\s*(?:think|analysis)\b|["\'](?:reasoning_content|hidden_reasoning)["\']\s*:', text):
            raise ValueError('知识库只接受可见事实，不接受图片或隐藏推理载荷')
        if evidence_status == 'observed' and not evidence_id.strip():
            raise ValueError('实际观察经验必须关联可见结果的 evidence_id')
        if source_kind == 'reference' and not source_url:
            raise ValueError('检索资料必须保留来源 URL')
        if source_url:
            url = urlsplit(source_url)
            if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
                raise ValueError('来源需要不含凭据的 HTTP(S) URL')
            if any(key.lower() in {'key', 'api_key', 'token', 'access_token', 'apikey'} for key, _ in parse_qsl(url.query)):
                raise ValueError('来源 URL 不能包含访问凭据')
        if external_id is not None and (not isinstance(external_id, str) or len(external_id) > 1000):
            raise ValueError('external_id 无效')
        ttl = _TTL[source_kind] if ttl_seconds is None else ttl_seconds
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int,float)) or not math.isfinite(ttl) or not 0 < ttl <= 10*365*86400):
            raise ValueError('缓存时限无效')
        now = float(self._clock())
        updated, _ = _timestamp(updated_at, now)
        with self._lock:
            self._require_open()
            subject, text, source_url, version, evidence_id = [self._redact(value.strip()) for value in (subject,text,source_url,version,evidence_id)]
            # A caller key allows refreshing one section without duplicate cache
            # records. Never persist the raw external key (it may contain data).
            identity = external_id if external_id is not None else json.dumps([subject,source_url,version],ensure_ascii=False)
            key = hashlib.sha256((source_kind+'\0'+identity).encode()).hexdigest()
            with self._db:
                self._db.execute('''INSERT INTO knowledge_entries
                    (external_key,subject,text,source_kind,source_url,version,updated_at,cached_at,expires_at,evidence_status,evidence_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(external_key) DO UPDATE SET
                    subject=excluded.subject,text=excluded.text,source_url=excluded.source_url,
                    version=excluded.version,updated_at=excluded.updated_at,cached_at=excluded.cached_at,
                    expires_at=excluded.expires_at,evidence_status=excluded.evidence_status,evidence_id=excluded.evidence_id''',
                    (key,subject,text,source_kind,source_url,version,updated,now,None if ttl is None else now+ttl,evidence_status,evidence_id))
                row = self._db.execute('SELECT * FROM knowledge_entries WHERE external_key=?',(key,)).fetchone()
                self._index(row['id'],subject,text)
            return self._record(row,now)

    def add_preference(self, subject, text) -> dict:
        return self.add(subject,text,source_kind='user_preference',evidence_status='user_stated',external_id=subject)

    def add_experience(self, subject, text, *, evidence_id='', verified=False, version='') -> dict:
        if not isinstance(verified,bool):
            raise ValueError('verified 必须为布尔值')
        return self.add(subject,text,source_kind='observed_user_game',version=version,
                        evidence_id=evidence_id,evidence_status='observed' if verified else 'unverified')

    def _record(self, row, now):
        result = {key: self._redact(row[key]) if isinstance(row[key],str) else row[key]
                  for key in ('id','subject','text','source_kind','source_url','version','updated_at','cached_at','expires_at','evidence_status','evidence_id')}
        result['stale'] = row['expires_at'] is not None and now >= row['expires_at']
        result['verified'] = row['evidence_status']=='observed'
        return result

    def retrieve(self, query: str, *, top_k=4, char_budget=2400, token_budget=1600,
                 source_kinds=None, include_stale=True) -> list[dict]:
        top_k = _positive(top_k,'top_k',20)
        char_budget = _positive(char_budget,'char_budget',100000)
        token_budget = _positive(token_budget,'token_budget',50000)
        if not isinstance(query,str) or len(query)>1000:
            raise ValueError('检索词需要是不超过1000字的文本')
        if source_kinds is None:
            kinds = sorted(SOURCE_KINDS)
        else:
            if isinstance(source_kinds,str):
                raise ValueError('source_kinds 需要类型集合')
            kinds = list(source_kinds)
            if not kinds or any(kind not in SOURCE_KINDS for kind in kinds):
                raise ValueError('来源筛选无效')
        terms = search_terms(query)[:64]
        if not terms:
            return []
        expression = ' OR '.join('"'+term.replace('"','""')+'"' for term in terms)
        normalized = _normalize(query)
        with self._lock:
            self._require_open()
            now = float(self._clock())
            rows = self._db.execute('''SELECT e.*, bm25(knowledge_search,4.0,1.0) AS score
                FROM knowledge_search JOIN knowledge_entries e ON e.id=knowledge_search.rowid
                WHERE knowledge_search MATCH ? AND e.source_kind IN ('''+','.join('?' for _ in kinds)+') ORDER BY score LIMIT ?',
                (expression,*kinds,max(40,top_k*12))).fetchall()
            candidates = []
            for row in rows:
                result = self._record(row,now)
                if result['stale'] and not include_stale:
                    continue
                exact_subject = normalized == _normalize(result['subject'])
                result['exact_match'] = exact_subject or normalized in _normalize(result['text'])
                candidates.append((exact_subject,result['exact_match'],not result['stale'],-row['score'],row['cached_at'],result))
            candidates.sort(key=lambda item:item[:-1],reverse=True)
            return self._pack([item[-1] for item in candidates],top_k,char_budget,token_budget,query)

    @staticmethod
    def _excerpt(text, query, length):
        if len(text)<=length:
            return text,False
        lowered=text.casefold()
        terms=search_terms(query)
        positions=[(len(term),lowered.find(term)) for term in terms if lowered.find(term)>=0]
        position=sorted(positions,key=lambda item:(-item[0],item[1]))[0][1] if positions else 0
        start=max(0,min(position-min(120,length//4),len(text)-length))
        excerpt=text[start:start+length]
        return excerpt,True

    @staticmethod
    def _pack(candidates, top_k, char_budget, token_budget, query=''):
        selected = []
        used_chars = 0; used_tokens = 0
        for entry in candidates:
            if len(selected)>=top_k:
                break
            result = dict(entry, truncated=False)
            # Short excerpts keep one large page from consuming all context.
            full_text=result['text']
            excerpt_length=min(900,len(full_text))
            result['text'],result['truncated']=KnowledgeStore._excerpt(full_text,query,excerpt_length)
            while result['text']:
                result['estimated_tokens']=0
                for _ in range(3):
                    encoded=json.dumps(result,ensure_ascii=False,separators=(',',':'))
                    cost=estimate_tokens(encoded)
                    if result['estimated_tokens']==cost:break
                    result['estimated_tokens']=cost
                encoded=json.dumps(result,ensure_ascii=False,separators=(',',':'))
                cost=estimate_tokens(encoded)
                if used_chars+len(encoded)<=char_budget and used_tokens+cost<=token_budget:
                    selected.append(result);used_chars+=len(encoded);used_tokens+=cost
                    break
                if excerpt_length<=32:
                    break
                excerpt_length-=max(1,excerpt_length//8)
                result['text'],result['truncated']=KnowledgeStore._excerpt(full_text,query,excerpt_length)
        return selected

    def preferences(self, *, top_k=8, char_budget=1800, token_budget=1200) -> list[dict]:
        top_k=_positive(top_k,'top_k',20)
        char_budget=_positive(char_budget,'char_budget',100000)
        token_budget=_positive(token_budget,'token_budget',50000)
        with self._lock:
            self._require_open()
            rows=self._db.execute("SELECT * FROM knowledge_entries WHERE source_kind='user_preference' ORDER BY cached_at DESC,id DESC LIMIT ?",(top_k,)).fetchall()
            return self._pack([dict(self._record(row,float(self._clock())),exact_match=False) for row in rows],top_k,char_budget,token_budget)

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close();self._closed=True

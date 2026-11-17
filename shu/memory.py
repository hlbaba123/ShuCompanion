"""Persist visible conversation text and restore recent completed dialogue turns.

This store does not persist provider payloads, tool calls/results, images, hidden
reasoning, or configuration. System status messages remain available to the UI
but are excluded from the model's restored conversational context.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import threading
from typing import Iterable, Iterator


VISIBLE_ROLES = frozenset({"user", "assistant", "system"})
MAX_TEXT_LENGTH = 65536


class MemoryClosedError(RuntimeError):
    """The conversation store has already been closed."""


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name}必须是正整数")
    return value


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Explicit {name} identity required")
    return value


def _known_secrets(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("secrets 需要是字符串集合")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("secrets 只能包含字符串")
        if value:
            result.append(value)
    # Replace a longer token before a shorter overlapping one.
    return tuple(sorted(set(result), key=len, reverse=True))


class ConversationMemory:
    """Thread-safe, SQLite-backed visible transcript.

    ``data_dir`` is a directory, not a database filename. ``secrets`` is an
    optional collection of credential values supplied by the owner for exact
    redaction; it is kept in memory only. Recognizable credential syntax is
    redacted even when this collection is empty. This class never reads keys.
    """

    def __init__(self, data_dir: Path, *, secrets: Iterable[str] = (), task_visible=None):
        self._task_visible = task_visible
        self._secrets = _known_secrets(secrets)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "conversation.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(
            self.database_path, timeout=10, isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL CHECK(length(content) > 0),
                created_at TEXT NOT NULL
            )
        """)

        with self._transaction():
            self._db.execute('CREATE TABLE IF NOT EXISTS conversation_task_links(message_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, task_id TEXT NOT NULL)')
            self._db.execute('CREATE TABLE IF NOT EXISTS conversation_deleted_tasks(task_id TEXT PRIMARY KEY)')
            self._db.execute('CREATE TABLE IF NOT EXISTS conversation_request_lifecycle(request_id TEXT PRIMARY KEY, hidden INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0)')
            # Admission precedes task routing. Keep the request identity in the
            # same commit as its text so a restart can finish that projection.
            self._db.execute('CREATE TABLE IF NOT EXISTS conversation_request_links(message_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL)')
            self._db.execute('CREATE INDEX IF NOT EXISTS conversation_request_messages ON conversation_request_links(request_id)')
            # Minimal ownership survives deletion, fencing delayed replies and
            # requests with no remaining transcript rows against reassignment.
            self._db.execute('CREATE TABLE IF NOT EXISTS conversation_request_tasks(request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL)')
            self._db.execute('INSERT OR IGNORE INTO conversation_request_links SELECT message_id,request_id FROM conversation_task_links')
            self._db.execute('INSERT OR IGNORE INTO conversation_request_tasks SELECT request_id,MIN(task_id) FROM conversation_task_links GROUP BY request_id HAVING COUNT(DISTINCT task_id)=1')

    def set_task_visibility(self, callback) -> None:
        self._task_visible = callback

    def _visible(self, task_id) -> bool:
        return not task_id or self._task_visible is None or bool(self._task_visible(task_id))

    def _request_task(self, request_id: str) -> str | None:
        rows = self._db.execute(
            'SELECT task_id FROM conversation_request_tasks WHERE request_id=? '
            'UNION SELECT task_id FROM conversation_task_links WHERE request_id=?',
            (request_id, request_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError('Request has conflicting task ownership')
        return rows[0]['task_id'] if rows else None

    def request_task(self,request_id):
        with self._lock:
            self._require_open()
            return self._request_task(_identity(request_id,'request'))

    def request_deleted(self,request_id):
        with self._lock:
            self._require_open()
            row=self._db.execute('SELECT deleted FROM conversation_request_lifecycle WHERE request_id=?',
                                  (_identity(request_id,'request'),)).fetchone()
            return bool(row and row['deleted'])

    def hide_request(self,request_id,hidden=True):
        if type(hidden) is not bool:raise ValueError('Hidden must be boolean')
        with self._lock,self._transaction():
            request_id=_identity(request_id,'request')
            if self._request_task(request_id) is not None:
                raise ValueError('Task-bound messages require task lifecycle management')
            if self.request_deleted(request_id):raise ValueError('Request was permanently deleted')
            self._db.execute('INSERT INTO conversation_request_lifecycle(request_id,hidden) VALUES(?,?) '
                'ON CONFLICT(request_id) DO UPDATE SET hidden=excluded.hidden',(request_id,int(hidden)))

    def delete_request_messages(self,request_id):
        with self._lock,self._transaction():
            request_id=_identity(request_id,'request')
            if self._request_task(request_id) is not None:
                raise ValueError('Task-bound messages cannot be deleted as ordinary chat')
            self._db.execute('INSERT INTO conversation_request_lifecycle VALUES(?,1,1) '
                'ON CONFLICT(request_id) DO UPDATE SET hidden=1,deleted=1',(request_id,))
            count=self._db.execute('DELETE FROM conversation_messages WHERE id IN '
                '(SELECT message_id FROM conversation_request_links WHERE request_id=?)',(request_id,)).rowcount
            self._db.execute('DELETE FROM conversation_request_links WHERE request_id=?',(request_id,))
            return count

    def _request_hidden(self,request_id):
        row=self._db.execute('SELECT hidden,deleted FROM conversation_request_lifecycle WHERE request_id=?',
                              (request_id,)).fetchone()
        return bool(row and (row['hidden'] or row['deleted']))

    def bind_request(self, request_id: str, task_id: str, message_ids: list[int]) -> None:
        """Bind explicit IDs and all durably admitted text for this request.

        Empty IDs are sufficient after restart. Recovered ownership may refer to
        a trashed task; visibility controls reads and does not erase attribution.
        """
        request_id = _identity(request_id, 'request')
        task_id = _identity(task_id, 'task')
        explicit_ids = {_positive_integer(identifier, 'message_id') for identifier in message_ids}
        with self._lock, self._transaction():
            self._bind_request(request_id, task_id, explicit_ids)

    def _bind_request(self, request_id, task_id, explicit_ids, *, allow_deleted=False):
        if self._request_hidden(request_id):raise ValueError('Chat request is recycled or deleted')
        if not allow_deleted and self._db.execute('SELECT 1 FROM conversation_deleted_tasks WHERE task_id=?',(task_id,)).fetchone():
            raise ValueError('Task has been deleted')
        previous_task = self._request_task(request_id)
        if previous_task is not None and previous_task != task_id:
            raise ValueError('Request ownership cannot be replaced')
        identifiers = explicit_ids | {
            row['message_id'] for row in self._db.execute(
                'SELECT message_id FROM conversation_request_links WHERE request_id=?', (request_id,))
        }
        for identifier in identifiers:
            if not self._db.execute('SELECT 1 FROM conversation_messages WHERE id=?',(identifier,)).fetchone():
                raise ValueError('Message does not exist')
            request = self._db.execute('SELECT request_id FROM conversation_request_links WHERE message_id=?', (identifier,)).fetchone()
            old = self._db.execute('SELECT * FROM conversation_task_links WHERE message_id=?',(identifier,)).fetchone()
            if (request and request['request_id'] != request_id) or (old and (old['request_id']!=request_id or old['task_id']!=task_id)):
                raise ValueError('Message ownership cannot be replaced')
        self._db.execute('INSERT OR IGNORE INTO conversation_request_tasks VALUES(?,?)', (request_id, task_id))
        for identifier in identifiers:
            self._db.execute('INSERT OR IGNORE INTO conversation_request_links VALUES(?,?)', (identifier, request_id))
            self._db.execute('INSERT OR IGNORE INTO conversation_task_links VALUES(?,?,?)',(identifier,request_id,task_id))

    def sync_request_bindings(self, rows: Iterable[dict]) -> None:
        """Recover authoritative workflow ``request_id``/``task_id`` bindings.

        Supply all lifecycle states, including trashed tasks. A pending target or
        matching user text is never evidence that a message belongs to a task.
        A task already deleted from this projection stays deleted if workflow
        cleanup was interrupted and its request rows are replayed on restart.
        """
        bindings = [(_identity(row['request_id'], 'request'), _identity(row['task_id'], 'task'))
                    for row in rows if row['task_id']]
        with self._lock, self._transaction():
            for request_id, task_id in bindings:
                self._bind_request(request_id, task_id, set(), allow_deleted=True)
            for task_id in {task for _, task in bindings}:
                if self._db.execute('SELECT 1 FROM conversation_deleted_tasks WHERE task_id=?', (task_id,)).fetchone():
                    self._delete_task_messages(task_id)

    def _delete_task_messages(self, task_id):
        self._db.execute('DELETE FROM conversation_request_links WHERE message_id IN (SELECT message_id FROM conversation_task_links WHERE task_id=?)', (task_id,))
        count = self._db.execute('DELETE FROM conversation_messages WHERE id IN (SELECT message_id FROM conversation_task_links WHERE task_id=?)',(task_id,)).rowcount
        self._db.execute('DELETE FROM conversation_task_links WHERE task_id=?',(task_id,))
        return count

    def delete_task(self, task_id: str) -> int:
        task_id = _identity(task_id, 'task')
        with self._lock, self._transaction():
            requests = self._db.execute(
                'SELECT request_id FROM conversation_request_tasks WHERE task_id=? '
                'UNION SELECT request_id FROM conversation_task_links WHERE task_id=?',
                (task_id, task_id),
            ).fetchall()
            for request in requests:
                self._bind_request(request['request_id'], task_id, set(), allow_deleted=True)
            self._db.execute('INSERT OR IGNORE INTO conversation_deleted_tasks VALUES(?)',(task_id,))
            return self._delete_task_messages(task_id)

    def _require_open(self) -> None:
        if self._closed:
            raise MemoryClosedError("会话记录已经关闭")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._require_open()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def set_secrets(self, values: Iterable[str]) -> None:
        """Update exact redaction tokens without writing those tokens to disk."""
        secrets = _known_secrets(values)
        with self._lock:
            self._require_open()
            self._secrets = secrets

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[Key 已隐藏]")
        # Do not retain values pasted in the usual environment/config forms.
        text = re.sub(
            r"(?i)(\b(?:DEEPSEEK_API_KEY|API_KEY|api-key)\b\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[A-Za-z0-9_.~+/=\-]+)",
            r"\1[Key 已隐藏]", text,
        )
        text = re.sub(r"\bsk-[A-Za-z0-9_.\-]{8,}", "[Key 已隐藏]", text)
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=\-]+", "Bearer [Key 已隐藏]", text)
        return text

    def append(self, role: str, text: str, *, request_id=None, task_id=None) -> dict:
        """Commit one visible text message; return its detached stored record."""
        if not isinstance(role, str) or role not in VISIBLE_ROLES:
            raise ValueError("只保存 user、assistant 或 system 可见文本")
        if not isinstance(text, str):
            raise TypeError("会话内容必须是文本，不能保存模型或工具载荷")
        if not text.strip() or "\x00" in text:
            raise ValueError("会话内容需要是有效的非空文本")
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError("单条会话内容过长")
        if request_id is not None:
            request_id = _identity(request_id, 'request')
        if task_id is not None:
            task_id = _identity(task_id, 'task')
            if request_id is None:
                raise ValueError('Task message requires request identity')
        with self._lock:
            content = self._redact(text)
            created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            with self._transaction():
                if request_id and self._request_hidden(request_id):
                    raise ValueError('Chat request is recycled or deleted')
                previous_task = self._request_task(request_id) if request_id else None
                if previous_task is not None:
                    if task_id is not None and task_id != previous_task:
                        raise ValueError('Request ownership cannot be replaced')
                    task_id = previous_task
                if task_id and (not self._visible(task_id) or self._db.execute('SELECT 1 FROM conversation_deleted_tasks WHERE task_id=?',(task_id,)).fetchone()):
                    raise ValueError('Task is not writable')
                cursor = self._db.execute(
                    "INSERT INTO conversation_messages(role, content, created_at) VALUES (?, ?, ?)",
                    (role, content, created_at),
                )
                identifier = cursor.lastrowid
                if request_id:
                    self._db.execute('INSERT INTO conversation_request_links VALUES(?,?)', (identifier, request_id))
                if task_id:
                    self._bind_request(request_id, task_id, set())
            return {"id": identifier, "role": role, "content": content, "created_at": created_at}

    def load_messages(self, limit: int = 100, *, task_id=None, request_id=None) -> list[dict]:
        """Return the latest visible records in chronological commit order."""
        limit = _positive_integer(limit, "limit")
        with self._lock:
            self._require_open()
            rows = self._db.execute(
                'SELECT m.id,m.role,m.content,m.created_at,l.task_id,r.request_id FROM conversation_messages m '
                'LEFT JOIN conversation_task_links l ON l.message_id=m.id '
                'LEFT JOIN conversation_request_links r ON r.message_id=m.id '
                'WHERE (? IS NULL OR l.task_id=?) AND (? IS NULL OR r.request_id=?) ORDER BY m.id DESC',
                (task_id,task_id,request_id,request_id)
            )
            records = []
            try:
                for row in rows:
                    if row['request_id'] and self._request_hidden(row['request_id']):continue
                    if not self._visible(row['task_id']):
                        continue
                    records.append({key: row[key] for key in ('id', 'role', 'content', 'created_at','task_id','request_id')})
                    if len(records) == limit:
                        break
            finally:
                rows.close()
            # Re-redact at the boundary in case the owner added a known secret
            # after an earlier write. Mutating this result cannot alter storage.
            return [dict(row, content=self._redact(row["content"])) for row in reversed(records)]

    def model_messages(self, last_turns: int = 12, *, task_id=None, request_id=None) -> list[dict]:
        """Restore complete user/assistant exchanges, without diagnostic status.

        Adjacent user messages before a reply form a single turn, as do adjacent
        assistant messages after that reply. A final unanswered user request is
        intentionally excluded: the caller can submit it separately rather than
        accidentally replaying an interrupted request as a completed exchange.
        A standalone assistant greeting has no user turn and is also excluded.
        """
        last_turns = _positive_integer(last_turns, "last_turns")
        with self._lock:
            self._require_open()
            # Read backwards until enough completed turns are found. This avoids
            # cutting a multi-message turn in half with an arbitrary row limit.
            rows = self._db.execute(
                "SELECT m.role, m.content, l.task_id,r.request_id FROM conversation_messages m LEFT JOIN conversation_task_links l ON l.message_id=m.id "
                "LEFT JOIN conversation_request_links r ON r.message_id=m.id "
                "WHERE m.role IN ('user', 'assistant') AND (? IS NULL OR l.task_id=?) "
                "AND (? IS NULL OR r.request_id=?) ORDER BY m.id DESC", (task_id,task_id,request_id,request_id)
            )
            reverse_turns: list[list[dict]] = []
            pending: list[dict] = []
            has_users = False
            for row in rows:
                if row['request_id'] and self._request_hidden(row['request_id']):continue
                if not self._visible(row["task_id"]): continue
                message = {"role": row["role"], "content": self._redact(row["content"])}
                if not pending:
                    # Ignore an incomplete request at the end of the transcript.
                    if message["role"] == "assistant":
                        pending = [message]
                    continue
                if message["role"] == "user":
                    has_users = True
                    pending.append(message)
                elif has_users:
                    reverse_turns.append(list(reversed(pending)))
                    if len(reverse_turns) == last_turns:
                        pending = []
                        break
                    pending, has_users = [message], False
                else:
                    pending.append(message)
            if pending and has_users and len(reverse_turns) < last_turns:
                reverse_turns.append(list(reversed(pending)))
            rows.close()
            return [message for turn in reversed(reverse_turns) for message in turn]

    def clear(self) -> None:
        """Clear transcript content atomically; keep monotonically increasing IDs."""
        with self._lock:
            with self._transaction():
                self._db.execute("DELETE FROM conversation_task_links")
                self._db.execute("DELETE FROM conversation_request_links")
                self._db.execute("DELETE FROM conversation_messages")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True


"""Durable task facts and guards for Shu's local execution service.

This module does not infer game progress from chat or generate input.  The
trusted game adapter records observations and confirmed results.  Asynchronous
callers should carry ``goal_version`` through every callback so an old model
response cannot advance a newer goal.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator
import uuid


class ActionRejected(RuntimeError):
    """An action or asynchronous update is no longer admissible."""


class TaskNotFound(KeyError):
    """The requested task does not exist."""


STATUSES = frozenset({
    "draft", "observing", "running", "paused", "needs_input",
    "completed", "failed", "cancelled",
})
ACTIVE_STATUSES = frozenset({"observing", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
UNVERIFIED_STATES = frozenset({"", "unknown", "unclassified", "unverified", "stale", "error"})
RESULT_STATES = frozenset({
    "battle_result", "battle_success", "battle_finished", "settlement",
    "victory", "result", "结算", "作战成功",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


def _epoch(value: Any) -> float:
    """Accept Unix seconds, an ISO timestamp, or a datetime (naive means UTC)."""
    if isinstance(value, bool):
        raise ValueError("截图时间格式不正确")
    try:
        if isinstance(value, datetime):
            moment = value
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            result = moment.timestamp()
        elif isinstance(value, str):
            try:
                result = float(value)
            except ValueError:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                result = moment.timestamp()
        else:
            result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("截图时间格式不正确") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("截图时间格式不正确")
    if result - time.time() > 1.0:
        raise ActionRejected("截图时间位于未来，请重新观察")
    return result


class TaskEngine:
    """Thread-safe SQLite-backed task manager.

    Every mutating method commits the task change and its event atomically.
    BEGIN IMMEDIATE also serializes concurrent writers using different engine
    instances.  Returned dictionaries are detached snapshots.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "tasks.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(
            self.database_path, timeout=10, isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                status TEXT NOT NULL,
                completed_runs INTEGER NOT NULL DEFAULT 0 CHECK(completed_runs >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                latest_observation_id TEXT,
                latest_observation_version INTEGER,
                latest_observation_at REAL,
                latest_observation_state TEXT,
                latest_window_id TEXT
            );
            CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                goal_version INTEGER NOT NULL,
                type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS task_events_by_task ON task_events(task_id, seq);
            CREATE TABLE IF NOT EXISTS observations (
                task_id TEXT NOT NULL REFERENCES tasks(id),
                observation_id TEXT NOT NULL,
                goal_version INTEGER NOT NULL,
                captured_at REAL NOT NULL,
                window_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, observation_id)
            );
            CREATE TABLE IF NOT EXISTS battle_results (
                task_id TEXT NOT NULL REFERENCES tasks(id),
                result_id TEXT NOT NULL,
                goal_version INTEGER NOT NULL,
                observation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, result_id)
            );
            PRAGMA user_version=1;
        """)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("任务数据库已经关闭")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _task_row(self, task_id: str) -> sqlite3.Row:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"找不到任务：{task_id}")
        return row

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task.update(task_id=task["id"], text=task["goal"], goal_version=task["version"])
        return task

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["details"] = json.loads(event["details"])
        return event

    @staticmethod
    def _check_version(row: sqlite3.Row, goal_version: int | None) -> None:
        if goal_version is not None:
            if type(goal_version) is not int or goal_version != row["version"]:
                raise ActionRejected("目标已改变，旧版本操作已取消")

    def _append_event(
        self, row: sqlite3.Row, event_type: str, message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if details is not None and not isinstance(details, dict):
            raise TypeError("事件详情必须是字典")
        encoded = json.dumps(details or {}, ensure_ascii=False, allow_nan=False)
        cursor = self._db.execute(
            "INSERT INTO task_events(task_id,goal_version,type,message,created_at,details) "
            "VALUES(?,?,?,?,?,?)",
            (row["id"], row["version"], event_type, message, _utc_now(), encoded),
        )
        return self._event_dict(self._db.execute(
            "SELECT * FROM task_events WHERE seq=?", (cursor.lastrowid,),
        ).fetchone())

    def _invalidate(self, task_id: str, *, status: str, goal: str | None = None) -> sqlite3.Row:
        """Bump the goal generation and clear all action-bearing observations."""
        if goal is None:
            self._db.execute(
                "UPDATE tasks SET version=version+1,status=?,updated_at=?,"
                "latest_observation_id=NULL,latest_observation_version=NULL,"
                "latest_observation_at=NULL,latest_observation_state=NULL,latest_window_id=NULL "
                "WHERE id=?", (status, _utc_now(), task_id),
            )
        else:
            self._db.execute(
                "UPDATE tasks SET goal=?,version=version+1,status=?,updated_at=?,"
                "latest_observation_id=NULL,latest_observation_version=NULL,"
                "latest_observation_at=NULL,latest_observation_state=NULL,latest_window_id=NULL "
                "WHERE id=?", (goal, status, _utc_now(), task_id),
            )
        return self._task_row(task_id)

    def submit_goal(self, text: str) -> dict[str, Any]:
        goal = _nonempty(text, "任务目标")
        task_id, now = uuid.uuid4().hex, _utc_now()
        with self._transaction():
            self._db.execute(
                "INSERT INTO tasks(id,goal,version,status,completed_runs,created_at,updated_at) "
                "VALUES(?,?,1,'draft',0,?,?)", (task_id, goal, now, now),
            )
            row = self._task_row(task_id)
            self._append_event(row, "goal_created", "已保存目标，等待连接与观察", {"goal": goal})
            return self._task_dict(row)

    def update_goal(self, task_id: str, text: str) -> dict[str, Any]:
        goal = _nonempty(text, "任务目标")
        with self._transaction():
            before = self._task_row(task_id)
            if before["status"] in TERMINAL_STATUSES:
                raise ActionRejected("任务已经结束，请新建目标")
            row = self._invalidate(task_id, status="draft", goal=goal)
            self._append_event(row, "goal_updated", "目标已更新，等待重新观察", {
                "previous_version": before["version"], "goal": goal,
            })
            return self._task_dict(row)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        with self._transaction():
            row = self._task_row(task_id)
            if row["status"] in TERMINAL_STATUSES:
                raise ActionRejected("任务已经结束")
            if row["status"] == "paused":
                return self._task_dict(row)
            row = self._invalidate(task_id, status="paused")
            self._append_event(row, "task_paused", "已暂停操作，旧动作已失效")
            return self._task_dict(row)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        with self._transaction():
            row = self._task_row(task_id)
            if row["status"] not in {"paused", "needs_input"}:
                raise ActionRejected("只有暂停或等待处理的任务可以继续")
            row = self._invalidate(task_id, status="observing")
            self._append_event(row, "task_resumed", "准备继续，需要新的游戏观察")
            return self._task_dict(row)

    def stop_task(self, task_id: str | None = None) -> dict[str, Any] | None:
        """Cancel an explicit task, or the newest unfinished task when omitted."""
        with self._transaction():
            if task_id is None:
                row = self._db.execute(
                    "SELECT * FROM tasks WHERE status NOT IN ('completed','failed','cancelled') "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                ).fetchone()
                if row is None:
                    return None
                task_id = row["id"]
            else:
                row = self._task_row(task_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._task_dict(row)
            row = self._invalidate(task_id, status="cancelled")
            self._append_event(row, "task_cancelled", "已停止操作，旧动作与观察已失效")
            return self._task_dict(row)

    def set_status(
        self, task_id: str, status: str, message: str = "", *,
        goal_version: int | None = None,
    ) -> dict[str, Any]:
        """Accept an executor status; it never invents observations or progress.

        Cancellation, pause, and resume should use their named methods.  A
        completed state needs a current recognized observation and active work.
        """
        if status not in STATUSES:
            raise ValueError("未知任务状态")
        with self._transaction():
            row = self._task_row(task_id)
            self._check_version(row, goal_version)
            if row["status"] in TERMINAL_STATUSES:
                if status == row["status"]:
                    return self._task_dict(row)
                raise ActionRejected("任务已经结束，不能推进状态")
            if status == "paused":
                if row["status"] == "paused":
                    return self._task_dict(row)
                row = self._invalidate(task_id, status="paused")
                self._append_event(row, "task_paused", message or "已暂停操作，旧动作已失效")
                return self._task_dict(row)
            if status == "cancelled":
                row = self._invalidate(task_id, status="cancelled")
                self._append_event(row, "task_cancelled", message or "已停止操作，旧动作与观察已失效")
                return self._task_dict(row)
            if row["status"] == "paused":
                raise ActionRejected("任务已暂停，请先继续任务")
            if status == "completed":
                if row["status"] not in ACTIVE_STATUSES or not self._has_known_observation(row):
                    raise ActionRejected("尚无已验证的游戏结果，不能标记完成")
                self._check_action(row, row["version"], row["latest_observation_id"], 5)
            if status == row["status"] and not message:
                return self._task_dict(row)
            if status in {"draft", "needs_input"} and status != row["status"]:
                row = self._invalidate(task_id, status=status)
                self._append_event(row, "status_changed", message or f"任务状态：{status}", {"status": status})
                return self._task_dict(row)
            self._db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=?",
                (status, _utc_now(), task_id),
            )
            row = self._task_row(task_id)
            self._append_event(row, "status_changed", message or f"任务状态：{status}", {"status": status})
            return self._task_dict(row)

    def complete_visual_goal(self, task_id: str, *, goal_version: int,
                             observation_id: str, evidence: str, message: str = "") -> dict[str, Any]:
        """Record a model visual goal check, separately from trusted battle results.

        The screenshot remains semantically unknown for settlement counting and
        generic validate_action. This does not certify a battle or add progress.
        """
        evidence = _nonempty(evidence, "画面证据")
        with self._transaction():
            row = self._task_row(task_id)
            self._check_version(row, goal_version)
            if row["status"] != "running":
                raise ActionRejected("任务未在执行，不能接受完成检查")
            if not observation_id or observation_id != row["latest_observation_id"]:
                raise ActionRejected("完成检查没有关联最新画面")
            age = time.time() - (row["latest_observation_at"] or 0)
            if age < -1 or age > 180:
                raise ActionRejected("完成检查的画面已过期，需要重新观察")
            self._db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?",
                             (_utc_now(), task_id))
            row = self._task_row(task_id)
            self._append_event(row, "visual_goal_checked", message or "模型依据新画面检查目标已达到", {
                "observation_id": observation_id, "evidence": evidence,
                "source": "model_visual_check", "battle_result_confirmed": False,
            })
            return self._task_dict(row)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            return self._task_dict(self._task_row(task_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return [self._task_dict(row) for row in self._db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC,id DESC",
            ).fetchall()]

    def events(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            if task_id is None:
                rows = self._db.execute("SELECT * FROM task_events ORDER BY seq").fetchall()
            else:
                self._task_row(task_id)
                rows = self._db.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY seq", (task_id,),
                ).fetchall()
            return [self._event_dict(row) for row in rows]

    def record_event(
        self, task_id: str, event_type: str, message: str,
        details: dict[str, Any] | None = None, *, goal_version: int | None = None,
    ) -> dict[str, Any]:
        """Append evidence or diagnostics without changing status or counts."""
        event_type = _nonempty(event_type, "事件类型")
        if not isinstance(message, str):
            raise TypeError("事件信息必须是文本")
        with self._transaction():
            row = self._task_row(task_id)
            self._check_version(row, goal_version)
            return self._append_event(row, event_type, message, details)

    def record_observation(
        self, task_id: str, observation_id: str, captured_at: Any, window_id: Any,
        state: str = "unknown", *, goal_version: int | None = None,
    ) -> dict[str, Any]:
        observation_id = _nonempty(observation_id, "观察编号")
        captured = _epoch(captured_at)
        if window_id is None or not str(window_id).strip():
            raise ValueError("游戏窗口编号不能为空")
        window_id = str(window_id).strip()
        state = _nonempty(state, "画面状态").lower()
        with self._transaction():
            row = self._task_row(task_id)
            self._check_version(row, goal_version)
            if row["status"] in TERMINAL_STATUSES or row["status"] == "paused":
                raise ActionRejected("任务已经停止或暂停，不能接受旧观察")
            if self._db.execute(
                "SELECT 1 FROM observations WHERE task_id=? AND observation_id=?",
                (task_id, observation_id),
            ).fetchone():
                raise ActionRejected("观察编号已使用，请重新截图")
            if row["latest_observation_at"] is not None and captured < row["latest_observation_at"]:
                raise ActionRejected("收到过时观察，已保留较新的画面")
            created_at = _utc_now()
            self._db.execute(
                "INSERT INTO observations(task_id,observation_id,goal_version,captured_at,window_id,state,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, observation_id, row["version"], captured, window_id, state, created_at),
            )
            self._db.execute(
                "UPDATE tasks SET latest_observation_id=?,latest_observation_version=?,"
                "latest_observation_at=?,latest_observation_state=?,latest_window_id=?,updated_at=? WHERE id=?",
                (observation_id, row["version"], captured, state, window_id, created_at, task_id),
            )
            row = self._task_row(task_id)
            observation = {
                "id": observation_id, "observation_id": observation_id,
                "task_id": task_id, "goal_version": row["version"],
                "captured_at": captured, "window_id": window_id, "state": state,
                "created_at": created_at,
            }
            self._append_event(row, "observation_recorded", "已记录游戏画面观察", observation)
            return observation

    @staticmethod
    def _has_known_observation(row: sqlite3.Row) -> bool:
        return (
            bool(row["latest_observation_id"])
            and row["latest_observation_version"] == row["version"]
            and row["latest_observation_state"] not in UNVERIFIED_STATES
        )

    def _check_action(
        self, row: sqlite3.Row, goal_version: int, observation_id: str,
        max_age_seconds: float,
    ) -> None:
        self._check_version(row, goal_version)
        if row["status"] not in ACTIVE_STATUSES:
            raise ActionRejected("任务尚未运行，或已暂停、停止")
        if not self._has_known_observation(row):
            raise ActionRejected("当前画面尚未确认，请先重新观察")
        if observation_id != row["latest_observation_id"]:
            raise ActionRejected("画面已经更新，旧观察上的操作已取消")
        age = time.time() - row["latest_observation_at"]
        if age < -1 or age > max_age_seconds:
            raise ActionRejected("游戏画面已过期，请重新观察")

    def validate_action(
        self, task_id: str, goal_version: int, observation_id: str,
        max_age_seconds: float = 5,
    ) -> bool:
        if type(goal_version) is not int or goal_version < 1:
            raise ActionRejected("动作必须关联明确的目标版本")
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)):
            raise ValueError("观察有效期必须是正数")
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("观察有效期必须是正数")
        with self._lock:
            self._ensure_open()
            self._check_action(self._task_row(task_id), goal_version, observation_id, max_age_seconds)
            return True

    def confirmed_battle_result(
        self, task_id: str, result_id: str, *, goal_version: int | None = None,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        """Count one trusted, recognized settlement exactly once per result ID.

        Do not expose this method as a model-facing progress setter.  The game
        adapter supplies a stable result ID after checking the settlement.
        """
        result_id = _nonempty(result_id, "结算编号")
        with self._transaction():
            row = self._task_row(task_id)
            self._check_version(row, goal_version)
            if self._db.execute(
                "SELECT 1 FROM battle_results WHERE task_id=? AND result_id=?", (task_id, result_id),
            ).fetchone():
                return {**self._task_dict(row), "result_id": result_id, "result_recorded": False}
            if row["status"] not in ACTIVE_STATUSES:
                raise ActionRejected("任务已暂停、停止或尚未运行，不能增加进度")
            if not self._has_known_observation(row) or row["latest_observation_state"] not in RESULT_STATES:
                raise ActionRejected("缺少已识别的结算画面，不能增加进度")
            observation_id = observation_id or row["latest_observation_id"]
            self._check_action(row, row["version"], observation_id, 5)
            if self._db.execute(
                "SELECT 1 FROM battle_results WHERE task_id=? AND observation_id=?",
                (task_id, observation_id),
            ).fetchone():
                raise ActionRejected("这张结算画面已经计数，不能重复增加进度")
            now = _utc_now()
            self._db.execute(
                "INSERT INTO battle_results(task_id,result_id,goal_version,observation_id,created_at) "
                "VALUES(?,?,?,?,?)", (task_id, result_id, row["version"], observation_id, now),
            )
            self._db.execute(
                "UPDATE tasks SET completed_runs=completed_runs+1,updated_at=? WHERE id=?", (now, task_id),
            )
            row = self._task_row(task_id)
            self._append_event(row, "battle_result_confirmed", "已确认一次作战结算", {
                "result_id": result_id, "observation_id": observation_id,
                "completed_runs": row["completed_runs"],
            })
            return {**self._task_dict(row), "result_id": result_id, "result_recorded": True}

    def recover_interrupted_tasks(self) -> list[dict[str, Any]]:
        """Application-owner startup recovery; do not call from passive readers."""
        with self._transaction():
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE status IN ('observing','running') ORDER BY created_at",
            ).fetchall()
            recovered = []
            for previous in rows:
                row = self._invalidate(previous["id"], status="needs_input")
                self._append_event(row, "task_interrupted", "检测到中断任务，需重新观察后继续")
                recovered.append(self._task_dict(row))
            return recovered

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self) -> "TaskEngine":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

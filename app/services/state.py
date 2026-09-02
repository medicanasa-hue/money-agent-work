import ast
import copy
import hashlib
import json
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger

from app.config import config
from app.models import const
from app.utils import utils


def _is_live_windows_process(process_id: int) -> bool:
    """Check a Windows process without relying on ``os.kill(pid, 0)``.

    Python's signal-zero probe can raise ``SystemError`` on Windows, including
    for a live child process.  ``OpenProcess`` reliably distinguishes a live
    process from a stale task-state PID while treating an access-denied system
    process as live.
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_bool,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if handle:
            try:
                return True
            finally:
                kernel32.CloseHandle(handle)
        return ctypes.get_last_error() == 5
    except (AttributeError, OSError, SystemError):
        return False


def _is_live_process(process_id: object) -> bool:
    """Return whether a persisted local worker process still exists."""
    if isinstance(process_id, bool):
        return False
    try:
        process_id = int(process_id)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        return _is_live_windows_process(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        # On Windows, ``os.kill(pid, 0)`` may surface an invalid-parameter
        # process probe as SystemError rather than OSError.  A stale journal
        # entry must not prevent the application from starting.
        return False
    return True


_PATCH_EXISTING_TASK_SCRIPT = """
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end

for index = 1, #ARGV, 2 do
    redis.call("HSET", KEYS[1], ARGV[index], ARGV[index + 1])
end

return 1
"""


# Base class for state management
class BaseState(ABC):
    @abstractmethod
    def update_task(self, task_id: str, state: int, progress: int = 0, **kwargs):
        pass

    @abstractmethod
    def get_task(self, task_id: str):
        pass

    @abstractmethod
    def get_all_tasks(self, page: int, page_size: int):
        pass

    @abstractmethod
    def patch_task(self, task_id: str, **kwargs) -> bool:
        """只更新已有任务的指定字段；任务不存在时返回 False。"""
        pass


# Memory state management
class MemoryState(BaseState):
    """In-memory task state with an optional local restart-recovery journal."""

    def __init__(self, state_dir=None, persist=False):
        self._tasks = {}
        self._lock = threading.RLock()
        self._state_dir = self._initialize_state_dir(state_dir, persist)
        self.recovered_interrupted_count = 0
        if self._state_dir:
            self._load_persisted_tasks()

    @staticmethod
    def _initialize_state_dir(state_dir, persist):
        if state_dir is None and not persist:
            return None
        try:
            directory = (
                Path(state_dir)
                if state_dir is not None
                else Path(utils.storage_dir("task-state", create=True))
            )
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except (OSError, TypeError, ValueError, RecursionError, UnicodeError):
            logger.warning("task-state recovery journal is unavailable")
            return None

    def _snapshot_path(self, task_id):
        task_hash = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()
        return self._state_dir / f"{task_hash}.json"

    def _save_task_snapshot(self, task):
        if not self._state_dir:
            return
        snapshot_path = self._snapshot_path(task["task_id"])
        temporary_path = snapshot_path.with_suffix(".tmp")
        try:
            temporary_path.write_text(
                json.dumps(task, ensure_ascii=False, default=str), encoding="utf-8"
            )
            temporary_path.replace(snapshot_path)
        except OSError:
            logger.warning("failed to persist task-state recovery journal")
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_task_snapshot(self, task_id):
        if not self._state_dir:
            return
        try:
            self._snapshot_path(task_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove task-state recovery journal")

    def _load_persisted_tasks(self):
        try:
            snapshot_paths = list(self._state_dir.glob("*.json"))
        except OSError:
            logger.warning("failed to read task-state recovery journal")
            return

        for snapshot_path in snapshot_paths:
            try:
                task = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                logger.warning("skipping unreadable task-state recovery entry")
                continue
            if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
                logger.warning("skipping invalid task-state recovery entry")
                continue

            try:
                task["progress"] = int(task.get("progress", 0))
                task["state"] = int(task.get("state"))
            except (TypeError, ValueError):
                logger.warning("skipping invalid task-state recovery entry")
                continue

            if (
                task["state"] == const.TASK_STATE_PROCESSING
                and not _is_live_process(task.get("worker_pid"))
            ):
                task.update(
                    state=const.TASK_STATE_FAILED,
                    interrupted=True,
                    error="task interrupted by application restart",
                )
                self.recovered_interrupted_count += 1
            self._tasks[task["task_id"]] = task
            self._save_task_snapshot(task)

    def get_all_tasks(self, page: int, page_size: int):
        start = (page - 1) * page_size
        end = start + page_size
        with self._lock:
            tasks = [copy.deepcopy(task) for task in self._tasks.values()]
            total = len(tasks)
        return tasks[start:end], total

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        if state == const.TASK_STATE_PROCESSING and "worker_pid" not in kwargs:
            kwargs["worker_pid"] = os.getpid()

        with self._lock:
            task = {
                "task_id": task_id,
                "state": state,
                "progress": progress,
                **kwargs,
            }
            self._tasks[task_id] = task
            self._save_task_snapshot(task)

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id, None)
            return copy.deepcopy(task) if task is not None else None

    def patch_task(self, task_id: str, **kwargs) -> bool:
        # 异步发布只应补充发布状态，不能覆盖已经保存的视频、字幕等结果。
        # 在同一把锁内完成存在性判断和字段合并，也可避免任务删除后
        # 被后台线程重建。
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.update(copy.deepcopy(kwargs))
            return True

    def delete_task(self, task_id: str):
        with self._lock:
            self._tasks.pop(task_id, None)
            self._delete_task_snapshot(task_id)


# Redis state management
class RedisState(BaseState):
    """
    Redis-backed task state.

    Trust boundary: Redis is expected to be private to this application. Task
    values are written by MoneyPrinterTurbo and converted back from strings for
    compatibility with existing state records. Do not expose this Redis database
    to untrusted writers without replacing deserialization with a stricter
    schema-based format.
    """

    def __init__(self, host="localhost", port=6379, db=0, password=None):
        import redis

        self._redis = redis.StrictRedis(host=host, port=port, db=db, password=password)

    def get_all_tasks(self, page: int, page_size: int):
        start = (page - 1) * page_size
        end = start + page_size
        tasks = []
        cursor = 0
        total = 0
        while True:
            # Redis 数据库中除了任务 Hash，还可能存在 RedisTaskManager 使用的
            # List 队列。只扫描 Hash 可以避免对队列执行 HGETALL 时触发
            # WRONGTYPE，同时保证 total 只统计真正的任务记录。
            cursor, keys = self._redis.scan(
                cursor,
                count=page_size,
                _type="HASH",
            )
            batch_start = total
            batch_size = len(keys)
            total += batch_size

            # Redis SCAN 是分批返回 key。分页切片必须基于“当前批次起始索引”
            # 计算，而不能用累积后的 total 反推，否则第一页会切到空数组，
            # 第二页也可能只返回部分数据。
            if batch_start < end and total > start:
                slice_start = max(0, start - batch_start)
                slice_end = min(batch_size, end - batch_start)
                for key in keys[slice_start:slice_end]:
                    task_data = self._redis.hgetall(key)
                    task = {
                        k.decode("utf-8"): self._convert_to_original_type(v)
                        for k, v in task_data.items()
                    }
                    tasks.append(task)

            # 即使当前页已经取满，也要继续 SCAN 到 cursor=0，
            # 因为调用方需要准确 total 来渲染分页信息。
            if cursor == 0:
                break
        return tasks, total

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        fields = {
            "task_id": task_id,
            "state": state,
            "progress": progress,
            **kwargs,
        }

        for field, value in fields.items():
            self._redis.hset(task_id, field, str(value))

    def get_task(self, task_id: str):
        task_data = self._redis.hgetall(task_id)
        if not task_data:
            return None

        task = {
            key.decode("utf-8"): self._convert_to_original_type(value)
            for key, value in task_data.items()
        }
        return task

    def patch_task(self, task_id: str, **kwargs) -> bool:
        if not kwargs:
            return False

        arguments = []
        for field, value in kwargs.items():
            arguments.extend((field, str(value)))

        # EXISTS 和 HSET 如果分成两条命令，后台发布线程与删除请求并发时，
        # HSET 可能在删除后重新创建一条残缺任务。Lua 脚本由 Redis 原子执行，
        # 可以保证任务不存在时不写入，且不会改变现有字段之外的数据。
        updated = self._redis.eval(
            _PATCH_EXISTING_TASK_SCRIPT,
            1,
            task_id,
            *arguments,
        )
        return bool(updated)

    def delete_task(self, task_id: str):
        self._redis.delete(task_id)

    @staticmethod
    def _convert_to_original_type(value):
        """
        Convert values written by this application back to common Python types.

        This compatibility parser assumes Redis is inside the application's
        trust boundary. If Redis can be written by untrusted clients, task state
        should move to a strict JSON/schema parser instead of open-ended literal
        conversion.
        """
        value_str = value.decode("utf-8")

        try:
            # try to convert byte string array to list
            return ast.literal_eval(value_str)
        except (ValueError, SyntaxError):
            pass

        if value_str.isdigit():
            return int(value_str)
        # Add more conversions here if needed
        return value_str


# Global state
_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)

state = (
    RedisState(
        host=_redis_host, port=_redis_port, db=_redis_db, password=_redis_password
    )
    if _enable_redis
    else MemoryState(persist=True)
)

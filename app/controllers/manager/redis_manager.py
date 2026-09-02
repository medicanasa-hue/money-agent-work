import inspect
import json
from typing import Dict

import redis
from loguru import logger

from app.controllers.manager.base_manager import TaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm

FUNC_MAP = {
    "start": tm.start,
    # 'start_test': tm.start_test
}


class RedisTaskManager(TaskManager):
    def __init__(
        self,
        max_concurrent_tasks: int,
        redis_url: str,
        max_queued_tasks: int = 100,
    ):
        self.redis_client = redis.Redis.from_url(redis_url)
        super().__init__(max_concurrent_tasks, max_queued_tasks=max_queued_tasks)

    def create_queue(self):
        return "task_queue"

    def enqueue(self, task: Dict):
        task_with_serializable_params = task.copy()
        # task.copy() 只复制最外层字典；如果直接改写嵌套 kwargs，会把调用方
        # 持有的 VideoParams 同步替换成 dict。后续日志或重试仍可能读取原任务，
        # 因此这里单独复制 kwargs，确保序列化过程没有意外副作用。
        task_kwargs = task.get("kwargs", {})
        task_with_serializable_params["kwargs"] = task_kwargs.copy()

        if "params" in task_kwargs and isinstance(task_kwargs["params"], VideoParams):
            task_with_serializable_params["kwargs"]["params"] = task_kwargs[
                "params"
            ].model_dump(warnings=False)

        # 将函数对象转换为其名称
        task_with_serializable_params["func"] = task["func"].__name__
        self.redis_client.rpush(self.queue, json.dumps(task_with_serializable_params))

    def dequeue(self):
        # lpop removes the record before validation. Discard stale or corrupt
        # records and keep looking so they cannot stop the remaining queue.
        while True:
            task_json = self.redis_client.lpop(self.queue)
            if task_json is None:
                return None

            task_info = None
            try:
                task_info = json.loads(task_json)
                return self._restore_task(task_info)
            except (TypeError, ValueError) as exc:
                # Validation errors can contain script text or local paths.
                # Record only the error class, never the input or exception text.
                logger.error("discarding invalid queued task ({})", type(exc).__name__)
                self._mark_discarded_task_failed(task_info)

    @staticmethod
    def _restore_task(task_info):
        if not isinstance(task_info, dict):
            raise ValueError("invalid task record")
        function_name = task_info.get("func")
        if not isinstance(function_name, str) or function_name not in FUNC_MAP:
            raise ValueError("unknown task function")
        args = task_info.get("args", [])
        kwargs = task_info.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("invalid task arguments")
        signature = inspect.signature(FUNC_MAP[function_name])
        bound = signature.bind(*args, **kwargs)
        task_id = bound.arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("invalid task identifier")
        params = VideoParams.model_validate(bound.arguments["params"])
        # Keep the canonical keyword shape so a legacy positional record can
        # still be serialized again if starting its worker fails.
        kwargs = {**bound.arguments, "params": params}
        return {**task_info, "func": FUNC_MAP[function_name], "args": [], "kwargs": kwargs}

    @staticmethod
    def _mark_discarded_task_failed(task_info):
        if not isinstance(task_info, dict):
            return
        kwargs = task_info.get("kwargs", {})
        args = task_info.get("args", [])
        task_id = kwargs.get("task_id") if isinstance(kwargs, dict) else None
        if task_id is None and isinstance(args, list) and args:
            task_id = args[0]
        if isinstance(task_id, str) and task_id:
            # Do not recreate records already deleted by the user.
            sm.state.patch_task(
                task_id,
                state=const.TASK_STATE_FAILED,
                failed_stage="dequeue",
                error="discarded stale or invalid queued task",
            )

    def is_queue_empty(self):
        return self.redis_client.llen(self.queue) == 0

    def queue_size(self):
        return self.redis_client.llen(self.queue)

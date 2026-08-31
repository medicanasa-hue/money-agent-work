import json
import unittest
from unittest.mock import MagicMock, patch

from loguru import logger

from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as state_service
from app.services import task as task_service


class TestInMemoryTaskManager(unittest.TestCase):
    def test_queue_operations_preserve_task_payload(self):
        """内存队列应保持函数、位置参数和关键字参数，不得改变任务内容。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        task = {"func": len, "args": ([1, 2],), "kwargs": {}}

        manager.enqueue(task)

        self.assertFalse(manager.is_queue_empty())
        self.assertEqual(manager.queue_size(), 1)
        self.assertEqual(manager.dequeue(), task)
        self.assertTrue(manager.is_queue_empty())

    def test_add_task_rejects_only_after_queue_limit(self):
        """并发名额用尽后允许排队到上限，超过上限才返回明确错误。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=0, max_queued_tasks=1)

        manager.add_task(len, [1])

        with self.assertRaises(TaskQueueFullError):
            manager.add_task(len, [2])

    def test_add_task_reserves_slot_before_background_thread_runs(self):
        """
        并发名额必须在线程启动前预占；即使 mock 的线程尚未进入 run_task，
        第二个请求也应进入队列，不能突破 max_concurrent_tasks。
        """
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)

        with patch.object(manager, "execute_task") as execute_task:
            manager.add_task(len, [1])
            manager.add_task(len, [2])

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1])
        self.assertEqual(manager.queue_size(), 1)

    def test_add_task_rolls_back_slot_when_thread_cannot_start(self):
        """线程启动失败不能永久占用并发名额，异常仍应交给调用方处理。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.add_task(len, [1])

        self.assertEqual(manager.current_tasks, 0)

    def test_task_done_starts_next_queued_task(self):
        """当前任务结束后应释放并发名额，并立即调度队列中的下一个任务。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        manager.current_tasks = 1
        manager.enqueue({"func": len, "args": ([1, 2],), "kwargs": {}})

        with patch.object(manager, "execute_task") as execute_task:
            manager.task_done()

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1, 2])
        self.assertTrue(manager.is_queue_empty())

    def test_task_done_requeues_task_when_thread_cannot_start(self):
        """出队后若线程启动失败，应回滚名额并把任务放回队列，避免任务丢失。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)
        manager.current_tasks = 1
        queued_task = {"func": len, "args": ([1, 2],), "kwargs": {}}
        manager.enqueue(queued_task)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.task_done()

        self.assertEqual(manager.current_tasks, 0)
        self.assertEqual(manager.dequeue(), queued_task)

    def test_run_task_releases_slot_after_failure(self):
        """任务函数抛出异常时 finally 仍必须释放名额，避免队列永久阻塞。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        manager.current_tasks = 1

        with patch.object(manager, "task_done") as task_done:
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                manager.run_task(MagicMock(side_effect=RuntimeError("task failed")))

        self.assertEqual(manager.current_tasks, 1)
        task_done.assert_called_once_with()

    def test_execute_task_starts_background_thread(self):
        """任务执行入口必须启动线程，并把函数参数完整传给 run_task。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        fake_thread = MagicMock()

        with patch(
            "app.controllers.manager.base_manager.threading.Thread",
            return_value=fake_thread,
        ) as thread:
            manager.execute_task(len, [1, 2])

        thread.assert_called_once_with(
            target=manager.run_task,
            args=(len, [1, 2]),
            kwargs={},
        )
        fake_thread.start.assert_called_once_with()


class TestRedisTaskManager(unittest.TestCase):
    def setUp(self):
        self.redis_client = MagicMock()
        self.state = state_service.MemoryState()
        state_patcher = patch.object(state_service, "state", self.state)
        self.addCleanup(state_patcher.stop)
        state_patcher.start()
        patcher = patch(
            "app.controllers.manager.redis_manager.redis.Redis.from_url",
            return_value=self.redis_client,
        )
        self.addCleanup(patcher.stop)
        from_url = patcher.start()
        self.manager = RedisTaskManager(
            max_concurrent_tasks=1,
            redis_url="redis://localhost:6379/0",
            max_queued_tasks=3,
        )
        from_url.assert_called_once_with("redis://localhost:6379/0")

    def test_enqueue_serializes_video_params_without_mutating_task(self):
        """
        Redis 只能存 JSON；VideoParams 应转换成字典，但原任务仍需保留模型，
        避免序列化副作用影响日志、重试或调用方后续读取。
        """
        params = VideoParams(video_subject="Coffee")
        task = {
            "func": task_service.start,
            "args": (),
            "kwargs": {"task_id": "task-1", "params": params},
        }

        self.manager.enqueue(task)

        self.assertIs(task["kwargs"]["params"], params)
        queue_name, payload = self.redis_client.rpush.call_args.args
        decoded = json.loads(payload)
        self.assertEqual(queue_name, "task_queue")
        self.assertEqual(decoded["func"], "start")
        self.assertEqual(decoded["kwargs"]["task_id"], "task-1")
        self.assertEqual(decoded["kwargs"]["params"]["video_subject"], "Coffee")

    def test_dequeue_restores_function_and_video_params(self):
        """从 Redis 取出的任务应恢复可调用函数和 VideoParams 模型。"""
        payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-1",
                "params": VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ),
            },
        }
        self.redis_client.lpop.return_value = json.dumps(payload)

        task = self.manager.dequeue()

        self.redis_client.lpop.assert_called_once_with("task_queue")
        self.assertIs(task["func"], task_service.start)
        self.assertIsInstance(task["kwargs"]["params"], VideoParams)
        self.assertEqual(task["kwargs"]["params"].video_subject, "Coffee")

    def test_empty_queue_and_size_use_redis_length(self):
        """队列判空和长度必须直接反映 Redis 当前列表长度。"""
        self.redis_client.lpop.return_value = None
        self.redis_client.llen.side_effect = [0, 2]

        self.assertIsNone(self.manager.dequeue())
        self.assertTrue(self.manager.is_queue_empty())
        self.assertEqual(self.manager.queue_size(), 2)

    def test_legacy_positional_task_can_be_requeued_after_restore(self):
        payload = {
            "func": "start",
            "args": ["legacy-task", {"video_subject": "Coffee"}],
            "kwargs": {"stop_at": "audio"},
        }
        self.redis_client.lpop.return_value = json.dumps(payload)
        restored = self.manager.dequeue()
        self.manager.enqueue(restored)
        encoded = json.loads(self.redis_client.rpush.call_args.args[1])
        self.assertEqual(encoded["args"], [])
        self.assertEqual(encoded["kwargs"]["task_id"], "legacy-task")
        self.assertEqual(encoded["kwargs"]["params"]["video_subject"], "Coffee")
        self.assertEqual(encoded["kwargs"]["stop_at"], "audio")

    def test_stale_params_fail_task_and_schedule_next_without_leaking_input(self):
        secret_input = "private script text must not enter validation logs"
        stale_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-stale",
                "params": {"video_subject": "Coffee", "video_fps": secret_input},
            },
        }
        valid_payload = {
            "func": "start",
            "args": [],
            "kwargs": {"task_id": "task-valid", "params": {"video_subject": "Tea"}},
        }
        self.state.update_task("task-stale", progress=3, request_id="request-stale")
        self.manager.current_tasks = 1
        self.redis_client.llen.return_value = 2
        self.redis_client.lpop.side_effect = [
            json.dumps(stale_payload),
            json.dumps(valid_payload),
        ]
        messages = []
        sink_id = logger.add(messages.append, format="{message}")
        self.addCleanup(logger.remove, sink_id)

        with patch.object(self.manager, "execute_task") as execute_task:
            self.manager.task_done()

        failed_task = self.state.get_task("task-stale")
        self.assertEqual(failed_task["state"], const.TASK_STATE_FAILED)
        self.assertEqual(failed_task["failed_stage"], "dequeue")
        self.assertEqual(failed_task["request_id"], "request-stale")
        self.assertNotIn(secret_input, failed_task["error"])
        self.assertNotIn(secret_input, "".join(messages))
        self.assertEqual(self.manager.current_tasks, 1)
        execute_task.assert_called_once()
        self.assertIs(execute_task.call_args.args[0], task_service.start)
        self.assertEqual(execute_task.call_args.kwargs["task_id"], "task-valid")
        self.assertEqual(execute_task.call_args.kwargs["params"].video_subject, "Tea")

    def test_dequeue_skips_corrupt_records_before_next_valid_task(self):
        valid_payload = {
            "func": "start",
            "kwargs": {"task_id": "task-valid", "params": {"video_subject": "Tea"}},
        }
        corrupt_records = [
            b"\xff",
            "",
            "{",
            "[]",
            json.dumps({"func": "removed-function", "kwargs": {}}),
            json.dumps({"func": [], "kwargs": {}}),
            json.dumps({"func": "start", "kwargs": []}),
            json.dumps({"func": "start", "args": {}, "kwargs": {}}),
            json.dumps({"func": "start", "kwargs": {"params": []}}),
            json.dumps({"func": "start", "kwargs": {"task_id": "missing-params"}}),
            json.dumps({"func": "start", "kwargs": {"params": {"video_subject": "test"}}}),
            json.dumps({"func": "start", "kwargs": {"task_id": "old", "params": {"video_subject": "test"}, "removed_argument": True}}),
        ]

        for corrupt_record in corrupt_records:
            with self.subTest(record=corrupt_record):
                self.redis_client.lpop.side_effect = [
                    corrupt_record,
                    json.dumps(valid_payload),
                ]

                task = self.manager.dequeue()

                self.assertEqual(task["kwargs"]["task_id"], "task-valid")
                self.assertIsInstance(task["kwargs"]["params"], VideoParams)

    def test_all_stale_records_leave_no_reserved_slot(self):
        stale_payload = {
            "func": "start",
            "kwargs": {
                "task_id": "task-stale",
                "params": {"video_subject": "Coffee", "video_fps": 0},
            },
        }
        self.manager.current_tasks = 1
        self.redis_client.llen.return_value = 1
        self.redis_client.lpop.side_effect = [json.dumps(stale_payload), None]

        with patch.object(self.manager, "execute_task") as execute_task:
            self.manager.task_done()

        self.assertEqual(self.manager.current_tasks, 0)
        execute_task.assert_not_called()

    def test_discarding_stale_record_does_not_recreate_deleted_state(self):
        stale_payload = {
            "func": "start",
            "kwargs": {
                "task_id": "task-deleted",
                "params": {"video_subject": "Coffee", "video_fps": 0},
            },
        }
        self.state.update_task("task-deleted")
        self.state.delete_task("task-deleted")
        self.redis_client.lpop.side_effect = [json.dumps(stale_payload), None]

        self.assertIsNone(self.manager.dequeue())
        self.assertIsNone(self.state.get_task("task-deleted"))


if __name__ == "__main__":
    unittest.main()


class _RecordingTaskManager(InMemoryTaskManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_tasks = []

    def execute_task(self, func, *args, **kwargs):
        self.started_tasks.append((func, args, kwargs))

class TestTaskManager(unittest.TestCase):
    def test_reserves_a_concurrency_slot_before_starting_a_task(self):
        manager = _RecordingTaskManager(max_concurrent_tasks=1)

        manager.add_task(lambda: None)
        manager.add_task(lambda: None)

        self.assertEqual(len(manager.started_tasks), 1)
        self.assertEqual(manager.current_tasks, 1)
        self.assertEqual(manager.queue_size(), 1)

        manager.task_done()

        self.assertEqual(len(manager.started_tasks), 2)
        self.assertEqual(manager.current_tasks, 1)
        self.assertEqual(manager.queue_size(), 0)

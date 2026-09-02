import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException


class TestControllerAuthentication(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        base.reset_auth_rate_limits()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        base.reset_auth_rate_limits()

    @staticmethod
    def _request(headers=None):
        return SimpleNamespace(
            headers=headers or {},
            url="http://localhost/api/v1/tasks",
        )

    def test_get_task_id_reuses_header_or_generates_uuid(self):
        """
        客户端提供 request ID 时需要原样保留，缺失时则生成可记录到日志和
        错误响应中的 UUID，保证两种入口都有可追踪标识。
        """
        self.assertEqual(
            base.get_task_id(self._request({"x-task-id": "request-123"})),
            "request-123",
        )

        generated = base.get_task_id(self._request())
        self.assertEqual(len(generated), 36)
        self.assertEqual(generated.count("-"), 4)

    def test_get_task_id_preserves_printable_trace_ids(self):
        for task_id in ("trace/123_abc.def:456", "istek-çığ-İstanbul", "x" * 128):
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    base.get_task_id(self._request({"x-task-id": task_id})), task_id
                )

    def test_get_task_id_replaces_malformed_or_unsafe_values_with_uuid(self):
        generated_id = UUID("00000000-0000-4000-8000-000000000001")
        invalid_values = (
            None, "", 123, b"request-123", object(), "line\nforged",
            "line\rforged", "column\tforged", "ansi\x1b[31m",
            "unicode\u2028separator", "x" * 129,
        )

        with patch.object(base, "uuid4", return_value=generated_id):
            for value in invalid_values:
                with self.subTest(value=value):
                    self.assertEqual(
                        base.get_task_id(self._request({"x-task-id": value})),
                        str(generated_id),
                    )

    def test_verify_token_does_not_log_unsafe_request_id(self):
        config.app["api_key"] = "secret"
        generated_id = UUID("00000000-0000-4000-8000-000000000001")
        with (
            patch.object(base, "uuid4", return_value=generated_id),
            patch("app.models.exception.logger.error") as log_error,
        ):
            with self.assertRaises(HttpException):
                base.verify_token(
                    self._request({"x-task-id": "attacker\nforged-log-entry"})
                )

        logged_error = log_error.call_args.args[0]
        self.assertIn(str(generated_id), logged_error)
        self.assertNotIn("attacker", logged_error)
        self.assertNotIn("forged-log-entry", logged_error)

    def test_verify_token_accepts_matching_key(self):
        """配置了 API Key 时，相同请求头必须正常通过鉴权。"""
        config.app["api_key"] = "secret"

        result = base.verify_token(self._request({"x-api-key": "secret"}))

        self.assertIsNone(result)

    def test_verify_token_rejects_missing_or_wrong_key(self):
        """
        缺失和错误的 API Key 都必须返回 401，并保留客户端 request ID，
        避免鉴权失败在日志中无法与调用方请求对应。
        """
        config.app["api_key"] = "secret"

        for provided_key in (None, "wrong"):
            with self.subTest(provided_key=provided_key):
                headers = {"x-task-id": "auth-request"}
                if provided_key is not None:
                    headers["x-api-key"] = provided_key

                with self.assertRaises(HttpException) as raised:
                    base.verify_token(self._request(headers))

                self.assertEqual(raised.exception.status_code, 401)
                self.assertIn("invalid API key", raised.exception.message)

    def test_new_router_preserves_common_prefix_and_dependencies(self):
        """所有 V1 路由都应复用统一前缀，并仅在传入时设置鉴权依赖。"""
        dependency = object()

        plain_router = new_router()
        protected_router = new_router(dependencies=[dependency])

        self.assertEqual(plain_router.prefix, "/api/v1")
        self.assertEqual(plain_router.tags, ["V1"])
        self.assertEqual(protected_router.dependencies, [dependency])


if __name__ == "__main__":
    unittest.main()

"""Application implementation - ASGI."""

import ipaddress
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.controllers import base
from app.models.exception import HttpException
from app.router import root_api_router
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """集中处理 API 进程启动恢复和关闭日志。"""
    logger.info("startup event")

    # 跨平台发布由当前进程线程池执行，不会在服务重启后恢复。启动时把 Redis
    # 中确认已失去执行进程的活动状态收敛为失败，避免任务永久无法删除。
    from app.services import task as task_service

    task_service.recover_interrupted_cross_posts()
    try:
        yield
    finally:
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def _is_loopback_host(listen_host: str | None) -> bool:
    host = (listen_host or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def warn_if_api_unprotected(api_key: str | None, listen_host: str | None) -> str | None:
    api_key = (api_key or "").strip()
    if api_key:
        return None

    if _is_loopback_host(listen_host):
        return None

    return (
        "API authentication is disabled while listen_host is not loopback. "
        "Set app.api_key or bind the API to 127.0.0.1/localhost for safer local use."
    )


def parse_cors_allowed_origins(raw_origins: str | None) -> list[str]:
    """Parse the explicit browser cross-origin allowlist."""
    if not raw_origins:
        return []
    return [
        origin.strip()
        for origin in raw_origins.split(",")
        if origin.strip()
    ]


def configure_cors(instance: FastAPI, allowed_origins: list[str]) -> None:
    """Configure CORS only when cross-origin browser access is explicit."""
    if not allowed_origins:
        logger.info(
            "browser cross-origin API access is disabled; set "
            "CORS_ALLOWED_ORIGINS to enable trusted origins"
        )
        return

    allow_all_origins = "*" in allowed_origins
    configured_api_key = config.app.get("api_key", "")
    if allow_all_origins and configured_api_key in (None, ""):
        logger.warning(
            "CORS allows every browser origin while API key authentication is "
            "disabled; configure app.api_key or restrict CORS_ALLOWED_ORIGINS"
        )

    instance.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=not allow_all_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=not allow_all_origins,
    )


def is_browser_origin_allowed(
    request: Request, allowed_origins: list[str]
) -> bool:
    """Allow server clients, same-origin browsers, and explicit origins."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    if "*" in allowed_origins or origin in allowed_origins:
        return True

    request_url = urlsplit(str(request.url))
    request_origin = f"{request_url.scheme}://{request_url.netloc}"
    return origin == request_origin


def configure_browser_access(
    instance: FastAPI, allowed_origins: list[str]
) -> None:
    """Reject untrusted browser origins and add CORS for trusted origins."""

    @instance.middleware("http")
    async def reject_untrusted_browser_origin(request: Request, call_next):
        if not is_browser_origin_allowed(request, allowed_origins):
            logger.warning("blocked untrusted browser origin")
            return JSONResponse(
                status_code=403,
                content=utils.get_response(
                    status=403,
                    message="cross-origin browser request is not allowed",
                ),
            )
        return await call_next(request)

    # Register CORS last so trusted preflights are handled before the active
    # Origin guard. Simple cross-origin requests still pass through the guard.
    configure_cors(instance, allowed_origins)


def should_protect_task_outputs(api_key: str | None, listen_host: str | None) -> bool:
    """A configured key applies even behind a loopback reverse proxy."""
    return api_key is not None and (
        not isinstance(api_key, str) or bool(api_key.strip())
    )


class TaskOutputStaticFiles(StaticFiles):
    """Use the same authentication and rate limit as the API for generated media."""

    async def get_response(self, path: str, scope):
        request = Request(scope)
        try:
            base.verify_token(request)
        except HttpException as exc:
            return exception_handler(request, exc)
        return await super().get_response(path, scope)


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    warning_message = warn_if_api_unprotected(
        api_key=config.app.get("api_key", ""),
        listen_host=config.listen_host,
    )
    if warning_message:
        logger.warning(warning_message)
    return instance


app = get_application()

cors_allowed_origins = parse_cors_allowed_origins(
    os.getenv("CORS_ALLOWED_ORIGINS", "")
)
configure_browser_access(app, cors_allowed_origins)

task_dir = utils.task_dir()
app.mount(
    "/tasks", TaskOutputStaticFiles(directory=task_dir, html=True), name=""
)

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")

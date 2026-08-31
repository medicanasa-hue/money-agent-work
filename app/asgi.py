"""Application implementation - ASGI."""

import ipaddress
import os
from contextlib import asynccontextmanager

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


def cors_configuration(
    listen_host: str | None, configured_origins: str | None
) -> tuple[list[str], bool]:
    """Use explicit browser origins for network hosts and safe local defaults."""
    origins = [
        origin.strip()
        for origin in str(configured_origins or "").split(",")
        if origin.strip()
    ]
    if origins:
        return origins, "*" not in origins
    if _is_loopback_host(listen_host):
        return ["*"], False
    return [], False


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

# Configures the CORS middleware for the FastAPI app
cors_allowed_origins_str = os.getenv("CORS_ALLOWED_ORIGINS", "")
origins, cors_allow_credentials = cors_configuration(
    config.listen_host,
    cors_allowed_origins_str,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_dir = utils.task_dir()
app.mount(
    "/tasks", TaskOutputStaticFiles(directory=task_dir, html=True), name=""
)

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")

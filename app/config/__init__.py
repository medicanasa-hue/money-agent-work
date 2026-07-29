import os
import re
import sys

from loguru import logger

from app.config import config
from app.utils import utils
from app.utils.logging_utils import configure_terminal_logger, format_log_record


DEFAULT_LOG_RETENTION_DAYS = 14
MAX_LOG_RETENTION_DAYS = 365
_QUERY_SECRET_PATTERN = re.compile(
    r"([?&](?:api[_-]?key|token|secret|password)=)[^&\s]+",
    re.IGNORECASE,
)
_AUTHORIZATION_PATTERN = re.compile(
    r"\b(authorization)\s*[:=]\s*(?:(?:bearer|apikey)\s+)?[^\s,;]+",
    re.IGNORECASE,
)
_AUTH_SCHEME_PATTERN = re.compile(r"\b(bearer|apikey)\s+[^\s,;]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b(api[_-]?key|access[_-]?token|token|secret|password)\s*([:=])\s*[^\s,;]+",
    re.IGNORECASE,
)


def redact_log_message(value: object) -> str:
    """Strip common credentials from text before writing it to any log sink."""
    message = str(value or "")
    message = _QUERY_SECRET_PATTERN.sub(r"\1<redacted>", message)
    message = _AUTHORIZATION_PATTERN.sub(r"\1: <redacted>", message)
    message = _AUTH_SCHEME_PATTERN.sub(r"\1 <redacted>", message)
    return _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2<redacted>", message)


def normalized_log_retention_days(value: object) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LOG_RETENTION_DAYS
    return min(MAX_LOG_RETENTION_DAYS, max(1, days))


def daily_log_file_path(log_dir: str) -> str:
    return os.path.join(str(log_dir), "server_{time:YYYY-MM-DD}.log")


def _redact_log_record(record) -> bool:
    record["message"] = redact_log_message(record["message"])
    return True


def _config_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def __init_logger():
    _lvl = config.log_level

    configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
        filter=_redact_log_record,
    )

    if not _config_bool(config.app.get("log_file_enabled", True), True):
        return

    try:
        log_dir = utils.storage_dir("logs", create=True)
        retention_days = normalized_log_retention_days(
            config.app.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)
        )
        logger.add(
            daily_log_file_path(log_dir),
            level=_lvl,
            format=format_log_record,
            rotation="00:00",
            retention=f"{retention_days} days",
            encoding="utf-8",
            backtrace=False,
            diagnose=False,
            enqueue=True,
            filter=_redact_log_record,
        )
    except Exception:
        logger.warning("File logging could not be configured.")


__init_logger()

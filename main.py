from ipaddress import ip_address

import uvicorn
from loguru import logger

from app.config import config


def _is_loopback_host(host):
    normalized_host = str(host or "").strip().strip("[]").lower()
    if normalized_host == "localhost":
        return True

    try:
        return ip_address(normalized_host).is_loopback
    except ValueError:
        return False


def _warn_if_network_api_is_unprotected():
    has_api_key = bool(str(config.app.get("api_key", "") or "").strip())
    if not _is_loopback_host(config.listen_host) and not has_api_key:
        logger.warning(
            "server is listening on a network interface without app.api_key "
            "protection; set app.api_key or bind listen_host to 127.0.0.1"
        )


def run():
    _warn_if_network_api_is_unprotected()
    logger.info(
        "start server, docs: http://127.0.0.1:" + str(config.listen_port) + "/docs"
    )
    uvicorn.run(
        app="app.asgi:app",
        host=config.listen_host,
        port=config.listen_port,
        reload=config.reload_debug,
        log_level="warning",
    )


if __name__ == "__main__":
    run()

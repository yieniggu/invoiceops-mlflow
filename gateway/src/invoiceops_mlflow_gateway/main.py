"""Process entrypoint: ``python -m invoiceops_mlflow_gateway.main``."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, Settings

# Libraries that log request URLs or connection details at INFO: never at that level here.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        print(f"gateway configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),  # noqa: S104 - container network only
        port=int(os.environ.get("GATEWAY_PORT", "8080")),
        proxy_headers=False,
        server_header=False,
        access_log=False,
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()

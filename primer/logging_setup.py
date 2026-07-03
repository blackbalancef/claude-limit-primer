"""Loguru configuration: stdout (for journald) + rotating primer.log."""

import sys

from loguru import logger

from primer.paths import LOG_PATH

# Match the old hand-rolled log line: "[2026-07-03 08:00:00 +0200] message".
_FORMAT = "[{time:YYYY-MM-DD HH:mm:ss ZZ}] {message}"


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stdout, format=_FORMAT, level="INFO", colorize=False)
    logger.add(LOG_PATH, format=_FORMAT, level="INFO", rotation="1 MB", retention=3)

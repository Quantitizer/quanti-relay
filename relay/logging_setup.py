"""Send standard-library `logging` records through loguru, so every module's log lines land in one
stream with one format. Several carried-over modules use `logging.getLogger`; without this their
INFO lines (logins, reconnects) are silently dropped."""
import inspect
import logging
import sys

from loguru import logger


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame, depth = frame.f_back, depth + 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


# Libraries that log request URLs: a Telegram bot token or a Discord webhook secret is in the URL.
QUIET_LIBRARIES = ("httpx", "httpcore")


def route_stdlib_logging(level: int = logging.INFO) -> None:
    # diagnose=False: a traceback never prints variable values (a secret in a local would show).
    logger.remove()
    logger.add(sys.stderr, level="DEBUG", diagnose=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=level, force=True)
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

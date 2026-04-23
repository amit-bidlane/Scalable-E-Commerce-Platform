import logging
import sys

from pythonjsonlogger.json import JsonFormatter

from app.core.config import settings

_LOGGING_CONFIGURED = False


class ServiceNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = record.name
        return True


def configure_logging(service_name: str) -> logging.Logger:
    global _LOGGING_CONFIGURED

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = JsonFormatter(
        fmt="{asctime}{levelname}{name}{message}",
        style="{",
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "name": "logger",
        },
    )
    handler.addFilter(ServiceNameFilter())
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    if not _LOGGING_CONFIGURED:
        root_logger.handlers.clear()
        root_logger.addHandler(handler)
        _LOGGING_CONFIGURED = True

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "aio_pika"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(log_level)

    return logging.getLogger(service_name)

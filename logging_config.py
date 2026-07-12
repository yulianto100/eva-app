"""Structured JSON logging configuration untuk EVA Converter."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_STANDARD_RECORD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}
_SENSITIVE_FIELD_NAMES = {"secret", "token", "password", "key", "authorization", "cookie"}


def set_request_id(request_id: str | None) -> None:
    """Set request id untuk konteks eksekusi saat ini."""
    _request_id.set(request_id)


def get_request_id() -> str | None:
    """Ambil request id dari konteks eksekusi saat ini."""
    return _request_id.get()


def clear_request_id() -> None:
    """Hapus request id dari konteks eksekusi saat ini."""
    _request_id.set(None)


def _is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_FIELD_NAMES)


def _safe_extra_value(name: str, value: Any) -> Any:
    if _is_sensitive_field(name):
        return "***"
    return value


class RequestContextFilter(logging.Filter):
    """Inject request_id dari contextvar ke setiap log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            request_id = get_request_id()
            if request_id:
                record.request_id = request_id
        return True


class JsonFormatter(logging.Formatter):
    """Format log records sebagai single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self._format_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        for field in ("job_id", "output_mode", "duration_ms", "request_id"):
            if hasattr(record, field):
                log_entry[field] = _safe_extra_value(field, getattr(record, field))

        for field, value in record.__dict__.items():
            if field in _STANDARD_RECORD_ATTRS or field in log_entry:
                continue
            if field.startswith("_"):
                continue
            log_entry[field] = _safe_extra_value(field, value)

        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        return json.dumps(log_entry, ensure_ascii=False, default=str)

    def _format_timestamp(self, record: logging.LogRecord) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"


class HumanFormatter(logging.Formatter):
    """Format readable untuk local development."""

    FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    DATE_FORMAT = "%H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self.FORMAT, datefmt=self.DATE_FORMAT)


class ContextLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter kecil untuk menambah context field tanpa mengubah formatter."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(kwargs.get("extra") or {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


def log_with_context(logger: logging.Logger, **context: Any) -> ContextLoggerAdapter:
    """Return logger adapter dengan context field tambahan."""
    return ContextLoggerAdapter(logger, context)


def should_use_json_logging(json_output: bool | None = None) -> bool:
    """Tentukan apakah log harus keluar sebagai JSON."""
    if json_output is not None:
        return json_output

    log_format = os.getenv("LOG_FORMAT", "auto").strip().lower()
    if log_format == "json":
        return True
    if log_format in {"text", "human"}:
        return False
    return os.path.exists("/.dockerenv")


def setup_logging(log_level: str = "INFO", json_output: bool | None = None) -> None:
    """
    Setup logging untuk seluruh aplikasi.

    Args:
        log_level: Level logging (DEBUG, INFO, WARNING, ERROR).
        json_output: Force JSON format. None = auto-detect.
    """
    level = getattr(logging, str(log_level or "INFO").upper(), logging.INFO)
    formatter: logging.Formatter = JsonFormatter() if should_use_json_logging(json_output) else HumanFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    logging.captureWarnings(True)


def context_extra(context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Bangun dict extra yang aman untuk logging."""
    extra = dict(context or {})
    extra.update(kwargs)
    return {key: _safe_extra_value(key, value) for key, value in extra.items()}

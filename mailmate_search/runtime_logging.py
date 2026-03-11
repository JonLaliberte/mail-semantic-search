"""Centralized runtime logging configuration."""

import atexit
import faulthandler
import logging
import signal
from contextlib import redirect_stderr
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional, TextIO

from mailmate_search.config import config

_LOGGING_CONFIGURED = False
_FAULT_LOG_STREAM: Optional[TextIO] = None


def _parse_log_level(value: str, default: int) -> int:
    """Parse logging level names safely."""
    level = getattr(logging, value.upper(), None)
    return level if isinstance(level, int) else default


def get_runtime_log_path() -> Path:
    """Return the configured runtime log path."""
    return config.log_path


def _close_fault_log_stream() -> None:
    global _FAULT_LOG_STREAM
    if _FAULT_LOG_STREAM is None:
        return
    try:
        _FAULT_LOG_STREAM.close()
    except OSError:
        pass
    _FAULT_LOG_STREAM = None


def _get_fault_log_stream() -> TextIO:
    global _FAULT_LOG_STREAM
    if _FAULT_LOG_STREAM is None or _FAULT_LOG_STREAM.closed:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        _FAULT_LOG_STREAM = open(config.log_path, "a", encoding="utf-8")
    return _FAULT_LOG_STREAM


class LoggerWriter:
    """File-like adapter that forwards writes to a logger."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""

    def write(self, message: str) -> int:
        if not message:
            return 0

        self._buffer += message.replace("\r", "\n")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self.logger.log(self.level, line)
        return len(message)

    def flush(self) -> None:
        line = self._buffer.strip()
        if line:
            self.logger.log(self.level, line)
        self._buffer = ""

    def isatty(self) -> bool:
        return False


def redirect_stderr_to_logger(
    logger: logging.Logger, level: int = logging.WARNING
) -> Iterator[LoggerWriter]:
    """Temporarily redirect stderr writes into a logger."""
    writer = LoggerWriter(logger, level)
    return redirect_stderr(writer)


def configure_logging() -> None:
    """Configure file logging for the CLI runtime."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    app_level = _parse_log_level(config.log_level, logging.INFO)
    third_party_level = _parse_log_level(
        config.log_third_party_level, logging.WARNING
    )
    handler_level = min(app_level, third_party_level)

    handler = RotatingFileHandler(
        config.log_path,
        maxBytes=max(config.log_max_bytes, 1),
        backupCount=max(config.log_backup_count, 1),
        encoding="utf-8",
    )
    handler.setLevel(handler_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [pid=%(process)d] %(message)s"
        )
    )

    root_logger = logging.getLogger()
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
    root_logger.setLevel(third_party_level)
    root_logger.addHandler(handler)

    logging.getLogger("mailmate_search").setLevel(app_level)
    for logger_name in (
        "sentence_transformers",
        "transformers",
        "huggingface_hub",
        "urllib3",
    ):
        third_party_logger = logging.getLogger(logger_name)
        third_party_logger.handlers.clear()
        third_party_logger.propagate = True
        third_party_logger.setLevel(third_party_level)

    logging.captureWarnings(True)
    atexit.register(_close_fault_log_stream)
    _LOGGING_CONFIGURED = True


def configure_runtime_diagnostics() -> None:
    """Enable on-demand traceback dumps for hung processes."""
    if not config.index_runtime_diagnostics:
        return

    configure_logging()
    fault_stream = _get_fault_log_stream()
    faulthandler.enable(file=fault_stream, all_threads=True)
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, file=fault_stream, all_threads=True)
        except RuntimeError:
            pass


def dump_runtime_traceback() -> None:
    """Write all thread tracebacks to the runtime log."""
    configure_logging()
    fault_stream = _get_fault_log_stream()
    faulthandler.dump_traceback(file=fault_stream, all_threads=True)
    fault_stream.flush()

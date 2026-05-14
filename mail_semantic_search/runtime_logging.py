"""Centralized runtime logging configuration."""

import atexit
import faulthandler
import logging
import signal
import tempfile
from contextlib import redirect_stderr
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional, TextIO

from mail_semantic_search.config import config

_LOGGING_CONFIGURED = False
_FAULT_LOG_STREAM: Optional[TextIO] = None
_EFFECTIVE_LOG_PATH: Optional[Path] = None


def _parse_log_level(value: str, default: int) -> int:
    """Parse logging level names safely."""
    level = getattr(logging, value.upper(), None)
    return level if isinstance(level, int) else default


def get_runtime_log_path() -> Path:
    """Return the runtime log path used by handlers and fault dumps."""
    if _EFFECTIVE_LOG_PATH is not None:
        return _EFFECTIVE_LOG_PATH
    return config.log_path


def _try_prepare_log_path(candidate: Path) -> Optional[Path]:
    """Return a resolved path if the file can be opened for append, else None."""
    path = candidate.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        return path.resolve()
    except OSError:
        return None


def _resolve_effective_log_path() -> Path:
    """Pick a writable log file: prefer LOG_PATH, then repo ./data/logs, then temp."""
    for candidate in (
        config.log_path,
        Path("./data/logs/mail-semantic-search.error.log"),
        Path(tempfile.gettempdir()) / "mail-semantic-search.error.log",
    ):
        resolved = _try_prepare_log_path(candidate)
        if resolved is not None:
            return resolved
    return Path(tempfile.gettempdir()) / "mail-semantic-search.error.log"


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
    configure_logging()
    log_path = get_runtime_log_path()
    if _FAULT_LOG_STREAM is None or _FAULT_LOG_STREAM.closed:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _FAULT_LOG_STREAM = open(log_path, "a", encoding="utf-8")
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
    global _LOGGING_CONFIGURED, _EFFECTIVE_LOG_PATH
    if _LOGGING_CONFIGURED:
        return

    preferred = config.log_path.expanduser()
    _EFFECTIVE_LOG_PATH = _resolve_effective_log_path()

    app_level = _parse_log_level(config.log_level, logging.INFO)
    third_party_level = _parse_log_level(
        config.log_third_party_level, logging.WARNING
    )
    handler_level = min(app_level, third_party_level)

    handler = RotatingFileHandler(
        str(_EFFECTIVE_LOG_PATH),
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

    logging.getLogger("mail_semantic_search").setLevel(app_level)
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

    try:
        preferred_resolved = preferred.resolve()
    except OSError:
        preferred_resolved = preferred
    if _EFFECTIVE_LOG_PATH != preferred_resolved:
        logging.getLogger(__name__).warning(
            "Could not use configured LOG_PATH %s (open failed or not permitted); "
            "writing logs to %s instead. Other processes (e.g. Docker) may still "
            "update the original file.",
            preferred,
            _EFFECTIVE_LOG_PATH,
        )


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

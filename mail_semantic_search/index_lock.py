"""One process at a time on the index, enforced by an OS file lock.

Chroma's local mode corrupts when two processes open the same index, and
opening it is not read-only (a query applies pending log entries to the
files). An MCP server that indexes on its own schedule therefore takes an
exclusive `flock` on a file next to the database for as long as it runs, and
every CLI command that opens the index takes the same lock for its run.

A kernel lock, not a PID written into the database: the kernel drops it the
moment the holder exits, however it exits (crash, SIGKILL, reboot), so there
is never a stale owner and PID reuse can't fake one. The file's contents
(pid, role, start time) are only for messages.

`flock` is per host: a process in a Docker container and one on the macOS
host don't see each other's locks through a bind mount. Don't run both
against the same data directory.
"""

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from mail_semantic_search.config import config
from mail_semantic_search.database import Database

logger = logging.getLogger(__name__)

SERVER_ROLE = "server"


def lock_path() -> Path:
    """The lock file, beside the SQLite database (its directory always exists)."""
    db = Path(config.database_path)
    return db.with_name(db.name + ".index-owner.lock")


class IndexLock:
    """An exclusive, non-blocking lock on the index for this process."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else lock_path()
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self, role: str) -> bool:
        """Take the lock if it's free; return whether this process now holds it."""
        if self._fd is not None:
            return True
        # os.open fds are not inherited by children (osascript, find), so a
        # subprocess can't keep the lock alive after we exit.
        fd = _open_lock_file(self.path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        info = {
            "pid": os.getpid(),
            "role": role,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(info).encode(), 0)
        except OSError:
            logger.debug("Could not record the lock holder", exc_info=True)
        _clear_stale_index_job_lock()
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def holder(path: Optional[Path] = None) -> Optional[Dict]:
    """Who holds the lock, or None if it's free. Never takes it for long."""
    # Not IndexLock.try_acquire: a probe must not rewrite the holder file or
    # clear job locks.
    target = Path(path) if path is not None else lock_path()
    try:
        fd = _open_lock_file(target)
    except OSError:
        return {}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_info(target)
        except OSError:
            return {}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _open_lock_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError:
        # Created by another user (a `sudo` run, a container running as
        # root). flock doesn't need write access; only the holder note, which
        # is just for messages, can't be written.
        return os.open(path, os.O_RDONLY)


def _read_info(path: Path) -> Dict:
    try:
        info = json.loads(path.read_text() or "{}")
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(info: Dict) -> str:
    """'the MCP server (pid=123)' or '`prune` (pid=456)' for messages."""
    pid = info.get("pid", "?")
    role = info.get("role")
    if role == SERVER_ROLE:
        return f"the MCP server (pid={pid})"
    if role:
        return f"`mail-semantic-search {role}` (pid={pid})"
    return f"another process (pid={pid})"


def _clear_stale_index_job_lock() -> None:
    """Drop an index-job lock row left behind by a process that's gone.

    Whoever holds the file lock is the only process using the index, so a
    job lock in `app_state` held by any other PID is stale by definition,
    even if that PID has since been reused by some unrelated process.
    """
    try:
        with Database() as database:
            held = database.get_backfill_lock()
            if held is not None and held.get("pid") != os.getpid():
                logger.info("Clearing a stale index job lock from pid=%s", held.get("pid"))
                database.release_backfill_lock()
    except Exception:
        logger.debug("Could not check the index job lock", exc_info=True)

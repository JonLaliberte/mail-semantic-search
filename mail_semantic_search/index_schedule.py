"""Incremental indexing on a timer, inside the MCP server process.

Chroma's local mode is not safe for two processes writing the same files: even
a search applies pending log entries to the on-disk segments. A server that
stays up while a separate `index --incremental` process writes every few
minutes is exactly that, and it has corrupted `chroma.sqlite3` in practice.

When MCP_INDEX_INTERVAL_SECONDS is set (HTTP transports only), the server runs
the incremental index itself on a background thread and holds the index lock
(`index_lock.py`, an OS file lock) for as long as it runs, so it is the only
process that opens the index. CLI commands that open the index (reads
included) take the same lock, so they refuse while the server is up, and a
server that starts during one of them waits: it retries the lock every
interval, and its tools answer "index in use" until it has it.

Stdio is excluded on purpose: MCP clients spawn one stdio server per app, so
several copies would each index on their own.
"""

import logging
import math
import os
import platform
import subprocess
import threading
import time
from typing import Callable, Optional

from fastmcp.exceptions import ToolError

from mail_semantic_search import resources
from mail_semantic_search.index import index_emails
from mail_semantic_search.index_lock import SERVER_ROLE, IndexLock, describe, holder

logger = logging.getLogger(__name__)

# Wait this long after startup before the first run, so the HTTP listener and
# the startup status probe aren't competing with a scan on the first seconds.
FIRST_RUN_DELAY_SECONDS = 30.0
MIN_INTERVAL_SECONDS = 30.0
MAX_INTERVAL_SECONDS = 86400.0
# Alert once when this many runs in a row can't index: another process holds
# the index lock, or (rarely) the index-job lock.
SKIP_ALERT_AFTER = 6
# On shutdown, how long to let an index batch in progress finish.
SHUTDOWN_WAIT_SECONDS = 60.0

_scheduler: Optional["IndexScheduler"] = None


def index_interval_seconds() -> float:
    """Read MCP_INDEX_INTERVAL_SECONDS; 0 (the default) turns the schedule off."""
    raw = os.getenv("MCP_INDEX_INTERVAL_SECONDS", "0")
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"MCP_INDEX_INTERVAL_SECONDS must be a number, got {raw!r}.")
    if value == 0:
        return 0.0
    if not math.isfinite(value) or not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise SystemExit(
            f"MCP_INDEX_INTERVAL_SECONDS must be 0 (off) or between "
            f"{MIN_INTERVAL_SECONDS:.0f} and {MAX_INTERVAL_SECONDS:.0f}, got {raw!r}."
        )
    return value


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification; macOS only, never raises."""
    if platform.system() != "Darwin":
        return
    script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except Exception:
        logger.debug("Could not post a notification", exc_info=True)


def _applescript_str(text: str) -> str:
    # Drop NULs and other control characters (osascript's argv can't carry a
    # NUL), then escape the two characters that end an AppleScript string.
    text = "".join(ch for ch in text if ch >= " " or ch == "\n")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class IndexScheduler:
    """Run the incremental index every `interval` seconds on a daemon thread."""

    def __init__(
        self,
        interval: float,
        first_delay: float = FIRST_RUN_DELAY_SECONDS,
        run_index: Callable[..., dict] = index_emails,
        notifier: Callable[[str, str], None] = notify,
        lock: Optional[IndexLock] = None,
    ):
        self.interval = interval
        self.lock = lock if lock is not None else IndexLock()
        # claim() runs on the scheduler thread and on tool calls.
        self._claim_lock = threading.Lock()
        self.first_delay = first_delay
        self._run_index = run_index
        self._notify = notifier
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Only alert when runs start failing, not on every failed run.
        self._failing = False
        self._skips_in_a_row = 0

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self._loop, name="mcp-index-schedule", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def shutdown(self, timeout: float = SHUTDOWN_WAIT_SECONDS) -> None:
        """Stop between batches and wait for the thread. Keeps the index lock.

        Releasing here would be too early: until the interpreter tears down,
        this process still has Chroma open (the keepalive store, perhaps a
        late tool call or the startup probe), and a restarted server or a CLI
        command could open the index alongside it. The kernel drops the lock
        when the process exits, a moment later.
        """
        self.stop(timeout)
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Index run still busy after %ss; exiting anyway", timeout)

    def claim(self) -> bool:
        """Hold the index lock, taking it now if it's free."""
        with self._claim_lock:
            if self.lock.held:
                return True
            try:
                claimed = self.lock.try_acquire(SERVER_ROLE)
                if not claimed and holder() is None:
                    # Freed just now, or another process's holder() probe had
                    # it for an instant: try once more before reporting busy.
                    claimed = self.lock.try_acquire(SERVER_ROLE)
            except OSError:
                logger.exception("Could not take the index lock")
                return False
            if claimed:
                logger.info("This server now owns the index and indexes new mail")
            return claimed

    def _loop(self) -> None:
        # Sleep between the end of one run and the start of the next, so a
        # slow run never overlaps the following one.
        delay = self.first_delay
        while not self._stop.wait(delay):
            try:
                self.run_once()
            except Exception:
                # run_once already guards the index run; this catches anything
                # else, so one bad pass can't end the schedule for good.
                logger.exception("Scheduled index pass crashed; will retry")
            delay = self.interval

    def run_once(self) -> Optional[dict]:
        """Run one incremental index pass. Never raises from the index run."""
        started = time.monotonic()
        if not self.claim():
            self._report_skip(f"the index is in use by {describe(holder() or {})}")
            return None
        try:
            # The indexer's print() progress goes to the server's stdout (its
            # log file under the HTTP transports). Don't redirect it: that
            # swaps sys.stdout for the whole process, and model loads on tool
            # threads swap it too, so the two could restore it out of order.
            with resources.indexing():
                try:
                    summary = self._run_index(
                        incremental=True,
                        show_progress=False,
                        embedding_service_factory=resources.get_embedding_service,
                        should_stop=self._stop.is_set,
                    )
                except Exception:
                    # Batches committed before the failure are in the loaded
                    # index too.
                    resources.mark_index_current()
                    raise
                if summary.get("ran"):
                    # This process wrote into the index it has loaded, so the
                    # next tool call has no reason to reload it from disk.
                    # Marked inside indexing(), so a tool call can't slip in
                    # between the run ending and the mark and reload anyway.
                    # A skipped run must not mark: whoever holds the job lock
                    # may be writing from outside this process.
                    resources.mark_index_current()
        except Exception as exc:
            logger.exception("Scheduled index run failed")
            self._report_failure(f"{type(exc).__name__}: {exc}")
            return None

        if not summary.get("ran"):
            # We hold the index lock and cleared stale job locks when we took
            # it, so a live job lock means a process outside the lock (Docker
            # on a bind mount, an older install) is indexing the same data.
            reason = "another process outside the index lock holds the index-job lock"
            self._report_skip(reason)
            if self._skips_in_a_row == 1:
                self._notify("Mail search index in use elsewhere", reason)
            return summary
        self._skips_in_a_row = 0

        if summary.get("batch_failures"):
            self._report_failure(
                f"{summary['batch_failures']} batch(es) failed; "
                "new mail will be retried on the next run"
            )
        else:
            if self._failing:
                logger.info("Scheduled index runs are succeeding again")
            self._failing = False

        logger.info(
            "Scheduled index run: indexed=%s skipped=%s failed_batches=%s in %.1fs",
            summary.get("indexed", 0),
            summary.get("skipped", 0),
            summary.get("batch_failures", 0),
            time.monotonic() - started,
        )
        return summary

    def _report_failure(self, detail: str) -> None:
        if not self._failing:
            self._notify("Mail search indexing failed", detail)
        self._failing = True

    def _report_skip(self, reason: str) -> None:
        self._skips_in_a_row += 1
        logger.info(
            "Scheduled index run skipped: %s (%s in a row)", reason, self._skips_in_a_row
        )
        if self._skips_in_a_row == SKIP_ALERT_AFTER:
            self._notify(
                "Mail search indexing is stuck",
                f"The last {SKIP_ALERT_AFTER} runs skipped: {reason}.",
            )


def start_index_schedule(interval: float) -> "IndexScheduler":
    """Take the index (or keep retrying for it) and start indexing on a timer."""
    global _scheduler
    scheduler = IndexScheduler(interval)
    if not scheduler.claim():
        logger.warning(
            "The index is in use by %s. This server will take it over when it's "
            "free (checked every %ss); until then its tools report the index as busy.",
            describe(holder() or {}),
            interval,
        )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Indexing new mail every %ss in this process", interval)
    return scheduler


def ensure_index_available() -> None:
    """Before a tool call: raise a ToolError if another process has the index.

    Only applies to a server running the schedule. Such a server wants the
    index to itself, so if a CLI command (say `prune`) holds it, opening
    Chroma here would put two processes on it.
    """
    if _scheduler is None or _scheduler.claim():
        return
    raise ToolError(
        f"The mail index is in use by {describe(holder() or {})}. "
        "Try again when it finishes."
    )


def shutdown() -> None:
    """Let an index batch in progress finish. The lock is held until exit.

    `_scheduler` stays set, so a tool call that arrives this late still goes
    through `ensure_index_available()`.
    """
    if _scheduler is not None:
        _scheduler.shutdown()

"""Indexing logic for emails."""

import faulthandler
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TypedDict

from tqdm import tqdm

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.mailmate_reader import (
    get_reader_status,
    parse_email_file,
    read_emails_batch,
    scan_eml_files,
)
from mail_semantic_search.runtime_logging import dump_runtime_traceback
from mail_semantic_search.vector_store import VectorStore

logger = logging.getLogger(__name__)


class IndexDiagnostics:
    """Emit periodic indexing heartbeats and dump stacks on prolonged stalls."""

    def __init__(self, enabled: bool, heartbeat_seconds: int, stall_dump_seconds: int):
        self.enabled = enabled
        self.heartbeat_seconds = max(heartbeat_seconds, 1)
        self.stall_dump_seconds = max(stall_dump_seconds, 0)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.batch_number = 0
        self.phase = "startup"
        self.total_seen = 0
        self.total_indexed = 0
        self.total_skipped = 0
        self.current_files: List[str] = []
        self.last_progress_time = time.monotonic()
        self.last_dump_time = 0.0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="index-diagnostics",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def start_batch(self, batch_number: int, batch: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.batch_number = batch_number
            self.phase = "filtering_batch"
            self.current_files = [email["file_path"] for email in batch[:2]]

    def set_phase(self, phase: str, emails: Optional[List[Dict[str, Any]]] = None) -> None:
        with self._lock:
            self.phase = phase
            if emails is not None:
                self.current_files = [email["file_path"] for email in emails[:2]]

    def mark_progress(self, total_seen: int, total_indexed: int, total_skipped: int) -> None:
        with self._lock:
            self.total_seen = total_seen
            self.total_indexed = total_indexed
            self.total_skipped = total_skipped
            self.last_progress_time = time.monotonic()

    def emit(self, message: str, level: int = logging.INFO) -> None:
        logger.log(level, "[diagnostic] %s", message)

    def _snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "batch_number": self.batch_number,
                "phase": self.phase,
                "total_seen": self.total_seen,
                "total_indexed": self.total_indexed,
                "total_skipped": self.total_skipped,
                "current_files": list(self.current_files),
                "reader_status": get_reader_status(),
                "last_progress_time": self.last_progress_time,
                "last_dump_time": self.last_dump_time,
            }

    def _record_dump_time(self, now: float) -> None:
        with self._lock:
            self.last_dump_time = now

    def _run(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            snapshot = self._snapshot()
            files = [Path(path).name for path in snapshot["current_files"] if path]
            reader_current = snapshot["reader_status"].get("current_file")
            if reader_current:
                reader_name = Path(reader_current).name
                if reader_name not in files:
                    files.append(reader_name)
            current_files = ", ".join(files) or "n/a"
            self.emit(
                "phase={phase} batch={batch} seen={seen} indexed={indexed} skipped={skipped} files={files}".format(
                    phase=snapshot["phase"],
                    batch=snapshot["batch_number"],
                    seen=snapshot["total_seen"],
                    indexed=snapshot["total_indexed"],
                    skipped=snapshot["total_skipped"],
                    files=current_files,
                )
            )

            if self.stall_dump_seconds <= 0:
                continue

            now = time.monotonic()
            stalled_for = now - snapshot["last_progress_time"]
            if stalled_for < self.stall_dump_seconds:
                continue
            if now - snapshot["last_dump_time"] < self.stall_dump_seconds:
                continue

            self.emit(
                f"No indexing progress for {int(stalled_for)}s during phase={snapshot['phase']}; dumping all thread tracebacks.",
                level=logging.WARNING,
            )
            if reader_current:
                self.emit(
                    f"Likely stuck while parsing file: {Path(reader_current).name}",
                    level=logging.WARNING,
                )
            dump_runtime_traceback()
            self._record_dump_time(now)


# Issue #12: TypedDict for better type hints
class AttachmentDict(TypedDict, total=False):
    """Type definition for attachment data."""
    filename: str
    content_type: str
    content_disposition: str
    size: int
    extracted_text: str


class EmailDict(TypedDict, total=False):
    """Type definition for email data from parser."""
    subject: str
    from_: str  # 'from' is reserved, use from_ internally
    to: str
    cc: str
    bcc: str
    date: Optional[datetime]
    message_id: str
    body: str
    file_path: str
    attachments: List[AttachmentDict]


def combine_email_text(email: Dict[str, Any]) -> str:
    """
    Combine email fields and attachment content into a single text for embedding.
    
    Args:
        email: Email data dictionary (see EmailDict for expected structure)
    
    Returns:
        Combined text string for embedding
    
    Includes:
    - Subject, from address, and body
    - Attachment filenames
    - Extracted text from attachments (with length limits)
    """
    subject = email.get("subject", "")
    body = email.get("body", "")[:config.body_preview_limit]
    from_addr = email.get("from", "")
    
    # Build base text
    text_parts = [subject, from_addr, body]
    
    # Add attachment information
    attachments = email.get("attachments", [])
    if attachments:
        text_parts.append("Attachments:")
        
        # Limit total attachment text to prevent embedding size issues
        max_chars_per_attachment = config.max_attachment_text_per_file
        max_total_attachment_chars = config.max_total_attachment_text
        total_attachment_chars = 0
        
        for attachment in attachments:
            filename = attachment.get("filename", "Unknown")
            text_parts.append(filename)
            
            # Add extracted text if available
            extracted_text = attachment.get("extracted_text")
            if extracted_text:
                # Truncate per-attachment text
                truncated_text = extracted_text[:max_chars_per_attachment]
                if len(extracted_text) > max_chars_per_attachment:
                    truncated_text += "..."
                
                # Check total limit
                if total_attachment_chars + len(truncated_text) > max_total_attachment_chars:
                    remaining = max_total_attachment_chars - total_attachment_chars
                    if remaining > 0:
                        truncated_text = truncated_text[:remaining] + "..."
                    text_parts.append(truncated_text)
                    break
                
                text_parts.append(truncated_text)
                total_attachment_chars += len(truncated_text)
    
    return "\n".join(text_parts).strip()


def _handle_move_detection(
    email: Dict[str, Any],
    database: Database,
    vector_store: VectorStore,
) -> bool:
    """Check if email's message_id is already indexed at a different path.

    If so, deletes the old SQLite row and Chroma vector so the caller can
    index the new path cleanly. Returns True if a move was detected and
    cleaned up, False otherwise.

    Skips empty/None message_id — those cannot be correlated by content.

    Note: SQLite deletion commits before Chroma deletion. If the process is killed
    between the two, the Chroma vector becomes an orphan. Search-time dedup (Task 5)
    handles orphaned vectors gracefully.
    """
    message_id = email.get("message_id")
    if not message_id:
        return False

    existing = database.get_email_by_message_id(message_id)
    if existing is None:
        return False

    old_path = existing["file_path"]
    if old_path == email["file_path"]:
        return False

    logger.info(
        "Move detected: message_id=%s old_path=%s new_path=%s",
        message_id,
        old_path,
        email["file_path"],
    )
    database.delete_email_by_file_path(old_path)
    # VectorStore.delete_email swallows ChromaError internally; no wrapper needed here.
    vector_store.delete_email(old_path)
    return True


def _print_lock_skip(held: Dict[str, Any]) -> None:
    """Print a KM-safe skip line describing the live lock holder.

    The user's Keyboard-Maestro rule pops a window when its captured output
    contains the substrings "error" or "warning" (case-insensitive). Both
    branches below intentionally avoid those words so incremental runs cleanly
    no-op while a longer job (a multi-hour reextract or another index run) is
    in flight.
    """
    pid = held.get("pid")
    started = held.get("started_at", "")
    if held.get("kind") == "backfill":
        print(
            f"Backfill in progress (pid={pid}, started {started}); "
            "skipping this incremental run."
        )
    else:
        print(
            f"Indexing already in progress (pid={pid}, started {started}); "
            "skipping this incremental run."
        )


def _check_backfill_lock_or_skip() -> bool:
    """Print a KM-safe skip line and return True if a live lock is held.

    Read-only check used by the single-file index path: it skips while either
    a backfill or another index run holds the lock, but never acquires one.
    """
    with Database() as database:
        held = database.get_backfill_lock()
    if held is None:
        return False
    _print_lock_skip(held)
    return True


def _acquire_index_lock_or_skip(kind: str) -> bool:
    """Acquire the index lock for this run; return True on success.

    Returns False (after printing a KM-safe skip line) when another live
    process — a backfill or a concurrent index run — already holds it, so the
    caller can cleanly no-op instead of duplicating work and contending on
    SQLite/Chroma writes. The lock is persisted in the app_state table, so it
    outlives this short-lived connection; release it via
    _release_index_lock_on_exit.
    """
    with Database() as database:
        try:
            database.acquire_backfill_lock(kind=kind)
            return True
        except RuntimeError:
            held = database.get_backfill_lock()
            if held is not None:
                _print_lock_skip(held)
            return False


@contextmanager
def _release_index_lock_on_exit() -> Iterator[None]:
    """Release the index lock when the run finishes (even on error/Ctrl-C).

    Acquisition happens up front via _acquire_index_lock_or_skip; this guard
    only guarantees the lock is cleared. It opens its own short-lived
    connection because the lock lives in the app_state table, not on any one
    connection.
    """
    try:
        yield
    finally:
        with Database() as database:
            database.release_backfill_lock()


def index_emails(
    limit: Optional[int] = None,
    skip_indexed: bool = True,
    show_progress: bool = True,
    incremental: bool = False,
) -> None:
    """Index all emails from the email directory."""
    email_dir = config.email_dir

    if not email_dir.exists():
        raise FileNotFoundError(
            f"Email directory not found: {email_dir}. "
            "Please set EMAIL_DIR in your .env file."
        )

    # Hold the index lock for the whole run so a second `index` /
    # `index --incremental` process cleanly no-ops instead of duplicating the
    # scan+embed work and contending on SQLite/Chroma writes. Also skips while
    # a backfill is in flight (its lock is "backfill"-kinded).
    lock_kind = "incremental" if (incremental and skip_indexed) else "full"
    if not _acquire_index_lock_or_skip(lock_kind):
        return

    print(f"Indexing emails from: {email_dir}")
    print(f"Using embedding model: {config.embedding_model}")
    print(f"Batch size: {config.batch_size}")
    if config.index_runtime_diagnostics:
        logger.info(
            "[diagnostic] Runtime diagnostics enabled for PID %s. "
            "Send SIGUSR1 to dump all thread tracebacks.",
            os.getpid(),
        )

    # Initialize services with context managers for proper cleanup. The lock
    # guard is entered first so it is exited last, releasing the lock even if
    # opening the database or vector store fails.
    with _release_index_lock_on_exit(), Database() as database, VectorStore() as vector_store:
        # Pre-run get_stats() costs ~10s on Chroma collections with hundreds
        # of thousands of rows when running under macOS Docker's filesystem
        # shim. The "Already indexed: X emails" log line was nice-to-have,
        # not load-bearing — we still print final totals at the end.

        date_cutoff = None
        if incremental and skip_indexed:
            watermark = database.get_incremental_scan_watermark()
            if watermark:
                overlap_seconds = max(config.incremental_overlap_seconds, 0)
                date_cutoff = datetime.fromtimestamp(
                    watermark.timestamp() - overlap_seconds
                )
                print(f"Incremental mode watermark: {watermark.isoformat()}")
                print(
                    "Incremental overlap: "
                    f"{overlap_seconds} seconds"
                )
                print(
                    "Incremental mode cutoff (mtime newer than): "
                    f"{date_cutoff.isoformat()}"
                )
            else:
                print(
                    "Incremental mode has no prior watermark yet; "
                    "scanning all files and recording a watermark on success."
                )

        # Materialize the candidate list once. On macOS Docker, each `find`
        # invocation over the maildir tree costs ~80s through the VirtIOFS
        # shim, so we run it exactly once per index_emails call and reuse
        # the result for both counting and iteration.
        candidate_paths: List[Path] = list(
            scan_eml_files(email_dir, show_progress=False, modified_after=date_cutoff)
        )

        if limit is not None:
            progress_total = limit
        else:
            progress_total = len(candidate_paths)
            print(f"Candidate emails to process: {progress_total}")

        # No-op fast path: when we know up front there are zero candidates,
        # advance the watermark and exit before loading the embedding model.
        # Only safe when limit is None (with a limit, progress_total is just
        # the bar ceiling and tells us nothing about actual candidate count).
        if limit is None and progress_total == 0:
            print("No new emails to index.")
            if incremental and skip_indexed:
                watermark = datetime.now()
                database.set_incremental_scan_watermark(watermark)
                print(
                    "Updated incremental scan watermark to: "
                    f"{watermark.isoformat()}"
                )
            return

        # Defer model load until the first batch that actually has work to
        # embed. If every candidate gets filtered out by the should_skip
        # callback, no batch reaches the embed step and the model never
        # loads — turns "lots of candidates, nothing new" into a fast run.
        embedding_service: Optional[EmbeddingService] = None
        diagnostics = IndexDiagnostics(
            enabled=config.index_runtime_diagnostics,
            heartbeat_seconds=config.index_heartbeat_seconds,
            stall_dump_seconds=config.index_stall_dump_seconds,
        )

        total_indexed = 0
        total_skipped = 0
        total_seen = 0
        batch_failures = 0
        run_interrupted = False

        # Pre-parse skip filter: stat each candidate and compare its mtime
        # against the stored row. Saves the cost of parse_email_file (HTML
        # strip, quoted-reply removal, attachment text extraction) for files
        # we'd discard anyway. Mutates the closed-over counter so we can
        # report total_skipped at the end.
        skipped_counter = [0]

        def _skip_unchanged(path: Path) -> bool:
            try:
                stored_mtime = database.get_indexed_mtime(str(path))
                if stored_mtime is None:
                    return False
                if path.stat().st_mtime == stored_mtime:
                    skipped_counter[0] += 1
                    return True
            except (OSError, IOError) as e:
                logger.debug(f"Could not check mtime for {path}: {e}")
            return False

        skip_callback = _skip_unchanged if skip_indexed else None

        # Process emails in batches
        batch_iter = read_emails_batch(
            email_dir,
            batch_size=config.batch_size,
            show_progress=show_progress,
            modified_after=date_cutoff,
            total_candidates=progress_total if show_progress else None,
            max_emails=limit,
            should_skip=skip_callback,
            candidates=candidate_paths,
        )

        pbar = None
        if show_progress:
            pbar = tqdm(total=progress_total, desc="Indexing emails", unit="emails")
        diagnostics.mark_progress(total_seen=0, total_indexed=0, total_skipped=0)
        diagnostics.start()

        try:
            for batch_number, batch in enumerate(batch_iter, start=1):
                if limit and total_indexed >= limit:
                    break

                diagnostics.start_batch(batch_number, batch)
                total_seen += len(batch)
                # The reader's should_skip callback has already filtered out
                # path+mtime matches, so everything in `batch` is new or changed.
                total_skipped = skipped_counter[0]

                # Run move detection on every batch member (cheap: one indexed
                # query by message_id). Files yielded here are by definition
                # not already at this path with matching mtime.
                emails_to_index = []
                if skip_indexed:
                    for email in batch:
                        _handle_move_detection(email, database, vector_store)
                        emails_to_index.append(email)
                else:
                    emails_to_index = batch

                diagnostics.mark_progress(
                    total_seen=total_seen,
                    total_indexed=total_indexed,
                    total_skipped=total_skipped,
                )

                if not emails_to_index:
                    continue

                # Lazy-load the embedding model on the first batch with work.
                # Kept outside the inner try/except so a load failure
                # propagates and terminates the run cleanly (matching the
                # behavior we'd have had with eager construction).
                if embedding_service is None:
                    diagnostics.set_phase(
                        "loading_embedding_model", emails_to_index
                    )
                    embedding_service = EmbeddingService()

                # Get file modification times
                diagnostics.set_phase("collecting_mtimes", emails_to_index)
                email_mtimes = {}
                for email in emails_to_index:
                    try:
                        file_path = Path(email["file_path"])
                        if file_path.exists():
                            email_mtimes[email["file_path"]] = file_path.stat().st_mtime
                    except (OSError, IOError) as e:
                        # Issue #11: Specific exception for file operations
                        logger.debug(f"Could not get mtime for {email['file_path']}: {e}")

                # Issue #17: Improved sync between SQLite and ChromaDB
                # Store in both databases, with proper error handling
                try:
                    # Combine email text for embedding first (before any DB writes)
                    diagnostics.set_phase("building_embedding_text", emails_to_index)
                    texts = [combine_email_text(email) for email in emails_to_index]

                    # Generate embeddings (before any DB writes)
                    diagnostics.set_phase("embedding_batch", emails_to_index)
                    embeddings = embedding_service.embed_texts(texts)

                    # Store metadata in SQLite (batch commit for efficiency)
                    diagnostics.set_phase("writing_sqlite_batch", emails_to_index)
                    for email in emails_to_index:
                        attachments = email.get("attachments", [])
                        file_mtime = email_mtimes.get(email["file_path"])
                        database.add_email(email, attachments, file_mtime, commit=False)
                    
                    # Store in vector database (upsert handles re-indexing)
                    diagnostics.set_phase("upserting_chromadb_batch", emails_to_index)
                    vector_store.add_emails(emails_to_index, embeddings, texts)
                    
                    # Commit SQLite only after ChromaDB succeeds
                    diagnostics.set_phase("committing_sqlite_batch", emails_to_index)
                    database.commit()

                    total_indexed += len(emails_to_index)
                    diagnostics.mark_progress(
                        total_seen=total_seen,
                        total_indexed=total_indexed,
                        total_skipped=total_skipped,
                    )
                    
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
                    # Issue #11 & #17: Log error and rollback SQLite
                    logger.error(f"Failed to index batch of {len(emails_to_index)} emails: {e}")
                    batch_failures += 1
                    try:
                        database.conn.rollback()
                    except sqlite3.Error:
                        pass
                    # Continue with next batch rather than failing entirely
                    continue

                if pbar is not None:
                    if limit is not None:
                        pbar.update(min(len(emails_to_index), max(limit - pbar.n, 0)))
                    else:
                        pbar.update(len(batch))
                    pbar.set_postfix(
                        {
                            "indexed": total_indexed,
                            "skipped": total_skipped,
                            "seen": total_seen,
                        }
                    )

        except KeyboardInterrupt:
            print("\nIndexing interrupted by user")
            run_interrupted = True
        finally:
            diagnostics.stop()
            if pbar is not None:
                pbar.close()
            # Pick up any skips that occurred after the last yielded batch
            # (the reader may skip trailing candidates with no batch boundary).
            total_skipped = skipped_counter[0]

        if incremental and skip_indexed and not run_interrupted and batch_failures == 0:
            watermark = datetime.now()
            database.set_incremental_scan_watermark(watermark)
            print(
                "Updated incremental scan watermark to: "
                f"{watermark.isoformat()}"
            )
        elif incremental and skip_indexed and batch_failures > 0:
            print(
                "Incremental watermark not advanced because one or more batches failed."
            )

        # Skip the final stats query when no rows were touched — it costs
        # ~10s on a large Chroma collection through Docker's FS shim, and
        # the totals would only repeat what we already know.
        print(f"\nIndexing complete!")
        print(f"Newly indexed: {total_indexed} emails")
        print(f"Skipped (already indexed): {total_skipped} emails")
        if total_indexed > 0:
            stats_after = vector_store.get_stats()
            db_stats_after = database.get_stats()
            print(f"Total indexed in ChromaDB: {stats_after['total_emails']} emails")
            print(f"Total indexed in database: {db_stats_after['total_emails']} emails")
            print(f"Total attachments: {db_stats_after['total_attachments']}")


def _reextract_single(
    file_path: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """Re-extract one indexed email; print a before/after diff.

    Acquires the backfill lock just for the duration so a concurrent
    `index --incremental` cleanly no-ops.
    """
    from mail_semantic_search.mailmate_reader import CURRENT_EXTRACTION_VERSION

    with Database() as database, VectorStore() as vector_store:
        cursor = database.conn.cursor()
        if file_path:
            cursor.execute(
                "SELECT * FROM emails WHERE file_path = ? LIMIT 1", (file_path,)
            )
        elif message_id:
            cursor.execute(
                "SELECT * FROM emails WHERE message_id = ? LIMIT 1", (message_id,)
            )
        else:
            raise ValueError("Pass --file-path or --message-id")

        row = cursor.fetchone()
        if row is None:
            print("No indexed email matched the selector.")
            return

        row_dict = dict(row)
        old_preview = (row_dict.get("body_preview") or "")[:200]
        old_version = row_dict.get("extraction_version", 0)

        path = Path(row_dict["file_path"])
        if not path.exists():
            print(f"missing_file: source .eml no longer exists: {row_dict['file_path']}")
            return

        database.acquire_backfill_lock()
        try:
            email_data = parse_email_file(path, base_dir=config.email_dir)
            if email_data is None:
                print("parse_failed: parse_email_file returned None")
                return

            text = combine_email_text(email_data)
            embedding_service = EmbeddingService()
            embeddings = embedding_service.embed_texts([text])

            try:
                file_mtime = path.stat().st_mtime
            except OSError:
                file_mtime = None

            database.add_email(
                email_data,
                email_data.get("attachments", []),
                file_mtime,
                commit=False,
            )
            vector_store.add_emails([email_data], embeddings, [text])
            database.commit()
        finally:
            database.release_backfill_lock()

        new_preview = (email_data.get("body") or "")[:200]
        print(f"file_path: {row_dict['file_path']}")
        print(f"extraction_version: {old_version} → {CURRENT_EXTRACTION_VERSION}")
        print("--- before (first 200 chars) ---")
        print(old_preview)
        print("--- after  (first 200 chars) ---")
        print(new_preview)


def _reextract_bulk(
    limit: Optional[int] = None,
    batch_size: int = 64,
    dry_run: bool = False,
) -> None:
    """Re-extract every row with stale extraction_version.

    Per-batch flow mirrors index_emails: parse all -> embed once for the
    whole batch -> bulk SQLite upsert -> bulk Chroma upsert -> commit. The
    single-shot embed is the perf-critical part — embedding 64 texts in one
    call is ~10x faster than 64 individual embed calls.
    """
    from mail_semantic_search.mailmate_reader import CURRENT_EXTRACTION_VERSION

    with Database() as database, VectorStore() as vector_store:
        cursor = database.conn.cursor()

        if dry_run:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM emails WHERE extraction_version < ?",
                (CURRENT_EXTRACTION_VERSION,),
            )
            cnt = cursor.fetchone()["cnt"]
            print(f"Dry run: {cnt} stale row(s) at extraction_version < {CURRENT_EXTRACTION_VERSION}.")
            return

        sql = "SELECT id, file_path FROM emails WHERE extraction_version < ? ORDER BY id"
        params: List[Any] = [CURRENT_EXTRACTION_VERSION]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]

        total = len(rows)
        if total == 0:
            print("No stale rows. Nothing to reextract.")
            return

        print(f"Reextract: {total} stale row(s) at extraction_version < {CURRENT_EXTRACTION_VERSION}.")

        database.acquire_backfill_lock()
        try:
            embedding_service = EmbeddingService()

            processed = 0
            missing = 0
            parse_failed = 0
            batch_errors = 0
            t_start = time.time()

            for batch_start in range(0, total, batch_size):
                batch = rows[batch_start : batch_start + batch_size]

                emails_to_write: List[Dict[str, Any]] = []
                mtimes: Dict[str, Optional[float]] = {}
                for r in batch:
                    path = Path(r["file_path"])
                    if not path.exists():
                        missing += 1
                        continue
                    try:
                        email_data = parse_email_file(path, base_dir=config.email_dir)
                    except (OSError, ValueError, TypeError) as e:
                        logger.warning("reextract: parse failed for %s: %s", path, e)
                        parse_failed += 1
                        continue
                    if email_data is None:
                        parse_failed += 1
                        continue
                    try:
                        mtimes[email_data["file_path"]] = path.stat().st_mtime
                    except OSError:
                        mtimes[email_data["file_path"]] = None
                    emails_to_write.append(email_data)

                if not emails_to_write:
                    pct = int(100 * (batch_start + len(batch)) / total)
                    print(
                        f"  Reextracted {batch_start + len(batch)}/{total} ({pct}%) "
                        f"ok={processed} missing={missing} parse_failed={parse_failed} batch_errors={batch_errors}"
                    )
                    continue

                try:
                    texts = [combine_email_text(e) for e in emails_to_write]
                    embeddings = embedding_service.embed_texts(texts)

                    for email_data in emails_to_write:
                        database.add_email(
                            email_data,
                            email_data.get("attachments", []),
                            mtimes.get(email_data["file_path"]),
                            commit=False,
                        )
                    vector_store.add_emails(emails_to_write, embeddings, texts)
                    database.commit()
                    processed += len(emails_to_write)
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
                    logger.warning("reextract: batch write failed (%s); rolling back", e)
                    try:
                        database.conn.rollback()
                    except sqlite3.Error:
                        pass
                    batch_errors += 1

                elapsed = time.time() - t_start
                done_count = batch_start + len(batch)
                pct = int(100 * done_count / total)
                rate = done_count / elapsed if elapsed > 0 else 0
                eta_s = int((total - done_count) / rate) if rate > 0 else 0
                eta_m = eta_s // 60
                print(
                    f"  Reextracted {done_count}/{total} ({pct}%) "
                    f"ok={processed} missing={missing} parse_failed={parse_failed} batch_errors={batch_errors} "
                    f"rate={rate:.1f}/s eta={eta_m}m"
                )
        finally:
            database.release_backfill_lock()

        print(
            f"Done. ok={processed} missing={missing} parse_failed={parse_failed} "
            f"batch_errors={batch_errors} target_version={CURRENT_EXTRACTION_VERSION}"
        )


def index_email_file(file_path: Path, force: bool = False) -> Dict[str, Any]:
    """Index a single .eml file.

    Args:
        file_path: Path to the .eml file (must live under config.email_dir).
        force: If True, re-embed and re-upsert even when the stored mtime matches.

    Returns:
        Dict with keys:
          status: one of "indexed", "skipped", "moved", "not_found", "failed"
          file_path: resolved path as string
          message: human-readable detail
    """
    resolved = Path(file_path).expanduser().resolve()
    result: Dict[str, Any] = {"status": "failed", "file_path": str(resolved), "message": ""}

    if not resolved.exists():
        result["status"] = "not_found"
        result["message"] = f"File does not exist: {resolved}"
        return result

    if _check_backfill_lock_or_skip():
        result["status"] = "skipped"
        result["message"] = "Backfill in progress; skipping this incremental run."
        return result

    email_dir = config.email_dir.resolve()
    try:
        resolved.relative_to(email_dir)
    except ValueError:
        result["message"] = f"File is outside EMAIL_DIR ({email_dir}): {resolved}"
        return result

    email_data = parse_email_file(resolved, base_dir=email_dir)
    if email_data is None:
        result["message"] = "parse_email_file returned None (unparseable or filtered)"
        return result

    with Database() as database, VectorStore() as vector_store:
        if not force and database.email_exists(email_data["file_path"]):
            try:
                current_mtime = resolved.stat().st_mtime
                file_hash = get_file_hash(email_data["file_path"])
                existing = database.get_email_by_file_hash(file_hash)
                if existing and existing.get("file_mtime") == current_mtime:
                    result["status"] = "skipped"
                    result["message"] = "Already indexed (mtime unchanged)"
                    return result
            except (OSError, IOError) as e:
                logger.debug(f"Could not check mtime for {resolved}: {e}")

        moved = _handle_move_detection(email_data, database, vector_store)

        try:
            file_mtime = resolved.stat().st_mtime
        except OSError:
            file_mtime = None

        embedding_service = EmbeddingService()
        text = combine_email_text(email_data)
        embeddings = embedding_service.embed_texts([text])

        try:
            database.add_email(
                email_data,
                email_data.get("attachments", []),
                file_mtime,
                commit=False,
            )
            vector_store.add_emails([email_data], embeddings, [text])
            database.commit()
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
            try:
                database.conn.rollback()
            except sqlite3.Error:
                pass
            result["message"] = f"Indexing failed: {e}"
            return result

        result["status"] = "moved" if moved else "indexed"
        result["message"] = (
            "Re-indexed at new path (old entry deleted)"
            if moved
            else "Indexed"
        )
        return result

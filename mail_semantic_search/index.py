"""Indexing logic for emails."""

import faulthandler
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from tqdm import tqdm

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.mailmate_reader import (
    count_eml_files,
    get_reader_status,
    read_emails_batch,
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


def index_emails(
    limit: Optional[int] = None,
    skip_indexed: bool = True,
    show_progress: bool = True,
    incremental: bool = False,
) -> None:
    """Index all emails from the MailMate directory."""
    email_dir = config.mailmate_email_dir

    if not email_dir.exists():
        raise FileNotFoundError(
            f"MailMate email directory not found: {email_dir}. "
            "Please set MAILMATE_EMAIL_DIR in your .env file."
        )

    print(f"Indexing emails from: {email_dir}")
    print(f"Using embedding model: {config.embedding_model}")
    print(f"Batch size: {config.batch_size}")
    if config.index_runtime_diagnostics:
        logger.info(
            "[diagnostic] Runtime diagnostics enabled for PID %s. "
            "Send SIGUSR1 to dump all thread tracebacks.",
            os.getpid(),
        )

    # Initialize services with context managers for proper cleanup
    with Database() as database, VectorStore() as vector_store:
        embedding_service = EmbeddingService()
        diagnostics = IndexDiagnostics(
            enabled=config.index_runtime_diagnostics,
            heartbeat_seconds=config.index_heartbeat_seconds,
            stall_dump_seconds=config.index_stall_dump_seconds,
        )

        # Get stats before indexing
        stats_before = vector_store.get_stats()
        db_stats_before = database.get_stats()
        print(f"Already indexed: {stats_before['total_emails']} emails in ChromaDB")
        print(f"Already indexed: {db_stats_before['total_emails']} emails in database")

        total_indexed = 0
        total_skipped = 0
        total_seen = 0
        batch_failures = 0
        run_interrupted = False

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

        if limit is not None:
            progress_total = limit
        else:
            progress_total = count_eml_files(email_dir, modified_after=date_cutoff)
            print(f"Candidate emails to process: {progress_total}")

        # Process emails in batches
        batch_iter = read_emails_batch(
            email_dir,
            batch_size=config.batch_size,
            show_progress=show_progress,
            modified_after=date_cutoff,
            total_candidates=progress_total if show_progress else None,
            max_emails=limit,
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

                # Filter out already indexed emails if requested
                emails_to_index = []
                if skip_indexed:
                    for email in batch:
                        file_path = Path(email["file_path"])
                        # Check if already indexed in database
                        if database.email_exists(email["file_path"]):
                            # Check file modification time
                            try:
                                current_mtime = file_path.stat().st_mtime
                                file_hash = get_file_hash(email["file_path"])
                                email_record = database.get_email_by_file_hash(file_hash)
                                if email_record and email_record.get("file_mtime") == current_mtime:
                                    total_skipped += 1
                                    continue
                            except (OSError, IOError) as e:
                                # Issue #11: Specific exception for file operations
                                logger.debug(f"Could not check file mtime for {email['file_path']}: {e}")
                        
                        # Also check ChromaDB
                        if vector_store.is_indexed(email["file_path"]):
                            # Still need to update if file changed, so continue
                            pass
                        
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

        # Get final stats
        stats_after = vector_store.get_stats()
        db_stats_after = database.get_stats()
        print(f"\nIndexing complete!")
        print(f"Newly indexed: {total_indexed} emails")
        print(f"Skipped (already indexed): {total_skipped} emails")
        print(f"Total indexed in ChromaDB: {stats_after['total_emails']} emails")
        print(f"Total indexed in database: {db_stats_after['total_emails']} emails")
        print(f"Total attachments: {db_stats_after['total_attachments']}")



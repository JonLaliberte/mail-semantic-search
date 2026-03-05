"""Indexing logic for emails."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from tqdm import tqdm

from mailmate_search.config import config
from mailmate_search.database import Database, get_file_hash
from mailmate_search.embedding_service import EmbeddingService
from mailmate_search.mailmate_reader import count_eml_files, read_emails_batch
from mailmate_search.vector_store import VectorStore

logger = logging.getLogger(__name__)


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
) -> None:
    """Index all emails from the MailMate directory."""
    email_dir = config.mailmate_email_dir

    if not email_dir.exists():
        print(f"Error: MailMate email directory not found: {email_dir}")
        print("Please set MAILMATE_EMAIL_DIR in your .env file")
        return

    print(f"Indexing emails from: {email_dir}")
    print(f"Using embedding model: {config.embedding_model}")
    print(f"Batch size: {config.batch_size}")

    # Initialize services with context managers for proper cleanup
    with Database() as database, VectorStore() as vector_store:
        embedding_service = EmbeddingService()

        # Get stats before indexing
        stats_before = vector_store.get_stats()
        db_stats_before = database.get_stats()
        print(f"Already indexed: {stats_before['total_emails']} emails in ChromaDB")
        print(f"Already indexed: {db_stats_before['total_emails']} emails in database")

        total_indexed = 0
        total_skipped = 0
        total_seen = 0

        date_cutoff = None
        if skip_indexed:
            date_cutoff = database.get_latest_indexed_email_date()
            if date_cutoff:
                print(f"Incremental mode cutoff (newer than): {date_cutoff.isoformat()}")

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

        try:
            for batch in batch_iter:
                if limit and total_indexed >= limit:
                    break

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

                if not emails_to_index:
                    continue

                # Get file modification times
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
                    texts = [combine_email_text(email) for email in emails_to_index]

                    # Generate embeddings (before any DB writes)
                    embeddings = embedding_service.embed_texts(texts)

                    # Store metadata in SQLite (batch commit for efficiency)
                    for email in emails_to_index:
                        attachments = email.get("attachments", [])
                        file_mtime = email_mtimes.get(email["file_path"])
                        database.add_email(email, attachments, file_mtime, commit=False)
                    
                    # Store in vector database (upsert handles re-indexing)
                    vector_store.add_emails(emails_to_index, embeddings, texts)
                    
                    # Commit SQLite only after ChromaDB succeeds
                    database.commit()

                    total_indexed += len(emails_to_index)
                    
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
                    # Issue #11 & #17: Log error and rollback SQLite
                    logger.error(f"Failed to index batch of {len(emails_to_index)} emails: {e}")
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
        finally:
            if pbar is not None:
                pbar.close()

        # Get final stats
        stats_after = vector_store.get_stats()
        db_stats_after = database.get_stats()
        print(f"\nIndexing complete!")
        print(f"Newly indexed: {total_indexed} emails")
        print(f"Skipped (already indexed): {total_skipped} emails")
        print(f"Total indexed in ChromaDB: {stats_after['total_emails']} emails")
        print(f"Total indexed in database: {db_stats_after['total_emails']} emails")
        print(f"Total attachments: {db_stats_after['total_attachments']}")



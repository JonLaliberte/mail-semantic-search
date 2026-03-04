"""SQLite database for email metadata storage."""

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mailmate_search.config import config

logger = logging.getLogger(__name__)


def get_file_hash(file_path: str) -> str:
    """Generate a hash for a file path.
    
    This function is used to create unique identifiers for emails based on their
    file path. The hash is used to link emails between the SQLite database and
    ChromaDB vector store.
    """
    return hashlib.md5(file_path.encode()).hexdigest()


def validate_file_path(file_path: str, base_dir: Optional[Path] = None) -> bool:
    """Validate that a file path is safe and within the expected directory.
    
    Issue #3: Optional security enhancement for path validation.
    
    Args:
        file_path: The file path to validate
        base_dir: Optional base directory the path should be within.
                  If None, uses config.mailmate_email_dir.
    
    Returns:
        True if the path is valid and within the base directory, False otherwise.
    """
    if base_dir is None:
        base_dir = config.mailmate_email_dir
    
    try:
        # Resolve both paths to handle symlinks and relative paths
        resolved_path = Path(file_path).resolve()
        resolved_base = base_dir.resolve()
        
        # Check path length (prevent extremely long paths)
        if len(str(resolved_path)) > 4096:  # Common filesystem limit
            logger.warning(f"Path too long: {file_path[:100]}...")
            return False
        
        # Check that the resolved path starts with the base directory
        return str(resolved_path).startswith(str(resolved_base))
    except (OSError, ValueError) as e:
        logger.warning(f"Invalid file path '{file_path}': {e}")
        return False


class Database:
    """SQLite database for storing email metadata."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection and create schema if needed.
        
        Note: This database is designed for single-threaded use. If multi-threading
        is needed in the future, implement connection pooling or use thread-local
        connections instead of sharing a single connection.
        """
        self.db_path = db_path or config.database_path
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        
        # Issue #15: Enable WAL mode for better performance and concurrent reads
        self.conn.execute("PRAGMA journal_mode=WAL")
        
        # Issue #1: Enable foreign key constraints (required for ON DELETE CASCADE)
        self.conn.execute("PRAGMA foreign_keys = ON")
        
        self._create_schema()

    def _create_schema(self) -> None:
        """Create database schema if it doesn't exist."""
        cursor = self.conn.cursor()

        # Create emails table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE,
                file_path TEXT UNIQUE NOT NULL,
                file_hash TEXT NOT NULL,
                subject TEXT,
                from_addr TEXT,
                to_addrs TEXT,
                cc_addrs TEXT,
                bcc_addrs TEXT,
                date DATETIME,
                body_preview TEXT,
                has_attachments BOOLEAN DEFAULT 0,
                attachment_count INTEGER DEFAULT 0,
                file_size INTEGER,
                indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                file_mtime REAL
            )
            """
        )

        # Create attachments table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                filename TEXT,
                content_type TEXT,
                file_extension TEXT,
                size INTEGER,
                content_disposition TEXT,
                FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
            )
            """
        )

        # Create indexes
        # Note: Indexes on to_addrs, cc_addrs, bcc_addrs help with exact matches
        # and LIKE queries with trailing wildcards (e.g., 'value%')
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_addr)",
            "CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date)",
            "CREATE INDEX IF NOT EXISTS idx_emails_has_attachments ON emails(has_attachments)",
            "CREATE INDEX IF NOT EXISTS idx_emails_file_hash ON emails(file_hash)",
            "CREATE INDEX IF NOT EXISTS idx_emails_to_addrs ON emails(to_addrs)",
            "CREATE INDEX IF NOT EXISTS idx_emails_cc_addrs ON emails(cc_addrs)",
            "CREATE INDEX IF NOT EXISTS idx_emails_bcc_addrs ON emails(bcc_addrs)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_email_id ON attachments(email_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_extension ON attachments(file_extension)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename)",
        ]

        for index_sql in indexes:
            cursor.execute(index_sql)

        self.conn.commit()

    def _get_file_hash(self, file_path: str) -> str:
        """Generate a hash for a file path."""
        return get_file_hash(file_path)

    def email_exists(self, file_path: str) -> bool:
        """Check if an email with this file path exists."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM emails WHERE file_path = ?", (file_path,))
        return cursor.fetchone() is not None

    def get_email_by_file_hash(self, file_hash: str) -> Optional[Dict]:
        """Get email record by file hash."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM emails WHERE file_hash = ?", (file_hash,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def add_email(
        self,
        email_data: Dict,
        attachments: List[Dict],
        file_mtime: Optional[float] = None,
        commit: bool = True,
    ) -> int:
        """Add an email and its attachments to the database.
        
        Uses upsert pattern to handle updates properly and wraps all operations
        in a transaction for data consistency.
        
        Args:
            email_data: Email metadata dictionary
            attachments: List of attachment dictionaries
            file_mtime: Optional file modification time
            commit: Whether to commit after inserting (set False for batch operations)
            
        Returns:
            The email ID (new or existing)
            
        Raises:
            sqlite3.Error: If database operation fails (transaction is rolled back)
        """
        cursor = self.conn.cursor()
        file_hash = self._get_file_hash(email_data["file_path"])

        # Get file size with specific exception handling (Issue #11)
        file_size = None
        try:
            file_path = Path(email_data["file_path"])
            if file_path.exists():
                file_size = file_path.stat().st_size
        except (OSError, IOError) as e:
            logger.debug(f"Could not get file size for {email_data['file_path']}: {e}")

        # Issue #9: Use config.body_preview_limit for consistent truncation
        body_preview = email_data.get("body", "")[:config.body_preview_limit] if email_data.get("body") else ""

        try:
            # Issue #16: Explicit transaction for atomic operations
            # Issue #1 & #8: Use upsert pattern (ON CONFLICT) to preserve ID and handle cascades properly
            cursor.execute(
                """
                INSERT INTO emails (
                    message_id, file_path, file_hash, subject, from_addr,
                    to_addrs, cc_addrs, bcc_addrs, date, body_preview,
                    has_attachments, attachment_count, file_size, file_mtime, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    message_id = excluded.message_id,
                    file_hash = excluded.file_hash,
                    subject = excluded.subject,
                    from_addr = excluded.from_addr,
                    to_addrs = excluded.to_addrs,
                    cc_addrs = excluded.cc_addrs,
                    bcc_addrs = excluded.bcc_addrs,
                    date = excluded.date,
                    body_preview = excluded.body_preview,
                    has_attachments = excluded.has_attachments,
                    attachment_count = excluded.attachment_count,
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    indexed_at = excluded.indexed_at
                """,
                (
                    email_data.get("message_id", ""),
                    email_data["file_path"],
                    file_hash,
                    email_data.get("subject", ""),
                    email_data.get("from", ""),
                    email_data.get("to", ""),
                    email_data.get("cc", ""),
                    email_data.get("bcc", ""),
                    email_data.get("date"),
                    body_preview,
                    len(attachments) > 0,
                    len(attachments),
                    file_size,
                    file_mtime,
                    datetime.now(),
                ),
            )

            # Get the email_id (works for both insert and update)
            if cursor.lastrowid:
                email_id = cursor.lastrowid
            else:
                # If lastrowid is 0, the row was updated - fetch the existing ID
                cursor.execute("SELECT id FROM emails WHERE file_path = ?", (email_data["file_path"],))
                row = cursor.fetchone()
                email_id = row["id"] if row else cursor.lastrowid

            # Delete existing attachments for this email (will cascade properly now with FK enabled)
            cursor.execute("DELETE FROM attachments WHERE email_id = ?", (email_id,))

            # Insert attachments
            for attachment in attachments:
                # Extract file extension with specific exception handling (Issue #11)
                filename = attachment.get("filename", "")
                file_extension = ""
                if filename:
                    try:
                        file_extension = Path(filename).suffix.lower().lstrip(".")
                    except (OSError, ValueError) as e:
                        logger.debug(f"Could not parse extension from {filename}: {e}")

                cursor.execute(
                    """
                    INSERT INTO attachments (
                        email_id, filename, content_type, file_extension,
                        size, content_disposition
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email_id,
                        filename,
                        attachment.get("content_type", ""),
                        file_extension,
                        attachment.get("size", 0),
                        attachment.get("content_disposition", ""),
                    ),
                )

            if commit:
                self.conn.commit()
            return email_id
            
        except sqlite3.Error:
            # Issue #8 & #16: Rollback on any error to maintain consistency
            self.conn.rollback()
            raise

    def commit(self) -> None:
        """Commit the current transaction.
        
        Use this after calling add_email with commit=False for batch operations.
        """
        self.conn.commit()

    def get_attachments(self, email_id: int) -> List[Dict]:
        """Get all attachments for an email."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM attachments WHERE email_id = ?", (email_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_attachments_batch(self, email_ids: List[int]) -> List[Dict]:
        """Get all attachments for multiple emails in a single query.
        
        This is more efficient than calling get_attachments() for each email.
        Handles large lists by batching the IN clause (Issue #6).
        """
        if not email_ids:
            return []
        
        cursor = self.conn.cursor()
        results = []
        
        # Issue #6: Batch large IN clauses to avoid SQLite limits and performance issues
        max_batch = config.MAX_IN_CLAUSE_SIZE
        for i in range(0, len(email_ids), max_batch):
            batch_ids = email_ids[i:i + max_batch]
            placeholders = ",".join("?" * len(batch_ids))
            cursor.execute(
                f"SELECT * FROM attachments WHERE email_id IN ({placeholders})",
                batch_ids,
            )
            results.extend([dict(row) for row in cursor.fetchall()])
        
        return results

    def get_stats(self) -> Dict:
        """Get database statistics."""
        cursor = self.conn.cursor()

        stats = {}

        # Total emails
        cursor.execute("SELECT COUNT(*) as count FROM emails")
        stats["total_emails"] = cursor.fetchone()["count"]

        # Total attachments
        cursor.execute("SELECT COUNT(*) as count FROM attachments")
        stats["total_attachments"] = cursor.fetchone()["count"]

        # Emails with attachments
        cursor.execute(
            "SELECT COUNT(*) as count FROM emails WHERE has_attachments = 1"
        )
        stats["emails_with_attachments"] = cursor.fetchone()["count"]

        # Date range
        cursor.execute(
            "SELECT MIN(date) as min_date, MAX(date) as max_date FROM emails WHERE date IS NOT NULL"
        )
        date_row = cursor.fetchone()
        stats["date_range"] = {
            "min": date_row["min_date"],
            "max": date_row["max_date"],
        }

        return stats

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


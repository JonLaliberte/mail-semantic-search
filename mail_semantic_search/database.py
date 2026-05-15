"""SQLite database for email metadata storage."""

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mail_semantic_search.config import config

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
                  If None, uses config.email_dir.
    
    Returns:
        True if the path is valid and within the base directory, False otherwise.
    """
    if base_dir is None:
        base_dir = config.email_dir
    
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
        self._migrate_message_id_uniqueness()

    def _create_schema(self) -> None:
        """Create database schema if it doesn't exist."""
        cursor = self.conn.cursor()

        # Create emails table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
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

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
            "CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id)",
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

    def _migrate_message_id_uniqueness(self) -> None:
        """Migrate legacy schema where message_id was globally unique.

        Some real-world mail stores contain duplicate Message-ID values across
        different folders or accounts. We key indexing by file_path, so message_id
        must not be globally unique.
        """
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(emails)")
        columns = [dict(row) for row in cursor.fetchall()]

        # If table has no message_id column or no uniqueness marker, nothing to do.
        message_id_cols = [col for col in columns if col["name"] == "message_id"]
        if not message_id_cols:
            return
        if not bool(message_id_cols[0].get("pk", 0)) and message_id_cols[0].get("notnull", 0) == 0:
            # PRAGMA table_info does not expose UNIQUE directly, so detect via indexes below.
            pass

        if not self._has_unique_index_on_column("emails", "message_id"):
            return

        logger.info("Migrating emails schema to remove unique constraint on message_id")

        # Rebuild emails table without UNIQUE(message_id), preserving IDs for FK integrity.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS emails_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
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
            cursor.execute(
                """
                INSERT INTO emails_new (
                    id, message_id, file_path, file_hash, subject, from_addr,
                    to_addrs, cc_addrs, bcc_addrs, date, body_preview,
                    has_attachments, attachment_count, file_size, indexed_at, file_mtime
                )
                SELECT
                    id, message_id, file_path, file_hash, subject, from_addr,
                    to_addrs, cc_addrs, bcc_addrs, date, body_preview,
                    has_attachments, attachment_count, file_size, indexed_at, file_mtime
                FROM emails
                """
            )
            cursor.execute("DROP TABLE emails")
            cursor.execute("ALTER TABLE emails_new RENAME TO emails")
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

        # Recreate non-unique indexes that are dropped with table rebuild.
        self._create_schema()

    def _has_unique_index_on_column(self, table_name: str, column_name: str) -> bool:
        """Return True if table has a unique index exactly on column_name."""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA index_list({table_name})")
        indexes = cursor.fetchall()

        for idx in indexes:
            unique = idx["unique"] if isinstance(idx, sqlite3.Row) else idx[2]
            idx_name = idx["name"] if isinstance(idx, sqlite3.Row) else idx[1]
            if not unique:
                continue
            cursor.execute(f"PRAGMA index_info({idx_name})")
            columns = cursor.fetchall()
            col_names = [
                col["name"] if isinstance(col, sqlite3.Row) else col[2]
                for col in columns
            ]
            if col_names == [column_name]:
                return True
        return False

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

    def get_email_by_message_id(self, message_id: str) -> Optional[Dict]:
        """Return the first email row matching message_id, or None.

        Returns None for empty/None message_id to avoid matching unaddressed emails.
        """
        if not message_id:
            return None
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM emails WHERE message_id = ? LIMIT 1",
            (message_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def delete_email_by_file_path(self, file_path: str, commit: bool = True) -> None:
        """Delete the email row (and cascaded attachments) for the given file path."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM emails WHERE file_path = ?", (file_path,))
        if commit:
            self.conn.commit()

    def dedup_by_message_id(self, vector_store) -> Tuple[int, int]:
        """Remove duplicate emails keeping the most-recently indexed per message_id.

        Skips rows where message_id is NULL or empty — those cannot be correlated.

        Returns:
            (removed_count, kept_count)
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT message_id, COUNT(*) AS cnt
            FROM emails
            WHERE message_id IS NOT NULL AND message_id != ''
            GROUP BY message_id
            HAVING cnt > 1
            """
        )
        duplicate_groups = cursor.fetchall()

        removed = 0
        kept = 0
        for i, row in enumerate(duplicate_groups):
            mid = row["message_id"] if isinstance(row, sqlite3.Row) else row[0]
            cursor.execute(
                """
                SELECT id, file_path, indexed_at
                FROM emails
                WHERE message_id = ?
                ORDER BY indexed_at DESC
                """,
                (mid,),
            )
            duplicates = cursor.fetchall()
            # Keep the first (most recent), delete the rest
            for dup in duplicates[1:]:
                fp = dup["file_path"] if isinstance(dup, sqlite3.Row) else dup[1]
                self.delete_email_by_file_path(fp, commit=False)
                vector_store.delete_email(fp)
                removed += 1
            kept += 1
            if (i + 1) % 1000 == 0:
                self.conn.commit()
                logger.info("dedup progress: %d/%d groups processed, %d rows removed so far", i + 1, len(duplicate_groups), removed)
        self.conn.commit()
        return removed, kept

    def count_duplicate_message_ids(self) -> Tuple[int, int]:
        """Count message_ids that have duplicates and total rows that would be removed.

        Returns:
            (group_count, rows_to_remove) — groups with duplicates and surplus row count
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM emails
            WHERE message_id IS NOT NULL AND message_id != ''
            GROUP BY message_id
            HAVING cnt > 1
            """
        )
        counts = cursor.fetchall()
        group_count = len(counts)
        rows_to_remove = sum(
            (row["cnt"] if isinstance(row, sqlite3.Row) else row[0]) - 1
            for row in counts
        )
        return group_count, rows_to_remove

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
                    email_data.get("message_id") or None,
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

    def list_inbox_emails(
        self,
        limit: int = 50,
        account: Optional[str] = None,
        date_after: Optional[datetime] = None,
        date_before: Optional[datetime] = None,
    ) -> List[Dict]:
        """Return inbox emails newest-first with a short body snippet.

        Inbox detection is path-based: MailMate stores INBOX messages under a
        `/INBOX.mailbox/` path segment, while archived mail lives elsewhere
        (e.g. `[Gmail].mailbox/All Mail.mailbox/`). `account` accepts the bare
        email form and is URL-encoded (`@` -> `%40`) to match the on-disk path.
        Date bounds are strict (`date > date_after`, `date < date_before`) so a
        caller can page by feeding the oldest result's date back as
        `date_before`.
        """
        # Clamp limit to [1, 500] to match documented behavior.
        clamped_limit = max(1, min(limit, 500))

        sql_parts = [
            "SELECT id, message_id, from_addr, to_addrs, subject, date,",
            "       has_attachments, body_preview",
            "FROM emails",
            "WHERE file_path LIKE ?",
        ]
        params: list = ["%/INBOX.mailbox/%"]

        if account:
            account_encoded = account.replace("@", "%40")
            sql_parts.append("AND file_path LIKE ?")
            params.append(f"%{account_encoded}%/INBOX.mailbox/%")

        if date_after is not None:
            sql_parts.append("AND date > ?")
            params.append(date_after)

        if date_before is not None:
            sql_parts.append("AND date < ?")
            params.append(date_before)

        sql_parts.append("ORDER BY date DESC")
        sql_parts.append("LIMIT ?")
        params.append(clamped_limit)

        cursor = self.conn.cursor()
        cursor.execute("\n".join(sql_parts), params)

        results: List[Dict] = []
        for row in cursor.fetchall():
            preview = row["body_preview"] or ""
            results.append(
                {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "from": row["from_addr"],
                    "to": row["to_addrs"],
                    "subject": row["subject"],
                    "date": row["date"],
                    "has_attachments": bool(row["has_attachments"]),
                    "body_snippet": preview[:200],
                }
            )
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

    def get_latest_indexed_email_date(self) -> Optional[datetime]:
        """Get the most recent parsed email date in the index."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT MAX(date) AS max_date FROM emails WHERE date IS NOT NULL")
        row = cursor.fetchone()
        if not row or not row["max_date"]:
            return None

        date_str = str(row["max_date"])
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            logger.debug(f"Could not parse latest indexed email date: {date_str}")
            return None

    def get_max_indexed_file_mtime(self) -> Optional[datetime]:
        """Return the latest indexed file modification time as a datetime."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT MAX(file_mtime) AS max_file_mtime FROM emails WHERE file_mtime IS NOT NULL")
        row = cursor.fetchone()
        if not row or row["max_file_mtime"] is None:
            return None

        try:
            return datetime.fromtimestamp(float(row["max_file_mtime"]))
        except (TypeError, ValueError, OSError) as e:
            logger.debug(f"Could not parse max indexed file mtime: {row['max_file_mtime']}: {e}")
            return None

    def get_incremental_scan_watermark(self) -> Optional[datetime]:
        """Return the persisted scan watermark, falling back to indexed file mtimes."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT value FROM app_state WHERE key = ?",
            ("incremental_scan_watermark",),
        )
        row = cursor.fetchone()
        if row and row["value"]:
            try:
                return datetime.fromisoformat(str(row["value"]))
            except ValueError:
                logger.debug(f"Could not parse incremental scan watermark: {row['value']}")

        return self.get_max_indexed_file_mtime()

    def set_incremental_scan_watermark(self, watermark: datetime) -> None:
        """Persist the last successful incremental scan watermark."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("incremental_scan_watermark", watermark.isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


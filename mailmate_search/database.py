"""SQLite database for email metadata storage."""

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mailmate_search.config import config


class Database:
    """SQLite database for storing email metadata."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection and create schema if needed."""
        self.db_path = db_path or config.database_path
        self.conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
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
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_addr)",
            "CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date)",
            "CREATE INDEX IF NOT EXISTS idx_emails_has_attachments ON emails(has_attachments)",
            "CREATE INDEX IF NOT EXISTS idx_emails_file_hash ON emails(file_hash)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_email_id ON attachments(email_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_extension ON attachments(file_extension)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename)",
        ]

        for index_sql in indexes:
            cursor.execute(index_sql)

        self.conn.commit()

    def _get_file_hash(self, file_path: str) -> str:
        """Generate a hash for a file path."""
        return hashlib.md5(file_path.encode()).hexdigest()

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
    ) -> int:
        """Add an email and its attachments to the database."""
        cursor = self.conn.cursor()

        file_hash = self._get_file_hash(email_data["file_path"])

        # Get file size
        file_size = None
        try:
            file_path = Path(email_data["file_path"])
            if file_path.exists():
                file_size = file_path.stat().st_size
        except Exception:
            pass

        # Insert email
        cursor.execute(
            """
            INSERT OR REPLACE INTO emails (
                message_id, file_path, file_hash, subject, from_addr,
                to_addrs, cc_addrs, bcc_addrs, date, body_preview,
                has_attachments, attachment_count, file_size, file_mtime, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                email_data.get("body", "")[:500] if email_data.get("body") else "",
                len(attachments) > 0,
                len(attachments),
                file_size,
                file_mtime,
                datetime.now(),
            ),
        )

        email_id = cursor.lastrowid

        # Delete existing attachments for this email
        cursor.execute("DELETE FROM attachments WHERE email_id = ?", (email_id,))

        # Insert attachments
        for attachment in attachments:
            # Extract file extension
            filename = attachment.get("filename", "")
            file_extension = ""
            if filename:
                try:
                    file_extension = Path(filename).suffix.lower().lstrip(".")
                except Exception:
                    pass

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

        self.conn.commit()
        return email_id

    def get_attachments(self, email_id: int) -> List[Dict]:
        """Get all attachments for an email."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM attachments WHERE email_id = ?", (email_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

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


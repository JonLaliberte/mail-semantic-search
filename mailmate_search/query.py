"""Query builder for filtering emails by metadata."""

from datetime import datetime
from typing import Dict, List, Optional

from mailmate_search.database import Database


class QueryBuilder:
    """Build SQL queries for filtering emails."""

    def __init__(self, database: Database):
        """Initialize query builder with database connection."""
        self.db = database

    def build_query(
        self,
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        subject: Optional[str] = None,
        subject_like: Optional[str] = None,
        date_after: Optional[datetime] = None,
        date_before: Optional[datetime] = None,
        has_attachments: Optional[bool] = None,
        attachment_type: Optional[str] = None,
        attachment_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """Build and execute a query with filters."""
        cursor = self.db.conn.cursor()

        # Start building query
        query = "SELECT DISTINCT e.* FROM emails e"
        conditions = []
        params = []

        # Join with attachments if needed
        needs_attachment_join = (
            attachment_type is not None or attachment_name is not None
        )
        if needs_attachment_join:
            query += " INNER JOIN attachments a ON e.id = a.email_id"

        # Build conditions
        if from_addr:
            conditions.append("e.from_addr LIKE ?")
            params.append(f"%{from_addr}%")

        if to_addr:
            conditions.append(
                "(e.to_addrs LIKE ? OR e.cc_addrs LIKE ? OR e.bcc_addrs LIKE ?)"
            )
            params.extend([f"%{to_addr}%", f"%{to_addr}%", f"%{to_addr}%"])

        if subject:
            conditions.append("e.subject = ?")
            params.append(subject)

        if subject_like:
            conditions.append("e.subject LIKE ?")
            params.append(f"%{subject_like}%")

        if date_after:
            conditions.append("e.date >= ?")
            params.append(date_after)

        if date_before:
            conditions.append("e.date <= ?")
            params.append(date_before)

        if has_attachments is not None:
            conditions.append("e.has_attachments = ?")
            params.append(1 if has_attachments else 0)

        if attachment_type:
            conditions.append("a.file_extension = ?")
            params.append(attachment_type.lower().lstrip("."))

        if attachment_name:
            conditions.append("a.filename LIKE ?")
            params.append(f"%{attachment_name}%")

        # Combine conditions
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        # Order by date descending
        query += " ORDER BY e.date DESC"

        # Add limit
        if limit:
            query += " LIMIT ?"
            params.append(limit)

        # Execute query
        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Convert to dicts and get attachments
        results = []
        for row in rows:
            email_dict = dict(row)
            email_id = email_dict["id"]
            attachments = self.db.get_attachments(email_id)
            email_dict["attachments"] = attachments
            results.append(email_dict)

        return results

    def get_email_by_id(self, email_id: int) -> Optional[Dict]:
        """Get a single email by ID."""
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
        row = cursor.fetchone()
        if row:
            email_dict = dict(row)
            email_dict["attachments"] = self.db.get_attachments(email_id)
            return email_dict
        return None

    def get_emails_by_file_hashes(self, file_hashes: List[str]) -> List[Dict]:
        """Get emails by their file hashes (for linking with ChromaDB results)."""
        if not file_hashes:
            return []

        cursor = self.db.conn.cursor()
        placeholders = ",".join("?" * len(file_hashes))
        cursor.execute(
            f"SELECT * FROM emails WHERE file_hash IN ({placeholders})", file_hashes
        )
        rows = cursor.fetchall()

        results = []
        for row in rows:
            email_dict = dict(row)
            email_id = email_dict["id"]
            email_dict["attachments"] = self.db.get_attachments(email_id)
            results.append(email_dict)

        return results


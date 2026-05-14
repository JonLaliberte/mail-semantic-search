from pathlib import Path

import pytest

from mail_semantic_search.database import Database


def _make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _add_email(db: Database, file_path: str, message_id: str, subject: str = "test") -> int:
    return db.add_email(
        {
            "file_path": file_path,
            "message_id": message_id,
            "subject": subject,
            "from": "a@example.com",
            "to": "b@example.com",
            "cc": "",
            "bcc": "",
            "date": "2024-01-01",
            "body": "body text",
        },
        attachments=[],
        file_mtime=1000.0,
    )


def test_get_email_by_message_id_returns_none_for_missing(tmp_path):
    db = _make_db(tmp_path)
    assert db.get_email_by_message_id("<notexist@x>") is None
    db.close()


def test_get_email_by_message_id_returns_row(tmp_path):
    db = _make_db(tmp_path)
    _add_email(db, "/emails/a.eml", "<abc@x>")
    row = db.get_email_by_message_id("<abc@x>")
    assert row is not None
    assert row["file_path"] == "/emails/a.eml"
    db.close()


def test_get_email_by_message_id_none_message_id(tmp_path):
    db = _make_db(tmp_path)
    _add_email(db, "/emails/b.eml", "")
    assert db.get_email_by_message_id("") is None   # must short-circuit, not scan
    assert db.get_email_by_message_id(None) is None  # type: ignore[arg-type]
    db.close()


def test_delete_email_by_file_path_removes_row(tmp_path):
    db = _make_db(tmp_path)
    _add_email(db, "/emails/c.eml", "<del@x>")
    assert db.email_exists("/emails/c.eml")
    db.delete_email_by_file_path("/emails/c.eml")
    assert not db.email_exists("/emails/c.eml")
    db.close()


def test_delete_email_by_file_path_noop_for_missing(tmp_path):
    db = _make_db(tmp_path)
    # Should not raise
    db.delete_email_by_file_path("/emails/nonexistent.eml")
    db.close()


def test_message_id_index_exists(tmp_path):
    db = _make_db(tmp_path)
    cursor = db.conn.cursor()
    cursor.execute("PRAGMA index_list(emails)")
    index_names = [row[1] for row in cursor.fetchall()]
    assert any("message_id" in name for name in index_names), (
        f"Expected a message_id index, found: {index_names}"
    )
    db.close()

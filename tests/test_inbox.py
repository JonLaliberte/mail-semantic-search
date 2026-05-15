from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mail_semantic_search.database import Database


INBOX_PREFIX = (
    "/Volumes/External Storage SSD/MailMate/Messages/IMAP/"
    "brainstormenterprises%40gmail.com@imap.gmail.com/INBOX.mailbox/Messages"
)
ARCHIVE_PREFIX = (
    "/Volumes/External Storage SSD/MailMate/Messages/IMAP/"
    "brainstormenterprises%40gmail.com@imap.gmail.com/"
    "[Gmail].mailbox/All Mail.mailbox"
)
OTHER_ACCOUNT_INBOX = (
    "/Volumes/External Storage SSD/MailMate/Messages/IMAP/"
    "other%40example.com@imap.example.com/INBOX.mailbox/Messages"
)


def _make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _add(
    db: Database,
    file_path: str,
    *,
    message_id: str = "",
    subject: str = "subject",
    date: datetime | None = None,
    body: str = "body",
    has_attachments: bool = False,
) -> int:
    attachments = [{"filename": "x.pdf"}] if has_attachments else []
    return db.add_email(
        {
            "file_path": file_path,
            "message_id": message_id,
            "subject": subject,
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "bcc": "",
            "date": date,
            "body": body,
        },
        attachments=attachments,
        file_mtime=1000.0,
    )


def test_list_inbox_emails_empty_db(tmp_path):
    db = _make_db(tmp_path)
    result = db.list_inbox_emails()
    assert result == []
    db.close()


def test_list_inbox_emails_returns_only_inbox(tmp_path):
    db = _make_db(tmp_path)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", subject="in inbox")
    _add(db, f"{ARCHIVE_PREFIX}/2.eml", message_id="<b@x>", subject="archived")
    rows = db.list_inbox_emails()
    subjects = [r["subject"] for r in rows]
    assert subjects == ["in inbox"]
    db.close()


def test_list_inbox_emails_filter_by_account_accepts_bare_email(tmp_path):
    db = _make_db(tmp_path)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", subject="mine")
    _add(db, f"{OTHER_ACCOUNT_INBOX}/2.eml", message_id="<b@x>", subject="other")
    rows = db.list_inbox_emails(account="brainstormenterprises@gmail.com")
    assert [r["subject"] for r in rows] == ["mine"]
    db.close()


def test_list_inbox_emails_no_account_returns_all_accounts(tmp_path):
    db = _make_db(tmp_path)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", subject="mine")
    _add(db, f"{OTHER_ACCOUNT_INBOX}/2.eml", message_id="<b@x>", subject="other")
    rows = db.list_inbox_emails()
    assert {r["subject"] for r in rows} == {"mine", "other"}
    db.close()


def test_list_inbox_emails_sorted_desc_and_capped_at_limit(tmp_path):
    db = _make_db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        _add(
            db,
            f"{INBOX_PREFIX}/{i}.eml",
            message_id=f"<{i}@x>",
            subject=f"row {i}",
            date=base + timedelta(days=i),
        )
    rows = db.list_inbox_emails(limit=3)
    assert [r["subject"] for r in rows] == ["row 4", "row 3", "row 2"]
    db.close()


def test_list_inbox_emails_limit_clamped(tmp_path):
    db = _make_db(tmp_path)
    base = datetime(2026, 1, 1)
    for i in range(3):
        _add(
            db,
            f"{INBOX_PREFIX}/{i}.eml",
            message_id=f"<{i}@x>",
            subject=f"row {i}",
            date=base + timedelta(days=i),
        )
    # Negative / zero → at least 1 row
    assert len(db.list_inbox_emails(limit=0)) == 1
    assert len(db.list_inbox_emails(limit=-5)) == 1
    # Over 500 → capped at 500 (we only have 3, but the call should not error)
    assert len(db.list_inbox_emails(limit=10_000)) == 3
    db.close()


def test_list_inbox_emails_date_before_is_strict(tmp_path):
    db = _make_db(tmp_path)
    boundary = datetime(2026, 1, 5, 12, 0, 0)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", subject="older",
         date=datetime(2026, 1, 4))
    _add(db, f"{INBOX_PREFIX}/2.eml", message_id="<b@x>", subject="exact",
         date=boundary)
    _add(db, f"{INBOX_PREFIX}/3.eml", message_id="<c@x>", subject="newer",
         date=datetime(2026, 1, 6))
    rows = db.list_inbox_emails(date_before=boundary)
    assert [r["subject"] for r in rows] == ["older"]
    db.close()


def test_list_inbox_emails_date_after_is_strict(tmp_path):
    db = _make_db(tmp_path)
    boundary = datetime(2026, 1, 5, 12, 0, 0)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", subject="older",
         date=datetime(2026, 1, 4))
    _add(db, f"{INBOX_PREFIX}/2.eml", message_id="<b@x>", subject="exact",
         date=boundary)
    _add(db, f"{INBOX_PREFIX}/3.eml", message_id="<c@x>", subject="newer",
         date=datetime(2026, 1, 6))
    rows = db.list_inbox_emails(date_after=boundary)
    assert [r["subject"] for r in rows] == ["newer"]
    db.close()


def test_list_inbox_emails_paging_roundtrip(tmp_path):
    db = _make_db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        _add(
            db,
            f"{INBOX_PREFIX}/{i}.eml",
            message_id=f"<{i}@x>",
            subject=f"row {i}",
            date=base + timedelta(days=i),
        )
    page1 = db.list_inbox_emails(limit=2)
    assert [r["subject"] for r in page1] == ["row 4", "row 3"]
    cursor = page1[-1]["date"]
    page2 = db.list_inbox_emails(limit=2, date_before=cursor)
    assert [r["subject"] for r in page2] == ["row 2", "row 1"]
    # No overlap with page1
    page1_ids = {r["id"] for r in page1}
    page2_ids = {r["id"] for r in page2}
    assert page1_ids.isdisjoint(page2_ids)
    db.close()


def test_list_inbox_emails_row_shape(tmp_path):
    db = _make_db(tmp_path)
    long_body = "x" * 5000
    _add(
        db,
        f"{INBOX_PREFIX}/1.eml",
        message_id="<a@x>",
        subject="hello",
        date=datetime(2026, 1, 1),
        body=long_body,
        has_attachments=True,
    )
    [row] = db.list_inbox_emails()
    assert row["message_id"] == "<a@x>"
    assert row["from"] == "sender@example.com"
    assert row["to"] == "recipient@example.com"
    assert row["subject"] == "hello"
    assert row["has_attachments"] is True  # not int
    assert isinstance(row["id"], int)
    assert len(row["body_snippet"]) == 200
    assert row["body_snippet"] == "x" * 200
    db.close()


def test_list_inbox_emails_null_body_preview(tmp_path):
    db = _make_db(tmp_path)
    _add(db, f"{INBOX_PREFIX}/1.eml", message_id="<a@x>", body="")
    [row] = db.list_inbox_emails()
    assert row["body_snippet"] == ""
    db.close()


# --- MCP tool integration ---


@pytest.fixture
def temp_db_path(tmp_path, monkeypatch):
    from mail_semantic_search.config import config as app_config

    db_file = tmp_path / "mcp.db"
    monkeypatch.setattr(app_config, "database_path", db_file)
    # Pre-populate with one inbox row.
    db = Database(db_file)
    _add(
        db,
        f"{INBOX_PREFIX}/1.eml",
        message_id="<a@x>",
        subject="hello",
        date=datetime(2026, 1, 1, 12, 0, 0),
        body="x" * 500,
    )
    db.close()
    return db_file


def test_mcp_list_inbox_emails_returns_payload(temp_db_path):
    from mail_semantic_search import mcp_server

    payload = mcp_server.list_inbox_emails()

    assert payload["filters"] == {
        "account": None,
        "limit": 50,
        "date_after": None,
        "date_before": None,
    }
    assert len(payload["results"]) == 1
    row = payload["results"][0]
    assert row["subject"] == "hello"
    assert len(row["body_snippet"]) == 200


def test_mcp_list_inbox_emails_parses_iso_dates(temp_db_path):
    from mail_semantic_search import mcp_server

    # Boundary excludes the lone row (strict <).
    payload = mcp_server.list_inbox_emails(date_before="2026-01-01T12:00:00")
    assert payload["results"] == []
    assert payload["filters"]["date_before"] == "2026-01-01T12:00:00"

    # After boundary excludes the lone row too (strict >).
    payload = mcp_server.list_inbox_emails(date_after="2026-01-01T12:00:00")
    assert payload["results"] == []


def test_mcp_list_inbox_emails_invalid_date_raises(temp_db_path):
    from mail_semantic_search import mcp_server

    with pytest.raises(ValueError):
        mcp_server.list_inbox_emails(date_before="not-a-date")

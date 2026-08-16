"""query_emails must never materialize the whole table.

An unbounded `query_emails()` used to run `SELECT DISTINCT e.* FROM emails
ORDER BY date DESC` with no LIMIT, fetchall() the lot, and copy it several
times on the way to JSON. On a 700k-row index that peaked at ~60 GB of RSS
and wedged the MCP server, so every query path is now capped.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mail_semantic_search.database import Database
from mail_semantic_search.query import QueryBuilder
from mail_semantic_search.search import query_email_records
from mail_semantic_search.service_models import QueryRequest


PREFIX = "/Volumes/External Storage SSD/MailMate/Messages/IMAP/acct/Archive.mailbox"


def _add(db: Database, index: int, *, subject: str | None = None) -> int:
    return db.add_email(
        {
            "file_path": f"{PREFIX}/{index}.eml",
            "message_id": f"<{index}@x>",
            "subject": subject if subject is not None else f"row {index}",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "bcc": "",
            "date": datetime(2026, 1, 1) + timedelta(days=index),
            "body": "body",
        },
        attachments=[],
        file_mtime=1000.0,
    )


@pytest.fixture
def db_with_rows(tmp_path: Path, monkeypatch):
    """12 indexed emails behind a cap of 5, so the cap is observable."""
    from mail_semantic_search.config import config as app_config

    db_file = tmp_path / "query.db"
    monkeypatch.setattr(app_config, "database_path", db_file)
    monkeypatch.setattr(app_config, "max_filtered_search_limit", 5)

    db = Database(db_file)
    for i in range(12):
        _add(db, i)
    db.close()
    return db_file


def test_query_with_no_limit_is_capped(db_with_rows):
    response = query_email_records(QueryRequest())

    assert len(response.results) == 5


def test_query_with_no_limit_reports_truncation(db_with_rows):
    response = query_email_records(QueryRequest())

    assert response.truncated is True
    assert response.limit == 5


def test_query_clamps_limit_above_cap(db_with_rows):
    response = query_email_records(QueryRequest(limit=10_000))

    assert len(response.results) == 5
    assert response.limit == 5
    assert response.truncated is True


def test_query_honors_limit_below_cap(db_with_rows):
    response = query_email_records(QueryRequest(limit=2))

    assert len(response.results) == 2
    assert response.limit == 2
    assert response.truncated is True


def test_query_limit_zero_returns_one_row_not_everything(db_with_rows):
    """`if limit:` treated 0 as "unbounded" — it must clamp up to 1 instead."""
    response = query_email_records(QueryRequest(limit=0))

    assert len(response.results) == 1


def test_query_not_truncated_when_all_matches_fit(db_with_rows):
    # Matches "row 1", "row 10", "row 11" — 3 rows, under the cap of 5.
    response = query_email_records(QueryRequest(subject_like="row 1"))

    assert len(response.results) == 3
    assert response.truncated is False


def test_build_query_without_limit_is_capped(db_with_rows):
    db = Database(db_with_rows)
    rows = QueryBuilder(db).build_query(limit=None)
    db.close()

    assert len(rows) == 5


def test_mcp_query_emails_payload_is_capped(db_with_rows):
    from mail_semantic_search import mcp_server

    payload = mcp_server.query_emails()

    assert len(payload["results"]) == 5
    assert payload["truncated"] is True
    assert payload["limit"] == 5

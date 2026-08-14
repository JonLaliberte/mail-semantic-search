"""Tests for MailMate deep-link fields on email results."""

from datetime import datetime
from pathlib import Path

import pytest

from mail_semantic_search.database import Database
from mail_semantic_search.links import build_link_fields, normalize_message_id


# --- message_url encoding ---


@pytest.mark.parametrize(
    "message_id,expected",
    [
        ("<abc123@example.com>", "message:%3Cabc123@example.com%3E"),
        ("abc123@example.com", "message:%3Cabc123@example.com%3E"),
        (
            "<D22041DC.25F53%person@company.com>",
            "message:%3CD22041DC.25F53%25person@company.com%3E",
        ),
        ("<a(b)c@example.com>", "message:%3Ca%28b%29c@example.com%3E"),
        (
            "<drdrang/notes/pull/1@github.com>",
            "message:%3Cdrdrang/notes/pull/1@github.com%3E",
        ),
        ("  <abc@example.com>  ", "message:%3Cabc@example.com%3E"),
        ("", None),
        (None, None),
    ],
)
def test_message_url_encoding(message_id, expected):
    assert build_link_fields(message_id, "subject")["message_url"] == expected


def test_literal_percent_is_encoded_exactly_once():
    # A literal `%25` in the header means percent-two-five, so it must survive
    # as `%2525` — a second quote() pass would produce `%252525`.
    fields = build_link_fields("<a%25b@example.com>", "s")
    assert fields["message_url"] == "message:%3Ca%2525b@example.com%3E"


def test_mid_url_is_unencoded():
    fields = build_link_fields("<D22041DC.25F53%person@company.com>", "s")
    assert fields["mid_url"] == "mid:D22041DC.25F53%person@company.com"


def test_missing_message_id_nulls_all_three_fields():
    assert build_link_fields(None, "subject") == {
        "message_url": None,
        "mid_url": None,
        "markdown_link": None,
    }
    assert build_link_fields("   ", "subject")["markdown_link"] is None
    assert build_link_fields("<>", "subject")["mid_url"] is None


def test_normalize_message_id_strips_brackets_and_whitespace():
    assert normalize_message_id("  <a@b>  ") == "a@b"
    assert normalize_message_id("a@b") == "a@b"
    assert normalize_message_id("") is None


# --- markdown_link ---


def test_markdown_label_strips_brackets():
    fields = build_link_fields("<a@b.com>", "[Alert] Server down")
    assert fields["markdown_link"] == "[Alert Server down](mid:a@b.com)"


def test_markdown_label_collapses_folded_header_whitespace():
    fields = build_link_fields("<a@b.com>", "Long\n\tsubject   line")
    assert fields["markdown_link"] == "[Long subject line](mid:a@b.com)"


def test_markdown_label_defaults_when_subject_missing():
    assert build_link_fields("<a@b.com>", None)["markdown_link"] == (
        "[(no subject)](mid:a@b.com)"
    )
    assert build_link_fields("<a@b.com>", "[]")["markdown_link"] == (
        "[(no subject)](mid:a@b.com)"
    )


def test_markdown_link_uses_message_scheme_when_configured():
    fields = build_link_fields("<a@b.com>", "Hi", scheme="message")
    assert fields["markdown_link"] == "[Hi](message:%3Ca@b.com%3E)"
    # The raw fields are unaffected by the scheme choice.
    assert fields["mid_url"] == "mid:a@b.com"


def test_markdown_link_falls_back_to_message_url_for_parens():
    # A bare `mid:` target containing parens would close the link early, so
    # the percent-encoded `message:` form is used instead.
    fields = build_link_fields("<a(b)c@example.com>", "Hi", scheme="mid")
    assert fields["markdown_link"] == "[Hi](message:%3Ca%28b%29c@example.com%3E)"


def test_link_scheme_env_var_drives_default(monkeypatch):
    from mail_semantic_search.config import config as app_config

    monkeypatch.setattr(app_config, "link_scheme", "message")
    assert build_link_fields("<a@b.com>", "Hi")["markdown_link"] == (
        "[Hi](message:%3Ca@b.com%3E)"
    )


# --- MAILMATE_LINK_SCHEME parsing ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "mid"),
        ("", "mid"),
        ("mid", "mid"),
        ("message", "message"),
        ("  MESSAGE  ", "message"),
        ("nonsense", "mid"),
    ],
)
def test_parse_link_scheme(raw, expected):
    from mail_semantic_search.config import Config

    assert Config._parse_link_scheme(raw) == expected


def test_unknown_link_scheme_warns_instead_of_raising(caplog):
    from mail_semantic_search.config import Config

    with caplog.at_level("WARNING", logger="mail_semantic_search.config"):
        assert Config._parse_link_scheme("mailto") == "mid"
    assert "MAILMATE_LINK_SCHEME" in caplog.text


# --- Integration: every list-returning tool emits the fields ---


LINK_KEYS = {"message_url", "mid_url", "markdown_link"}

INBOX_PREFIX = (
    "/Volumes/External Storage SSD/MailMate/Messages/IMAP/"
    "someone%40example.com@imap.example.com/INBOX.mailbox/Messages"
)


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch):
    """Point config at a temp DB holding two inbox rows."""
    from mail_semantic_search.config import config as app_config

    db_file = tmp_path / "links.db"
    monkeypatch.setattr(app_config, "database_path", db_file)

    db = Database(db_file)
    for i, message_id in enumerate(["<a@x.com>", "<b(paren)@x.com>"]):
        db.add_email(
            {
                "file_path": f"{INBOX_PREFIX}/{i}.eml",
                "message_id": message_id,
                "subject": f"row {i}",
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "cc": "",
                "bcc": "",
                "date": datetime(2026, 1, 1 + i, 12, 0, 0),
                "body": "body text",
            },
            attachments=[],
            file_mtime=1000.0,
        )
    db.close()
    return db_file


def test_list_inbox_emails_results_carry_link_fields(seeded_db):
    from mail_semantic_search import mcp_server

    results = mcp_server.list_inbox_emails()["results"]
    assert len(results) == 2
    for row in results:
        assert LINK_KEYS <= row.keys()
        assert row["message_url"].startswith("message:")
        assert row["mid_url"].startswith("mid:")
        assert row["markdown_link"].startswith("[")
    # message_id itself is untouched.
    assert {r["message_id"] for r in results} == {"<a@x.com>", "<b(paren)@x.com>"}
    # Inbox rows keep their original key names.
    assert results[0]["from"] == "sender@example.com"


def test_query_emails_results_carry_link_fields(seeded_db):
    from mail_semantic_search import mcp_server

    results = mcp_server.query_emails(from_addr="sender@example.com")["results"]
    assert len(results) == 2
    for row in results:
        assert LINK_KEYS <= row.keys()
        assert row["mid_url"].startswith("mid:")


def test_search_emails_results_carry_link_fields(monkeypatch, seeded_db):
    """Stub the vector store + embedder so search runs without an index."""
    from mail_semantic_search import search as search_module
    from mail_semantic_search.database import get_file_hash

    file_path = f"{INBOX_PREFIX}/0.eml"

    class _FakeVectorStore:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_stats(self):
            return {"total_emails": 1}

        def search(self, embedding, n_results):
            return [
                {
                    "file_path": file_path,
                    "file_hash": get_file_hash(file_path),
                    "distance": 0.1,
                    "document": "body text",
                }
            ]

    class _FakeEmbeddingService:
        def embed_query(self, query):
            return [0.0]

    monkeypatch.setattr(search_module, "VectorStore", _FakeVectorStore)
    monkeypatch.setattr(search_module, "EmbeddingService", _FakeEmbeddingService)

    from mail_semantic_search import mcp_server

    results = mcp_server.search_emails(query="anything", rerank=False)["results"]
    assert len(results) == 1
    row = results[0]
    assert LINK_KEYS <= row.keys()
    assert row["message_id"] == "<a@x.com>"
    assert row["message_url"] == "message:%3Ca@x.com%3E"
    assert row["mid_url"] == "mid:a@x.com"
    assert row["markdown_link"] == "[row 0](mid:a@x.com)"


def test_results_without_message_id_get_null_link_fields(monkeypatch, seeded_db):
    from mail_semantic_search import search as search_module

    class _FakeVectorStore:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_stats(self):
            return {"total_emails": 1}

        def search(self, embedding, n_results):
            return [{"file_path": "/nowhere/unindexed.eml", "distance": 0.2}]

    class _FakeEmbeddingService:
        def embed_query(self, query):
            return [0.0]

    monkeypatch.setattr(search_module, "VectorStore", _FakeVectorStore)
    monkeypatch.setattr(search_module, "EmbeddingService", _FakeEmbeddingService)

    from mail_semantic_search import mcp_server

    [row] = mcp_server.search_emails(query="anything", rerank=False)["results"]
    assert row["message_url"] is None
    assert row["mid_url"] is None
    assert row["markdown_link"] is None

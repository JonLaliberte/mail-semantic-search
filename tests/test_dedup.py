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


def test_vector_store_delete_email_removes_entry(tmp_path, monkeypatch):
    """delete_email() should remove the Chroma document for the given file path."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")

    vs = VectorStore()
    email = {
        "file_path": "/emails/vec.eml",
        "subject": "hello",
        "from": "a@x.com",
        "to": "b@x.com",
        "date": "2024-01-01",
        "message_id": "<vec@x>",
        "attachments": [],
    }
    fake_embedding = [0.1] * 768  # BGE-base dimension
    vs.add_emails([email], [fake_embedding])
    assert vs.is_indexed("/emails/vec.eml")

    vs.delete_email("/emails/vec.eml")
    assert not vs.is_indexed("/emails/vec.eml")

    # Verify idempotent: deleting non-existent path must not raise
    vs.delete_email("/emails/never_indexed.eml")


def test_dedup_by_message_id_removes_older_duplicate(tmp_path, monkeypatch):
    """dedup keeps the most-recently indexed row for each message_id."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")

    db = Database(tmp_path / "dedup.db")
    vs = VectorStore()

    # Add two rows with same message_id — simulate a moved email
    _add_email(db, "/emails/old.eml", "<dup@x>", subject="old")
    # Force indexed_at to be older
    db.conn.execute(
        "UPDATE emails SET indexed_at = '2024-01-01 00:00:00' WHERE file_path = ?",
        ("/emails/old.eml",),
    )
    db.conn.commit()

    _add_email(db, "/emails/new.eml", "<dup@x>", subject="new")

    # Add both to Chroma
    fake_emb = [0.1] * 768
    vs.add_emails(
        [{"file_path": "/emails/old.eml", "subject": "old", "from": "a@x.com",
          "to": "b@x.com", "date": "2024-01-01", "message_id": "<dup@x>", "attachments": []}],
        [fake_emb],
    )
    vs.add_emails(
        [{"file_path": "/emails/new.eml", "subject": "new", "from": "a@x.com",
          "to": "b@x.com", "date": "2024-01-01", "message_id": "<dup@x>", "attachments": []}],
        [fake_emb],
    )

    removed, kept = db.dedup_by_message_id(vs)

    assert removed == 1
    assert kept == 1
    assert not db.email_exists("/emails/old.eml")
    assert db.email_exists("/emails/new.eml")
    assert not vs.is_indexed("/emails/old.eml")
    assert vs.is_indexed("/emails/new.eml")

    db.close()


def test_dedup_by_message_id_skips_empty_message_id(tmp_path, monkeypatch):
    """Rows with NULL/empty message_id must not be deduped against each other."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma2")

    db = Database(tmp_path / "dedup2.db")
    vs = VectorStore()

    _add_email(db, "/emails/noid1.eml", "", subject="no id 1")
    _add_email(db, "/emails/noid2.eml", "", subject="no id 2")

    removed, kept = db.dedup_by_message_id(vs)

    assert removed == 0
    assert db.email_exists("/emails/noid1.eml")
    assert db.email_exists("/emails/noid2.eml")

    db.close()


def test_dedup_by_message_id_noop_when_no_duplicates(tmp_path, monkeypatch):
    """dedup returns (0, 0) when there are no duplicate message_ids."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma3")

    db = Database(tmp_path / "dedup3.db")
    vs = VectorStore()

    _add_email(db, "/emails/unique1.eml", "<unique1@x>", subject="unique 1")
    _add_email(db, "/emails/unique2.eml", "<unique2@x>", subject="unique 2")

    removed, kept = db.dedup_by_message_id(vs)

    assert removed == 0
    assert kept == 0
    assert db.email_exists("/emails/unique1.eml")
    assert db.email_exists("/emails/unique2.eml")

    db.close()


def test_dedup_results_by_message_id():
    from mail_semantic_search.search import _dedup_results_by_message_id

    results = [
        {"message_id": "<a@x>", "distance": 0.1, "file_path": "/a1.eml"},
        {"message_id": "<a@x>", "distance": 0.3, "file_path": "/a2.eml"},  # worse
        {"message_id": "<b@x>", "distance": 0.2, "file_path": "/b.eml"},
        {"message_id": "",      "distance": 0.05, "file_path": "/c.eml"},  # no id
        {"message_id": "",      "distance": 0.07, "file_path": "/d.eml"},  # no id
    ]

    deduped = _dedup_results_by_message_id(results)
    file_paths = [r["file_path"] for r in deduped]

    assert len(deduped) == 4
    assert "/a1.eml" in file_paths       # best score
    assert "/a2.eml" not in file_paths   # worse dupe removed
    assert "/b.eml" in file_paths
    assert "/c.eml" in file_paths        # no-id rows always kept
    assert "/d.eml" in file_paths


def test_dedup_results_by_message_id_best_arrives_second():
    """Replacement fires when the better-scoring duplicate appears later in the list."""
    from mail_semantic_search.search import _dedup_results_by_message_id

    results = [
        {"message_id": "<z@x>", "distance": 0.5, "file_path": "/z_worse.eml"},
        {"message_id": "<z@x>", "distance": 0.1, "file_path": "/z_better.eml"},  # better, arrives second
    ]

    deduped = _dedup_results_by_message_id(results)
    file_paths = [r["file_path"] for r in deduped]

    assert len(deduped) == 1
    assert "/z_better.eml" in file_paths
    assert "/z_worse.eml" not in file_paths


def test_handle_move_detection_replaces_old_path(tmp_path, monkeypatch):
    """When a new file has a message_id already in DB at a different path,
    the old DB+Chroma entry should be removed and the new path NOT yet in DB."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.database import Database
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "move.db")
    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "move_chroma")

    db = Database()
    vs = VectorStore()

    # Index the "old" file path with a known message_id
    db.add_email(
        {"file_path": "/emails/old.eml", "message_id": "<move@x>", "subject": "moved",
         "from": "a@x.com", "to": "b@x.com", "cc": "", "bcc": "",
         "date": "2024-01-01", "body": "Body"},
        attachments=[],
        file_mtime=1000.0,
    )
    vs.add_emails(
        [{"file_path": "/emails/old.eml", "subject": "moved", "from": "a@x.com",
          "to": "b@x.com", "date": "2024-01-01", "message_id": "<move@x>",
          "attachments": []}],
        [[0.1] * 768],
    )

    # Call move detection for the new path (not yet in DB)
    from mail_semantic_search.index import _handle_move_detection
    moved = _handle_move_detection(
        {"file_path": "/emails/new.eml", "message_id": "<move@x>"},
        db,
        vs,
    )

    assert moved is True
    assert not db.email_exists("/emails/old.eml")   # old path removed from SQLite
    assert not vs.is_indexed("/emails/old.eml")     # old path removed from Chroma
    assert not db.email_exists("/emails/new.eml")   # new path NOT yet indexed (caller's job)

    db.close()

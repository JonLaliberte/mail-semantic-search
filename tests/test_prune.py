"""Tests for pruning index entries whose backing .eml file no longer exists."""

from pathlib import Path

import pytest

from mail_semantic_search.database import Database


def _add_email(db: Database, file_path: str, message_id: str = "<m@x>", subject: str = "test") -> int:
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


def _email_dict(file_path: str, message_id: str = "<m@x>") -> dict:
    return {
        "file_path": file_path,
        "subject": "s",
        "from": "a@x.com",
        "to": "b@x.com",
        "date": "2024-01-01",
        "message_id": message_id,
        "attachments": [],
    }


def test_count_missing_files_with_present_paths(tmp_path):
    """A row whose path is absent from the present set (and from disk) counts as missing."""
    db = Database(tmp_path / "t.db")
    _add_email(db, "/emails/gone.eml", "<gone@x>")
    _add_email(db, "/emails/here.eml", "<here@x>")

    missing, present = db.count_missing_files(present_paths={"/emails/here.eml"})

    assert missing == 1
    assert present == 1
    db.close()


def test_prune_removes_orphan_keeps_present(tmp_path, monkeypatch):
    """prune deletes the row+vector for a vanished file and leaves the live one intact."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")

    db = Database(tmp_path / "p.db")
    vs = VectorStore()

    present_file = tmp_path / "present.eml"
    present_file.write_text("hi")
    present = str(present_file)
    gone = str(tmp_path / "gone.eml")  # never created on disk

    _add_email(db, present, "<present@x>")
    _add_email(db, gone, "<gone@x>")
    emb = [0.1] * 768
    vs.add_emails([_email_dict(present, "<present@x>")], [emb])
    vs.add_emails([_email_dict(gone, "<gone@x>")], [emb])

    removed, kept = db.prune_missing_files(vs)

    assert removed == 1
    assert kept == 1
    assert db.email_exists(present)
    assert not db.email_exists(gone)
    assert vs.is_indexed(present)
    assert not vs.is_indexed(gone)
    db.close()


def test_prune_present_paths_fast_path_trusts_set(tmp_path, monkeypatch):
    """A path in present_paths is kept without a stat — the scan set is authoritative."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma_fast")

    db = Database(tmp_path / "fast.db")
    vs = VectorStore()
    _add_email(db, "/emails/trusted.eml", "<trusted@x>")

    removed, kept = db.prune_missing_files(vs, present_paths={"/emails/trusted.eml"})

    assert removed == 0
    assert kept == 1
    assert db.email_exists("/emails/trusted.eml")
    db.close()


def test_prune_path_outside_scan_root_confirmed_by_stat(tmp_path, monkeypatch):
    """A path absent from present_paths but present on disk is kept (stat fallback).

    Guards against deleting rows for emails stored outside the scanned root.
    """
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma_out")

    db = Database(tmp_path / "out.db")
    vs = VectorStore()
    real = tmp_path / "outside.eml"
    real.write_text("x")
    _add_email(db, str(real), "<outside@x>")

    # Empty scan set: only the disk stat keeps this row alive.
    removed, kept = db.prune_missing_files(vs, present_paths=set())

    assert removed == 0
    assert kept == 1
    assert db.email_exists(str(real))
    db.close()


def test_prune_idempotent(tmp_path, monkeypatch):
    """Re-running prune removes nothing the second time."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma_idem")

    db = Database(tmp_path / "idem.db")
    vs = VectorStore()
    gone = str(tmp_path / "missing.eml")
    _add_email(db, gone, "<gone@x>")

    r1, _ = db.prune_missing_files(vs)
    r2, _ = db.prune_missing_files(vs)

    assert r1 == 1
    assert r2 == 0
    db.close()


def test_prune_batches_multiple_orphans(tmp_path, monkeypatch):
    """All orphans are removed even when they span multiple commit batches."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma_batch")

    db = Database(tmp_path / "batch.db")
    vs = VectorStore()
    for i in range(5):
        _add_email(db, str(tmp_path / f"gone{i}.eml"), f"<g{i}@x>")

    removed, kept = db.prune_missing_files(vs, batch_size=2)

    assert removed == 5
    assert kept == 0
    db.close()


def test_vector_store_delete_emails_batch(tmp_path, monkeypatch):
    """delete_emails removes every listed path's vector and no-ops on an empty list."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.vector_store import VectorStore

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma_vsb")

    vs = VectorStore()
    emb = [0.1] * 768
    for p in ["/e/a.eml", "/e/b.eml", "/e/c.eml"]:
        vs.add_emails([_email_dict(p)], [emb])

    vs.delete_emails(["/e/a.eml", "/e/b.eml"])

    assert not vs.is_indexed("/e/a.eml")
    assert not vs.is_indexed("/e/b.eml")
    assert vs.is_indexed("/e/c.eml")

    # Empty list must be a no-op, not an error.
    vs.delete_emails([])

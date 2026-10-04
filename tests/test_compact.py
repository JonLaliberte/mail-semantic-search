"""Tests for rebuilding the Chroma collection to shed tombstoned vectors."""

import pytest

from mail_semantic_search import vector_store as vs_mod
from mail_semantic_search.vector_store import VectorStore


def _email(n: int) -> dict:
    return {
        "file_path": f"/emails/{n}.eml",
        "subject": f"subject {n}",
        "from": "a@x.com",
        "to": "b@x.com",
        "date": "2024-01-01",
        "message_id": f"<{n}@x>",
        "attachments": [],
    }


def _embedding(n: int) -> list:
    vec = [0.0] * 768
    vec[n] = 1.0
    return vec


@pytest.fixture
def store(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")
    vs = VectorStore()
    emails = [_email(n) for n in range(10)]
    vs.add_emails(emails, [_embedding(n) for n in range(10)], [f"doc {n}" for n in range(10)])
    vs.delete_emails([f"/emails/{n}.eml" for n in range(5)])
    yield vs
    vs.close()


def _collection_names(vs: VectorStore) -> set:
    return {c.name for c in vs.client.list_collections()}


def test_compact_keeps_only_live_rows_and_their_data(store):
    assert store.compact(batch_size=2) == 5

    assert _collection_names(store) == {"emails"}
    assert store.get_stats()["total_emails"] == 5
    assert not store.is_indexed("/emails/0.eml")
    doc = store.get_email_document("/emails/7.eml")
    assert doc["document"] == "doc 7"
    assert doc["metadata"]["message_id"] == "<7@x>"

    hit = store.search(_embedding(7), n_results=1)[0]
    assert hit["file_path"] == "/emails/7.eml"
    # Cosine space must survive the rebuild: an identical vector is distance 0.
    assert hit["distance"] == pytest.approx(0.0, abs=1e-5)
    assert store.collection.metadata == {"hnsw:space": "cosine"}


def test_compacted_store_is_visible_to_a_fresh_handle(store):
    store.compact()

    with VectorStore() as reopened:
        assert reopened.get_stats()["total_emails"] == 5
        assert reopened.is_indexed("/emails/9.eml")


def test_compact_reports_progress(store):
    seen = []
    store.compact(batch_size=2, progress=lambda done, total: seen.append((done, total)))

    assert seen[-1] == (5, 5)


def test_compact_resumes_an_interrupted_copy(store):
    partial = store.client.get_or_create_collection(
        name=vs_mod._COMPACTING_NAME, metadata=store.collection.metadata
    )
    rows = store.collection.get(
        ids=[store._get_file_hash("/emails/5.eml")],
        include=["embeddings", "metadatas", "documents"],
    )
    partial.upsert(
        ids=rows["ids"],
        embeddings=rows["embeddings"],
        metadatas=rows["metadatas"],
        documents=rows["documents"],
    )

    assert store.compact(batch_size=2) == 5
    assert _collection_names(store) == {"emails"}


def test_compact_finishes_an_interrupted_swap(store, monkeypatch):
    """A crash between the two renames leaves no 'emails'; the next open
    recreates it empty. Re-running must promote the rebuilt copy, not that."""
    monkeypatch.setattr(
        VectorStore, "_finish_swap", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        store.compact()
    monkeypatch.undo()

    import mail_semantic_search.config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "chromadb_path", store.chromadb_path)
    with VectorStore() as reopened:
        assert reopened.get_stats()["total_emails"] == 0
        assert reopened.compact() == 5
        assert _collection_names(reopened) == {"emails"}
        assert reopened.is_indexed("/emails/9.eml")


def test_compact_refuses_to_swap_on_count_mismatch(store, monkeypatch):
    real_count = type(store.collection).count
    monkeypatch.setattr(
        type(store.collection),
        "count",
        lambda self: real_count(self) + (1 if self.name == "emails" else 0),
    )

    with pytest.raises(RuntimeError, match="leaving the original in place"):
        store.compact()
    monkeypatch.undo()

    assert store.get_stats()["total_emails"] == 5


def test_close_releases_the_shared_chroma_system(tmp_path, monkeypatch):
    """The HNSW index lives in Chroma's per-path system; it is only freed
    when the last handle closes."""
    import mail_semantic_search.config as cfg_mod
    from chromadb.api.shared_system_client import SharedSystemClient

    path = tmp_path / "chroma_close"
    monkeypatch.setattr(cfg_mod.config, "chromadb_path", path)
    key = str(path.absolute())

    first, second = VectorStore(), VectorStore()
    first.close()
    assert key in SharedSystemClient._identifier_to_system
    second.close()
    assert key not in SharedSystemClient._identifier_to_system


def test_compact_removes_the_retired_index_from_disk(tmp_path, monkeypatch):
    """Chroma drops a collection's rows but leaves its HNSW directory behind."""
    import mail_semantic_search.config as cfg_mod

    root = tmp_path / "chroma_disk"
    monkeypatch.setattr(cfg_mod.config, "chromadb_path", root)
    vs = VectorStore()
    # Past Chroma's sync threshold, so the index is actually written to disk.
    count = 1100
    for start in range(0, count, 500):
        batch = range(start, min(start + 500, count))
        vs.add_emails(
            [_email(n) for n in batch],
            [[float(n), 1.0] + [0.0] * 766 for n in batch],
        )
    vs.search([1.0] * 768, n_results=1)
    before = {p.parent.name for p in root.glob("*/header.bin")}
    assert len(before) == 1

    vs.compact()
    vs.search([1.0] * 768, n_results=1)
    vs.close()

    after = {p.parent.name for p in root.glob("*/header.bin")}
    assert after.isdisjoint(before)
    assert len(after) <= 1

    vs_mod.vacuum_chroma_sqlite(root)
    with VectorStore() as reopened:
        assert reopened.get_stats()["total_emails"] == count


def test_compact_vectors_command(store, tmp_path, monkeypatch):
    from click.testing import CliRunner

    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.cli import main

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "cli.db")
    store.close()

    dry = CliRunner().invoke(main, ["compact-vectors", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "5 live vectors" in dry.output and "Dry run" in dry.output

    result = CliRunner().invoke(main, ["compact-vectors"])
    assert result.exit_code == 0, result.output
    assert "Done. 5 vectors" in result.output

    with VectorStore() as reopened:
        assert reopened.get_stats()["total_emails"] == 5
        assert {c.name for c in reopened.client.list_collections()} == {"emails"}

"""A long-lived store must pick up writes made by another process.

Chroma loads its HNSW index once per client and never reloads it, so the MCP
server kept serving deleted vectors (metadata None, crashing search) and never
saw newly indexed mail until it went idle.
"""

import subprocess
import sys
import textwrap

import pytest

import mail_semantic_search.config as cfg_mod
from mail_semantic_search import resources
from mail_semantic_search.vector_store import VectorStore, index_generation

DIM = 8


def _vec(i):
    return [float(i == j) + 0.01 * i for j in range(DIM)]


def _in_other_process(chromadb_path, body):
    script = textwrap.dedent(
        f"""
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(path={str(chromadb_path)!r}, settings=Settings(anonymized_telemetry=False))
        col = client.get_collection("emails")
        """
    ) + textwrap.dedent(body)
    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")
    for name in ("_keepalive_store", "_keepalive_generation"):
        monkeypatch.setattr(resources, name, None)
    monkeypatch.setattr(resources, "_active", 0)
    with VectorStore() as store:
        store.collection.add(
            ids=[f"id{i}" for i in range(20)],
            embeddings=[_vec(i) for i in range(20)],
            metadatas=[{"file_path": f"/m/{i}.eml"} for i in range(20)],
        )
    yield tmp_path / "chroma"
    if resources._keepalive_store is not None:
        resources._keepalive_store.close()


def test_index_generation_rises_on_writes_from_another_process(store_path):
    before = index_generation(store_path)
    _in_other_process(store_path, 'col.delete(ids=["id3"])')

    assert index_generation(store_path) > before


def test_index_generation_is_none_without_an_index(tmp_path):
    assert index_generation(tmp_path / "missing") is None


def test_mcp_search_sees_another_process_deletes_and_adds(store_path):
    with resources.in_use(), VectorStore() as store:
        assert len(store.search(_vec(0), n_results=20)) == 20

    _in_other_process(
        store_path,
        f"""
        col.delete(ids=[f"id{{i}}" for i in range(0, 20, 2)])
        col.add(ids=["new"], embeddings=[{_vec(99)!r}], metadatas=[{{"file_path": "/m/new.eml"}}])
        """,
    )

    with resources.in_use(), VectorStore() as store:
        paths = [hit["file_path"] for hit in store.search(_vec(0), n_results=20)]

    assert "/m/0.eml" not in paths
    assert "/m/new.eml" in paths
    assert len(paths) == 11


def test_search_skips_vectors_deleted_while_the_store_is_open(store_path):
    with VectorStore() as store:
        store.search(_vec(0), n_results=1)  # Chroma loads the index on first query
        _in_other_process(store_path, 'col.delete(ids=[f"id{i}" for i in range(0, 20, 2)])')
        paths = [hit["file_path"] for hit in store.search(_vec(0), n_results=20)]

    assert "/m/0.eml" not in paths
    assert len(paths) == 10

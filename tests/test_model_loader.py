"""Tests for local-cache-first model loading."""

import pytest

from mail_semantic_search.model_loader import load_model_local_first


def test_prefers_local_cache():
    """A cached model loads without ever allowing a network fetch."""
    calls = []

    def loader(name, **kwargs):
        calls.append((name, kwargs))
        return "model"

    result = load_model_local_first(loader, "BAAI/bge-base-en-v1.5", cache_folder="/cache")

    assert result == "model"
    assert len(calls) == 1
    assert calls[0] == ("BAAI/bge-base-en-v1.5", {"cache_folder": "/cache", "local_files_only": True})


def test_falls_back_to_download_when_not_cached():
    """An uncached model falls back to a network-allowed load."""
    calls = []

    def loader(name, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("model not found in local cache")
        return "downloaded"

    result = load_model_local_first(loader, "BAAI/bge-base-en-v1.5", cache_folder="/cache")

    assert result == "downloaded"
    assert len(calls) == 2
    assert calls[1] == {"cache_folder": "/cache"}


def test_propagates_download_failure():
    """When both the local and network loads fail, the network error surfaces."""

    def loader(name, **kwargs):
        if kwargs.get("local_files_only"):
            raise OSError("model not found in local cache")
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    with pytest.raises(RuntimeError, match="client has been closed"):
        load_model_local_first(loader, "BAAI/bge-base-en-v1.5")


def test_embedding_service_loads_local_first(monkeypatch):
    """EmbeddingService never hits the network when the model is cached."""
    from mail_semantic_search import embedding_service as module

    calls = []

    class FakeModel:
        def get_sentence_embedding_dimension(self):
            return 768

    def fake_sentence_transformer(name, **kwargs):
        calls.append(kwargs)
        return FakeModel()

    monkeypatch.setattr(module, "SentenceTransformer", fake_sentence_transformer)
    module.EmbeddingService()

    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True


def test_reranker_loads_local_first(monkeypatch):
    """CrossEncoderReranker never hits the network when the model is cached."""
    from mail_semantic_search import reranker as module

    calls = []

    def fake_cross_encoder(name, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(module, "CrossEncoder", fake_cross_encoder)
    module.CrossEncoderReranker()

    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True

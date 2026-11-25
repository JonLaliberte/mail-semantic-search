"""On MPS, model inputs must come in a small fixed set of shapes.

PyTorch's MPS backend keeps GPU memory for every distinct input shape and
never returns it, so variable-shaped batches leak (17 GB over 12 days in a
long-running MCP server). These tests pin the padding that bounds the shapes;
`scripts/measure_schedule_memory.py` measures the memory itself.
"""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from mail_semantic_search import fixed_shapes
from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.fixed_shapes import padded_batch
from mail_semantic_search.reranker import CrossEncoderReranker


@pytest.mark.parametrize("count", range(1, 100))
def test_padded_batch_uses_few_batch_sizes(count):
    padded, size = padded_batch(list(range(count)), 32, None)
    assert size in {1, 2, 4, 8, 16, 32}
    assert len(padded) % size == 0
    assert padded[:count] == list(range(count))
    assert all(item is None for item in padded[count:])
    # No more padding than one batch's worth.
    assert len(padded) - count < size


class FakeSentenceTransformer:
    """Records the batches encode() would run, as (rows, padded length)."""

    def __init__(self, device_type):
        self.device = SimpleNamespace(type=device_type)
        self.shapes = set()
        module = SimpleNamespace()
        module.tokenize = lambda texts, padding=True: (texts, padding)
        self.modules = [module]

    def __getitem__(self, index):
        return self.modules[index]

    def encode(self, texts, batch_size=32, show_progress_bar=False):
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            _, padding = self[0].tokenize(batch)
            length = 512 if padding == "max_length" else max(len(t) for t in batch)
            self.shapes.add((len(batch), length))
        return np.array([[float(len(t))] for t in texts])


def make_service(device_type):
    service = EmbeddingService.__new__(EmbeddingService)
    service._encode_lock = threading.Lock()
    service.model = FakeSentenceTransformer(device_type)
    service._fixed_shapes = fixed_shapes.uses_mps(service.model)
    if service._fixed_shapes:
        fixed_shapes.pad_sentence_transformer_to_max_length(service.model)
    return service


def random_batches():
    rng = np.random.default_rng(0)
    for _ in range(200):
        yield ["x" * int(rng.integers(1, 2000)) for _ in range(int(rng.integers(1, 41)))]


def test_embedding_service_bounds_shapes_on_mps():
    service = make_service("mps")
    for texts in random_batches():
        embeddings = service.embed_texts(texts)
        # One result per input text, in order; padding rows are dropped.
        assert embeddings == [[float(len(t))] for t in texts]
    service.embed_query("short query")
    assert service.model.shapes <= {(n, 512) for n in (1, 2, 4, 8, 16, 32)}


def test_embedding_service_leaves_cpu_batches_alone():
    service = make_service("cpu")
    texts = ["a", "bbb", "cc"]
    assert service.embed_texts(texts) == [[1.0], [3.0], [2.0]]
    assert service.model.shapes == {(3, 3)}


class FakeTokenizer:
    def __init__(self):
        self.calls = []
        self.model_max_length = 512

    def __call__(self, batch, **kwargs):
        self.calls.append((len(batch), kwargs))
        return kwargs


class FakeCrossEncoder:
    def __init__(self, device_type):
        self.device = SimpleNamespace(type=device_type)
        self.tokenizer = FakeTokenizer()

    def predict(self, pairs, batch_size=32):
        for start in range(0, len(pairs), batch_size):
            self.tokenizer(pairs[start : start + batch_size], padding=True, truncation=True)
        return np.array([float(len(text)) for _, text in pairs])


def make_reranker(device_type):
    reranker = CrossEncoderReranker.__new__(CrossEncoderReranker)
    reranker.model = FakeCrossEncoder(device_type)
    reranker._fixed_shapes = fixed_shapes.uses_mps(reranker.model)
    if reranker._fixed_shapes:
        fixed_shapes.pad_cross_encoder_to_max_length(reranker.model)
    return reranker


def test_reranker_bounds_shapes_on_mps():
    reranker = make_reranker("mps")
    candidates = [{"document": "x" * n} for n in (5, 50, 3, 20, 7)]
    ranked = reranker.rerank("query", candidates, top_k=5)

    assert [len(r["document"]) for r in ranked] == [50, 20, 7, 5, 3]
    tokenizer = reranker.model.tokenizer._tokenizer
    assert [rows for rows, _ in tokenizer.calls] == [8]
    assert all(kw["padding"] == "max_length" for _, kw in tokenizer.calls)
    # The proxy still exposes the real tokenizer's attributes.
    assert reranker.model.tokenizer.model_max_length == 512


def test_reranker_leaves_cpu_batches_alone():
    reranker = make_reranker("cpu")
    reranker.rerank("query", [{"document": "abc"}, {"document": "a"}], top_k=2)
    assert reranker.model.tokenizer.calls == [(2, {"padding": True, "truncation": True})]

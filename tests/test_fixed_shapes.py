"""On MPS, model inputs must come in a small fixed set of shapes.

PyTorch's MPS backend keeps GPU memory for every distinct input shape and
never returns it, so variable-shaped batches leak (17 GB over 12 days in a
long-running MCP server). These tests build a tiny BERT on disk and run it
through the installed sentence-transformers' real `encode()` / `predict()`, so
they check the tensors that actually reach the forward pass on whatever
version is installed. They run on CPU; `install` is the same code on MPS.
`scripts/measure_schedule_memory.py` measures the memory itself on a Mac.
"""

import random
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from transformers import BertConfig, BertForSequenceClassification, BertModel, BertTokenizerFast

from mail_semantic_search import fixed_shapes
from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.reranker import CrossEncoderReranker

st = pytest.importorskip("sentence_transformers")

WORDS = [f"w{i}" for i in range(200)]
ALLOWED = {(b, n) for b in (1, 2, 4, 8, 16, 32) for n in (128, 512)}
MAX_LENGTH = 512


@pytest.mark.parametrize("n,expected", [(1, 1), (3, 4), (16, 16), (32, 32), (33, 64)])
def test_bucket(n, expected):
    assert fixed_shapes.bucket(n) == expected


@pytest.mark.parametrize(
    "n,max_length,expected",
    [(1, 512, 128), (128, 512, 128), (129, 512, 512), (512, 512, 512),
     (5, 64, 64), (300, None, 512), (5, None, 128)],
)
def test_length_bucket(n, max_length, expected):
    assert fixed_shapes.length_bucket(n, max_length) == expected


def _save_tiny_bert(path, model_cls, **config_kwargs):
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"] + WORDS
    (path / "vocab.txt").write_text("\n".join(vocab) + "\n")
    tokenizer = BertTokenizerFast(
        vocab_file=str(path / "vocab.txt"), model_max_length=MAX_LENGTH
    )
    torch.manual_seed(0)
    config = BertConfig(
        vocab_size=len(vocab), hidden_size=32, num_hidden_layers=2,
        num_attention_heads=2, intermediate_size=64,
        max_position_embeddings=MAX_LENGTH, **config_kwargs,
    )
    model_cls(config).save_pretrained(path)
    tokenizer.save_pretrained(path)
    return str(path)


@pytest.fixture(scope="module")
def embedder_path(tmp_path_factory):
    return _save_tiny_bert(tmp_path_factory.mktemp("embedder"), BertModel)


@pytest.fixture(scope="module")
def cross_encoder_path(tmp_path_factory):
    return _save_tiny_bert(
        tmp_path_factory.mktemp("cross"), BertForSequenceClassification, num_labels=1
    )


def _record_shapes(model):
    """Collect (batch, length) of every input that reaches the BERT embeddings."""
    shapes = set()
    embeddings = next(m for m in model.modules() if type(m).__name__ == "BertEmbeddings")
    embeddings.register_forward_hook(
        lambda module, args, output: shapes.add(tuple(output.shape[:2]))
    )
    return shapes


def _texts(rng, count):
    return [" ".join(rng.choices(WORDS, k=rng.randrange(0, 600))) for _ in range(count)]


def test_sentence_transformer_forward_shapes_are_bounded(embedder_path):
    plain = st.SentenceTransformer(embedder_path, device="cpu")
    padded = st.SentenceTransformer(embedder_path, device="cpu")
    fixed_shapes.install(padded)
    fixed_shapes.install(padded)  # idempotent
    shapes = _record_shapes(padded)

    rng = random.Random(0)
    for _ in range(40):
        texts = _texts(rng, rng.randint(1, 40))
        np.testing.assert_allclose(
            padded.encode(texts), plain.encode(texts), rtol=1e-4, atol=1e-5
        )
    np.testing.assert_allclose(
        padded.encode(["w1 w2"]), plain.encode(["w1 w2"]), rtol=1e-4, atol=1e-5
    )

    assert shapes and shapes <= ALLOWED
    assert {n for _, n in shapes} == {128, 512}


def test_cross_encoder_forward_shapes_are_bounded(cross_encoder_path):
    plain = st.CrossEncoder(cross_encoder_path, device="cpu")
    padded = st.CrossEncoder(cross_encoder_path, device="cpu")
    fixed_shapes.install(padded)
    shapes = _record_shapes(padded)

    rng = random.Random(1)
    for _ in range(20):
        pairs = [("w1 w2", text) for text in _texts(rng, rng.randint(1, 40))]
        np.testing.assert_allclose(
            padded.predict(pairs), plain.predict(pairs), rtol=1e-4, atol=1e-5
        )

    assert shapes and shapes <= ALLOWED


def test_services_install_only_on_mps(monkeypatch):
    installed = []
    monkeypatch.setattr(fixed_shapes, "install", installed.append)

    def fake_loader(device_type):
        model = SimpleNamespace(
            device=SimpleNamespace(type=device_type),
            get_sentence_embedding_dimension=lambda: 8,
        )
        return lambda cls, name, **kwargs: model

    for device_type, expected in (("cpu", 0), ("mps", 1)):
        installed.clear()
        monkeypatch.setattr(
            "mail_semantic_search.embedding_service.load_model_local_first",
            fake_loader(device_type),
        )
        monkeypatch.setattr(
            "mail_semantic_search.reranker.load_model_local_first",
            fake_loader(device_type),
        )
        EmbeddingService()
        CrossEncoderReranker()
        assert len(installed) == 2 * expected

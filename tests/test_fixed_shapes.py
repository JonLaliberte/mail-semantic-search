"""On MPS, model inputs must come in a small fixed set of shapes.

PyTorch's MPS backend caches GPU resources by input shape. On PyTorch 2.10
the cache retained 17 GB after 12 days in a long-running MCP server.
These tests build a tiny BERT on disk and run it
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
     (5, 64, 64), (300, None, 512), (5, None, 128),
     # Truncation below 512 (all-MiniLM-L6-v2 truncates at 256).
     (129, 256, 256), (256, 256, 256),
     # Long-context models step by powers of two above 512, capped.
     (600, 8192, 1024), (1025, 8192, 2048), (5000, 8192, 8192), (600, 1000, 1000)],
)
def test_length_bucket(n, max_length, expected):
    assert fixed_shapes.length_bucket(n, max_length) == expected


def _save_tiny_bert(path, model_cls, positions=MAX_LENGTH, **config_kwargs):
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"] + WORDS
    (path / "vocab.txt").write_text("\n".join(vocab) + "\n")
    tokenizer = BertTokenizerFast(
        vocab_file=str(path / "vocab.txt"), model_max_length=positions
    )
    torch.manual_seed(0)
    config = BertConfig(
        vocab_size=len(vocab), hidden_size=32, num_hidden_layers=2,
        num_attention_heads=2, intermediate_size=64,
        max_position_embeddings=positions, **config_kwargs,
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
    embeddings = next(m for m in model.modules() if type(m).__name__.endswith("Embeddings"))
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


def _save_tiny_xlm_roberta(path):
    """XLM-RoBERTa-style model: pad ID 1, position IDs counted from non-pad IDs.

    The tokenizer has XLM-R's special tokens and pair template but one token
    per character, so a text's token count is easy to control.
    """
    from tokenizers import Regex, Tokenizer, models, pre_tokenizers, processors
    from transformers import PreTrainedTokenizerFast, XLMRobertaConfig, XLMRobertaForSequenceClassification

    vocab = {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "<mask>": 4}
    for code in range(32, 127):
        vocab.setdefault(chr(code), len(vocab))
    backend = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
    backend.pre_tokenizer = pre_tokenizers.Split(Regex("."), "isolated")
    backend.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>",
        pair="<s> $A </s> </s> $B </s>",
        special_tokens=[("<s>", 0), ("</s>", 2)],
    )
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, bos_token="<s>", eos_token="</s>", cls_token="<s>",
        sep_token="</s>", pad_token="<pad>", unk_token="<unk>", mask_token="<mask>",
        model_max_length=512,
    )
    assert tokenizer.pad_token_id == 1
    torch.manual_seed(0)
    config = XLMRobertaConfig(
        vocab_size=len(vocab), hidden_size=32, num_hidden_layers=2, num_attention_heads=2,
        intermediate_size=64, max_position_embeddings=514, pad_token_id=1,
        bos_token_id=0, eos_token_id=2, type_vocab_size=1, num_labels=1,
    )
    XLMRobertaForSequenceClassification(config).save_pretrained(path)
    tokenizer.save_pretrained(path)
    return str(path)


def test_pads_with_the_models_pad_token_id(tmp_path):
    """A 129-token input pads on a pad-ID-1 model without overrunning positions."""
    path = _save_tiny_xlm_roberta(tmp_path)
    plain = st.CrossEncoder(path, device="cpu")
    padded = st.CrossEncoder(path, device="cpu")
    fixed_shapes.install(padded)
    shapes = _record_shapes(padded)

    def tokens(pair):
        return len(plain.tokenizer(*pair)["input_ids"])

    # 129 tokens: one past the short bucket, so it pads to the long bucket,
    # 512 (the tokenizer's limit, two below the 514-entry position table).
    long_pair = ("q", "x" * 124)
    assert tokens(long_pair) == 129
    # A partial batch (3 rows -> 4) mixing short and boundary-length inputs.
    pairs = [long_pair, ("q", "short"), ("q", "y" * 500)]
    np.testing.assert_allclose(
        padded.predict(pairs), plain.predict(pairs), rtol=1e-4, atol=1e-5
    )
    np.testing.assert_allclose(
        padded.predict([("q", "hi")]), plain.predict([("q", "hi")]), rtol=1e-4, atol=1e-5
    )
    assert shapes == {(4, 512), (1, 128)}


def test_long_bucket_stops_at_the_truncation_length(tmp_path):
    """Pad to what the model is fed, not to its position table.

    Long-context models (bge-m3, nomic-embed-text) have 8192 positions; padding
    every long input to that would make each batch enormous.
    """
    path = _save_tiny_bert(tmp_path, BertModel, positions=8192)
    plain = st.SentenceTransformer(path, device="cpu")
    rng = random.Random(2)
    texts = [" ".join(rng.choices(WORDS, k=k)) for k in (5, 200, 700, 1500)]

    for limit, lengths in ((256, {128, 256}), (1024, {128, 512, 1024})):
        plain.max_seq_length = limit
        # Installed after setting the limit, as the services do at load time.
        model = st.SentenceTransformer(path, device="cpu")
        model.max_seq_length = limit
        fixed_shapes.install(model)
        shapes = _record_shapes(model)
        for text in texts:
            np.testing.assert_allclose(
                model.encode([text]), plain.encode([text]), rtol=1e-4, atol=1e-5
            )
        assert {n for _, n in shapes} == lengths


def test_install_skips_models_without_a_hugging_face_module():
    model = torch.nn.Sequential(torch.nn.Linear(4, 2))
    fixed_shapes.install(model)  # no TypeError
    x = torch.ones(3, 4)
    assert model(x).shape == (3, 2)


def test_cut_leaves_two_dimensional_outputs_alone():
    """pooler_output is (batch, hidden), even when hidden equals the padded length."""
    torch.manual_seed(0)
    config = BertConfig(
        vocab_size=50, hidden_size=128, num_hidden_layers=1, num_attention_heads=2,
        intermediate_size=64, max_position_embeddings=512,
    )
    model = BertModel(config).eval()
    fixed_shapes.install(model)
    ids = torch.randint(5, 50, (3, 10))
    out = model(input_ids=ids, attention_mask=torch.ones_like(ids))
    assert out.last_hidden_state.shape == (3, 10, 128)
    assert out.pooler_output.shape == (3, 128)

"""Keep model inputs to a few fixed shapes on Apple's GPU (MPS).

PyTorch's MPS backend keeps GPU memory for every distinct input shape it has
run, and `torch.mps.empty_cache()` does not give it back. Sentence-transformers
pads each batch only to its own longest text, and the last batch of a call is
whatever is left over, so on real mail nearly every batch is a shape the
backend hasn't seen yet. A long-lived MCP server that embeds new mail every few
minutes therefore grows without bound: one ran up 17 GB of GPU memory in 12
days, and 120 random-length batches in isolation take it from 2 GB to 14 GB.

So on MPS every sequence is padded to the model's maximum length and every
batch is padded with empty texts up to a power of two. That leaves at most
log2(batch size) + 1 shapes per model (6 at a batch size of 32), and memory
stays flat after they have all been seen once. Padding changes nothing in the
results: attention masks hide the pad tokens, and the padding rows are dropped.

On CPU and CUDA none of this applies: their allocators reuse memory across
shapes, so the padding would only cost time.
"""

from typing import Any, List, Tuple


def uses_mps(model: Any) -> bool:
    """True when the model's weights live on Apple's GPU."""
    device = getattr(model, "device", None)
    return getattr(device, "type", None) == "mps"


def padded_batch(items: List[Any], max_batch_size: int, filler: Any) -> Tuple[List[Any], int]:
    """Pad `items` so every batch has the same power-of-two size.

    Returns the padded list and the batch size to encode it with. The caller
    keeps the first `len(items)` results.
    """
    size = 1
    while size < min(len(items), max_batch_size):
        size *= 2
    padding = (-len(items)) % size
    return list(items) + [filler] * padding, size


def pad_sentence_transformer_to_max_length(model: Any) -> None:
    """Make a SentenceTransformer pad every sequence to its max length."""
    module = model[0]
    tokenize = module.tokenize

    def tokenize_max_length(texts, padding=True, **kwargs):
        return tokenize(texts, padding="max_length", **kwargs)

    module.tokenize = tokenize_max_length


class _MaxLengthTokenizer:
    """Tokenizer proxy that always pads to the tokenizer's max length."""

    def __init__(self, tokenizer: Any):
        self._tokenizer = tokenizer

    def __call__(self, *args, **kwargs):
        kwargs["padding"] = "max_length"
        kwargs.setdefault("truncation", True)
        return self._tokenizer(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tokenizer, name)


def pad_cross_encoder_to_max_length(model: Any) -> None:
    """Make a CrossEncoder pad every sequence to its max length."""
    if not isinstance(model.tokenizer, _MaxLengthTokenizer):
        model.tokenizer = _MaxLengthTokenizer(model.tokenizer)

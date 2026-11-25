"""Keep model inputs to a few fixed shapes on Apple's GPU (MPS).

PyTorch's MPS backend keeps GPU memory for every distinct input shape it has
run, and `torch.mps.empty_cache()` does not give it back. Batches are padded
only to their own longest text, and the last batch of a call is whatever is
left over, so on real mail nearly every batch is a shape the backend hasn't
seen yet. A long-lived MCP server that embeds new mail every few minutes
therefore grows without bound: one ran up 17 GB of GPU memory in 12 days, and
120 random-length batches in isolation take it from 2 GB to 14 GB.

`install` wraps the forward pass of the Hugging Face model inside a
SentenceTransformer or CrossEncoder. Before each pass it pads the token
tensors: sequences of up to `SHORT_LENGTH` tokens to `SHORT_LENGTH`, longer
ones to the model's maximum, and the batch size up to the next power of two.
Right after the pass it cuts every output back to the original shape. With
batches of up to 32 a model then sees at most 2 lengths x 6 batch sizes = 12
shapes, and memory stops growing once those have been seen.

Why two lengths: the GPU memory is dominated by the max-length shapes, which
indexing needs anyway (on BGE-base plus the MiniLM reranker, all max-length
shapes cost ~4.8 GB once warm). The short bucket adds ~0.2 GB to that and
keeps a search query at ~7 ms instead of ~14 ms. Finer buckets (64/128/256)
saved under 1 ms more and cost another ~0.8 GB.

Results don't change: padded tokens are masked out of attention, padded rows
are separate inputs that get dropped, and callers never see the padding.
Wrapping the model's forward rather than sentence-transformers' tokenization
keeps this independent of that library's internals, which changed in 5.4.

On CPU and CUDA none of this applies: their allocators reuse memory across
shapes, so the padding would only cost time.
"""

import functools
from typing import Any, Optional

SHORT_LENGTH = 128


def uses_mps(model: Any) -> bool:
    """True when the model's weights live on Apple's GPU."""
    device = getattr(model, "device", None)
    return getattr(device, "type", None) == "mps"


def bucket(n: int) -> int:
    """Round n up to a power of two."""
    size = 1
    while size < n:
        size *= 2
    return size


def length_bucket(n: int, max_length: Optional[int]) -> int:
    """`SHORT_LENGTH` for short sequences, else the model's max length."""
    if n <= SHORT_LENGTH:
        return SHORT_LENGTH if max_length is None else min(SHORT_LENGTH, max_length)
    if max_length is None or n > max_length:
        return bucket(n)
    return max_length


def _hf_model(model: Any) -> Any:
    """The outermost Hugging Face model inside a sentence-transformers model."""
    from transformers import PreTrainedModel

    for module in model.modules():
        if isinstance(module, PreTrainedModel):
            return module
    raise TypeError(f"No Hugging Face model inside {type(model).__name__}")


def install(model: Any) -> None:
    """Pad every forward pass of `model` to a bounded set of shapes.

    Works on a SentenceTransformer, a CrossEncoder, or a bare Hugging Face
    model. Installing twice is a no-op.
    """
    hf_model = _hf_model(model)
    forward = hf_model.forward
    if getattr(forward, "_fixed_shapes", False):
        return
    max_length = getattr(hf_model.config, "max_position_embeddings", None)

    # Replace the instance's forward rather than registering module hooks:
    # sentence-transformers 5.4+ calls `model.forward(...)` directly, which
    # skips hooks. `wraps` keeps the signature it inspects to filter inputs.
    @functools.wraps(forward)
    def padded_forward(*args, **kwargs):
        shape, args, kwargs = _pad(args, kwargs, max_length)
        output = forward(*args, **kwargs)
        if shape is None:
            return output
        return _cut(output, *shape)

    padded_forward._fixed_shapes = True
    hf_model.forward = padded_forward


def _pad(args, kwargs, max_length):
    """Pad the token tensors; returns (original and padded shape, args, kwargs)."""
    import torch

    reference = kwargs.get("input_ids")
    if reference is None:
        reference = args[0] if args else kwargs.get("attention_mask")
    if not isinstance(reference, torch.Tensor) or reference.dim() != 2:
        return None, args, kwargs
    rows, length = reference.shape
    padded_rows = bucket(rows)
    padded_length = length_bucket(length, max_length)
    if (padded_rows, padded_length) == (rows, length):
        return None, args, kwargs

    def pad(tensor, name=None):
        if not isinstance(tensor, torch.Tensor) or tuple(tensor.shape[:2]) != (rows, length):
            return tensor
        out = tensor.new_zeros((padded_rows, padded_length) + tuple(tensor.shape[2:]))
        out[:rows, :length] = tensor
        if name == "attention_mask":
            # A row with nothing to attend to can produce NaNs; padded rows
            # attend to their first token instead. They are dropped anyway.
            out[rows:, 0] = 1
        return out

    args = tuple(pad(a) for a in args)
    kwargs = {k: pad(v, k) for k, v in kwargs.items()}
    return (rows, length, padded_rows, padded_length), args, kwargs


def _cut(value, rows, length, padded_rows, padded_length):
    import torch

    if isinstance(value, torch.Tensor):
        if value.dim() >= 1 and value.shape[0] == padded_rows:
            value = value[:rows]
            if value.dim() >= 2 and value.shape[1] == padded_length:
                value = value[:, :length]
        return value
    if isinstance(value, dict):  # transformers ModelOutput is a dict
        for key in list(value.keys()):
            value[key] = _cut(value[key], rows, length, padded_rows, padded_length)
        return value
    if isinstance(value, tuple):
        return type(value)(_cut(v, rows, length, padded_rows, padded_length) for v in value)
    return value

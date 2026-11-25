"""Cross-encoder reranking for semantic search candidates."""

from contextlib import ExitStack, redirect_stdout
import os
import logging
from typing import Dict, List

from sentence_transformers import CrossEncoder

from mail_semantic_search.config import config
from mail_semantic_search.fixed_shapes import (
    pad_cross_encoder_to_max_length,
    padded_batch,
    uses_mps,
)
from mail_semantic_search.model_loader import load_model_local_first
from mail_semantic_search.runtime_logging import (
    LoggerWriter,
    configure_logging,
    redirect_stderr_to_logger,
)

logger = logging.getLogger(__name__)

# CrossEncoder.predict's default; on MPS the batch is padded up to a power of
# two no larger than this (see fixed_shapes.py).
PREDICT_BATCH_SIZE = 32


class CrossEncoderReranker:
    """Local cross-encoder reranker."""

    def __init__(self) -> None:
        configure_logging()
        cache_dir = str(config.model_cache_dir.absolute())
        os.environ["TRANSFORMERS_CACHE"] = cache_dir
        os.environ["HF_HOME"] = cache_dir
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(LoggerWriter(logger, logging.WARNING)))
            stack.enter_context(redirect_stderr_to_logger(logger, logging.WARNING))
            self.model = load_model_local_first(
                CrossEncoder,
                config.reranker_model,
                cache_folder=cache_dir,
            )
        # Same reason as EmbeddingService: bound the input shapes on MPS.
        self._fixed_shapes = uses_mps(self.model)
        if self._fixed_shapes:
            pad_cross_encoder_to_max_length(self.model)

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        """Rerank candidates and return top_k results."""
        if not candidates:
            return []

        pairs = []
        working = []
        for item in candidates:
            text = self._candidate_text(item)
            if not text:
                continue
            pairs.append((query, text))
            working.append(item)

        if not pairs:
            return candidates[:top_k]

        batch_size = PREDICT_BATCH_SIZE
        padded = pairs
        if self._fixed_shapes:
            padded, batch_size = padded_batch(pairs, PREDICT_BATCH_SIZE, (query, ""))
        scores = self.model.predict(padded, batch_size=batch_size)[: len(pairs)]
        scored = []
        for item, score in zip(working, scores):
            updated = dict(item)
            updated["rerank_score"] = float(score)
            scored.append(updated)

        scored.sort(key=lambda r: r.get("rerank_score", float("-inf")), reverse=True)
        return scored[:top_k]

    def _candidate_text(self, item: Dict) -> str:
        """Build candidate text for reranking."""
        document = str(item.get("document", "") or "").strip()
        if document:
            return document[: config.reranker_max_text_chars]

        subject = str(item.get("subject", "") or "").strip()
        preview = str(item.get("body_preview", "") or "").strip()
        if subject and preview:
            return f"{subject}\n{preview}"[: config.reranker_max_text_chars]
        return (subject or preview)[: config.reranker_max_text_chars]

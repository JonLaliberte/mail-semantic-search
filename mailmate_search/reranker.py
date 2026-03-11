"""Cross-encoder reranking for semantic search candidates."""

import os
from typing import Dict, List

from sentence_transformers import CrossEncoder

from mailmate_search.config import config


class CrossEncoderReranker:
    """Local cross-encoder reranker."""

    def __init__(self) -> None:
        cache_dir = str(config.model_cache_dir.absolute())
        os.environ["TRANSFORMERS_CACHE"] = cache_dir
        os.environ["HF_HOME"] = cache_dir
        self.model = CrossEncoder(config.reranker_model)

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

        scores = self.model.predict(pairs)
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

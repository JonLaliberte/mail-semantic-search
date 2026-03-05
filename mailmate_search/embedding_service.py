"""Embedding service using sentence-transformers."""

import os
from typing import List, Optional

from sentence_transformers import SentenceTransformer

from mailmate_search.config import config

# Human-friendly aliases documented in README -> canonical HF model IDs.
MODEL_ALIASES = {
    "BGE-base-en-v1.5": "BAAI/bge-base-en-v1.5",
    "BGE-small-en-v1.5": "BAAI/bge-small-en-v1.5",
    "nomic-embed-text-v1": "nomic-ai/nomic-embed-text-v1",
    "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
}


def resolve_model_name(model_name: str) -> str:
    """Resolve documented aliases to canonical Hugging Face model IDs."""
    return MODEL_ALIASES.get(model_name, model_name)


class EmbeddingService:
    """Service for generating embeddings using sentence-transformers."""

    def __init__(self, model_name: Optional[str] = None):
        """Initialize the embedding service with a model."""
        configured_model = model_name or config.embedding_model
        self.model_name = resolve_model_name(configured_model)

        # Set cache directory for models
        cache_dir = str(config.model_cache_dir.absolute())
        os.environ["TRANSFORMERS_CACHE"] = cache_dir
        os.environ["HF_HOME"] = cache_dir

        print(f"Loading embedding model: {self.model_name}")
        print(f"Model cache directory: {cache_dir}")
        self.model = SentenceTransformer(
            self.model_name, cache_folder=cache_dir
        )
        print(f"Model loaded successfully. Embedding dimension: {self.model.get_sentence_embedding_dimension()}")

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a list of texts."""
        if not texts:
            return []
        return self.model.encode(texts, show_progress_bar=False).tolist()

    def embed_query(self, query: str) -> List[float]:
        """Generate embedding for a single query."""
        return self.model.encode([query], show_progress_bar=False)[0].tolist()

    def get_embedding_dimension(self) -> int:
        """Get the dimension of embeddings produced by this model."""
        return self.model.get_sentence_embedding_dimension()


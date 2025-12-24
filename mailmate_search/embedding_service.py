"""Embedding service using sentence-transformers."""

import os
from typing import List, Optional

from sentence_transformers import SentenceTransformer

from mailmate_search.config import config


class EmbeddingService:
    """Service for generating embeddings using sentence-transformers."""

    def __init__(self, model_name: Optional[str] = None):
        """Initialize the embedding service with a model."""
        self.model_name = model_name or config.embedding_model

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


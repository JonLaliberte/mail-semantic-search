"""Configuration management for MailMate search."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self):
        # Embedding model configuration
        self.embedding_model: str = os.getenv(
            "EMBEDDING_MODEL", "BGE-base-en-v1.5"
        )

        # MailMate email directory
        mailmate_dir = os.getenv(
            "MAILMATE_EMAIL_DIR",
            os.path.expanduser(
                "~/Library/Application Support/MailMate/Messages"
            ),
        )
        self.mailmate_email_dir = Path(mailmate_dir)

        # ChromaDB storage path
        chromadb_path = os.getenv("CHROMADB_PATH", "./data/chromadb")
        self.chromadb_path = Path(chromadb_path)

        # Model cache directory
        model_cache_dir = os.getenv("MODEL_CACHE_DIR", "./data/models")
        self.model_cache_dir = Path(model_cache_dir)

        # Processing configuration
        self.batch_size: int = int(os.getenv("BATCH_SIZE", "32"))
        self.search_results: int = int(os.getenv("SEARCH_RESULTS", "10"))

        # Ensure directories exist
        self.chromadb_path.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return (
            f"Config("
            f"embedding_model={self.embedding_model}, "
            f"mailmate_email_dir={self.mailmate_email_dir}, "
            f"chromadb_path={self.chromadb_path}, "
            f"batch_size={self.batch_size}, "
            f"search_results={self.search_results}"
            f")"
        )


# Global config instance
config = Config()



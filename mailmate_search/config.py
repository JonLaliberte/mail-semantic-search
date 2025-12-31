"""Configuration management for MailMate search."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration loaded from environment variables."""

    # Default limits for text processing
    DEFAULT_BODY_PREVIEW_LIMIT = 2000
    DEFAULT_MAX_ATTACHMENT_TEXT_PER_FILE = 2000
    DEFAULT_MAX_TOTAL_ATTACHMENT_TEXT = 5000
    DEFAULT_MAX_FILTERED_SEARCH_LIMIT = 1000

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

        # Database path
        database_path = os.getenv("DATABASE_PATH", "./data/database.db")
        self.database_path = Path(database_path)

        # Processing configuration with validation
        batch_size = int(os.getenv("BATCH_SIZE", "32"))
        if batch_size < 1:
            batch_size = 32
        elif batch_size > 500:
            batch_size = 500
        self.batch_size: int = batch_size

        self.search_results: int = int(os.getenv("SEARCH_RESULTS", "10"))

        # Text processing limits (configurable via env vars)
        self.body_preview_limit: int = int(
            os.getenv("BODY_PREVIEW_LIMIT", str(self.DEFAULT_BODY_PREVIEW_LIMIT))
        )
        self.max_attachment_text_per_file: int = int(
            os.getenv("MAX_ATTACHMENT_TEXT_PER_FILE", str(self.DEFAULT_MAX_ATTACHMENT_TEXT_PER_FILE))
        )
        self.max_total_attachment_text: int = int(
            os.getenv("MAX_TOTAL_ATTACHMENT_TEXT", str(self.DEFAULT_MAX_TOTAL_ATTACHMENT_TEXT))
        )
        self.max_filtered_search_limit: int = int(
            os.getenv("MAX_FILTERED_SEARCH_LIMIT", str(self.DEFAULT_MAX_FILTERED_SEARCH_LIMIT))
        )

        # Ensure directories exist
        self.chromadb_path.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

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



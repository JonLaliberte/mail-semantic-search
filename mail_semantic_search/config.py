"""Configuration management for mail-semantic-search."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration loaded from environment variables."""

    # Default limits for text processing
    DEFAULT_BODY_PREVIEW_LIMIT = 5000
    DEFAULT_MAX_ATTACHMENT_TEXT_PER_FILE = 2000
    DEFAULT_MAX_TOTAL_ATTACHMENT_TEXT = 5000
    DEFAULT_MAX_FILTERED_SEARCH_LIMIT = 1000
    
    # Display constants (Issue #14 - magic numbers)
    MAX_ATTACHMENTS_DISPLAY = 3
    MAX_PREVIEW_LENGTH = 200
    MAX_CHROMADB_METADATA_LENGTH = 500
    MAX_ATTACHMENT_TYPES_STORED = 5
    MAX_ATTACHMENTS_FOR_METADATA = 10
    
    # Size limits for memory management (Issues #5, #19)
    DEFAULT_MAX_EMAIL_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    DEFAULT_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB for text extraction
    
    # Database batch limits (Issue #6)
    MAX_IN_CLAUSE_SIZE = 500
    DEFAULT_QUERY_PARSER_TIMEOUT_SECONDS = 8
    DEFAULT_RERANK_MAX_CANDIDATES = 50
    DEFAULT_RERANK_MAX_TEXT_CHARS = 1200
    DEFAULT_INDEX_HEARTBEAT_SECONDS = 60
    DEFAULT_INDEX_STALL_DUMP_SECONDS = 300
    DEFAULT_INCREMENTAL_OVERLAP_SECONDS = 24 * 60 * 60
    DEFAULT_QUOTE_STRIP_TIMEOUT_SECONDS = 2.0
    DEFAULT_QUOTE_STRIP_MAX_CHARS = 50000
    DEFAULT_QUOTE_STRIP_MAX_LINES = 1500
    DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
    DEFAULT_LOG_BACKUP_COUNT = 5

    def __init__(self):
        # Embedding model configuration
        self.embedding_model: str = os.getenv(
            "EMBEDDING_MODEL", "BGE-base-en-v1.5"
        )

        # Email directory
        email_dir_str = os.getenv(
            "EMAIL_DIR",
            os.path.expanduser(
                "~/Library/Application Support/MailMate/Messages"
            ),
        )
        self.email_dir = Path(email_dir_str)

        # ChromaDB storage path
        chromadb_path = os.getenv("CHROMADB_PATH", "./data/chromadb")
        self.chromadb_path = Path(chromadb_path)

        # Model cache directory
        model_cache_dir = os.getenv("MODEL_CACHE_DIR", "./data/models")
        self.model_cache_dir = Path(model_cache_dir)

        # Database path
        database_path = os.getenv("DATABASE_PATH", "./data/database.db")
        self.database_path = Path(database_path)

        # Staging dir for emails+attachments copied out for LLM access. Defaults
        # under ~/Documents so Claude Desktop's filesystem sandbox can read them
        # — the source EMAIL_DIR is often on an external volume that the LLM
        # context cannot access.
        staging_dir = os.getenv(
            "STAGING_DIR",
            os.path.expanduser("~/Documents/mailmate-staged"),
        )
        self.staging_dir = Path(staging_dir)

        # Runtime logging
        log_path = os.getenv("LOG_PATH", "./data/logs/mail-semantic-search.error.log")
        self.log_path = Path(log_path)
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.log_third_party_level: str = os.getenv(
            "LOG_THIRD_PARTY_LEVEL", "WARNING"
        ).upper()
        self.log_max_bytes: int = int(
            os.getenv("LOG_MAX_BYTES", str(self.DEFAULT_LOG_MAX_BYTES))
        )
        self.log_backup_count: int = int(
            os.getenv("LOG_BACKUP_COUNT", str(self.DEFAULT_LOG_BACKUP_COUNT))
        )

        # Processing configuration with validation
        batch_size = int(os.getenv("BATCH_SIZE", "32"))
        if batch_size < 1:
            batch_size = 32
        elif batch_size > 500:
            batch_size = 500
        self.batch_size: int = batch_size

        # Search results with validation (Issue #10)
        search_results = int(os.getenv("SEARCH_RESULTS", "10"))
        if search_results < 1:
            search_results = 10
        elif search_results > 10000:
            search_results = 10000
        self.search_results: int = search_results

        # Phase 1: local query parser configuration
        self.query_parser_enabled: bool = self._parse_bool(
            os.getenv("QUERY_PARSER_ENABLED", "false")
        )
        self.query_parser_endpoint: str = os.getenv(
            "QUERY_PARSER_ENDPOINT", "http://localhost:11434/api/generate"
        )
        self.query_parser_model: str = os.getenv(
            "QUERY_PARSER_MODEL", "llama3.1:8b"
        )
        self.query_parser_timeout_seconds: int = int(
            os.getenv(
                "QUERY_PARSER_TIMEOUT_SECONDS",
                str(self.DEFAULT_QUERY_PARSER_TIMEOUT_SECONDS),
            )
        )

        # Phase 2: local cross-encoder reranker configuration
        self.rerank_enabled: bool = self._parse_bool(
            os.getenv("RERANK_ENABLED", "false")
        )
        self.reranker_model: str = os.getenv(
            "RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        self.rerank_max_candidates: int = int(
            os.getenv(
                "RERANK_MAX_CANDIDATES", str(self.DEFAULT_RERANK_MAX_CANDIDATES)
            )
        )
        self.reranker_max_text_chars: int = int(
            os.getenv(
                "RERANK_MAX_TEXT_CHARS", str(self.DEFAULT_RERANK_MAX_TEXT_CHARS)
            )
        )

        # Indexing diagnostics
        self.index_runtime_diagnostics: bool = self._parse_bool(
            os.getenv("INDEX_RUNTIME_DIAGNOSTICS", "true")
        )
        self.index_heartbeat_seconds: int = int(
            os.getenv(
                "INDEX_HEARTBEAT_SECONDS",
                str(self.DEFAULT_INDEX_HEARTBEAT_SECONDS),
            )
        )
        self.index_stall_dump_seconds: int = int(
            os.getenv(
                "INDEX_STALL_DUMP_SECONDS",
                str(self.DEFAULT_INDEX_STALL_DUMP_SECONDS),
            )
        )
        self.incremental_overlap_seconds: int = int(
            os.getenv(
                "INCREMENTAL_OVERLAP_SECONDS",
                str(self.DEFAULT_INCREMENTAL_OVERLAP_SECONDS),
            )
        )

        # Quoted-reply stripping safeguards
        self.quote_strip_enabled: bool = self._parse_bool(
            os.getenv("QUOTE_STRIP_ENABLED", "true")
        )
        self.quote_strip_timeout_seconds: float = float(
            os.getenv(
                "QUOTE_STRIP_TIMEOUT_SECONDS",
                str(self.DEFAULT_QUOTE_STRIP_TIMEOUT_SECONDS),
            )
        )
        self.quote_strip_max_chars: int = int(
            os.getenv(
                "QUOTE_STRIP_MAX_CHARS",
                str(self.DEFAULT_QUOTE_STRIP_MAX_CHARS),
            )
        )
        self.quote_strip_max_lines: int = int(
            os.getenv(
                "QUOTE_STRIP_MAX_LINES",
                str(self.DEFAULT_QUOTE_STRIP_MAX_LINES),
            )
        )

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
        
        # Size limits for memory management (Issues #5, #19)
        self.max_email_file_size: int = int(
            os.getenv("MAX_EMAIL_FILE_SIZE", str(self.DEFAULT_MAX_EMAIL_FILE_SIZE))
        )
        self.max_attachment_size: int = int(
            os.getenv("MAX_ATTACHMENT_SIZE", str(self.DEFAULT_MAX_ATTACHMENT_SIZE))
        )

        # Ensure directories exist
        self.chromadb_path.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_bool(value: str) -> bool:
        """Parse bool-like env strings safely."""
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def __repr__(self) -> str:
        return (
            f"Config("
            f"embedding_model={self.embedding_model}, "
            f"email_dir={self.email_dir}, "
            f"chromadb_path={self.chromadb_path}, "
            f"batch_size={self.batch_size}, "
            f"search_results={self.search_results}"
            f")"
        )


# Global config instance
config = Config()



"""Shared service request/response models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class SearchRequest:
    """Structured semantic search request."""

    query: str
    from_addr: Optional[str] = None
    to_addr: Optional[str] = None
    subject: Optional[str] = None
    subject_like: Optional[str] = None
    date_after: Optional[datetime] = None
    date_before: Optional[datetime] = None
    has_attachments: Optional[bool] = None
    attachment_type: Optional[str] = None
    attachment_name: Optional[str] = None
    limit: Optional[int] = None
    auto_filters: Optional[bool] = None
    rerank: Optional[bool] = None


@dataclass
class QueryRequest:
    """Structured metadata-only query request."""

    from_addr: Optional[str] = None
    to_addr: Optional[str] = None
    subject: Optional[str] = None
    subject_like: Optional[str] = None
    date_after: Optional[datetime] = None
    date_before: Optional[datetime] = None
    has_attachments: Optional[bool] = None
    attachment_type: Optional[str] = None
    attachment_name: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class SearchResponse:
    """Structured semantic search response."""

    query: str
    effective_query: str
    parser_applied: bool
    rerank_applied: bool
    indexed_emails: int
    filters: Dict[str, Any]
    results: List[Dict[str, Any]] = field(default_factory=list)
    message: Optional[str] = None


@dataclass
class QueryResponse:
    """Structured metadata query response."""

    filters: Dict[str, Any]
    results: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StatusResponse:
    """Structured status payload."""

    embedding_model: str
    email_directory: str
    chromadb_path: str
    database_path: str
    total_indexed_emails: int
    total_emails: int
    total_attachments: int
    emails_with_attachments: int
    date_range: Dict[str, Optional[str]]
    batch_size: int
    search_results: int

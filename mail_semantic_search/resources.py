"""Process-wide handles on the models and vector index, with idle release.

A search needs the embedding model, the reranker, and Chroma's HNSW index —
the last of which Chroma holds entirely in RAM (gigabytes on a large mailbox).
A one-shot CLI run gives all of that back when it exits, but the MCP server
lives for days and is idle for nearly all of them.

So the server brackets every tool call with `in_use()`, which keeps the index
and models loaded across back-to-back calls, and runs a reaper that drops them
once nothing has used them for a while. The next call reloads them (a second
or two) transparently.
"""

import gc
import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.reranker import CrossEncoderReranker
from mail_semantic_search.vector_store import VectorStore

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Separate from `_lock` so a multi-second model load never blocks `in_use()`,
# which the server enters on its event loop.
_load_lock = threading.Lock()
_active = 0
_last_used = time.monotonic()
_embedding_service: Optional[EmbeddingService] = None
_reranker: Optional[CrossEncoderReranker] = None
# Never queried directly: holding one store open keeps Chroma's refcounted
# per-path system (and with it the loaded index) alive between tool calls.
_keepalive_store: Optional[VectorStore] = None


def get_embedding_service() -> EmbeddingService:
    """Return the shared embedding model, loading it on first use."""
    global _embedding_service
    with _load_lock:
        if _embedding_service is None:
            _embedding_service = EmbeddingService()
        return _embedding_service


def get_reranker() -> CrossEncoderReranker:
    """Return the shared reranker model, loading it on first use."""
    global _reranker
    with _load_lock:
        if _reranker is None:
            _reranker = CrossEncoderReranker()
        return _reranker


@contextmanager
def in_use() -> Iterator[None]:
    """Mark the shared resources busy for the duration of one request."""
    global _active, _keepalive_store
    with _lock:
        _active += 1
        if _keepalive_store is None:
            try:
                _keepalive_store = VectorStore()
            except Exception as exc:
                # Tools that never touch Chroma must keep working when the
                # index can't be opened; those that do will report it themselves.
                logger.debug("Could not hold the vector store open: %s", exc)
    try:
        yield
    finally:
        with _lock:
            global _last_used
            _active -= 1
            _last_used = time.monotonic()


def release_if_idle(idle_seconds: float) -> bool:
    """Drop the index and models if unused for `idle_seconds`. Returns whether it did."""
    global _embedding_service, _reranker, _keepalive_store
    with _lock:
        loaded = _keepalive_store or _embedding_service or _reranker
        if not loaded or _active or time.monotonic() - _last_used < idle_seconds:
            return False
        store, _keepalive_store = _keepalive_store, None
        _embedding_service = _reranker = None
        if store is not None:
            store.close()

    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.backends.mps.is_available():
        # Best effort: MPS hands back only part of what the models used.
        torch.mps.empty_cache()
    logger.info("Released vector index and models after %ss idle", idle_seconds)
    return True


def start_idle_reaper(idle_seconds: float) -> threading.Thread:
    """Start a daemon thread that releases the resources once they go idle."""

    def _reap() -> None:
        while True:
            time.sleep(min(idle_seconds, 30))
            try:
                release_if_idle(idle_seconds)
            except Exception:
                logger.exception("Idle release failed")

    thread = threading.Thread(target=_reap, name="mcp-idle-reaper", daemon=True)
    thread.start()
    return thread

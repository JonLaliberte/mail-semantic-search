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
from mail_semantic_search.config import config
from mail_semantic_search.reranker import CrossEncoderReranker
from mail_semantic_search.vector_store import VectorStore, index_generation

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Separate from `_lock` so a multi-second model load never blocks `in_use()`,
# which the server enters on its event loop.
_load_lock = threading.Lock()
_active = 0
# Scheduled index runs in progress. They hold the shared model without
# counting as tool calls, so they keep it loaded but don't reset the idle timer.
_indexing = 0
_last_used = time.monotonic()
_embedding_service: Optional[EmbeddingService] = None
_reranker: Optional[CrossEncoderReranker] = None
# Never queried directly: holding one store open keeps Chroma's refcounted
# per-path system (and with it the loaded index) alive between tool calls.
_keepalive_store: Optional[VectorStore] = None
# Chroma's write sequence number when the keepalive store was opened.
_keepalive_generation: Optional[int] = None


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
    global _active, _keepalive_store, _keepalive_generation
    with _lock:
        if (
            not _active
            and not _indexing
            and _keepalive_store is not None
            and index_generation(config.chromadb_path) != _keepalive_generation
        ):
            # An index run in another process wrote since we loaded. Chroma
            # never reloads a loaded index, so it would keep returning deleted
            # vectors and miss new mail; closing the last handle frees it.
            _keepalive_store.close()
            _keepalive_store = None
            logger.info("Vector index changed on disk; reloading it")
        _active += 1
        _open_keepalive_locked()
    try:
        yield
    finally:
        with _lock:
            global _last_used
            _active -= 1
            _last_used = time.monotonic()


def _open_keepalive_locked() -> None:
    """Hold one store open if none is; call with `_lock` held."""
    global _keepalive_store, _keepalive_generation
    if _keepalive_store is not None:
        return
    try:
        # Read before opening: a write in between only costs a reload.
        _keepalive_generation = index_generation(config.chromadb_path)
        _keepalive_store = VectorStore()
    except Exception as exc:
        # Tools that never touch Chroma must keep working when the
        # index can't be opened; those that do will report it themselves.
        logger.debug("Could not hold the vector store open: %s", exc)


@contextmanager
def indexing() -> Iterator[None]:
    """Keep the shared models and index loaded while a scheduled run uses them.

    Without this the idle reaper could drop the model mid-run, and the next
    tool call would load a second copy beside the one the run still holds.
    Holding the keepalive store also means the run's own store is never the
    last handle on Chroma's shared system, so closing it can't stop that
    system while a tool call is opening it.
    """
    global _indexing
    with _lock:
        _indexing += 1
        _open_keepalive_locked()
    try:
        yield
    finally:
        with _lock:
            _indexing -= 1


def mark_index_current() -> None:
    """Accept the index's current write sequence as already seen.

    Call after this process writes to the index itself (the scheduled index
    run). Its writes already went into the loaded index, so the next tool call
    has no reason to throw it away and load it again from disk.
    """
    global _keepalive_generation
    with _lock:
        if _keepalive_store is not None:
            _keepalive_generation = index_generation(config.chromadb_path)


def release_if_idle(idle_seconds: float) -> bool:
    """Drop the index and models if unused for `idle_seconds`. Returns whether it did."""
    global _embedding_service, _reranker, _keepalive_store
    with _lock:
        loaded = _keepalive_store or _embedding_service or _reranker
        if (
            not loaded
            or _active
            or _indexing
            or time.monotonic() - _last_used < idle_seconds
        ):
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

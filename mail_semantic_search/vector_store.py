"""Vector database operations using ChromaDB."""

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List, Optional

import chromadb
from chromadb.config import Settings

from mail_semantic_search.config import config
from mail_semantic_search.database import get_file_hash

logger = logging.getLogger(__name__)

COLLECTION_NAME = "emails"
# Scratch names used only while `VectorStore.compact` is rebuilding the index.
_COMPACTING_NAME = "emails_compacting"
_RETIRED_NAME = "emails_retired"


def vacuum_chroma_sqlite(chromadb_path: Path) -> None:
    """Shrink Chroma's SQLite file after rows were dropped.

    Compaction writes a second copy of every document and its full-text index
    before dropping the first, which leaves the file at twice its live size.
    Call with no store open on the path.
    """
    conn = sqlite3.connect(str(Path(chromadb_path) / "chroma.sqlite3"))
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


class VectorStore:
    """Vector store for email embeddings using ChromaDB."""

    def __init__(self):
        """Initialize the vector store."""
        self.chromadb_path = config.chromadb_path
        self.client = chromadb.PersistentClient(
            path=str(self.chromadb_path.absolute()),
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def _get_file_hash(self, file_path: str) -> str:
        """Generate a hash for a file path to use as ID."""
        return get_file_hash(file_path)

    def is_indexed(self, file_path: str, mtime: Optional[float] = None) -> bool:
        """Check if an email file has already been indexed."""
        file_id = self._get_file_hash(file_path)
        try:
            results = self.collection.get(ids=[file_id])
            if results["ids"]:
                # If mtime is provided, we could check if file was modified
                # For now, just check if it exists
                return True
        except chromadb.errors.ChromaError as e:
            # Issue #11: Log specific ChromaDB errors
            logger.debug(f"ChromaDB lookup failed for {file_path}: {e}")
        return False

    def get_email_document(self, file_path: str) -> Optional[Dict]:
        """Return stored Chroma document and metadata for one indexed email."""
        file_id = self._get_file_hash(file_path)
        try:
            results = self.collection.get(ids=[file_id])
        except chromadb.errors.ChromaError as e:
            logger.debug(f"ChromaDB retrieval failed for {file_path}: {e}")
            return None

        ids = results.get("ids") or []
        if not ids:
            return None

        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []

        return {
            "id": ids[0],
            "document": documents[0] if documents else "",
            "metadata": metadatas[0] if metadatas else {},
        }

    def add_emails(
        self,
        emails: List[Dict],
        embeddings: List[List[float]],
        texts: Optional[List[str]] = None,
    ) -> None:
        """
        Add emails and their embeddings to the vector store.
        
        Args:
            emails: List of email dictionaries
            embeddings: List of embedding vectors
            texts: Optional list of combined text strings (from combine_email_text).
                   If not provided, will generate from email data.
        """
        if not emails or not embeddings:
            return

        ids = [self._get_file_hash(email["file_path"]) for email in emails]
        
        # Use provided texts if available (should include attachment content from combine_email_text)
        # Otherwise generate fallback text
        if texts is None or len(texts) != len(emails):
            texts = []
            for email in emails:
                subject = email.get("subject", "")
                body = email.get("body", "")[:1000]
                text = f"{subject}\n{body}"
                
                # Add attachment filenames
                attachments = email.get("attachments", [])
                if attachments:
                    attachment_names = ", ".join(
                        att.get("filename", "Unknown") for att in attachments[:5]
                    )
                    if len(attachments) > 5:
                        attachment_names += f" (+{len(attachments) - 5} more)"
                    text += f"\nAttachments: {attachment_names}"
                
                texts.append(text)
        
        metadatas = []
        for email in emails:
            attachments = email.get("attachments", [])
            attachment_count = len(attachments)
            
            # Get attachment types/extensions for filtering
            attachment_types = []
            if attachments:
                # Issue #14: Use config constant instead of magic number
                for att in attachments[:config.MAX_ATTACHMENTS_FOR_METADATA]:
                    filename = att.get("filename", "")
                    if filename:
                        ext = filename.split(".")[-1].lower() if "." in filename else ""
                        if ext and ext not in attachment_types:
                            attachment_types.append(ext)
            
            # Issue #14: Use config constants instead of magic numbers
            max_len = config.MAX_CHROMADB_METADATA_LENGTH
            metadata = {
                "subject": email["subject"][:max_len],
                "from": email["from"][:max_len],
                "to": email["to"][:max_len],
                "date": str(email["date"]) if email["date"] else "",
                "message_id": email["message_id"][:max_len],
                "file_path": email["file_path"],
                "attachment_count": attachment_count,
            }
            
            # Add attachment types if any
            if attachment_types:
                metadata["attachment_types"] = ",".join(attachment_types[:config.MAX_ATTACHMENT_TYPES_STORED])
            
            metadatas.append(metadata)

        # Issue #18: Use upsert() instead of add() to handle re-indexing of modified files
        # add() fails if IDs already exist, upsert() updates existing entries
        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )

    def delete_email(self, file_path: str) -> None:
        """Remove the Chroma document for the given file path."""
        file_id = self._get_file_hash(file_path)
        try:
            self.collection.delete(ids=[file_id])
        except chromadb.errors.ChromaError as e:
            logger.debug(f"ChromaDB delete failed for {file_path}: {e}")

    def delete_emails(self, file_paths: List[str]) -> None:
        """Remove the Chroma documents for a batch of file paths.

        Deleting IDs that are not present is a no-op in Chroma, so this is
        safe even when the vector store is already in sync with disk.
        """
        if not file_paths:
            return
        ids = [self._get_file_hash(fp) for fp in file_paths]
        try:
            self.collection.delete(ids=ids)
        except chromadb.errors.ChromaError as e:
            logger.debug(f"ChromaDB batch delete failed ({len(ids)} ids): {e}")

    def search(
        self, query_embedding: List[float], n_results: int = 10
    ) -> List[Dict]:
        """Search for similar emails."""
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
        )

        emails = []
        if results["ids"] and len(results["ids"][0]) > 0:
            for i in range(len(results["ids"][0])):
                email_data = {
                    "id": results["ids"][0][i],
                    "distance": results["distances"][0][i]
                    if results["distances"]
                    else None,
                    "subject": results["metadatas"][0][i].get("subject", "")
                    if results["metadatas"]
                    else "",
                    "from": results["metadatas"][0][i].get("from", "")
                    if results["metadatas"]
                    else "",
                    "to": results["metadatas"][0][i].get("to", "")
                    if results["metadatas"]
                    else "",
                    "date": results["metadatas"][0][i].get("date", "")
                    if results["metadatas"]
                    else "",
                    "message_id": results["metadatas"][0][i].get(
                        "message_id", ""
                    )
                    if results["metadatas"]
                    else "",
                    "file_path": results["metadatas"][0][i].get("file_path", "")
                    if results["metadatas"]
                    else "",
                    "document": results["documents"][0][i]
                    if results["documents"]
                    else "",
                }
                emails.append(email_data)

        return emails

    def get_stats(self) -> Dict:
        """Get statistics about the indexed emails."""
        count = self.collection.count()
        return {"total_emails": count}

    def index_bytes_on_disk(self) -> int:
        """Total size of the HNSW vector files, which Chroma loads fully into RAM."""
        return sum(
            f.stat().st_size for f in Path(self.chromadb_path).glob("*/data_level0.bin")
        )

    def compact(
        self,
        batch_size: int = 1000,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Rebuild the collection so deleted vectors stop occupying the index.

        Chroma's local HNSW index only tombstones deletions — the slots are
        never reused — and the whole index is held in RAM, so a store that has
        been through `migrate-paths` or heavy pruning carries every dead vector
        in memory forever. Copying the live rows into a fresh collection and
        swapping it in is the only way to get that space back. Embeddings are
        reused; nothing is re-embedded.

        Resumable: an interrupted copy continues where it stopped, and an
        interrupted swap is finished on the next call. Returns the number of
        vectors in the rebuilt collection.
        """
        names = {c.name for c in self.client.list_collections()}
        if _RETIRED_NAME in names and _COMPACTING_NAME in names:
            # Died between the two renames of a previous swap.
            return self._finish_swap()
        if _RETIRED_NAME in names:
            # Died after the swap but before dropping the old collection.
            self.client.delete_collection(_RETIRED_NAME)
            names.discard(_RETIRED_NAME)

        source = self.collection
        target = self.client.get_or_create_collection(
            name=_COMPACTING_NAME, metadata=source.metadata
        )
        resuming = target.count() > 0
        batch_size = min(batch_size, self.client.get_max_batch_size())

        ids = source.get(include=[])["ids"]
        total = len(ids)
        for start in range(0, total, batch_size):
            batch_ids = ids[start : start + batch_size]
            if resuming:
                copied = set(target.get(ids=batch_ids, include=[])["ids"])
                batch_ids = [i for i in batch_ids if i not in copied]
            if batch_ids:
                rows = source.get(
                    ids=batch_ids, include=["embeddings", "metadatas", "documents"]
                )
                target.upsert(
                    ids=rows["ids"],
                    embeddings=rows["embeddings"],
                    metadatas=rows["metadatas"],
                    documents=rows["documents"],
                )
            if progress:
                progress(min(start + batch_size, total), total)

        copied_count, source_count = target.count(), source.count()
        if copied_count != source_count:
            raise RuntimeError(
                f"Compacted collection has {copied_count} vectors but the original "
                f"has {source_count}; leaving the original in place. Was the index "
                "written to during compaction? Re-run to retry."
            )

        source.modify(name=_RETIRED_NAME)
        return self._finish_swap()

    def _finish_swap(self) -> int:
        """Promote the rebuilt collection and drop the retired one."""
        names = {c.name for c in self.client.list_collections()}
        if COLLECTION_NAME in names:
            # Anything that opened the store mid-swap (this constructor
            # included) recreated an empty collection under the real name.
            stray = self.client.get_collection(COLLECTION_NAME)
            if stray.count() > 0:
                raise RuntimeError(
                    f"Cannot finish compaction: a non-empty {COLLECTION_NAME!r} "
                    f"collection exists alongside {_COMPACTING_NAME!r}."
                )
            self.client.delete_collection(COLLECTION_NAME)
        target = self.client.get_collection(_COMPACTING_NAME)
        target.modify(name=COLLECTION_NAME)
        self.client.delete_collection(_RETIRED_NAME)
        self.collection = target
        self._remove_orphaned_segment_dirs()
        return target.count()

    def _remove_orphaned_segment_dirs(self) -> None:
        """Delete HNSW directories Chroma left behind for dropped collections.

        `delete_collection` removes the segment's rows but not its directory,
        so without this the retired index stays on disk at full size.
        """
        root = Path(self.chromadb_path)
        try:
            conn = sqlite3.connect(f"file:{root / 'chroma.sqlite3'}?mode=ro", uri=True)
            try:
                live = {row[0] for row in conn.execute("SELECT id FROM segments")}
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("Skipping orphaned segment cleanup: %s", e)
            return
        if not live:
            return
        for header in root.glob("*/header.bin"):
            if header.parent.name not in live:
                shutil.rmtree(header.parent, ignore_errors=True)

    def close(self) -> None:
        """Drop this handle on the Chroma client.

        Chroma shares one system per path and refcounts its clients; when the
        last one closes, the in-memory HNSW index is freed. Callers that want
        the index to stay loaded between uses hold a store open themselves
        (see `resources`).
        """
        close = getattr(self.client, "close", None)  # absent before chromadb 1.x
        if close is not None:
            close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


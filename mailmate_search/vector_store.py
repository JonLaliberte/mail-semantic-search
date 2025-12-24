"""Vector database operations using ChromaDB."""

import hashlib
from pathlib import Path
from typing import Dict, List, Optional

import chromadb
from chromadb.config import Settings

from mailmate_search.config import config


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
            name="emails",
            metadata={"hnsw:space": "cosine"},
        )

    def _get_file_hash(self, file_path: str) -> str:
        """Generate a hash for a file path to use as ID."""
        return hashlib.md5(file_path.encode()).hexdigest()

    def is_indexed(self, file_path: str, mtime: Optional[float] = None) -> bool:
        """Check if an email file has already been indexed."""
        file_id = self._get_file_hash(file_path)
        try:
            results = self.collection.get(ids=[file_id])
            if results["ids"]:
                # If mtime is provided, we could check if file was modified
                # For now, just check if it exists
                return True
        except Exception:
            pass
        return False

    def add_emails(
        self,
        emails: List[Dict],
        embeddings: List[List[float]],
    ) -> None:
        """Add emails and their embeddings to the vector store."""
        if not emails or not embeddings:
            return

        ids = [self._get_file_hash(email["file_path"]) for email in emails]
        texts = [
            f"{email['subject']}\n{email['body'][:1000]}"
            for email in emails
        ]
        metadatas = [
            {
                "subject": email["subject"][:500],  # ChromaDB has metadata size limits
                "from": email["from"][:500],
                "to": email["to"][:500],
                "date": str(email["date"]) if email["date"] else "",
                "message_id": email["message_id"][:500],
                "file_path": email["file_path"],
            }
            for email in emails
        ]

        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )

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


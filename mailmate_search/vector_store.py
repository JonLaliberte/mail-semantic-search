"""Vector database operations using ChromaDB."""

from pathlib import Path
from typing import Dict, List, Optional

import chromadb
from chromadb.config import Settings

from mailmate_search.config import config
from mailmate_search.database import get_file_hash


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
        except Exception:
            pass
        return False

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
                for att in attachments[:10]:  # Limit to first 10 to avoid metadata size issues
                    filename = att.get("filename", "")
                    if filename:
                        ext = filename.split(".")[-1].lower() if "." in filename else ""
                        if ext and ext not in attachment_types:
                            attachment_types.append(ext)
            
            metadata = {
                "subject": email["subject"][:500],  # ChromaDB has metadata size limits
                "from": email["from"][:500],
                "to": email["to"][:500],
                "date": str(email["date"]) if email["date"] else "",
                "message_id": email["message_id"][:500],
                "file_path": email["file_path"],
                "attachment_count": attachment_count,
            }
            
            # Add attachment types if any
            if attachment_types:
                metadata["attachment_types"] = ",".join(attachment_types[:5])  # Limit types
            
            metadatas.append(metadata)

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

    def close(self) -> None:
        """Close the vector store connection."""
        # ChromaDB PersistentClient handles cleanup automatically,
        # but we provide this method for consistency
        pass

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


"""Search logic for emails."""

import hashlib
from datetime import datetime
from typing import List, Optional

from mailmate_search.config import config
from mailmate_search.database import Database
from mailmate_search.embedding_service import EmbeddingService
from mailmate_search.query import QueryBuilder
from mailmate_search.vector_store import VectorStore


def format_date(date_str: str) -> str:
    """Format date string for display."""
    if not date_str:
        return "Unknown date"
    try:
        # Try parsing ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str


def display_results(results: List[dict], show_attachments: bool = False) -> None:
    """Display search results in a readable format."""
    if not results:
        print("No results found.")
        return

    print(f"\nFound {len(results)} results:\n")
    print("=" * 80)

    for i, result in enumerate(results, 1):
        score = result.get("similarity", result.get("distance", 0))
        if isinstance(score, (int, float)) and score < 1:
            score = 1 - score  # Convert distance to similarity
        print(f"\n[{i}] Similarity: {score:.3f}" if isinstance(score, (int, float)) else f"\n[{i}]")
        print(f"From: {result.get('from', result.get('from_addr', 'Unknown'))}")
        print(f"Subject: {result.get('subject', 'No subject')}")
        print(f"Date: {format_date(str(result.get('date', '')))}")
        print(f"File: {result.get('file_path', 'Unknown')}")
        
        # Show attachments if available
        attachments = result.get("attachments", [])
        if attachments and show_attachments:
            print(f"Attachments ({len(attachments)}):")
            for att in attachments[:3]:  # Show first 3
                filename = att.get("filename", "Unknown")
                size = att.get("size", 0)
                size_str = f" ({size:,} bytes)" if size > 0 else ""
                print(f"  - {filename}{size_str}")
            if len(attachments) > 3:
                print(f"  ... and {len(attachments) - 3} more")
        
        if result.get("document"):
            preview = result["document"][:200]
            print(f"Preview: {preview}...")
        print("-" * 80)


def search_emails(
    query: str,
    from_addr: Optional[str] = None,
    to_addr: Optional[str] = None,
    subject: Optional[str] = None,
    date_after: Optional[datetime] = None,
    date_before: Optional[datetime] = None,
    has_attachments: Optional[bool] = None,
    attachment_type: Optional[str] = None,
    attachment_name: Optional[str] = None,
    show_attachments: bool = False,
) -> None:
    """Search for emails matching a query with optional filters."""
    print(f"Searching for: '{query}'")
    print(f"Using embedding model: {config.embedding_model}")

    # Initialize services
    database = Database()
    embedding_service = EmbeddingService()
    vector_store = VectorStore()
    query_builder = QueryBuilder(database)

    # Check if any emails are indexed
    stats = vector_store.get_stats()
    db_stats = database.get_stats()
    if stats["total_emails"] == 0 and db_stats["total_emails"] == 0:
        print("No emails indexed yet. Please run 'index' command first.")
        database.close()
        return

    print(f"Searching in {stats['total_emails']} indexed emails...")

    # Check if we have filters
    has_filters = any(
        [
            from_addr,
            to_addr,
            subject,
            date_after,
            date_before,
            has_attachments is not None,
            attachment_type,
            attachment_name,
        ]
    )

    if has_filters:
        # Hybrid search: filter first, then vector search on results
        print("Applying filters...")
        filtered_emails = query_builder.build_query(
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            date_after=date_after,
            date_before=date_before,
            has_attachments=has_attachments,
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            limit=None,  # Get all filtered results first
        )

        if not filtered_emails:
            print("No emails match the filters.")
            database.close()
            return

        print(f"Found {len(filtered_emails)} emails matching filters, searching semantically...")

        # Get file hashes for filtered emails
        file_hashes = [
            hashlib.md5(email["file_path"].encode()).hexdigest()
            for email in filtered_emails
        ]

        # Generate query embedding
        query_embedding = embedding_service.embed_query(query)

        # Search vector database with larger limit, then filter by file hashes
        vector_results = vector_store.search(
            query_embedding, n_results=min(len(filtered_emails) * 2, 1000)
        )

        # Filter vector results to only include filtered emails
        filtered_hashes_set = set(file_hashes)
        filtered_vector_results = [
            r for r in vector_results
            if hashlib.md5(r.get("file_path", "").encode()).hexdigest() in filtered_hashes_set
        ]

        # Limit to requested number of results
        filtered_vector_results = filtered_vector_results[:config.search_results]

        # Enrich with database metadata
        results = []
        for vr in filtered_vector_results:
            file_hash = hashlib.md5(vr.get("file_path", "").encode()).hexdigest()
            db_email = database.get_email_by_file_hash(file_hash)
            if db_email:
                # Merge vector search result with database metadata
                result = {**vr, **db_email}
                result["similarity"] = 1 - vr.get("distance", 0)
                results.append(result)
            else:
                results.append(vr)

    else:
        # Pure vector search
        query_embedding = embedding_service.embed_query(query)
        vector_results = vector_store.search(
            query_embedding, n_results=config.search_results
        )

        # Enrich with database metadata
        results = []
        for vr in vector_results:
            file_hash = hashlib.md5(vr.get("file_path", "").encode()).hexdigest()
            db_email = database.get_email_by_file_hash(file_hash)
            if db_email:
                result = {**vr, **db_email}
                result["similarity"] = 1 - vr.get("distance", 0)
                results.append(result)
            else:
                results.append(vr)

    # Display results
    display_results(results, show_attachments=show_attachments)
    
    database.close()



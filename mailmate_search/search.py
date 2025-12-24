"""Search logic for emails."""

from datetime import datetime
from typing import List

from mailmate_search.config import config
from mailmate_search.embedding_service import EmbeddingService
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


def display_results(results: List[dict]) -> None:
    """Display search results in a readable format."""
    if not results:
        print("No results found.")
        return

    print(f"\nFound {len(results)} results:\n")
    print("=" * 80)

    for i, result in enumerate(results, 1):
        score = 1 - result.get("distance", 0)  # Convert distance to similarity
        print(f"\n[{i}] Similarity: {score:.3f}")
        print(f"From: {result.get('from', 'Unknown')}")
        print(f"Subject: {result.get('subject', 'No subject')}")
        print(f"Date: {format_date(result.get('date', ''))}")
        print(f"File: {result.get('file_path', 'Unknown')}")
        if result.get("document"):
            preview = result["document"][:200]
            print(f"Preview: {preview}...")
        print("-" * 80)


def search_emails(query: str) -> None:
    """Search for emails matching a query."""
    print(f"Searching for: '{query}'")
    print(f"Using embedding model: {config.embedding_model}")

    # Initialize services
    embedding_service = EmbeddingService()
    vector_store = VectorStore()

    # Check if any emails are indexed
    stats = vector_store.get_stats()
    if stats["total_emails"] == 0:
        print("No emails indexed yet. Please run 'index' command first.")
        return

    print(f"Searching in {stats['total_emails']} indexed emails...")

    # Generate query embedding
    query_embedding = embedding_service.embed_query(query)

    # Search vector database
    results = vector_store.search(
        query_embedding, n_results=config.search_results
    )

    # Display results
    display_results(results)


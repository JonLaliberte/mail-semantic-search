"""CLI interface for MailMate search."""

import click

from mailmate_search.config import config
from mailmate_search.index import index_emails
from mailmate_search.search import search_emails
from mailmate_search.vector_store import VectorStore


@click.group()
@click.version_option(version="0.1.0")
def main():
    """MailMate AI Search Tool - Semantic search for your emails."""
    pass


@main.command()
@click.option(
    "--limit",
    type=int,
    help="Limit the number of emails to index (for testing)",
)
@click.option(
    "--no-skip",
    is_flag=True,
    help="Re-index all emails even if already indexed",
)
def index(limit: int, no_skip: bool):
    """Index emails from MailMate directory."""
    index_emails(limit=limit, skip_indexed=not no_skip, show_progress=True)


@main.command()
@click.argument("query", required=True)
def search(query: str):
    """Search for emails using natural language query."""
    search_emails(query)


@main.command()
def status():
    """Show indexing status and statistics."""
    vector_store = VectorStore()
    stats = vector_store.get_stats()

    print("MailMate Search Status")
    print("=" * 40)
    print(f"Embedding Model: {config.embedding_model}")
    print(f"MailMate Directory: {config.mailmate_email_dir}")
    print(f"ChromaDB Path: {config.chromadb_path}")
    print(f"Total Indexed Emails: {stats['total_emails']}")
    print(f"Batch Size: {config.batch_size}")
    print(f"Search Results: {config.search_results}")


if __name__ == "__main__":
    main()


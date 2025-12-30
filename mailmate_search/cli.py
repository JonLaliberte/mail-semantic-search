"""CLI interface for MailMate search."""

from datetime import datetime
from typing import Optional

import click

from mailmate_search.config import config
from mailmate_search.database import Database
from mailmate_search.index import index_emails
from mailmate_search.query import QueryBuilder
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
@click.option("--from", "from_addr", help="Filter by sender (partial match)")
@click.option("--to", "to_addr", help="Filter by recipient (partial match)")
@click.option("--subject", help="Filter by exact subject")
@click.option("--subject-like", help="Filter by subject (partial match)")
@click.option("--date-after", help="Filter by date after (YYYY-MM-DD)")
@click.option("--date-before", help="Filter by date before (YYYY-MM-DD)")
@click.option("--has-attachments", is_flag=True, help="Only show emails with attachments")
@click.option("--no-attachments", is_flag=True, help="Only show emails without attachments")
@click.option("--attachment-type", help="Filter by attachment file extension (e.g., pdf, jpg)")
@click.option("--attachment-name", help="Filter by attachment filename (partial match)")
@click.option("--show-attachments", is_flag=True, help="Show attachment details in results")
def search(
    query: str,
    from_addr: Optional[str],
    to_addr: Optional[str],
    subject: Optional[str],
    subject_like: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    has_attachments: bool,
    no_attachments: bool,
    attachment_type: Optional[str],
    attachment_name: Optional[str],
    show_attachments: bool,
):
    """Search for emails using natural language query with optional filters."""
    # Parse dates
    parsed_date_after = None
    parsed_date_before = None
    
    if date_after:
        try:
            parsed_date_after = datetime.fromisoformat(date_after)
        except ValueError:
            click.echo(f"Error: Invalid date format for --date-after: {date_after}", err=True)
            return
    
    if date_before:
        try:
            parsed_date_before = datetime.fromisoformat(date_before)
        except ValueError:
            click.echo(f"Error: Invalid date format for --date-before: {date_before}", err=True)
            return
    
    # Handle attachment filter
    has_attachments_flag = None
    if has_attachments:
        has_attachments_flag = True
    elif no_attachments:
        has_attachments_flag = False
    
    search_emails(
        query,
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        date_after=parsed_date_after,
        date_before=parsed_date_before,
        has_attachments=has_attachments_flag,
        attachment_type=attachment_type,
        attachment_name=attachment_name,
        show_attachments=show_attachments,
    )


@main.command()
@click.option("--from", "from_addr", help="Filter by sender (partial match)")
@click.option("--to", "to_addr", help="Filter by recipient (partial match)")
@click.option("--subject", help="Filter by exact subject")
@click.option("--subject-like", help="Filter by subject (partial match)")
@click.option("--date-after", help="Filter by date after (YYYY-MM-DD)")
@click.option("--date-before", help="Filter by date before (YYYY-MM-DD)")
@click.option("--has-attachments", is_flag=True, help="Only show emails with attachments")
@click.option("--no-attachments", is_flag=True, help="Only show emails without attachments")
@click.option("--attachment-type", help="Filter by attachment file extension (e.g., pdf, jpg)")
@click.option("--attachment-name", help="Filter by attachment filename (partial match)")
@click.option("--limit", type=int, help="Limit number of results")
@click.option("--show-attachments", is_flag=True, help="Show attachment details")
def query(
    from_addr: Optional[str],
    to_addr: Optional[str],
    subject: Optional[str],
    subject_like: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    has_attachments: bool,
    no_attachments: bool,
    attachment_type: Optional[str],
    attachment_name: Optional[str],
    limit: Optional[int],
    show_attachments: bool,
):
    """Query emails using metadata filters (no semantic search)."""
    database = Database()
    query_builder = QueryBuilder(database)
    
    # Parse dates
    parsed_date_after = None
    parsed_date_before = None
    
    if date_after:
        try:
            parsed_date_after = datetime.fromisoformat(date_after)
        except ValueError:
            click.echo(f"Error: Invalid date format for --date-after: {date_after}", err=True)
            database.close()
            return
    
    if date_before:
        try:
            parsed_date_before = datetime.fromisoformat(date_before)
        except ValueError:
            click.echo(f"Error: Invalid date format for --date-before: {date_before}", err=True)
            database.close()
            return
    
    # Handle attachment filter
    has_attachments_flag = None
    if has_attachments:
        has_attachments_flag = True
    elif no_attachments:
        has_attachments_flag = False
    
    results = query_builder.build_query(
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        subject_like=subject_like,
        date_after=parsed_date_after,
        date_before=parsed_date_before,
        has_attachments=has_attachments_flag,
        attachment_type=attachment_type,
        attachment_name=attachment_name,
        limit=limit,
    )
    
    # Display results
    from mailmate_search.search import display_results
    display_results(results, show_attachments=show_attachments)
    
    database.close()


@main.command()
def status():
    """Show indexing status and statistics."""
    database = Database()
    vector_store = VectorStore()
    
    vector_stats = vector_store.get_stats()
    db_stats = database.get_stats()

    print("MailMate Search Status")
    print("=" * 40)
    print(f"Embedding Model: {config.embedding_model}")
    print(f"MailMate Directory: {config.mailmate_email_dir}")
    print(f"ChromaDB Path: {config.chromadb_path}")
    print(f"Database Path: {config.database_path}")
    print(f"\nChromaDB Statistics:")
    print(f"  Total Indexed Emails: {vector_stats['total_emails']}")
    print(f"\nDatabase Statistics:")
    print(f"  Total Emails: {db_stats['total_emails']}")
    print(f"  Total Attachments: {db_stats['total_attachments']}")
    print(f"  Emails with Attachments: {db_stats['emails_with_attachments']}")
    if db_stats['date_range']['min']:
        print(f"  Date Range: {db_stats['date_range']['min']} to {db_stats['date_range']['max']}")
    print(f"\nConfiguration:")
    print(f"  Batch Size: {config.batch_size}")
    print(f"  Search Results: {config.search_results}")
    
    database.close()


if __name__ == "__main__":
    main()

